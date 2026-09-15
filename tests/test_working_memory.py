#!/usr/bin/env python3
"""Working Memory Manifest: occupancy, pressure, redaction, stored vs in-context."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import body_state as bs  # noqa: E402
import deskd as d  # noqa: E402
import memory as botmem  # noqa: E402
import telemetry as tel  # noqa: E402
import working_memory as wm  # noqa: E402
from memory import MemoryManager, Role, RuleBasedSummarizer, TokenBudget  # noqa: E402


class PressureAndRedactionTests(unittest.TestCase):
    def test_pressure_bands_include_edges(self) -> None:
        self.assertEqual(wm.pressure_from_utilization(0.0), "GREEN")
        self.assertEqual(wm.pressure_from_utilization(0.449999), "GREEN")
        self.assertEqual(wm.pressure_from_utilization(0.45), "YELLOW")
        self.assertEqual(wm.pressure_from_utilization(0.649999), "YELLOW")
        self.assertEqual(wm.pressure_from_utilization(0.65), "ORANGE")
        self.assertEqual(wm.pressure_from_utilization(0.799999), "ORANGE")
        self.assertEqual(wm.pressure_from_utilization(0.80), "RED")
        self.assertEqual(wm.pressure_from_utilization(0.899999), "RED")
        self.assertEqual(wm.pressure_from_utilization(0.90), "CRITICAL")
        self.assertEqual(wm.pressure_from_utilization(1.0), "CRITICAL")
        self.assertIsNone(wm.pressure_from_utilization(None))

    def test_occupied_percents_are_of_occupied_not_capacity(self) -> None:
        sections = {"core": 7231, "conversation": 21600, "retrieved_memory": 14200}
        occ = wm.occupied_percents(sections)
        total = 7231 + 21600 + 14200
        self.assertAlmostEqual(occ["conversation"], 21600 / total * 100.0, places=4)
        self.assertNotAlmostEqual(occ["conversation"], 21600 / 262144 * 100.0, places=2)
        stats = wm.occupancy_stats(68342, 262144)
        self.assertEqual(stats["capacity_tokens"], 262144)
        self.assertEqual(stats["used_tokens"], 68342)
        self.assertEqual(stats["free_tokens"], 262144 - 68342)
        self.assertEqual(stats["pressure"], "GREEN")
        self.assertAlmostEqual(stats["utilization"], 68342 / 262144, places=6)

    def test_section_tokens_use_count_tokens_local_not_char_div_four(self) -> None:
        text = "Hi. What would you like to work on?"
        n, est = wm.count_section_tokens(text)
        self.assertTrue(est)
        self.assertEqual(n, tel.count_tokens_local(text))
        self.assertNotEqual(n, len(text) // 4)

    def test_redacts_secrets_and_drops_chain_of_thought(self) -> None:
        blob = {
            "api_key": "sk-test-not-real",
            "password": "hunter2",
            "token": "abc",
            "authorization": "Bearer secret-value",
            "nested": {"cookie": "sid=1", "ok": "visible"},
            "thought": "should never appear",
            "chain_of_thought": "hidden",
            "scratchpad": "private",
            "text": 'api_key=sk-leaked password=nope Bearer abcdefghijklmnop',
        }
        out = wm.redact_secrets(blob)
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["token"], "[REDACTED]")
        self.assertEqual(out["authorization"], "[REDACTED]")
        self.assertEqual(out["nested"]["cookie"], "[REDACTED]")
        self.assertEqual(out["nested"]["ok"], "visible")
        self.assertNotIn("thought", out)
        self.assertNotIn("chain_of_thought", out)
        self.assertNotIn("scratchpad", out)
        self.assertNotIn("sk-leaked", out["text"])
        self.assertNotIn("nope", out["text"])
        self.assertIn("[REDACTED]", out["text"])

    def test_why_loaded_from_reason_codes_not_model_prose(self) -> None:
        text = wm.why_loaded(["ACTIVE_TASK", "SEMANTIC_MATCH", "USER_CORRECTION"])
        self.assertIn("active task", text.lower())
        self.assertIn("semantic match", text.lower())
        self.assertIn("correction", text.lower())
        self.assertIn("Unknown", wm.why_loaded([]))


class ManifestAssembleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d.HERMES_DESKS = self.root / "desks"
        d.HERMES_DESKS.mkdir()
        self.bot = d.Bot("b_wmmanifest01", "Teela", "", "", "qwen38-27b-q5", "🤖")
        self.bot.workspace = self.root / "ws"
        self.bot.workspace.mkdir()
        self.bot.root = self.root / "bot"
        self.bot.root.mkdir()
        self.bot._memory = MemoryManager(
            self.bot.workspace / ".memory",
            summarizer=RuleBasedSummarizer(),
            model_id="qwen38-27b-q5",
        )

    def tearDown(self) -> None:
        wm._ledgers.pop(self.bot.id, None)
        self.tmp.cleanup()

    def test_unmeasured_occupancy_is_unavailable_not_zero(self) -> None:
        snap = wm.build_summary(self.bot)
        self.assertIsNone(snap["used_tokens"])
        self.assertEqual(snap["used_status"], "unavailable")
        self.assertIsNone(snap["pressure"])
        self.assertNotEqual(snap["used_tokens"], 0)

    def test_occupancy_from_ingest_usage_not_display_string(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 812, "completion_tokens": 44, "total_tokens": 856})
        self.assertEqual(self.bot.context_used, 856)
        snap = wm.build_summary(self.bot)
        self.assertEqual(snap["used_tokens"], 856)
        self.assertEqual(snap["capacity_tokens"], self.bot.context_window)
        self.assertFalse(snap["used_estimated"])
        self.assertEqual(snap["context_source"], "local_runtime")
        util = 856 / self.bot.context_window
        self.assertEqual(snap["pressure"], wm.pressure_from_utilization(util))
        self.assertEqual(snap["capacity_tokens"], tel.resolve_context_window("qwen38-27b-q5", self.bot.context_window))

    def test_live_tick_does_not_tokenize_or_scan_db(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        orig = tel.count_tokens_local

        def boom(*_a, **_k):
            raise AssertionError("live occupancy tick must not re-tokenize")

        tel.count_tokens_local = boom  # type: ignore[method-assign]
        orig_count = self.bot.memory.store.count

        def db_boom(*_a, **_k):
            raise AssertionError("live occupancy tick must not query the facts table")

        self.bot.memory.store.count = db_boom  # type: ignore[method-assign]
        try:
            extra = wm.note_occupancy(self.bot)
        finally:
            tel.count_tokens_local = orig  # type: ignore[method-assign]
            self.bot.memory.store.count = orig_count  # type: ignore[method-assign]
        self.assertEqual(extra.get("pressure"), wm.pressure_from_utilization(110 / self.bot.context_window))

    def test_occupancy_drop_is_not_compaction(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 100000, "completion_tokens": 0, "total_tokens": 100000})
        self.bot.ingest_usage({"prompt_tokens": 80000, "completion_tokens": 0, "total_tokens": 80000})
        self.assertEqual(self.bot.context_used, 80000)
        hist = wm.build_history(self.bot).get("history") or []
        self.assertGreaterEqual(len(hist), 2)
        later = hist[-1]
        self.assertEqual(later["tokens_used"], 80000)
        self.assertEqual(later["tokens_removed"], 20000)
        self.assertFalse(later["compaction"])
        self.assertFalse(later["retrieval"])

    def test_empty_retrieve_does_not_mark_history_retrieval(self) -> None:
        assembled = self.bot.memory.assemble_context(
            TokenBudget.for_model("qwen38-27b-q5"), "zzzznonexistentqueryxyz"
        )
        selected = (assembled.retrieval or {}).get("selected") or []
        self.assertEqual(selected, [])
        wm.capture_turn(
            self.bot,
            {"messages": [{"role": "user", "content": "zzzznonexistentqueryxyz"}]},
            assembled,
        )
        self.bot.ingest_usage({"prompt_tokens": 5000, "completion_tokens": 10, "total_tokens": 5010})
        hist = wm.build_history(self.bot).get("history") or []
        self.assertTrue(hist)
        self.assertFalse(hist[-1]["retrieval"])
        self.assertFalse(hist[-1]["compaction"])

    def test_injected_memory_marks_retrieval_and_real_compact_marks_compaction(self) -> None:
        mgr = self.bot.memory
        mgr.write("look_at_user correction: face the person", ["correction", "procedural"])
        assembled = mgr.assemble_context(TokenBudget.for_model("qwen38-27b-q5"), "look_at_user correction")
        self.assertTrue((assembled.retrieval or {}).get("selected"))
        wm.capture_turn(self.bot, {"messages": [{"role": "user", "content": "look_at_user"}]}, assembled)
        self.bot.ingest_usage({"prompt_tokens": 2000, "completion_tokens": 20, "total_tokens": 2020})
        hist = wm.build_history(self.bot).get("history") or []
        self.assertTrue(hist[-1]["retrieval"])
        self.assertFalse(hist[-1]["compaction"])

        compacting = MemoryManager(
            self.root / "compact-wm" / ".memory",
            summarizer=RuleBasedSummarizer(),
            compact_threshold_tokens=80,
            min_verbatim_turns=1,
        )
        self.bot._memory = compacting
        for i in range(8):
            compacting.append_turn(Role.USER, f"turn {i} " + ("word " * 40))
        kinds = [e.get("type") for e in compacting.context_events]
        self.assertIn("CONTEXT_COMPACTED", kinds)
        wm.capture_turn(self.bot, {"messages": [{"role": "user", "content": "hi"}]}, None)
        self.bot.ingest_usage({"prompt_tokens": 3000, "completion_tokens": 10, "total_tokens": 3010})
        hist2 = wm.build_history(self.bot).get("history") or []
        self.assertTrue(hist2[-1]["compaction"])

    def test_stored_only_vs_in_context_with_reason_codes(self) -> None:
        mgr = self.bot.memory
        kept = mgr.write("look_at_user correction: orient the head toward the user", ["correction", "gaze", "procedural"])
        stored = mgr.write("unrelated pineapple orchard layout on the north field", ["orchard", "pineapple"])
        assembled = mgr.assemble_context(TokenBudget.for_model("qwen38-27b-q5"), "look_at_user correction gaze")
        payload = {
            "messages": [
                {"role": "system", "content": "You are Teela.\n" + assembled.text},
                {"role": "user", "content": "look at the user"},
            ]
        }
        wm.capture_turn(self.bot, payload, assembled)
        items = wm.build_items(self.bot)
        ids = {it["id"] for it in items.get("items") or []}
        self.assertIn(f"mem_{kept}", ids)
        hit = next(it for it in items["items"] if it["id"] == f"mem_{kept}")
        self.assertTrue(hit.get("in_context"))
        self.assertIn("SEMANTIC_MATCH", hit.get("reason_codes") or [])
        self.assertIn("USER_CORRECTION", hit.get("reason_codes") or [])
        self.assertTrue(hit.get("why_loaded"))
        self.assertNotIn("chain", (hit.get("why_loaded") or "").lower())
        mems = wm.build_memories(self.bot, q="pineapple", limit=20)
        recs = mems.get("records") or []
        self.assertTrue(recs)
        pine = next(r for r in recs if r["id"] == f"mem_{stored}")
        self.assertFalse(pine.get("currently_in_context"))
        self.assertEqual(pine.get("presence"), "STORED ONLY")
        snap = wm.build_summary(self.bot)
        self.assertGreaterEqual(snap["memory"]["stored_total"], 2)
        self.assertGreaterEqual(snap["memory"]["currently_loaded"], 1)
        search = wm.build_search(self.bot, "pineapple")
        pine_hit = next(r for r in search["results"] if r["id"] == f"mem_{stored}")
        self.assertEqual(pine_hit["presence"], "STORED ONLY")
        gaze = wm.build_search(self.bot, "look_at_user")
        gaze_hit = next(r for r in gaze["results"] if r["id"] == f"mem_{kept}")
        self.assertEqual(gaze_hit["presence"], "IN CONTEXT")

    def test_body_matches_store_and_stale_threshold(self) -> None:
        store = bs.store_for(self.bot.id, self.bot.root / "body_state.sqlite")
        store.apply_commanded(
            {"neck_pan": -12.4, "neck_tilt": 7.1, "right_shoulder": 10},
            pose="standing",
            motion="idle",
        )
        snap = store.snapshot()
        body = wm.build_summary(self.bot)["body_state"]
        self.assertNotEqual(body.get("status"), "unavailable")
        self.assertEqual(body.get("pose"), snap.get("pose"))
        self.assertEqual(body.get("revision"), snap.get("revision"))
        self.assertAlmostEqual(float(body["head"]["pan"]), float(snap["joints"]["neck_pan"]["actual"]), places=1)
        block = bs.compact_block(snap)
        self.assertIn("standing", block)
        store._ts = time.time() - 19.4
        stale = wm.build_summary(self.bot)["body_state"]
        self.assertEqual(stale.get("status"), "stale")
        self.assertIn("Stale", stale.get("reason") or "")
        self.assertGreater(stale.get("freshness_ms") or 0, wm.BODY_STALE_MS)

    def test_kv_world_perception_unavailable_not_zero(self) -> None:
        snap = wm.build_summary(self.bot)
        self.assertEqual(snap["kv"]["status"], "unavailable")
        self.assertNotEqual(snap["kv"].get("currently_populated"), 0)
        self.assertEqual(snap["world_state"]["status"], "unavailable")
        self.assertEqual(snap["perception"]["status"], "unavailable")
        self.assertNotEqual(snap["kv"].get("capacity"), 0)
        self.assertNotEqual(snap["world_state"].get("objects"), 0)

    def test_snapshot_redacts_injected_secret_and_omits_cot(self) -> None:
        mgr = self.bot.memory
        mgr.write('login recipe uses api_key=sk-secret-value password=hunter2', ["secret-test"])
        assembled = mgr.assemble_context(TokenBudget.for_model("qwen38-27b-q5"), "login recipe api_key")
        payload = {
            "messages": [
                {"role": "system", "content": assembled.text},
                {"role": "user", "content": "remember the login"},
            ],
            "password": "should-not-leak",
        }
        wm.capture_turn(self.bot, payload, assembled)
        snap = wm.build_snapshot(self.bot)
        blob = json.dumps(snap)
        self.assertNotIn("sk-secret-value", blob)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("should-not-leak", blob)
        self.assertNotIn("chain_of_thought", blob)
        self.assertNotIn("scratchpad", blob)
        self.assertNotIn("hidden_reasoning", blob)

    def test_active_task_from_momentum(self) -> None:
        from teela_cl.deliberation import InteractionContext, save_momentum

        root = self.bot.workspace / ".teela"
        save_momentum(
            root,
            InteractionContext(
                goal="Calibrate visual head tracking",
                active_context="Verify gaze follows target",
                conversation_momentum="physical_training",
                last_attempt_id="att_1",
            ),
        )
        task = wm.build_summary(self.bot)["active_task"]
        self.assertNotEqual(task.get("status"), "unavailable")
        self.assertEqual(task.get("goal"), "Calibrate visual head tracking")
        self.assertTrue(task.get("state"))

    def test_no_forbidden_fields_in_manifest_payloads(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45})
        for payload in (
            wm.build_summary(self.bot),
            wm.build_items(self.bot),
            wm.build_snapshot(self.bot),
            wm.build_history(self.bot),
        ):
            blob = json.dumps(payload).lower()
            for key in ("chain_of_thought", "scratchpad", "hidden_reasoning", "private_reasoning"):
                self.assertNotIn(key, blob)


class WorkingMemoryHttpTests(unittest.TestCase):
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
        d.TOKEN_PATH.write_text("wm-test-token", encoding="utf-8")
        self.bot = d.Bot("b_wmhttp000001", "Mem", "", "", "qwen38-27b-q5", "x")
        self.bot.root = home / "botroot"
        self.bot.root.mkdir()
        self.bot.workspace.mkdir(parents=True)
        self.bot._memory = MemoryManager(
            self.bot.workspace / ".memory",
            summarizer=RuleBasedSummarizer(),
            model_id="qwen38-27b-q5",
        )
        d.bots[self.bot.id] = self.bot
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        d.bots.pop(self.bot.id, None)
        wm._ledgers.pop(self.bot.id, None)
        d.HERMES_DESKS = self._orig_desks
        d.TOKEN_PATH = self._orig_token
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = None if body is None else json.dumps(body).encode()
        hdrs = {"Authorization": "Bearer wm-test-token", "Content-Type": "application/json"}
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

    def test_summary_items_filters_and_snapshot(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 812, "completion_tokens": 44, "total_tokens": 856})
        kept = self.bot.memory.write("look_at_user correction: face the person", ["correction", "procedural"])
        self.bot.memory.write("unrelated arm calibration procedure for elbow", ["arm", "calibration"])
        assembled = self.bot.memory.assemble_context(
            TokenBudget.for_model("qwen38-27b-q5"), "look_at_user correction"
        )
        d.assemble_teela_executive_payload(self.bot, "look_at_user correction")
        st, summary = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory")
        self.assertEqual(st, 200, summary)
        self.assertEqual(summary["used_tokens"], 856)
        self.assertEqual(summary["used_tokens"], self.bot.context_used)
        self.assertEqual(summary["capacity_tokens"], self.bot.context_window)
        self.assertEqual(summary["pressure"], wm.pressure_from_utilization(856 / self.bot.context_window))
        self.assertGreaterEqual(summary["memory"]["stored_total"], 2)
        st, items = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/items?section=retrieved_memory")
        self.assertEqual(st, 200, items)
        ids = {it["id"] for it in items.get("items") or []}
        self.assertIn(f"mem_{kept}", ids)
        st, pinned = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/items?pinned=true")
        self.assertEqual(st, 200, pinned)
        for it in pinned.get("items") or []:
            self.assertTrue(it.get("pinned"))
        st, limited = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/items?limit=1")
        self.assertEqual(st, 200, limited)
        self.assertLessEqual(len(limited.get("items") or []), 1)
        st, snap = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/snapshot")
        self.assertEqual(st, 200, snap)
        self.assertEqual(snap["context_capacity"], self.bot.context_window)
        st, alias = self._req("GET", f"/v1/debug/context?bot_id={self.bot.id}")
        self.assertEqual(st, 200, alias)
        self.assertEqual(alias["used_tokens"], 856)
        st, again = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory")
        self.assertEqual(st, 200, again)
        self.assertEqual(again["used_tokens"], 856)
        self.assertEqual(again["used_tokens"], summary["used_tokens"])
        self.assertIsNotNone(assembled)

    def test_cluster_token_cannot_read_manifest(self) -> None:
        st, _ = self._req(
            "GET",
            f"/v1/bots/{self.bot.id}/working-memory",
            headers={"Authorization": "", "X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertIn(st, (401, 403))
        st, _ = self._req(
            "GET",
            f"/v1/debug/context?bot_id={self.bot.id}",
            headers={"Authorization": "", "X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertIn(st, (401, 403))

    def test_loopback_memory_is_not_the_ui_transport(self) -> None:
        src = (ROOT / "ui" / "working-memory.js").read_text(encoding="utf-8")
        self.assertNotIn("/v1/memory/", src)
        self.assertIn("/working-memory", src)


class WorkingMemoryUiTests(unittest.TestCase):
    def test_indicator_and_drawer_in_classic_scripts(self) -> None:
        index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        js = (ROOT / "ui" / "working-memory.js").read_text(encoding="utf-8")
        self.assertIn("wm-indicator", index)
        self.assertIn("🧠", index)
        self.assertIn("wm-drawer", index)
        self.assertIn("Working Memory", index)
        self.assertIn("Inspect Context Items", index)
        self.assertIn("Long-Term Memory", index)
        self.assertIn("Retrieval", index)
        self.assertIn("Context History", index)
        css = (ROOT / "ui" / "hermesbot.css").read_text(encoding="utf-8")
        tabs = css.split(".wm-tabs {", 1)[1].split(".wm-tab {", 1)[0]
        self.assertIn("flex-wrap: wrap", tabs)
        self.assertIn("flex: 0 0 auto", tabs)
        self.assertNotIn("overflow-x: auto", tabs)
        self.assertIn("ACTIVE NOW", js)
        self.assertIn("STORED ONLY", js)
        self.assertIn("IN CONTEXT", js)
        self.assertNotIn("type=\"module\"", index)
        self.assertIn('<script src="/working-memory.js', index)
        self.assertIn("WorkingMemory.syncFromBot", app)
        self.assertIn("WorkingMemory.onSelectBot", app)
        self.assertIn("function onSelectBot", js)
        self.assertIn("boundBotId", js)
        self.assertIn("fetchGen", js)
        self.assertNotIn("from './working-memory", app)
        self.assertNotIn("from \"./working-memory", js)

    def _node(self) -> str:
        from shutil import which

        found = which("node")
        if found:
            return found
        cand = Path.home() / ".local/share/mamba/envs/grokdesk/bin/node"
        if cand.is_file():
            return str(cand)
        self.skipTest("node binary not on PATH")
        return "node"

    def test_node_syntax_and_format_compact(self) -> None:
        node = self._node()
        for rel in ("ui/working-memory.js", "ui/app.js"):
            result = subprocess.run(
                [node, "--check", str(ROOT / rel)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const src = fs.readFileSync('ui/working-memory.js', 'utf8');
vm.runInThisContext(src);
assert.equal(WorkingMemory.pressureFromUtilization(68342/262144), 'GREEN');
assert.equal(WorkingMemory.formatCompact(68342, 262144, 'GREEN'), '🧠 68K / 262K · GREEN');
assert.ok(WorkingMemory.formatCompact(null, 262144, null).includes('Unavailable'));
console.log('frontend formula ok');
"""
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frontend formula ok", result.stdout)

    def test_existing_context_meter_still_present(self) -> None:
        index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("context-usage", index)
        self.assertIn("context-dot", index)
        self.assertIn(">Context</span>", index)
        self.assertIn("data-pressure", app)
        self.assertIn("function renderMeta(", app)
        self.assertIn("id=\"mic\"", index)
        self.assertIn("robot-simulator", index)


if __name__ == "__main__":
    unittest.main()
