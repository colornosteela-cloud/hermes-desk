#!/usr/bin/env python3
"""Profile-aware shared memory: build vs embodied on the shipped assemble path."""

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
import cognitive_profile as cprof  # noqa: E402
import context_manager as cm  # noqa: E402
import deskd as d  # noqa: E402
import embodied_memory as emem  # noqa: E402
import working_memory as wm  # noqa: E402
from memory import MemoryManager, Role, RuleBasedSummarizer  # noqa: E402


class ProfileMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d.HERMES_DESKS = self.root / "desks"
        d.HERMES_DESKS.mkdir()

    def tearDown(self) -> None:
        wm._ledgers.pop("b_build00000001", None)
        wm._ledgers.pop("b_embod00000001", None)
        self.tmp.cleanup()

    def _bot(self, bid: str, *, kind: str = "", profile: str | None = None, name: str = "Bot") -> d.Bot:
        bot = d.Bot(bid, name, "", "", "qwen38-27b-q5", "🤖", kind=kind)
        bot.workspace = self.root / f"ws_{bid}"
        bot.workspace.mkdir()
        bot.root = self.root / f"root_{bid}"
        bot.root.mkdir()
        bot._memory = MemoryManager(
            bot.workspace / ".memory",
            summarizer=RuleBasedSummarizer(),
            model_id="qwen38-27b-q5",
        )
        bot.messages = []
        if profile:
            bot.cognitive_profile = profile
        return bot

    def test_explicit_profile_beats_display_name(self) -> None:
        teela_named = self._bot("b_build00000001", profile=cprof.PROFILE_BUILD, name="Teela")
        self.assertEqual(cprof.profile_name(teela_named), cprof.PROFILE_BUILD)
        self.assertFalse(cprof.has_cap(teela_named, "body_state"))
        embodied = self._bot("b_embod00000001", kind="teela-brain", name="Notes")
        self.assertEqual(cprof.profile_name(embodied), cprof.PROFILE_EMBODIED)
        self.assertTrue(cprof.has_cap(embodied, "body_state"))

    def test_build_bot_omits_embodied_sections_embodied_can_load(self) -> None:
        build = self._bot("b_build00000001", kind="hermes")
        teela = self._bot("b_embod00000001", kind="teela-brain")
        build.memory.write("project decision: keep occupancy honest", ["semantic", "project"])
        teela.memory.write("project decision: keep occupancy honest", ["semantic", "project"])
        facade = emem.for_bot(teela)
        facade.observe("blue_cup", kind="object", attrs={"location": "desk"}, source="CAMERA_LEFT")
        facade.record_event(type="object_interaction", subject="user", action="picked_up", object="blue_cup")
        store = bs.store_for(teela.id, teela.root / "body_state.sqlite")
        store.apply_commanded({"neck_pan": -12.4, "right_shoulder": 0}, pose="standing")
        b_payload = d.assemble_teela_executive_payload(build, "what is the project status")
        t_payload = d.assemble_teela_executive_payload(teela, "where is the cup")
        b_blob = json.dumps(b_payload)
        t_blob = json.dumps(t_payload)
        self.assertNotIn("[TEELA BODY NOW]", b_blob)
        self.assertNotIn("[TEELA WORLD NOW]", b_blob)
        self.assertNotIn("blue_cup", b_blob)
        self.assertIn("[TEELA BODY NOW]", t_blob)
        self.assertIn("[TEELA WORLD NOW]", t_blob)
        self.assertIn("blue_cup", t_blob)
        b_sum = wm.build_summary(build)
        t_sum = wm.build_summary(teela)
        self.assertEqual(b_sum["cognitive_profile"], cprof.PROFILE_BUILD)
        self.assertEqual(t_sum["cognitive_profile"], cprof.PROFILE_EMBODIED)
        self.assertEqual(b_sum["body_state"]["status"], "unavailable")
        self.assertNotEqual(b_sum["body_state"].get("pose"), 0)
        self.assertIsNone(b_sum["sections"].get("body_state") if b_sum.get("assembly_at") else None)
        self.assertEqual(t_sum["world_state"]["status"], "ok")
        self.assertIn("blue_cup", t_sum["world_state"].get("objects") or {})
        self.assertEqual(t_sum["perception"]["status"], "ok")
        self.assertGreaterEqual(int(t_sum["perception"].get("event_count") or 0), 1)
        self.assertNotEqual(t_sum["body_state"].get("status"), "unavailable")
        self.assertEqual(t_sum["body_state"].get("authority"), "MiniOS/body-state")

    def test_minios_body_overrides_conversation_claim(self) -> None:
        teela = self._bot("b_embod00000001", kind="teela-brain")
        teela.memory.write("My right arm is raised.", ["episodic", "body"])
        teela.messages.append({"role": "assistant", "text": "My right arm is raised."})
        store = bs.store_for(teela.id, teela.root / "body_state.sqlite")
        store.apply_commanded(
            {"right_shoulder": 0.0, "right_elbow": 0.0, "neck_pan": 0.0},
            pose="standing",
            motion="idle",
        )
        payload = d.assemble_teela_executive_payload(teela, "is your right arm raised")
        blob = json.dumps(payload).lower()
        self.assertIn("right arm", blob)
        self.assertIn("resting", blob)
        self.assertIn("minios/body-state", blob)
        body = wm.build_summary(teela)["body_state"]
        self.assertEqual(body.get("authority"), "MiniOS/body-state")
        self.assertEqual(body.get("confirmation"), "current_confirmed")
        assembled = teela.memory.last_assembly
        if assembled is not None:
            retrieved = [it for it in assembled.items if it.get("section") == "retrieved_memory"]
            self.assertFalse(any("right arm is raised" in str(it.get("title") or "").lower() for it in retrieved))

    def test_stale_world_object_is_not_currently_visible(self) -> None:
        teela = self._bot("b_embod00000001", kind="teela-brain")
        facade = emem.for_bot(teela)
        facade.observe("blue_cup", kind="object", attrs={"location": "desk"}, confidence=0.93, source="CAMERA_LEFT")
        first = d.assemble_teela_executive_payload(teela, "what is on the desk")
        self.assertIn("blue_cup", json.dumps(first))
        world = wm.build_summary(teela)["world_state"]
        self.assertEqual(world["status"], "ok")
        self.assertTrue((world.get("objects") or {}).get("blue_cup", {}).get("currently_visible"))
        facade.mark_stale("blue_cup", reason="removed")
        second = d.assemble_teela_executive_payload(teela, "is the cup still there")
        blob = json.dumps(second)
        self.assertIn("Stale (not current)", blob)
        world2 = wm.build_summary(teela)["world_state"]
        self.assertNotIn("blue_cup", world2.get("objects") or {})
        self.assertIn("blue_cup", world2.get("stale") or {})

    def test_perception_frames_do_not_fill_linearly(self) -> None:
        teela = self._bot("b_embod00000001", kind="teela-brain")
        facade = emem.for_bot(teela)
        for i in range(40):
            facade.ingest_frame(98123 + i, f"frame {98123 + i} user visible rgb")
            facade.record_event(type="presence", subject="user", action="visible", object="workspace")
        facade.record_event(type="object_interaction", subject="user", action="picked_up", object="blue_cup")
        stats = facade.perception_stats()
        self.assertGreaterEqual(int((stats or {}).get("dropped_frames") or 0), 40)
        self.assertLessEqual(int((stats or {}).get("event_count") or 0), 3)
        payload = d.assemble_teela_executive_payload(teela, "what did you see")
        blob = json.dumps(payload)
        self.assertLess(blob.lower().count("frame 981"), 3)
        self.assertIn("picked_up", blob)
        n = int((teela.memory.last_assembly or type("A", (), {"token_count": 0})()).token_count)
        self.assertLess(n, 8000)

    def test_sensorimotor_episode_records_closed_loop(self) -> None:
        import virtual_body

        teela = self._bot("b_embod00000001", kind="teela-brain")
        store = bs.store_for(teela.id, teela.root / "body_state.sqlite")
        store.apply_commanded({"neck_pan": -30.0}, pose="standing")
        hit = virtual_body.teela_args_from_intent("look at me")
        self.assertIsNotNone(hit)
        name, args = hit
        result = d.dispatch_teela_minios_tool(teela, name, args)
        self.assertTrue(isinstance(result, dict))
        facade = emem.for_bot(teela)
        hits = facade.retrieve_episodes("look at the user")
        self.assertTrue(hits, "MiniOS look/orient must write a sensorimotor episode")
        ep = hits[0]
        self.assertEqual(ep.get("type"), "sensorimotor_episode")
        self.assertIn("head_pan", ep.get("before") or {})
        self.assertTrue(ep.get("action"))
        self.assertNotEqual((ep.get("action") or {}).get("command"), "succeeded")
        self.assertIn("head_pan", ep.get("actual_result") or {})
        self.assertIn("success", ep.get("outcome") or {})
        self.assertIn("final_error_deg", ep.get("metrics") or {})
        if str((result or {}).get("status") or "") in {"executing", "started"} and result.get("ok") is not True:
            self.assertFalse((ep.get("outcome") or {}).get("success"))
        payload = d.assemble_teela_executive_payload(teela, "look at the user again")
        blob = json.dumps(payload)
        self.assertIn("SENSORIMOTOR EPISODE", blob)
        self.assertIn("look_at_user", blob)

    def test_embodied_correction_attaches_to_skill_and_retry_episode(self) -> None:
        import virtual_body

        teela = self._bot("b_embod00000001", kind="teela-brain")
        store = bs.store_for(teela.id, teela.root / "body_state.sqlite")
        store.apply_commanded({"neck_pan": -12.0}, pose="standing")
        hit = virtual_body.teela_args_from_intent("look at me")
        self.assertIsNotNone(hit)
        d.dispatch_teela_minios_tool(teela, hit[0], hit[1])
        teela._teela_applied_caps = ["orient_head"]
        teela._teela_tools_used = [hit[0]]
        d.teela_record_attempt(teela, "look at me")
        corr = "When you look at me, turn your head toward me first."
        d.learn_from_user(teela, corr)
        d.teela_handle_feedback_turn(teela, corr)
        payload = d.assemble_teela_executive_payload(teela, "please look at me")
        blob = json.dumps(payload)
        self.assertIn("turn your head toward me first", blob)
        items = wm.build_items(teela).get("items") or []
        self.assertTrue(
            any("USER_CORRECTION" in (it.get("reason_codes") or []) or "SKILL_MATCH" in (it.get("reason_codes") or []) for it in items)
        )
        d.dispatch_teela_minios_tool(teela, hit[0], hit[1])
        facade = emem.for_bot(teela)
        sk = facade.motor_skill("look_at_user")
        self.assertTrue(sk)
        self.assertTrue(sk.get("corrections") or sk.get("last_correction"))
        eps = facade.retrieve_episodes("look_at_user")
        self.assertGreaterEqual(len(eps), 2)
        self.assertTrue(any((e.get("outcome") or {}).get("user_correction") or e.get("user_feedback") for e in eps))

    def test_durable_embodied_knowledge_survives_reconstruct_live_body_wins(self) -> None:
        teela = self._bot("b_embod00000001", kind="teela-brain")
        facade = emem.for_bot(teela)
        facade.relate("monitor", "ON", "desk", source="CAMERA_LEFT")
        d.learn_from_user(teela, "When you look at me, turn your head toward me first.")
        hit = __import__("virtual_body").teela_args_from_intent("look at me")
        if hit:
            d.dispatch_teela_minios_tool(teela, hit[0], hit[1])
        store = bs.store_for(teela.id, teela.root / "body_state.sqlite")
        store.apply_commanded({"neck_pan": 18.0}, pose="standing")
        teela.memory.write("My head is currently turned 90 degrees left", ["episodic", "pose"])
        path = teela.root / "body_state.sqlite"
        bs._stores.clear()
        restored = bs.BodyStateStore(path, bot_id=teela.id)
        self.assertEqual(restored.snapshot().get("confirmation"), "last_known")
        self.assertAlmostEqual(float(restored.snapshot()["joints"]["neck_pan"]["actual"]), 18.0, places=1)
        restored.close()
        bs._stores.clear()
        live = bs.store_for(teela.id, path)
        live.apply_measured({"neck_pan": 18.0}, pose="standing", source="simulation")
        self.assertEqual(live.snapshot().get("confirmation"), "current_confirmed")
        payload = d.assemble_teela_executive_payload(teela, "look at me")
        blob = json.dumps(payload)
        self.assertIn("turn your head toward me first", blob)
        self.assertIn("monitor ON desk", blob)
        self.assertIn("SENSORIMOTOR EPISODE", blob)
        body_chunk = blob.split("[TEELA BODY NOW]")[-1][:800] if "[TEELA BODY NOW]" in blob else blob
        self.assertNotIn("90 degrees", body_chunk)

    def test_proposed_procedure_is_not_auto_validated(self) -> None:
        teela = self._bot("b_embod00000001", kind="teela-brain")
        facade = emem.for_bot(teela)
        facade.propose_procedure("look_at_user", ["guess the angle"], source="QWEN_INFERENCE")
        sk = facade.motor_skill("look_at_user")
        self.assertFalse(sk.get("validated"))
        self.assertIsNotNone(sk.get("proposed_procedure"))
        fail = facade.record_episode(
            {
                "goal": "look_at_user",
                "action": {"skill": "look_at_user"},
                "outcome": {"success": False},
            }
        )
        sk2 = facade.validate_from_episode("look_at_user", fail)
        self.assertFalse(sk2.get("validated"))
        self.assertIsNotNone(sk2.get("proposed_procedure"))


