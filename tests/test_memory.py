#!/usr/bin/env python3
"""Three-tier bot memory: compaction, FTS5, isolation, crash safety."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
import memory as botmem  # noqa: E402
from cluster import bot_id_from_path  # noqa: E402
from memory import (  # noqa: E402
    MemoryManager,
    Role,
    RuleBasedSummarizer,
    TokenBudget,
    extract_tool_result,
)


class MemoryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _mgr(self, name: str = "bot", **kwargs) -> MemoryManager:
        kwargs.setdefault("summarizer", RuleBasedSummarizer())
        kwargs.setdefault("compact_threshold_tokens", 4000)
        kwargs.setdefault("min_verbatim_turns", 4)
        return MemoryManager(self.root / name / ".memory", **kwargs)

    def test_session_summary_survives_restart(self) -> None:
        a = self._mgr("resume")
        a.append_turn(Role.USER, "Build the hallway roster merge")
        a.append_turn(
            Role.ASSISTANT,
            "Decided to keep per-bot isolation because cluster must not share memory.",
        )
        a.summary.goal = "Build the hallway roster merge"
        a.summary.dead_ends.append({"text": "sharing SQLite over the mesh", "timestamp": 1})
        a.summary.save(a.summary_path)
        b = self._mgr("resume")
        self.assertEqual(b.summary.goal, "Build the hallway roster merge")
        self.assertEqual(b.summary.dead_ends[0]["text"], "sharing SQLite over the mesh")

    def test_compaction_fires_within_one_overflow_turn(self) -> None:
        mgr = self._mgr("compact", compact_threshold_tokens=200, min_verbatim_turns=2)
        for i in range(8):
            mgr.append_turn(Role.USER, f"turn {i} " + ("word " * 40))
        self.assertLessEqual(mgr.working.total_tokens, 200 + 1)
        self.assertLessEqual(len(mgr.working), 8)
        self.assertGreaterEqual(len(mgr.working), 2)
        self.assertTrue(mgr.summary_path.is_file())

    def test_dead_ends_persist_across_compaction(self) -> None:
        mgr = self._mgr("de", compact_threshold_tokens=80, min_verbatim_turns=1)
        mgr.append_turn(Role.ASSISTANT, "Using trait objects failed because XPU kernels hang.")
        mgr.append_turn(Role.USER, "try something else " + ("x" * 400))
        texts = [x["text"] for x in mgr.summary.dead_ends]
        self.assertTrue(any("failed because" in t.lower() for t in texts))
        mgr.append_turn(Role.USER, "continue " + ("y" * 400))
        texts2 = [x["text"] for x in mgr.summary.dead_ends]
        self.assertTrue(any("failed because" in t.lower() for t in texts2))

    def test_fts5_ranked_not_substring(self) -> None:
        mgr = self._mgr("fts")
        mgr.write("apple pie recipe uses cinnamon", ["food", "apple"])
        mgr.write("pineapple belongs to bromeliads", ["food", "pineapple"])
        mgr.write("apple orchard layout on the north field", ["orchard", "apple"])
        hits = mgr.retrieve("apple", limit=10)
        texts = [h.text for h in hits]
        self.assertTrue(texts, "expected FTS hits")
        self.assertTrue(all("apple" in t.lower() for t in texts))
        self.assertFalse(any("pineapple belongs" in t for t in texts))

    def test_kill9_during_write_does_not_corrupt(self) -> None:
        mgr = self._mgr("crash")
        mgr.write("durable fact", ["keep"])
        db = str(mgr.db_path)
        script = (
            "import os, sqlite3, sys\n"
            "conn = sqlite3.connect(sys.argv[1])\n"
            "conn.execute('PRAGMA journal_mode=WAL')\n"
            "conn.execute('BEGIN IMMEDIATE')\n"
            "conn.execute('INSERT INTO facts(text, tags, created_at) VALUES (?,?,?)', "
            "('partial-row', '', 1))\n"
            "os.kill(os.getpid(), 9)\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, db], check=False)
        self.assertNotEqual(proc.returncode, 0)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        rows = [r[0] for r in conn.execute("SELECT text FROM facts")]
        conn.close()
        self.assertIn("durable fact", rows)
        self.assertNotIn("partial-row", rows)

    def test_two_bots_isolated_directories(self) -> None:
        a = self._mgr("botA")
        b = self._mgr("botB")
        a.write("secret alphawordxyz only in bot A", ["alpha"])
        b.write("secret betawordxyz only in bot B", ["beta"])
        self.assertTrue((self.root / "botA" / ".memory" / "facts.sqlite").is_file())
        self.assertTrue((self.root / "botB" / ".memory" / "facts.sqlite").is_file())
        self.assertTrue(a.retrieve("alphawordxyz"))
        self.assertFalse(b.retrieve("alphawordxyz"))
        self.assertFalse(a.retrieve("betawordxyz"))

    def test_assemble_never_exceeds_budget(self) -> None:
        mgr = self._mgr("budget")
        for i in range(20):
            mgr.append_turn(Role.USER, f"user {i} " + ("token " * 30))
            mgr.append_turn(Role.ASSISTANT, f"asst {i} " + ("token " * 30))
        mgr.write("recalled architecture uses trait objects", ["arch"])
        budget = TokenBudget(total_context=8000, completion_reserve=1000, available_for_memory=120)
        assembled = mgr.assemble_context(budget, "architecture")
        self.assertLessEqual(assembled.token_count, budget.available_for_memory)
        self.assertNotIn("\x00", assembled.text)

    def test_video_and_image_bytes_never_in_retrieve_or_assemble(self) -> None:
        mgr = self._mgr("media")
        ws = self.root / "ws"
        ws.mkdir()
        rec = ws / "recordings"
        rec.mkdir()
        video = rec / "repro.mp4"
        video.write_bytes(b"\x00\x00fake-mp4-bytes\xff" * 50)
        img = ws / "shot.jpg"
        img.write_bytes(b"\xff\xd8\xfffake-jpeg" * 20)
        mgr.write("bug repro recording", ["bug"], media_path=str(video), media_type="video", workspace=ws)
        mgr.write("login screen caption", ["ui"], media_path=str(img), media_type="image", workspace=ws)
        rows = mgr.retrieve("bug", limit=5)
        self.assertTrue(rows)
        blob = json.dumps([r.to_dict() for r in rows])
        self.assertNotIn("fake-mp4-bytes", blob)
        self.assertTrue(any(r.media_type and r.media_type.value == "video" for r in rows))
        assembled = mgr.assemble_context(TokenBudget.for_model("qwen38-27b"), "bug")
        q5 = TokenBudget.for_model("qwen38-27b-q5")
        self.assertEqual(q5.total_context, 262144)
        self.assertEqual(q5.available_for_memory, 16000)
        small = TokenBudget.for_model("qwen3-vl-8b")
        self.assertEqual(small.total_context, 32768)
        self.assertEqual(small.available_for_memory, 4000)
        self.assertNotIn("fake-mp4-bytes", assembled.text)
        self.assertNotIn("fake-jpeg", assembled.text)
        img_hits = mgr.retrieve("login", limit=5)
        self.assertTrue(img_hits)
        self.assertTrue(all("bytes" not in (h.to_dict()) for h in img_hits))
        self.assertTrue(img_hits[0].media_path and img_hits[0].media_path.startswith("media/"))

    def test_tool_result_extraction_strips_dumps(self) -> None:
        huge = "line\n" * 5000
        out = extract_tool_result(huge)
        self.assertLess(len(out), len(huge))
        self.assertIn("truncated", out)
        self.assertIn("image omitted", extract_tool_result("iVBORw0KGgoAAA"))

    def test_inject_merges_into_existing_system_message(self) -> None:
        assembled = botmem.AssembledContext(text="# Session memory\nGoal: stay isolated", token_count=8)
        payload = {
            "messages": [
                {"role": "system", "content": "You are a desk bot."},
                {"role": "user", "content": "hi"},
            ]
        }
        out = botmem.inject_assembled_messages(payload, assembled)
        roles = [m["role"] for m in out["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertTrue(out["messages"][0]["content"].startswith("You are a desk bot."))
        self.assertIn("# Session memory", out["messages"][0]["content"])

    def test_parse_llm_proxy_path(self) -> None:
        bid, rest = botmem.parse_llm_proxy_path("/v1/llm/b_abc123def456/chat/completions")
        self.assertEqual(bid, "b_abc123def456")
        self.assertEqual(rest, "/chat/completions")
        bid, rest = botmem.parse_llm_proxy_path("/v1/llm/chat/completions")
        self.assertIsNone(bid)
        self.assertEqual(rest, "/chat/completions")
        bid, rest = botmem.parse_llm_proxy_path("/v1/llm/b_abc123def456/models")
        self.assertEqual(rest, "/models")

    def test_child_base_url_is_bot_scoped(self) -> None:
        url = d.rewrite_child_base_url("http://127.0.0.1:8000/v1", bot_id="b_deadbeef0001")
        self.assertIn("/v1/llm/b_deadbeef0001", url)
        self.assertNotIn("/v1/bots/", url)

    def test_memory_routes_are_not_cluster_proxied(self) -> None:
        self.assertIsNone(bot_id_from_path("/v1/memory/b_abc123def456/write"))
        self.assertIsNone(bot_id_from_path("/v1/memory/b_abc123def456/retrieve"))

    def test_mcp_tools_have_no_cross_host_bot_id(self) -> None:
        src = (ROOT / "deskd" / "memory_mcp.py").read_text(encoding="utf-8")
        self.assertIn('"name": "memory_write"', src)
        self.assertIn('"name": "memory_retrieve"', src)
        self.assertNotIn('"bot_id"', src)
        self.assertIn("/v1/memory/{BOT}/write", src)
        self.assertIn("/v1/memory/{BOT}/retrieve", src)
        self.assertIn('if "--bot" in args', src)


class MemoryHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self._orig_desks = d.HERMES_DESKS
        d.HERMES_DESKS = home / "desks"
        d.HERMES_DESKS.mkdir()
        token_dir = home / "run"
        token_dir.mkdir()
        self._orig_token = d.TOKEN_PATH
        d.TOKEN_PATH = token_dir / "token"
        d.TOKEN_PATH.write_text("mem-test-token", encoding="utf-8")
        self.bot = d.Bot("b_memhttp00001", "Mem", "", "", "qwen38-27b", "x")
        self.bot.workspace.mkdir(parents=True)
        d.bots[self.bot.id] = self.bot
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        d.bots.pop(self.bot.id, None)
        d.HERMES_DESKS = self._orig_desks
        d.TOKEN_PATH = self._orig_token
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = None if body is None else json.dumps(body).encode()
        hdrs = {"Authorization": "Bearer mem-test-token", "Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if raw is not None:
            hdrs["Content-Length"] = str(len(raw))
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode() or "null")
        except json.JSONDecodeError:
            parsed = data
        return resp.status, parsed

    def test_write_and_retrieve_roundtrip(self) -> None:
        st, out = self._req(
            "POST",
            f"/v1/memory/{self.bot.id}/write",
            {"text": "switched to trait objects for X because Y", "tags": ["arch"]},
        )
        self.assertEqual(st, 200, out)
        self.assertIn("id", out)
        st, out = self._req(
            "POST",
            f"/v1/memory/{self.bot.id}/retrieve",
            {"query": "trait objects", "limit": 5},
        )
        self.assertEqual(st, 200, out)
        recs = out.get("records") or []
        self.assertTrue(recs)
        self.assertIn("trait objects", recs[0]["text"])
        self.assertNotIn("bytes", recs[0])

    def test_cluster_token_cannot_read_memory(self) -> None:
        st, _ = self._req(
            "POST",
            f"/v1/memory/{self.bot.id}/retrieve",
            {"query": "x"},
            headers={"Authorization": "", "X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertIn(st, (401, 403))

    def test_unknown_bot_is_404(self) -> None:
        st, _ = self._req("POST", "/v1/memory/b_nope00000000/retrieve", {"query": "x"})
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
