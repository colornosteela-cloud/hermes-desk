#!/usr/bin/env python3
"""Intelligent working-set: select, persist, compact, reconstruct — shipped assemble path."""

from __future__ import annotations

import json
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

import body_state as bs  # noqa: E402
import context_manager as cm  # noqa: E402
import deskd as d  # noqa: E402
import working_memory as wm  # noqa: E402
from memory import MemoryManager, Role, RuleBasedSummarizer, TokenBudget  # noqa: E402


class ContextManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d.HERMES_DESKS = self.root / "desks"
        d.HERMES_DESKS.mkdir()
        self.bot = d.Bot("b_ctxmgr000001", "Teela", "", "", "qwen38-27b-q5", "🤖")
        self.bot.workspace = self.root / "ws"
        self.bot.workspace.mkdir()
        self.bot.root = self.root / "bot"
        self.bot.root.mkdir()
        self.bot._memory = MemoryManager(
            self.bot.workspace / ".memory",
            summarizer=RuleBasedSummarizer(),
            model_id="qwen38-27b-q5",
        )
        self.bot.messages = []

    def tearDown(self) -> None:
        wm._ledgers.pop(self.bot.id, None)
        self.tmp.cleanup()

    def _fill_chat(self, n: int, prefix: str = "chatter") -> None:
        mgr = self.bot.memory
        for i in range(n):
            u = f"{prefix} {i} " + ("word " * 80)
            a = f"ack {i} " + ("word " * 40)
            mgr.append_turn(Role.USER, u)
            mgr.append_turn(Role.ASSISTANT, a)
            self.bot.messages.append({"role": "user", "text": u})
            self.bot.messages.append({"role": "assistant", "text": a})

    def test_ordinary_turns_stay_bounded(self) -> None:
        counts: list[int] = []
        for i in range(36):
            u = f"hello {i} how are you"
            a = f"fine {i}"
            self.bot.memory.append_turn(Role.USER, u)
            self.bot.memory.append_turn(Role.ASSISTANT, a)
            self.bot.messages.append({"role": "user", "text": u})
            self.bot.messages.append({"role": "assistant", "text": a})
            payload = d.assemble_teela_executive_payload(self.bot, u)
            assembled = self.bot.memory.last_assembly
            if assembled is not None:
                counts.append(assembled.token_count)
                self.assertLess(assembled.token_count, 262144)
            roles = [m.get("role") for m in payload.get("messages") or []]
            self.assertLessEqual(roles.count("user") + roles.count("assistant"), 30)
        self.assertTrue(counts)
        self.assertLess(max(counts), 50000)
        self.assertLessEqual(self.bot.memory.working.total_tokens, 4000 + 2000)

    def test_correction_persists_and_returns_after_compact(self) -> None:
        correction = "No, when I ask you to look at me, turn your head toward me first."
        self.bot.memory.append_turn(Role.USER, correction)
        self.bot.messages.append({"role": "user", "text": correction})
        self._fill_chat(24)
        self.assertTrue(self.bot.memory.force_compact() or any(e.get("type") == "CONTEXT_COMPACTED" for e in self.bot.memory.context_events))
        kinds = [e.get("type") for e in self.bot.memory.context_events]
        self.assertIn("CONTEXT_COMPACTED", kinds)
        hits = self.bot.memory.retrieve("look at me head", limit=8)
        self.assertTrue(any("turn your head" in h.text.lower() or "look at me" in h.text.lower() for h in hits))
        payload = d.assemble_teela_executive_payload(self.bot, "please look at me")
        self.assertTrue(payload.get("messages"))
        items = wm.build_items(self.bot).get("items") or []
        corr = [
            it
            for it in items
            if "USER_CORRECTION" in (it.get("reason_codes") or [])
            or "look" in str(it.get("title") or "").lower()
        ]
        self.assertTrue(corr, items)
        self.assertTrue(any(it.get("in_context") for it in corr))
        self.assertTrue(
            any("USER_CORRECTION" in (it.get("reason_codes") or []) or "SKILL_MATCH" in (it.get("reason_codes") or []) for it in corr)
        )

    def test_finish_fast_chat_records_occupancy_for_stall_reply(self) -> None:
        h = d.Handler.__new__(d.Handler)
        h._finish_fast_chat(self.bot, "One second — my local brain stalled. Say that again?")
        self.assertGreater(self.bot.context_used, 0)
        self.assertTrue(self.bot.context_source)
        snap = wm.build_summary(self.bot)
        self.assertEqual(snap["used_tokens"], self.bot.context_used)
        self.assertIsNotNone(snap["pressure"])

    def test_executive_llama_usage_ingests_occupancy(self) -> None:
        def fake_complete(_payload):
            return {
                "choices": [{"message": {"content": "Hello. What would you like to work on?"}}],
                "usage": {"prompt_tokens": 812, "completion_tokens": 18, "total_tokens": 830},
            }

        line = d.run_teela_executive_turn(self.bot, "hi", completer=fake_complete)
        self.assertTrue(line)
        self.assertEqual(self.bot.context_used, 830)
        self.assertEqual(self.bot.context_source, "local_runtime")
        snap = wm.build_summary(self.bot)
        self.assertEqual(snap["used_tokens"], 830)
        self.assertEqual(snap["pressure"], wm.pressure_from_utilization(830 / self.bot.context_window))

    def test_executive_llama_timings_set_tok_s(self) -> None:
        def fake_complete(_payload):
            return {
                "choices": [{"message": {"content": "Hello. What would you like to work on?"}}],
                "usage": {"prompt_tokens": 812, "completion_tokens": 18, "total_tokens": 830},
                "timings": {
                    "predicted_n": 18,
                    "predicted_ms": 225.0,
                    "predicted_per_second": 80.0,
                },
            }

        line = d.run_teela_executive_turn(self.bot, "hi", completer=fake_complete)
        self.assertTrue(line)
        self.assertEqual(self.bot.tps, 80.0)
        self.assertEqual(self.bot.speed_source, "local_runtime")
        h = d.Handler.__new__(d.Handler)
        h._finish_fast_chat(self.bot, line)
        self.assertEqual(self.bot.tps, 80.0)
        self.assertEqual(self.bot.speed_source, "local_runtime")

    def test_leftover_momentum_goal_is_not_an_eternal_task(self) -> None:
        from teela_cl.deliberation import InteractionContext, save_momentum

        cm.save_task_checkpoint(
            self.bot,
            {"goal": "wave at me", "status": "RUNNING", "current_step": "wave"},
        )
        save_momentum(
            self.bot.workspace / ".teela",
            InteractionContext(
                goal="wave at me",
                conversation_momentum="talk",
                user_feedback_expected=False,
                active_context="",
            ),
        )
        payload = d.assemble_teela_executive_payload(self.bot, "hi")
        blob = json.dumps(payload)
        self.assertNotIn("# Active task checkpoint", blob)
        rec = cm.load_task_checkpoint(self.bot)
        self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed")
        task = wm.build_summary(self.bot).get("active_task") or {}
        self.assertEqual(task.get("status"), "unavailable")

    def test_talk_does_not_revive_completed_checkpoint_from_leftover_goal(self) -> None:
        from teela_cl.deliberation import InteractionContext, save_momentum

        cm.save_task_checkpoint(self.bot, {"goal": "wave at me", "status": "RUNNING"})
        cm.complete_task(self.bot)
        save_momentum(
            self.bot.workspace / ".teela",
            InteractionContext(goal="wave at me", conversation_momentum="talk"),
        )
        payload = d.assemble_teela_executive_payload(self.bot, "hello")
        self.assertNotIn("# Active task checkpoint", json.dumps(payload))
        rec = cm.load_task_checkpoint(self.bot)
        self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed")

        # Leftover training momentum (physical_training + feedback expected) must
        # not resurrect the same completed goal either.
        cm.complete_task(self.bot)
        save_momentum(
            self.bot.workspace / ".teela",
            InteractionContext(
                goal="wave at me",
                conversation_momentum="physical_training",
                user_feedback_expected=True,
                active_context="physical_training",
            ),
        )
        payload = d.assemble_teela_executive_payload(self.bot, "please look at me")
        blob = json.dumps(payload)
        self.assertNotIn("# Active task checkpoint", blob)
        self.assertNotIn("wave at me", blob)
        rec = cm.load_task_checkpoint(self.bot)
        self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed")
        task = wm.build_summary(self.bot).get("active_task") or {}
        self.assertEqual(task.get("status"), "unavailable")

        # Distinct leftover goal after a *different* completed task must not
        # open a new RUNNING checkpoint for any leftover-revival utterance,
        # including generic continue/resume (momentum must never write RUNNING).
        cm.save_task_checkpoint(
            self.bot,
            {
                "goal": "Draft the long design document",
                "status": "RUNNING",
                "current_step": "write findings",
                "remaining_steps": ["outline", "write"],
                "workspace": "section findings",
            },
        )
        d.assemble_teela_executive_payload(self.bot, "thanks, done")
        rec = cm.load_task_checkpoint(self.bot)
        self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed")
        save_momentum(
            self.bot.workspace / ".teela",
            InteractionContext(
                goal="wave at me",
                conversation_momentum="physical_training",
                user_feedback_expected=True,
                active_context="physical_training",
            ),
        )
        for utterance in (
            "hi",
            "please look at me",
            "continue",
            "keep going",
            "resume",
            "continue the design",
            "go on",
            "what's next",
        ):
            payload = d.assemble_teela_executive_payload(self.bot, utterance)
            blob = json.dumps(payload)
            rec = cm.load_task_checkpoint(self.bot)
            task = wm.build_summary(self.bot).get("active_task") or {}
            self.assertNotIn("# Active task checkpoint", blob, utterance)
            self.assertNotIn("Goal: wave at me", blob, utterance)
            self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed", utterance)
            self.assertNotEqual(str((rec or {}).get("goal") or "").lower(), "wave at me", utterance)
            self.assertEqual(task.get("status"), "unavailable", utterance)

    def test_live_assemble_completes_large_task_on_done(self) -> None:
        cm.save_task_checkpoint(
            self.bot,
            {
                "goal": "Draft the long design document",
                "status": "RUNNING",
                "current_step": "write findings",
                "remaining_steps": ["outline", "write", "review", "ship"],
                "workspace": ("section findings and evidence. " * 400),
            },
        )
        big = d.assemble_teela_executive_payload(self.bot, "continue the design")
        self.assertIn("# Active task checkpoint", json.dumps(big))
        n_big = int((self.bot.memory.last_assembly or type("A", (), {"token_count": 0})()).token_count)
        done = d.assemble_teela_executive_payload(self.bot, "thanks, done")
        self.assertNotIn("# Active task checkpoint", json.dumps(done))
        rec = cm.load_task_checkpoint(self.bot)
        self.assertEqual(str((rec or {}).get("status") or "").lower(), "completed")
        n_after = int((self.bot.memory.last_assembly or type("A", (), {"token_count": 10**9})()).token_count)
        self.assertLess(n_after, n_big)

    def test_checkpoint_loaded_emits_once_per_distinct_checkpoint(self) -> None:
        cm.save_task_checkpoint(
            self.bot,
            {
                "goal": "Calibrate visual head tracking",
                "status": "RUNNING",
                "current_step": "Verify gaze",
                "remaining_steps": ["measure", "adjust"],
            },
        )
        d.assemble_teela_executive_payload(self.bot, "continue")
        d.assemble_teela_executive_payload(self.bot, "continue")
        hist = wm.build_history(self.bot)
        loaded = [e for e in hist.get("events") or [] if e.get("type") == "TASK_CHECKPOINT_LOADED"]
        self.assertEqual(len(loaded), 1, loaded)

    def test_task_checkpoint_survives_compact_and_continue(self) -> None:
        cm.save_task_checkpoint(
            self.bot,
            {
                "goal": "Calibrate visual head tracking",
                "status": "RUNNING",
                "current_step": "Verify gaze follows target",
                "remaining_steps": ["measure", "adjust", "confirm"],
                "next_action": "Verify gaze follows target",
            },
        )
        self._fill_chat(20)
        self.assertTrue(self.bot.memory.force_compact() or "CONTEXT_COMPACTED" in [e.get("type") for e in self.bot.memory.context_events])
        payload = d.assemble_teela_executive_payload(self.bot, "continue")
        blob = json.dumps(payload)
        self.assertIn("Calibrate visual head tracking", blob)
        self.assertIn("Verify gaze follows target", blob)
        hist = wm.build_history(self.bot)
        types = {e.get("type") for e in hist.get("events") or []}
        self.assertIn("TASK_CHECKPOINT_SAVED", types)
        self.assertIn("TASK_CHECKPOINT_LOADED", types)

    def test_live_body_overrides_stale_memory(self) -> None:
        store = bs.store_for(self.bot.id, self.bot.root / "body_state.sqlite")
        store.apply_commanded(
            {"neck_pan": -12.4, "neck_tilt": 7.1},
            pose="standing",
            motion="idle",
        )
        self.bot.memory.write(
            "[TEELA BODY NOW]\nMode: simulated\nRevision: 9\nMotion: idle pose=kneel waving=True\nHead: yaw 40°",
            ["body", "pose"],
        )
        payload = d.assemble_teela_executive_payload(self.bot, "what is your pose")
        sys = str((payload.get("messages") or [{}])[0].get("content") or "")
        self.assertIn("standing", sys.lower())
        snap = store.snapshot()
        self.assertEqual(snap.get("pose"), "standing")
        body = wm.build_summary(self.bot)["body_state"]
        self.assertEqual(body.get("pose"), "standing")
        assembled = self.bot.memory.last_assembly
        if assembled is not None:
            retrieved = [it for it in assembled.items if it.get("section") == "retrieved_memory"]
            self.assertFalse(any("pose=kneel" in str(it.get("title") or "") for it in retrieved))

    def test_retrieval_caps_to_relevant_subset(self) -> None:
        mgr = self.bot.memory
        mgr.write("left arm encoder fault yesterday during raise", ["episodic", "left-arm"])
        mgr.write("left arm did not lift to commanded angle yesterday", ["episodic", "left-arm"])
        mgr.write("left arm calibration notes from yesterday session", ["episodic", "left-arm"])
        mgr.write("right arm wave greeting procedure taught last week", ["procedural", "wave"])
        mgr.write("pineapple orchard layout on the north field", ["orchard"])
        mgr.write("unrelated desktop window tiling preference", ["ui"])
        assembled = mgr.assemble_context(
            TokenBudget.for_model("qwen38-27b-q5"),
            "Why didn't your left arm move correctly yesterday?",
        )
        selected = (assembled.retrieval or {}).get("selected") or []
        self.assertLessEqual(len(selected), 3)
        blob = " ".join(str(x.get("title") or "") for x in selected).lower()
        self.assertIn("left arm", blob)
        self.assertNotIn("pineapple", blob)
        self.assertNotIn("desktop window", blob)
        if selected:
            self.assertTrue(assembled.retrieval.get("candidates_examined", 0) >= len(selected))

    def test_empty_retrieve_still_not_a_retrieval_event(self) -> None:
        assembled = self.bot.memory.assemble_context(
            TokenBudget.for_model("qwen38-27b-q5"),
            "zzzznonexistentqueryxyz",
        )
        self.assertEqual((assembled.retrieval or {}).get("selected") or [], [])
        wm.capture_turn(self.bot, {"messages": [{"role": "user", "content": "zzzz"}]}, assembled)
        self.bot.ingest_usage({"prompt_tokens": 4000, "completion_tokens": 10, "total_tokens": 4010})
        hist = wm.build_history(self.bot)
        self.assertTrue(hist.get("history"))
        self.assertFalse(hist["history"][-1]["retrieval"])
        self.assertNotIn("MEMORY_RETRIEVED", [e.get("type") for e in hist.get("events") or []])

    def test_large_task_expands_then_contracts_with_real_compact(self) -> None:
        small = cm.assemble_for_bot(
            self.bot,
            [{"role": "user", "content": "hi"}],
            "hi",
        )
        cm.save_task_checkpoint(
            self.bot,
            {
                "goal": "Draft the long design document",
                "status": "RUNNING",
                "current_step": "write findings",
                "remaining_steps": ["outline", "write", "review", "ship"],
                "workspace": ("section findings and evidence. " * 500),
            },
        )
        big = cm.assemble_for_bot(
            self.bot,
            [{"role": "user", "content": "continue the design"}],
            "continue the design",
        )
        self.assertGreater(big.token_count, small.token_count)
        self.assertLess(big.token_count, 262144)
        cm.complete_task(self.bot)
        self._fill_chat(20)
        self.assertTrue(self.bot.memory.force_compact())
        after = cm.assemble_for_bot(
            self.bot,
            [{"role": "user", "content": "thanks, done"}],
            "thanks, done",
        )
        self.assertLess(after.token_count, big.token_count)
        kinds = [e.get("type") for e in self.bot.memory.context_events]
        self.assertIn("CONTEXT_COMPACTED", kinds)
        compact_ev = [e for e in self.bot.memory.context_events if e.get("type") == "CONTEXT_COMPACTED"]
        self.assertTrue(compact_ev)
        self.assertIn("before", compact_ev[-1])
        self.assertIn("after", compact_ev[-1])

    def test_occupancy_drop_still_not_compaction(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 100000, "completion_tokens": 0, "total_tokens": 100000})
        self.bot.ingest_usage({"prompt_tokens": 80000, "completion_tokens": 0, "total_tokens": 80000})
        hist = wm.build_history(self.bot).get("history") or []
        self.assertGreaterEqual(len(hist), 2)
        self.assertEqual(hist[-1]["tokens_used"], 80000)
        self.assertFalse(hist[-1]["compaction"])

    def test_frame_dumps_do_not_flood_working_set(self) -> None:
        dump = "\n".join(f"frame {98123 + i} raw rgb description" for i in range(12))
        from memory import extract_tool_result

        trimmed = extract_tool_result(dump)
        self.assertIn("perception frames omitted", trimmed)
        self.assertLess(len(trimmed), len(dump))
        self.bot.memory.write(dump, ["perception", "frames"])
        assembled = self.bot.memory.assemble_context(
            TokenBudget.for_model("qwen38-27b-q5"),
            "frame 98123 raw rgb",
        )
        selected = (assembled.retrieval or {}).get("selected") or []
        self.assertFalse(any("98123" in str(x.get("title") or "") for x in selected))

    def test_world_perception_kv_stay_unavailable(self) -> None:
        d.assemble_teela_executive_payload(self.bot, "hello")
        snap = wm.build_summary(self.bot)
        self.assertEqual(snap["world_state"]["status"], "unavailable")
        self.assertEqual(snap["perception"]["status"], "unavailable")
        self.assertEqual(snap["kv"]["status"], "unavailable")

    def test_no_cot_and_redaction_on_manager_path(self) -> None:
        self.bot.memory.write("keep api_key=sk-secret-value password=hunter2 out", ["secret-test"])
        assembled = cm.assemble_for_bot(
            self.bot,
            [{"role": "user", "content": "keep login recipe"}],
            "keep login recipe api_key",
        )
        payload = {"messages": [{"role": "system", "content": assembled.text}], "password": "nope"}
        wm.capture_turn(self.bot, payload, assembled)
        blob = json.dumps(wm.build_snapshot(self.bot))
        self.assertNotIn("sk-secret-value", blob)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("chain_of_thought", blob)
        self.assertNotIn("scratchpad", blob)