class ProfileMemoryHttpTests(unittest.TestCase):
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
        d.TOKEN_PATH.write_text("profile-token", encoding="utf-8")
        self.build = d.Bot("b_buildhttp0001", "Coder", "", "", "qwen38-27b-q5", "x", kind="hermes")
        self.teela = d.Bot("b_embodhttp0001", "Teela", "", "", "qwen38-27b-q5", "x", kind="teela-brain")
        for bot in (self.build, self.teela):
            bot.root = home / f"root_{bot.id}"
            bot.root.mkdir()
            bot.workspace.mkdir(parents=True)
            bot._memory = MemoryManager(
                bot.workspace / ".memory",
                summarizer=RuleBasedSummarizer(),
                model_id="qwen38-27b-q5",
            )
            d.bots[bot.id] = bot
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        d.bots.pop(self.build.id, None)
        d.bots.pop(self.teela.id, None)
        wm._ledgers.pop(self.build.id, None)
        wm._ledgers.pop(self.teela.id, None)
        d.HERMES_DESKS = self._orig_desks
        d.TOKEN_PATH = self._orig_token
        self.tmp.cleanup()

    def _req(self, path: str, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Authorization": "Bearer profile-token"}
        if headers:
            hdrs.update(headers)
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode() or "null")
        except json.JSONDecodeError:
            parsed = data.decode()
        return resp.status, parsed

    def test_http_manifest_profile_split(self) -> None:
        self.build.ingest_usage({"prompt_tokens": 1200, "completion_tokens": 20, "total_tokens": 1220})
        self.teela.ingest_usage({"prompt_tokens": 1800, "completion_tokens": 40, "total_tokens": 1840})
        facade = emem.for_bot(self.teela)
        facade.observe("blue_cup", attrs={"location": "desk"})
        store = bs.store_for(self.teela.id, self.teela.root / "body_state.sqlite")
        store.apply_commanded({"neck_pan": 4.0}, pose="standing")
        d.assemble_teela_executive_payload(self.build, "summarize the repo")
        d.assemble_teela_executive_payload(self.teela, "where is the cup")
        st1, a = self._req(f"/v1/bots/{self.build.id}/working-memory")
        st2, b = self._req(f"/v1/bots/{self.build.id}/working-memory")
        st3, t = self._req(f"/v1/bots/{self.teela.id}/working-memory")
        st4, t2 = self._req(f"/v1/bots/{self.teela.id}/working-memory")
        self.assertEqual([st1, st2, st3, st4], [200, 200, 200, 200])
        self.assertEqual(a["used_tokens"], 1220)
        self.assertEqual(a["used_tokens"], b["used_tokens"])
        self.assertEqual(t["used_tokens"], 1840)
        self.assertEqual(t["used_tokens"], t2["used_tokens"])
        self.assertEqual(a["cognitive_profile"], cprof.PROFILE_BUILD)
        self.assertEqual(t["cognitive_profile"], cprof.PROFILE_EMBODIED)
        self.assertEqual(a["body_state"]["status"], "unavailable")
        self.assertEqual(a["world_state"]["status"], "unavailable")
        self.assertNotEqual(a["body_state"].get("joints"), 0)
        self.assertEqual(t["world_state"]["status"], "ok")
        self.assertNotEqual(t["body_state"].get("status"), "unavailable")
        stc, _ = self._req(
            f"/v1/bots/{self.teela.id}/working-memory",
            headers={"Authorization": "", "X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertIn(stc, (401, 403))


class ProfileMemoryUiTests(unittest.TestCase):
    def test_chrome_still_classic(self) -> None:
        index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "ui" / "working-memory.js").read_text(encoding="utf-8")
        self.assertIn("wm-indicator", index)
        self.assertIn('<script src="/working-memory.js', index)
        self.assertNotIn('type="module"', index)
        self.assertIn("IN CONTEXT", js)
        self.assertIn("Body State", js)
        self.assertIn("World State", js)

    def test_node_check(self) -> None:
        from shutil import which

        node = which("node")
        if not node:
            cand = Path.home() / ".local/share/mamba/envs/grokdesk/bin/node"
            node = str(cand) if cand.is_file() else None
        if not node:
            self.skipTest("node binary not on PATH")
        result = subprocess.run([node, "--check", str(ROOT / "ui" / "working-memory.js")], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