class ContextManagerHttpTests(unittest.TestCase):
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
        d.TOKEN_PATH.write_text("cm-test-token", encoding="utf-8")
        self.bot = d.Bot("b_cmhttp0000001", "Mem", "", "", "qwen38-27b-q5", "x")
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

    def _req(self, method: str, path: str, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Authorization": "Bearer cm-test-token"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode() or "null")
        except json.JSONDecodeError:
            parsed = data
        return resp.status, parsed

    def test_manifest_after_assemble_compact_retrieve(self) -> None:
        self.bot.ingest_usage({"prompt_tokens": 2200, "completion_tokens": 40, "total_tokens": 2240})
        self.bot.memory.write("look_at_user correction: face the person", ["correction", "procedural"])
        for i in range(16):
            self.bot.memory.append_turn(Role.USER, f"pad {i} " + ("word " * 50))
        self.bot.memory.force_compact()
        d.assemble_teela_executive_payload(self.bot, "look_at_user correction")
        st1, a = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory")
        st2, b = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory")
        self.assertEqual(st1, 200, a)
        self.assertEqual(st2, 200, b)
        self.assertEqual(a["used_tokens"], 2240)
        self.assertEqual(a["used_tokens"], self.bot.context_used)
        self.assertEqual(a["used_tokens"], b["used_tokens"])
        self.assertEqual(a["capacity_tokens"], self.bot.context_window)
        self.assertEqual(a["pressure"], wm.pressure_from_utilization(2240 / self.bot.context_window))
        st, items = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/items?section=retrieved_memory")
        self.assertEqual(st, 200, items)
        st, hist = self._req("GET", f"/v1/bots/{self.bot.id}/working-memory/history")
        self.assertEqual(st, 200, hist)
        types = {e.get("type") for e in hist.get("events") or []}
        if "MEMORY_RETRIEVED" in types:
            self.assertTrue(items.get("items"))
        drops = [h for h in hist.get("history") or [] if h.get("compaction")]
        if drops:
            self.assertTrue(any(e.get("type") == "CONTEXT_COMPACTED" for e in hist.get("events") or []))
        st, _ = self._req(
            "GET",
            f"/v1/bots/{self.bot.id}/working-memory",
            headers={"Authorization": "", "X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertIn(st, (401, 403))


class ContextManagerUiTests(unittest.TestCase):
    def test_manifest_chrome_and_no_new_module(self) -> None:
        index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "ui" / "working-memory.js").read_text(encoding="utf-8")
        self.assertIn("wm-indicator", index)
        self.assertIn("wm-drawer", index)
        self.assertIn('<script src="/working-memory.js', index)
        self.assertNotIn('type="module"', index)
        self.assertIn("IN CONTEXT", js)
        self.assertIn("STORED ONLY", js)

    def test_node_check_touched_scripts(self) -> None:
        from shutil import which

        node = which("node")
        if not node:
            cand = Path.home() / ".local/share/mamba/envs/grokdesk/bin/node"
            node = str(cand) if cand.is_file() else None
        if not node:
            self.skipTest("node binary not on PATH")
        for rel in ("ui/working-memory.js", "ui/app.js"):
            result = subprocess.run([node, "--check", str(ROOT / rel)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
