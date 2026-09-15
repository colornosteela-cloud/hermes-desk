#!/usr/bin/env python3
"""MiniOS workspace safety, dev-system detection, and frontend security smoke tests."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
from surfaces import resolve_chrome_bin  # noqa: E402


class MiniOSWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.bot = d.Bot("b_test", "Test", "", "", "grok-4.6", "🤖")
        self.bot.workspace = self.workspace

    def tearDown(self) -> None:
        try:
            self.bot.stop_dev_process()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_ubuntu_home_folders_are_created(self) -> None:
        self.bot.ensure_home_dirs()
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Trash"):
            self.assertTrue((self.workspace / name).is_dir(), name)

    def test_text_document_writes_under_documents(self) -> None:
        self.bot.ensure_home_dirs()
        target = d.workspace_target(self.bot, "Documents/Untitled.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hello from notepad", encoding="utf-8")
        self.assertEqual((self.workspace / "Documents" / "Untitled.txt").read_text(encoding="utf-8"), "hello from notepad")

    def test_trash_moves_folder_into_trash(self) -> None:
        self.bot.ensure_home_dirs()
        folder = self.workspace / "Documents" / "old"
        folder.mkdir(parents=True)
        (folder / "note.txt").write_text("x", encoding="utf-8")
        out = self.bot.trash_item("Documents/old")
        self.assertTrue(out.get("ok"))
        self.assertFalse(folder.exists())
        self.assertTrue((self.workspace / "Trash" / "old" / "note.txt").is_file())

    def test_empty_trash_and_permanent_delete(self) -> None:
        self.bot.root = self.base / "bot-root"
        self.bot.root.mkdir()
        self.bot.ensure_home_dirs()
        pic = self.workspace / "Pictures" / "shot.jpg"
        pic.write_bytes(b"jpeg")
        self.bot.messages = [{"role": "assistant", "text": "shot", "images": [{"path": "Pictures/shot.jpg", "mime": "image/jpeg"}]}]
        self.bot.trash_item("Pictures/shot.jpg")
        self.assertFalse(pic.exists())
        self.assertTrue((self.workspace / "Trash" / "shot.jpg").is_file())
        self.assertEqual(self.bot.messages[0].get("images"), [])
        vid = self.workspace / "Videos" / "clip.mp4"
        vid.parent.mkdir(exist_ok=True)
        vid.write_bytes(b"mp4")
        self.bot.delete_item("Videos/clip.mp4")
        self.assertFalse(vid.exists())
        leftover = self.workspace / "Trash" / "old.txt"
        leftover.write_text("x", encoding="utf-8")
        out = self.bot.empty_trash()
        self.assertTrue(out.get("ok"))
        self.assertGreaterEqual(out.get("removed"), 1)
        self.assertFalse(leftover.exists())
        self.assertTrue((self.workspace / "Trash").is_dir())

    def test_workspace_target_accepts_child(self) -> None:
        p = self.workspace / "src" / "app.js"
        p.parent.mkdir()
        p.write_text("ok", encoding="utf-8")
        self.assertEqual(d.workspace_target(self.bot, "src/app.js", must_exist=True), p.resolve())

    def test_workspace_target_rejects_sibling_prefix_escape(self) -> None:
        sibling = self.base / "workspace-secret"
        sibling.mkdir()
        (sibling / "token.txt").write_text("secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            d.workspace_target(self.bot, "../workspace-secret/token.txt", must_exist=True)

    def test_shared_workspace_requires_user_grant(self) -> None:
        other_ws = self.base / "other-ws"
        other_ws.mkdir()
        (other_ws / "note.txt").write_text("hello from other", encoding="utf-8")
        (other_ws / ".memory").mkdir()
        (other_ws / ".memory" / "secret.txt").write_text("private", encoding="utf-8")
        other = d.Bot("b_other", "Other", "", "", "grok-4.6", "🤖")
        other.workspace = other_ws
        d.bots[self.bot.id] = self.bot
        d.bots[other.id] = other
        try:
            with self.assertRaises(PermissionError):
                d.shared_workspace_target(self.bot, other, "note.txt", must_exist=True)
            other.workspace_share_with = [self.bot.id]
            target = d.shared_workspace_target(self.bot, other, "note.txt", must_exist=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello from other")
            with self.assertRaises(PermissionError):
                d.shared_workspace_target(self.bot, other, ".memory/secret.txt")
        finally:
            d.bots.pop(self.bot.id, None)
            d.bots.pop(other.id, None)

    def test_prefill_progress_is_tui_line_not_thought(self) -> None:
        class _FakeBot:
            id = "b_gb"
            kind = "hermes"
            status = "Ready"
            surface = "chat"
            control = "agent_controlled"
            acp = None
            _prefill_stop = None

        events: list = []
        real_emit = d.emit
        d.emit = lambda ev: events.append(ev)
        try:
            bot = _FakeBot()
            d.start_local_prefill_progress(bot, 100)
            try:
                kinds = [
                    e.get("update", {}).get("sessionUpdate")
                    for e in events
                    if e.get("type") == "session.update"
                ]
                self.assertIn("agent_progress", kinds)
                self.assertNotIn("agent_thought_chunk", kinds, "prefill wait must not create a Thought block")
                prog = next(e for e in events if e.get("type") == "session.update")
                self.assertIn("Reading the local-model prompt (100 tokens)", prog["update"]["content"]["text"])
                statuses = [e.get("text") for e in events if e.get("type") == "status"]
                self.assertEqual(statuses[0], "Thinking…")
            finally:
                d.stop_local_prefill_progress(bot)
        finally:
            d.emit = real_emit

    def test_detect_node_dev_system(self) -> None:
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build", "test": "vitest run", "dev": "vite"}}),
            encoding="utf-8",
        )
        dev = self.bot.detect_dev_system()
        self.assertEqual(dev["kind"], "node")
        self.assertEqual(dev["commands"]["build"], "npm run build")
        self.assertEqual(dev["commands"]["test"], "npm test")
        self.assertEqual(dev["commands"]["run"], "npm run dev")

    def test_background_dev_process_can_start_and_stop(self) -> None:
        command = "python3 -u -c \"import time; print('MINIOS_READY', flush=True); time.sleep(30)\""
        result = self.bot.start_dev_process(command)
        self.assertTrue(result["running"])
        deadline = time.time() + 3
        while time.time() < deadline and "MINIOS_READY" not in self.bot.dev_process_status()["output"]:
            time.sleep(0.03)
        self.assertIn("MINIOS_READY", self.bot.dev_process_status()["output"])
        stopped = self.bot.stop_dev_process()
        self.assertFalse(stopped["running"])

    def test_dated_media_paths(self) -> None:
        rel = d.dated_media_relpath("Pictures", "chat", ".png", hint="image.png", when=datetime(2026, 9, 3, 14, 30, 52))
        self.assertEqual(rel, "Pictures/2026/September/2026-09-03_14-30-52-chat.png")
        rel_v = d.dated_media_relpath("Videos", "video", ".mp4", hint="clip.mp4", when=datetime(2026, 9, 3, 8, 1, 0))
        self.assertEqual(rel_v, "Videos/2026/September/2026-09-03_08-01-00-video.mp4")

    def test_robot_joint_and_pose_commands(self) -> None:
        import robot_sim

        st = robot_sim.default_state()
        waved = robot_sim.apply(st, {"cmd": "pose", "pose": "wave"})
        self.assertTrue(waved["ok"])
        self.assertEqual(waved["pose"], "wave")
        self.assertEqual(waved["motion"], "waving")
        self.assertGreaterEqual(waved["joints"]["right_shoulder"], 20)
        self.assertLess(waved["joints"]["right_shoulder"], 40)
        self.assertGreaterEqual(waved["joints"]["right_elbow"], 100)
        self.assertLess(waved["joints"]["left_shoulder"], 30)
        self.assertEqual(waved["joints"]["right_wrist"], 0)
        self.assertLessEqual(waved["joints"].get("right_shoulder_out") or 0, 5)
        corrected = robot_sim.apply(
            st,
            {"cmd": "joints", "joints": {"right_shoulder": 40}, "pose": "wave", "motion": "waving"},
        )
        self.assertEqual(corrected["pose"], "wave")
        self.assertEqual(corrected["motion"], "waving")
        self.assertEqual(corrected["joints"]["right_shoulder"], 40)
        held = robot_sim.apply(st, {"cmd": "pose", "pose": "wave", "motion": "idle"})
        self.assertEqual(held["motion"], "idle")
        self.assertEqual(held["pose"], "wave")
        stopped = robot_sim.apply(st, {"cmd": "stop"})
        self.assertEqual(stopped["motion"], "idle")
        self.assertEqual(stopped["pose"], "neutral")
        robot_sim.apply(st, {"cmd": "pose", "pose": "arms_forward"})
        from_action = robot_sim.apply(st, {"cmd": "stop"})
        self.assertEqual(from_action["motion"], "idle")
        self.assertEqual(from_action["pose"], "neutral")
        self.assertLess(from_action["joints"]["right_shoulder"], 30)
        jointed = robot_sim.apply(st, {"cmd": "joint", "joint": "neck_pan", "value": 30})
        self.assertTrue(jointed["ok"])
        self.assertEqual(jointed["joints"]["neck_pan"], 30)
        st["joints"]["left_hip"] = 12
        st["joints"]["left_elbow"] = 0
        nudged = robot_sim.apply(st, {"cmd": "joint", "joint": "left_elbow", "dir": "flex", "delta": 10})
        self.assertTrue(nudged["ok"])
        self.assertEqual(nudged["joints"]["left_hip"], 12)
        self.assertEqual(nudged["joints"]["left_elbow"], 10)
        panned = robot_sim.apply(st, {"cmd": "nudge", "joint": "neck_pan", "dir": "left"})
        self.assertGreater(panned["joints"]["neck_pan"], 30)
        bad = robot_sim.apply(st, {"cmd": "joint", "joint": "left_wing", "value": 1})
        self.assertFalse(bad["ok"])
        tpose = robot_sim.apply(st, {"cmd": "pose", "pose": "tpose"})
        self.assertEqual(tpose["joints"]["left_shoulder_out"], 90)
        self.assertEqual(tpose["joints"]["right_shoulder_out"], 90)
        walked = robot_sim.apply(st, {"cmd": "walk", "direction": "left"})
        self.assertEqual(walked["motion"], "walking")
        self.assertEqual(walked["walk_direction"], "left")
        self.assertEqual(walked["heading"], "east")
        west = robot_sim.apply(st, {"cmd": "walk", "direction": "right"})
        self.assertEqual(west["heading"], "west")
        north = robot_sim.apply(st, {"cmd": "walk", "direction": "back"})
        self.assertEqual(north["heading"], "south")
        self.assertEqual(north["walk_direction"], "back")

    def test_robot_commanded_live_delta_and_phase(self) -> None:
        import robot_sim

        idle = robot_sim.public_status(robot_sim.default_state())
        self.assertIsNone(idle["phase"])
        self.assertIsNone(idle["temp"])
        self.assertIsNone(idle["load"])
        self.assertIsNone(idle["fall_flag"])
        self.assertEqual(idle["commanded"], idle["joints"])
        self.assertEqual(idle["live"], idle["commanded"])
        self.assertTrue(all(v == 0 for v in idle["delta"].values()))
        self.assertEqual(set(idle["commanded"]), set(robot_sim.JOINTS))
        spoken = robot_sim.describe_body(robot_sim.default_state())
        self.assertNotIn("commanded", spoken.lower())
        self.assertNotIn("Walk phase", spoken)
        self.assertNotIn("percent through a walking step", spoken)

        st = robot_sim.default_state()
        walked = robot_sim.apply(st, {"cmd": "walk"})
        self.assertEqual(walked["motion"], "walking")
        self.assertIsNotNone(walked["phase"])
        self.assertGreaterEqual(walked["phase"], 0.0)
        self.assertLess(walked["phase"], 1.0)
        self.assertIsNone(walked["temp"])
        self.assertIsNone(walked["load"])
        self.assertIsNone(walked["fall_flag"])
        self.assertTrue(all(v == 0 for v in walked["delta"].values()))
        self.assertIsNotNone(st.get("walk_started"))

        st["walk_started"] = 1000.0
        self.assertAlmostEqual(robot_sim.public_status(st, now=1000.0)["phase"], 0.0, places=5)
        half = 1000.0 + math.pi / robot_sim.WALK_PHASE_RAD_S
        self.assertAlmostEqual(robot_sim.public_status(st, now=half)["phase"], 0.5, places=5)
        cycle = 1000.0 + (2.0 * math.pi) / robot_sim.WALK_PHASE_RAD_S
        self.assertAlmostEqual(robot_sim.public_status(st, now=cycle)["phase"], 0.0, places=5)

        st["phase"] = 0.42
        self.assertAlmostEqual(robot_sim.public_status(st, now=half)["phase"], 0.42, places=5)
        st["phase"] = None

        mid = robot_sim.describe_body(st, now=half)
        self.assertIn("walking", mid.lower())
        self.assertIn("50 percent", mid)
        deg = robot_sim.describe_body(st, want_degrees=True, now=half)
        self.assertIn("Walk phase 0.50.", deg)
        self.assertNotIn("percent through a walking step", deg)
        self.assertNotIn("not matching the pose", deg)
        idle_match = dict(st)
        idle_match["motion"] = "idle"
        idle_match["pose"] = "ready"
        idle_match["walk_started"] = None
        idle_match["phase"] = None
        self.assertIn("Commanded matches live.", robot_sim.describe_body(idle_match, want_degrees=True))

        st["joints"] = dict(st["joints"])
        st["joints"]["right_knee"] = 10
        st["live"] = {**st["joints"], "right_knee": 40}
        lagged = robot_sim.public_status(st, now=half)
        self.assertAlmostEqual(lagged["commanded"]["right_knee"], 10)
        self.assertAlmostEqual(lagged["live"]["right_knee"], 40)
        self.assertAlmostEqual(lagged["delta"]["right_knee"], 30)
        self.assertEqual(lagged["joints"]["right_knee"], lagged["commanded"]["right_knee"])
        walk_lag = robot_sim.describe_body(st, now=half)
        self.assertNotIn("not matching the pose", walk_lag)
        idle_lag = dict(st)
        idle_lag["motion"] = "idle"
        idle_lag["pose"] = "ready"
        idle_lag["walk_started"] = None
        idle_lag["phase"] = None
        lag_speech = robot_sim.describe_body(idle_lag)
        self.assertIn("not matching the pose", lag_speech)
        self.assertNotIn("right_knee", lag_speech)
        lag_deg = robot_sim.describe_body(idle_lag, want_degrees=True)
        self.assertIn("right knee", lag_deg)
        self.assertIn("commanded 10", lag_deg)
        self.assertIn("live 40", lag_deg)

        st["live"] = {"neck_pan": 12}
        partial = robot_sim.public_status(st, now=half)
        self.assertAlmostEqual(partial["live"]["neck_pan"], 12)
        self.assertAlmostEqual(partial["delta"]["neck_pan"], 12.0)
        self.assertEqual(partial["live"]["left_elbow"], partial["commanded"]["left_elbow"])
        self.assertEqual(partial["delta"]["left_elbow"], 0)

        st["temp"] = {"right_knee": 41.2}
        st["load"] = {"right_knee": 18}
        st["fall_flag"] = False
        hw = robot_sim.public_status(st, now=half)
        self.assertEqual(hw["temp"]["right_knee"], 41.2)
        self.assertEqual(hw["load"]["right_knee"], 18)
        self.assertIs(hw["fall_flag"], False)

        stopped = robot_sim.apply(st, {"cmd": "stop"})
        self.assertIsNone(stopped["phase"])
        self.assertIsNone(st.get("walk_started"))
        self.assertIsNone(st.get("phase"))
        still = robot_sim.describe_body(st)
        self.assertNotIn("percent through a walking step", still)

        body = robot_sim.default_state()
        posed = robot_sim.apply(body, {"cmd": "pose", "pose": "kneel_left"})
        self.assertIn("kneeling on my left knee", robot_sim.describe_body(body))
        self.assertNotIn("crouched", robot_sim.describe_body(body))
        live_only = robot_sim.apply(body, {"cmd": "live", "live": dict(robot_sim.POSES["home"])})
        self.assertEqual(live_only["pose"], "kneel_left")
        self.assertLess(live_only["live"]["left_knee"], 20)
        self.assertGreater(live_only["commanded"]["left_knee"], 80)
        self.assertIn("standing", robot_sim.describe_body(body))
        self.assertNotIn("kneeling", robot_sim.describe_body(body))
        self.assertIn("standing", live_only["spoken"].lower())
        stood = robot_sim.apply(body, {"cmd": "pose", "pose": "home"})
        self.assertIsNotNone(body.get("live"))
        self.assertLess(body["live"]["left_knee"], 20)
        self.assertIn("standing", robot_sim.describe_body(body))
        commanded_home = robot_sim.describe_body(body, which="commanded")
        self.assertIn("standing", commanded_home)

    def test_prompt_queue_runs_one_turn_at_a_time(self) -> None:
        self.assertTrue(self.bot.take_prompt_turn())
        self.assertFalse(self.bot.take_prompt_turn())
        self.bot.queue_prompt("wave", [])
        self.bot.queue_prompt("sit", [])
        nxt = self.bot.finish_prompt_turn()
        self.assertEqual(nxt[0], "wave")
        nxt = self.bot.finish_prompt_turn()
        self.assertEqual(nxt[0], "sit")
        self.assertIsNone(self.bot.finish_prompt_turn())
        self.assertTrue(self.bot.take_prompt_turn())
        self.bot.queue_prompt("ignored", [])
        self.bot.cancel_prompt_queue()
        self.assertIsNone(self.bot.finish_prompt_turn())
        self.assertTrue(self.bot.take_prompt_turn())

    def test_stop_and_walk_right_plan_survives_live_telemetry(self) -> None:
        import robot_sim

        self.bot.robot_state = robot_sim.default_state()
        robot_sim.apply(self.bot.robot_state, {"cmd": "walk", "direction": "left"})
        plan = robot_sim.infer_command("stop and walk right")
        result, _ev = self.bot.apply_robot(plan)
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.bot.robot_state.get("motion"), "idle")
        self.assertIsNotNone(self.bot._plan_timer)
        self.assertIsNotNone(self.bot.robot_state.get("plan"))
        self.bot.apply_robot({"cmd": "live", "live": dict(self.bot.robot_state.get("joints") or {})})
        self.assertIsNotNone(self.bot._plan_timer)
        self.assertEqual((self.bot.robot_state.get("plan") or {}).get("i"), 0)
        old = self.bot._plan_timer
        if old is not None:
            old.cancel()
        self.bot._plan_timer = None

    def test_infer_robot_command_raises_right_hand(self) -> None:
        import robot_sim

        cmd = robot_sim.infer_command("can you raise your right hand")
        self.assertEqual(cmd["cmd"], "joint")
        self.assertGreater(cmd["joints"]["left_shoulder"], 90)
        self.assertIsNone(robot_sim.infer_command("don't raise your right hand"))
        self.assertEqual(robot_sim.infer_command("put your hands down")["pose"], "home")
        self.assertEqual(robot_sim.infer_command("wave")["pose"], "wave")
        wave_pose = robot_sim.POSES["wave"]
        self.assertLess(wave_pose["right_shoulder"], 90)
        self.assertGreaterEqual(wave_pose["right_elbow"], 80)
        self.assertLess(wave_pose["left_shoulder"], 25)
        self.assertEqual(robot_sim.infer_command("can you tilt your body right")["pose"], "lean_right")
        self.assertEqual(robot_sim.infer_command("lean left")["pose"], "lean_left")
        self.assertEqual(robot_sim.infer_command("sit down")["pose"], "sit")
        self.assertEqual(robot_sim.infer_command("can you sit")["pose"], "sit")
        self.assertEqual(robot_sim.infer_command("yes sit")["pose"], "sit")
        self.assertTrue(robot_sim.looks_like_motor("can you sit"))
        self.assertTrue(robot_sim.looks_like_motor("yes sit"))
        self.assertIsNotNone(d.resolve_motor_command("can you sit"))
        one = robot_sim.infer_command("raise your hand")
        self.assertEqual(one["cmd"], "joint")
        self.assertGreater(one["joints"]["right_shoulder"], 90)
        self.assertNotIn("left_shoulder", one["joints"])
        a_hand = robot_sim.infer_command("raise a hand")
        self.assertGreater(a_hand["joints"]["right_shoulder"], 90)
        self.assertNotIn("left_shoulder", a_hand["joints"])
        arm = robot_sim.infer_command("lift your arm")
        self.assertGreater(arm["joints"]["right_shoulder"], 90)
        fwd = robot_sim.infer_command("move your arm forward")
        self.assertEqual(fwd["cmd"], "joint")
        self.assertGreaterEqual(fwd["joints"]["right_shoulder"], 70)
        self.assertLessEqual(fwd["joints"]["right_shoulder"], 100)
        self.assertNotIn("left_shoulder", fwd["joints"])
        fwd_l = robot_sim.infer_command("move your left arm forward")
        self.assertEqual(fwd_l["joints"]["right_shoulder"], 78)
        self.assertEqual(fwd_l.get("arm_kind"), "forward")
        self.assertLessEqual(fwd_l["joints"].get("right_shoulder_out", 0), 5)
        up_l = robot_sim.infer_command("raise your left arm")
        self.assertGreater(up_l["joints"]["right_shoulder"], 120)
        self.assertEqual(up_l.get("arm_kind"), "raise")
        self.assertNotIn("left_shoulder", fwd_l["joints"])
        self.assertLess(fwd_l["joints"].get("right_elbow", 0), 20)
        fwd_r = robot_sim.infer_command("move your right arm forward")
        self.assertEqual(fwd_r["joints"]["left_shoulder"], 78)
        self.assertLess(fwd_r["joints"].get("left_elbow", 0), 20)
        both_fwd = robot_sim.infer_command("arms forward")
        self.assertEqual(both_fwd["pose"], "arms_forward")
        self.assertNotIn("left_shoulder", arm["joints"])
        both = robot_sim.infer_command("raise your hands")
        self.assertEqual(both["pose"], "hands_up")
        right_up = {"joints": {"right_shoulder": 145, "left_shoulder": 0}, "history": [{"act": "raise-right"}]}
        other = robot_sim.infer_command("the other hand", right_up)
        self.assertGreater(other["joints"]["left_shoulder"], 90)
        self.assertNotIn("right_shoulder", other["joints"])
        self.assertTrue(robot_sim.looks_like_motor("the other one", right_up))
        self.assertEqual(robot_sim.infer_command("now the left")["joints"]["right_shoulder"], 145)
        down = robot_sim.infer_command("put it down", right_up)
        self.assertEqual(down["joints"]["right_shoulder"], 0)
        raised_leg = robot_sim.apply(robot_sim.default_state(), {"cmd": "pose", "pose": "right_leg_raise"})
        self.assertEqual(robot_sim.infer_command("ok, put your leg down", raised_leg)["pose"], "home")
        self.assertEqual(robot_sim.infer_command("put your leg down")["pose"], "home")
        self.assertEqual(robot_sim.infer_command("put it down", raised_leg)["pose"], "home")
        self.assertEqual(robot_sim.infer_command("raise your leg")["pose"], "right_leg_raise")
        raise_plan = robot_sim.infer_command("stop and then raise your leg")
        self.assertEqual(raise_plan["cmd"], "plan")
        self.assertEqual(raise_plan["steps"][0]["cmd"], "stop")
        self.assertEqual(raise_plan["steps"][1].get("pose") or raise_plan["steps"][1].get("cmd"), "right_leg_raise")
        yes = d.resolve_motor_command("yes", prior_user="can you sit")
        self.assertEqual(yes["pose"], "sit")
        seq = robot_sim.infer_command("wave then sit")
        self.assertEqual(seq["cmd"], "plan")
        self.assertEqual(seq["steps"][0]["pose"], "wave")
        self.assertEqual(seq["steps"][1]["pose"], "sit")
        both = robot_sim.infer_command("wave and sit")
        self.assertEqual(both["cmd"], "plan")
        self.assertEqual([s["pose"] for s in both["steps"]], ["wave", "sit"])
        trio = robot_sim.infer_command("wave, arms forward, sit")
        self.assertEqual(trio["cmd"], "plan")
        self.assertEqual([s.get("pose") for s in trio["steps"]], ["wave", "arms_forward", "sit"])
        walk_after = robot_sim.infer_command("wave then walk right")
        self.assertEqual(walk_after["cmd"], "plan")
        self.assertEqual(walk_after["steps"][0]["pose"], "wave")
        self.assertEqual(walk_after["steps"][1]["cmd"], "walk")
        self.assertEqual(walk_after["steps"][1]["direction"], "right")
        stop_then = robot_sim.infer_command("stop then wave")
        self.assertEqual(stop_then["cmd"], "plan")
        self.assertEqual(stop_then["steps"][0]["cmd"], "stop")
        self.assertEqual(stop_then["steps"][1]["pose"], "wave")
        stop_and = robot_sim.infer_command("stop and arms forward")
        self.assertEqual(stop_and["cmd"], "plan")
        self.assertEqual(stop_and["steps"][0]["cmd"], "stop")
        self.assertEqual(stop_and["steps"][1]["pose"], "arms_forward")
        stop_walk = robot_sim.infer_command("stop and walk right")
        self.assertEqual(stop_walk["cmd"], "plan")
        self.assertEqual(stop_walk["steps"][0]["cmd"], "stop")
        self.assertEqual(stop_walk["steps"][1]["cmd"], "walk")
        self.assertEqual(stop_walk["steps"][1]["direction"], "right")
        self.assertIn("stop", (stop_walk.get("why") or "").lower())
        self.assertIn("walk right", (stop_walk.get("why") or "").lower())
        left_then_right = robot_sim.infer_command("walk left, then right")
        self.assertEqual(left_then_right["cmd"], "plan")
        self.assertEqual(left_then_right["steps"][0]["direction"], "left")
        self.assertEqual(left_then_right["steps"][1]["cmd"], "walk")
        self.assertEqual(left_then_right["steps"][1]["direction"], "right")
        st_plan = robot_sim.default_state()
        robot_sim.apply(st_plan, {"cmd": "walk", "direction": "left"})
        first = robot_sim.apply(st_plan, stop_walk)
        self.assertEqual(first["motion"], "idle")
        self.assertIsNotNone(st_plan.get("plan"))
        robot_sim.apply(st_plan, {"cmd": "live", "live": dict(st_plan.get("joints") or {})})
        self.assertIsNotNone(st_plan.get("plan"))
        self.assertEqual(st_plan["plan"]["i"], 0)
        nxt_walk = robot_sim.advance_plan(st_plan)
        self.assertEqual(nxt_walk["cmd"], "walk")
        self.assertEqual(nxt_walk["direction"], "right")
        walked = robot_sim.apply(st_plan, {**nxt_walk, "_plan_step": True})
        self.assertEqual(walked["motion"], "walking")
        self.assertEqual(walked["walk_direction"], "right")
        st_stop = robot_sim.default_state()
        robot_sim.apply(st_stop, {"pose": "wave", "cmd": "pose"})
        ran_stop = robot_sim.apply(st_stop, stop_then)
        self.assertEqual(ran_stop["pose"], "neutral")
        self.assertIsNotNone(st_stop.get("plan"))
        self.assertTrue(robot_sim.is_plan_echo(st_stop, {"cmd": "pose", "pose": "custom"}))
        nxt_wave = robot_sim.advance_plan(st_stop)
        self.assertEqual(nxt_wave["pose"], "wave")
        robot_sim.apply(st_stop, {**nxt_wave, "_plan_step": True})
        self.assertEqual(st_stop["pose"], "wave")
        self.assertEqual(robot_sim.infer_command("sit")["pose"], "sit")
        self.assertEqual(robot_sim.infer_command("get comfortable")["pose"], "sit")
        self.assertEqual(robot_sim.infer_command("get comfortable", {"pose": "sit"})["pose"], "relaxed")
        self.assertEqual(robot_sim.infer_command("say hi")["pose"], "wave")
        self.assertIsNone(robot_sim.infer_command("hello"))
        self.assertFalse(robot_sim.looks_like_motor("hello"))
        self.assertEqual(robot_sim.chat_acting("hello")["pose"], "wave")
        self.assertEqual(robot_sim.chat_acting("thanks")["joint"], "neck_tilt")
        self.assertEqual(robot_sim.infer_command("do some body movements")["cmd"], "demo")
        self.assertEqual(robot_sim.infer_command("perform some movements")["cmd"], "demo")
        self.assertTrue(robot_sim.looks_like_motor("do some body movements"))
        self.assertTrue(robot_sim.looks_like_motor("say hi"))
        self.assertTrue(robot_sim.looks_like_motor("get comfortable"))
        stretch = robot_sim.infer_command("stretch a bit")
        self.assertEqual(stretch["cmd"], "plan")
        self.assertEqual(stretch["steps"][0]["pose"], "hands_up")
        self.assertEqual(stretch["steps"][1]["pose"], "home")
        st = robot_sim.default_state()
        ran = robot_sim.apply(st, seq)
        self.assertTrue(ran["ok"])
        self.assertEqual(st["pose"], "wave")
        self.assertEqual(st["plan"]["i"], 0)
        nxt = robot_sim.advance_plan(st)
        self.assertEqual(nxt["pose"], "sit")
        robot_sim.apply(st, {**nxt, "_plan_step": True})
        self.assertEqual(st["pose"], "sit")
        st2 = robot_sim.default_state()
        robot_sim.apply(st2, seq)
        self.assertTrue(robot_sim.is_plan_echo(st2, {"cmd": "pose", "pose": "wave"}))
        robot_sim.apply(st2, {"cmd": "pose", "pose": "wave"})
        self.assertIsNotNone(st2.get("plan"))
        self.assertEqual(st2["plan"]["i"], 0)
        self.assertIn("plan", d._MOTOR_JSON_SYS)
        raised = {
            "joints": {"right_shoulder": 100, "right_elbow": 20, "left_shoulder": 0, "left_elbow": 5},
            "pose": "custom",
            "history": [{"act": "raise-right", "applied": {"right_shoulder": 100, "right_elbow": 20}}],
        }
        higher = robot_sim.infer_command("a bit higher", raised, prior="raise your hand")
        self.assertGreater(higher["joints"]["right_shoulder"], 100)
        self.assertNotIn("left_shoulder", higher["joints"])
        self.assertTrue(robot_sim.looks_like_motor("a bit higher", raised))
        self.assertFalse(robot_sim.looks_like_motor("a bit more"))
        self.assertIsNone(d.resolve_motor_command("a bit more"))
        more = d.resolve_motor_command("a bit more", state=raised, prior_user="raise your hand")
        self.assertGreater(more["joints"]["right_shoulder"], 100)
        look = {
            "joints": {"neck_pan": 40},
            "history": [{"applied": {"neck_pan": 40}}],
        }
        other_way = robot_sim.infer_command("the other way", look, prior="look left")
        self.assertLess(other_way["joints"]["neck_pan"], 0)
        waved = {
            "joints": dict(robot_sim.POSES["wave"]),
            "pose": "wave",
            "history": [{"act": "wave", "pose": "wave", "applied": {"right_shoulder": 32, "right_elbow": 124}}],
        }
        wave_up = robot_sim.infer_command("a little higher", waved, prior="wave")
        self.assertGreater(wave_up["joints"]["right_shoulder"], 32)
        src = (ROOT / "deskd" / "deskd.py").read_text(encoding="utf-8")
        self.assertIn("Previous user turn", src)
        self.assertIn("def interpret_motor_nl_think", src)
        self.assertIn("Live joints", src)
        self.assertEqual(robot_sim.infer_command("t-pose")["pose"], "tpose")
        self.assertEqual(robot_sim.infer_command("look left")["joint"], "neck_pan")
        self.assertEqual(robot_sim.infer_command("nod")["joint"], "neck_tilt")
        self.assertEqual(robot_sim.infer_command("lean forward")["joint"], "upper_back_pitch")
        self.assertEqual(robot_sim.short_reply({"pose": "bow", "motion": "idle"}), "Bowing.")
        self.assertEqual(robot_sim.short_reply({"pose": "home", "motion": "idle"}), "Standing straight.")
        self.assertEqual(robot_sim.short_reply({"motion": "walking", "pose": "walk-cycle"}), "Walking in place, facing you, south.")
        self.assertEqual(robot_sim.short_reply({"motion": "walking", "pose": "walk-cycle", "heading": "east", "walk_direction": "left"}), "Walking left.")
        self.assertEqual(robot_sim.infer_command("walk")["cmd"], "walk")
        self.assertEqual(robot_sim.infer_command("walk left")["direction"], "left")
        self.assertEqual(robot_sim.infer_command("walk east")["direction"], "left")
        self.assertEqual(robot_sim.infer_command("walk right")["direction"], "right")
        self.assertEqual(robot_sim.infer_command("walk west")["direction"], "right")
        self.assertEqual(robot_sim.infer_command("kneel right")["pose"], "kneel_right")
        self.assertEqual(robot_sim.infer_command("kneel left")["pose"], "kneel_left")
        self.assertEqual(robot_sim.infer_command("kneel on your right knee")["pose"], "kneel_right")
        self.assertEqual(robot_sim.infer_command("kneel")["pose"], "kneel_right")
        self.assertEqual(robot_sim.infer_command("kneel on both knees")["pose"], "kneel_both")
        self.assertEqual(robot_sim.infer_command("kneel both")["pose"], "kneel_both")
        self.assertEqual(robot_sim.short_reply({"pose": "kneel_right", "motion": "idle"}), "Kneeling on the right knee.")
        self.assertEqual(robot_sim.short_reply({"pose": "kneel_both", "motion": "idle"}), "Kneeling on both knees.")
        both = robot_sim.POSES["kneel_both"]
        self.assertEqual(both["left_hip"], 0)
        self.assertEqual(both["left_knee"], 100)
        self.assertEqual(both["left_ankle"], 40)
        self.assertEqual(both["right_hip"], 0)
        self.assertEqual(both["right_knee"], 100)
        self.assertEqual(both["right_ankle"], 40)
        kneel_l = robot_sim.POSES["kneel_left"]
        self.assertEqual(kneel_l["left_hip"], 0)
        self.assertEqual(kneel_l["left_hip_out"], 0)
        self.assertEqual(kneel_l["left_knee"], 100)
        self.assertEqual(kneel_l["left_ankle"], 40)
        self.assertEqual(kneel_l["right_hip"], 90)
        self.assertEqual(kneel_l["right_hip_out"], 0)
        self.assertEqual(kneel_l["right_knee"], 63)
        self.assertEqual(kneel_l["right_ankle"], 25)
        kneel_r = robot_sim.POSES["kneel_right"]
        self.assertEqual(kneel_r["right_hip"], 0)
        self.assertEqual(kneel_r["right_knee"], 100)
        self.assertEqual(kneel_r["right_ankle"], 40)
        self.assertEqual(kneel_r["left_hip"], 90)
        self.assertEqual(kneel_r["left_knee"], 63)
        self.assertEqual(kneel_r["left_ankle"], 25)
        self.assertEqual(robot_sim.infer_command("walk backwards")["direction"], "back")
        self.assertEqual(robot_sim.infer_command("walk north")["direction"], "back")
        self.assertEqual(robot_sim.infer_command("can you walk")["cmd"], "walk")
        self.assertEqual(robot_sim.infer_command("can you stop walking")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("stop walking")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("please stop")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("stop")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("stop waving")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("you are not waving")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("you are not waiving")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("stop doing that")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("go back to neutral")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("that's enough")["cmd"], "stop")
        self.assertEqual(robot_sim.infer_command("you can stop")["cmd"], "stop")
        self.assertTrue(robot_sim.looks_like_motor("you can stop"))
        self.assertTrue(robot_sim.looks_like_motor("can you stop walking"))
        self.assertTrue(robot_sim.looks_like_motor("stop"))
        self.assertIsNone(robot_sim.infer_command("don't stop walking"))
        self.assertGreater(robot_sim.infer_command("bend your right elbow")["joints"]["left_elbow"], 40)
        self.assertGreater(robot_sim.infer_command("raise your left knee")["joints"]["right_hip"], 20)
        self.assertEqual(robot_sim.infer_command("stand straight")["pose"], "home")
        self.assertTrue(robot_sim.looks_like_motor("can you slump a little"))
        self.assertTrue(robot_sim.looks_like_motor("lean forward"))
        self.assertFalse(robot_sim.looks_like_motor("what time is it"))
        self.assertFalse(robot_sim.looks_like_motor("walk me through the code"))
        self.assertFalse(robot_sim.looks_like_motor("look at this screenshot"))
        self.assertFalse(robot_sim.looks_like_motor("on the other hand"))
        self.assertFalse(robot_sim.looks_like_motor("I'm ready"))
        self.assertFalse(robot_sim.looks_like_motor("pay attention"))
        self.assertIsNone(robot_sim.infer_command("on the other hand"))
        self.assertIsNone(robot_sim.infer_command("your hand"))
        self.assertIsNone(d.resolve_motor_command("thanks for the hand"))
        self.assertIsNone(d.resolve_motor_command("I'm ready when you are"))
        self.assertTrue(robot_sim.looks_like_body_query("what are your arms doing"))
        self.assertTrue(robot_sim.looks_like_body_query("are your hands up?"))
        self.assertTrue(robot_sim.looks_like_body_query("are you kneeling"))
        self.assertTrue(robot_sim.looks_like_body_query("what do you look like"))
        self.assertTrue(robot_sim.looks_like_body_query("how do you look"))
        self.assertTrue(robot_sim.looks_like_body_query("how do you feel"))
        self.assertTrue(robot_sim.looks_like_body_query("How do you feel? Anything that can be fixed or updated?"))
        self.assertIsNone(d.resolve_motor_command("are you kneeling"))
        self.assertFalse(robot_sim.looks_like_body_query("can you raise your hands?"))
        self.assertFalse(robot_sim.looks_like_body_query("put your hands up"))
        self.assertFalse(robot_sim.looks_like_body_query("could you move your right arm up?"))
        self.assertTrue(robot_sim.looks_like_motor("can you raise your hands?"))
        self.assertTrue(robot_sim.looks_like_motor("put your hands up"))
        self.assertTrue(robot_sim.looks_like_motor("could you move your right arm up?"))
        self.assertEqual(robot_sim.infer_command("can you raise your hands?")["pose"], "hands_up")
        self.assertEqual(robot_sim.infer_command("put your hands up")["pose"], "hands_up")
        self.assertGreater(robot_sim.infer_command("could you move your right arm up?")["joints"]["left_shoulder"], 90)
        self.assertEqual(robot_sim.infer_command("bow")["pose"], "bow")
        self.assertEqual(robot_sim.infer_command("shrug")["pose"], "shrug")
        self.assertTrue(robot_sim.looks_like_motor("bow"))
        self.assertFalse(robot_sim.looks_like_motor("what are your arms doing"))
        self.assertFalse(robot_sim.looks_like_motor("are your hands up"))
        self.assertIsNone(d.resolve_motor_command("are your hands up"))
        from unittest.mock import patch
        with patch.object(d, "interpret_motor_nl", return_value=None):
            with patch.object(
                d, "interpret_motor_nl_think", return_value={"cmd": "pose", "pose": "wave"}
            ):
                escalated = d.resolve_motor_command("could you slump a little")
        self.assertEqual(escalated["pose"], "wave")
        down = robot_sim.describe_body({"joints": robot_sim.POSES["home"], "pose": "home"})
        self.assertIn("down by my side", down)
        self.assertIn("standing", down)
        self.assertIn("weight over", down)
        for name, pose in robot_sim.POSES.items():
            if name in {"kneel_left", "kneel_right", "kneel_both"}:
                continue
            support = robot_sim.sagittal_support(pose)
            self.assertTrue(support["over_support"], name)
        self.assertNotIn("145", down)
        up = robot_sim.describe_body(
            {"joints": {"right_shoulder": 145, "right_elbow": 20, "left_shoulder": 0, "left_elbow": 5}}
        )
        self.assertIn("right arm is raised", up)
        self.assertIn("left arm is down", up)
        bowed = robot_sim.describe_body({"pose": "bow", "joints": robot_sim.POSES["bow"]})
        self.assertIn("bow", bowed.lower())
        kneel_l = robot_sim.describe_body({"pose": "kneel_left", "joints": robot_sim.POSES["kneel_left"]})
        self.assertIn("kneeling on my left knee", kneel_l)
        self.assertNotIn("crouched", kneel_l)
        self.assertNotIn("sitting", kneel_l)
        kneel_b = robot_sim.describe_body({"pose": "kneel_both", "joints": robot_sim.POSES["kneel_both"]})
        self.assertIn("kneeling on both knees", kneel_b)
        self.assertNotIn("standing, facing you, weight over my feet", kneel_b)
        stale = robot_sim.describe_body({"pose": "kneel_left", "joints": robot_sim.POSES["home"]})
        self.assertIn("standing", stale)
        self.assertNotIn("kneeling", stale)
        live_stand = robot_sim.describe_body({
            "pose": "kneel_left",
            "joints": robot_sim.POSES["kneel_left"],
            "live": dict(robot_sim.POSES["home"]),
        })
        self.assertIn("standing", live_stand)
        self.assertNotIn("kneeling", live_stand)
        waved = robot_sim.describe_body({
            "pose": "wave",
            "motion": "waving",
            "joints": robot_sim.POSES["wave"],
        })
        self.assertTrue("waving" in waved.lower() or "held in a wave" in waved.lower(), waved)
        self.assertIn("right arm is in front of my chest", waved)
        self.assertNotIn("right arm is down by my side", waved)
        self.assertNotIn("hand is up", waved.lower())
        self.assertNotIn("raised up", waved.lower())
        self.assertIn("not raised overhead", waved.lower())
        query_wave = robot_sim.short_reply(
            {"pose": "wave", "motion": "waving", "joints": robot_sim.POSES["wave"]},
            query=True,
        )
        self.assertTrue("waving" in query_wave.lower() or "held in a wave" in query_wave.lower(), query_wave)
        self.assertIn("in front of my chest", query_wave)
        self.assertIn("hips", bowed.lower())
        lean = robot_sim.describe_body(
            {"pose": "custom", "joints": {"upper_back_pitch": 18, "left_hip": 0, "right_hip": 0, "neck_tilt": 0}}
        )
        self.assertIn("leaning forward", lean)
        self.assertNotIn("I'm bowing", lean)
        st = robot_sim.default_state()
        robot_sim.apply(st, {"cmd": "walk"})
        robot_sim.remember_move(st, {"cmd": "walk"}, user="walk", now=1000)
        robot_sim.apply(st, {"cmd": "stop"})
        robot_sim.remember_move(st, {"cmd": "stop"}, user="stop walking", now=1025)
        robot_sim.apply(st, {"cmd": "pose", "pose": "bow"})
        robot_sim.remember_move(st, {"cmd": "pose", "pose": "bow"}, user="bow", now=1030)
        timed = robot_sim.describe_body_timed(st, now=1031)
        self.assertIn("Past:", timed)
        self.assertIn("Present:", timed)
        self.assertIn("Future:", timed)
        self.assertIn("walking", timed.lower())
        self.assertIn("bow", timed.lower())
        self.assertIn("I will stay in this bow", timed)
        self.assertGreaterEqual(robot_sim.POSES["bow"]["left_ankle"], 0)
        self.assertGreaterEqual(robot_sim.POSES["bow"]["right_ankle"], 0)
        planted = robot_sim.apply(robot_sim.default_state(), {"cmd": "pose", "pose": "bow"})
        self.assertGreaterEqual(planted["joints"]["left_ankle"], 0)
        support = robot_sim.sagittal_support(planted["joints"])
        self.assertGreaterEqual(support["com_x"], support["heel_x"] - 8)
        self.assertLessEqual(support["com_x"], support["toe_x"] + 8)
        self.assertLess(abs(support["err"]), 12)
        lean_g = robot_sim.apply_gravity({"upper_back_pitch": 18, "left_hip": 0, "right_hip": 0, "left_knee": 5, "right_knee": 5, "left_ankle": -8, "right_ankle": -8})
        self.assertGreaterEqual(lean_g["left_ankle"], -4)
        lean_s = robot_sim.sagittal_support(lean_g)
        self.assertLess(abs(lean_s["err"]), 16)
        self.assertEqual(robot_sim.relative_ago(1000, 1005), "just now")
        self.assertEqual(robot_sim.relative_ago(1000, 1025), "25 seconds ago")
        self.assertEqual(robot_sim.relative_ago(1000, 1080), "1 minute ago")
        self.assertTrue(d.motion.needs_vision("point at the cup"))
        self.assertFalse(d.motion.needs_vision("wave"))
        self.assertIsNone(d.resolve_motor_command("walk me through the tests"))
        self.assertIsNotNone(d.resolve_motor_command("lean forward"))
        parsed = robot_sim.validate_command(
            {"cmd": "joint", "joints": {"upper_back_pitch": 18, "nope": 9}}
        )
        self.assertEqual(parsed["joints"]["upper_back_pitch"], 18)
        self.assertNotIn("nope", parsed["joints"])
        self.assertIsNone(robot_sim.validate_command({"cmd": None}))
        self.assertIsNone(robot_sim.infer_command("what model are you?"))
        self.assertEqual(
            d._parse_motor_json('```json\n{"cmd":"pose","pose":"wave"}\n```')["pose"],
            "wave",
        )
        self.assertIn("forward", robot_sim._arm_phrase(90, 8, 0))
        self.assertNotIn("side", robot_sim._arm_phrase(90, 8, 0))
        self.assertIn("raised", robot_sim._arm_phrase(145, 20, 0))
        self.assertIn("side", robot_sim._arm_phrase(0, 0, 90))
        fwd_desc = robot_sim.describe_body(
            {"joints": {"right_shoulder": 90, "right_elbow": 8, "right_shoulder_out": 0, "left_shoulder": 0, "left_elbow": 5}}
        )
        self.assertIn("forward", fwd_desc)
        self.assertIn("right arm is", fwd_desc)
        wrong = {"cmd": "joint", "joints": {"left_shoulder": 145, "left_elbow": 20}}
        fixed = robot_sim.ensure_motor_matches_text(wrong, "move your left arm forward")
        self.assertEqual(fixed["joints"]["right_shoulder"], 78)
        self.assertLessEqual(fixed["joints"].get("right_elbow", 0), 20)
        self.assertNotIn("left_shoulder", fixed["joints"])
        both = robot_sim.ensure_motor_matches_text({"cmd": "pose", "pose": "hands_up"}, "raise your left arm")
        self.assertEqual(both["cmd"], "joint")
        self.assertGreater(both["joints"]["right_shoulder"], 120)
        self.assertNotIn("left_shoulder", both["joints"])
        self.assertTrue(robot_sim.joints_mismatch_intent({"right_shoulder": 145, "right_shoulder_out": 0}, "move your left arm forward"))
        self.assertFalse(robot_sim.joints_mismatch_intent({"right_shoulder": 90, "right_shoulder_out": 0, "right_elbow": 8}, "move your left arm forward"))
        said = robot_sim.confirm_move({}, {"cmd": "joint", "joints": {"right_shoulder": 90}}, "move your left arm forward")
        self.assertIn("left", said.lower())
        self.assertIn("forward", said.lower())
        mixed = robot_sim.apply(
            robot_sim.default_state(),
            {"cmd": "joint", "joints": {"right_shoulder": 90, "nope_joint": 1}},
        )
        self.assertTrue(mixed["ok"])
        self.assertEqual(mixed["joints"]["right_shoulder"], 90)
        intent = robot_sim.parse_limb_intent("move your left arm forward")
        self.assertEqual(intent["kind"], "forward")
        self.assertEqual(intent["viewer_side"], "left")
        self.assertEqual(robot_sim._expected_robot_sides(intent), ["right"])
        raised_left = {
            "joints": {"right_shoulder": 145, "right_elbow": 20, "left_shoulder": 0},
            "history": [{"act": "raise-right", "applied": {"right_shoulder": 145, "right_elbow": 20}, "user": "raise your left arm"}],
        }
        nxt = robot_sim.infer_command("now forward", raised_left, prior="raise your left arm")
        self.assertEqual(nxt["joints"]["right_shoulder"], 78)
        self.assertNotIn("left_shoulder", nxt["joints"])
        again = robot_sim.infer_command("do it again", raised_left, prior="raise your left arm")
        self.assertGreater(again["joints"]["right_shoulder"], 120)
        self.assertEqual(robot_sim.chat_acting("nice to meet you")["pose"], "wave")
        self.assertIsNone(robot_sim.chat_acting("hello", {"motion": "walking"}))
        self.assertTrue(robot_sim.looks_like_motor("now forward", raised_left, prior="raise your left arm"))

    def test_body_memory_saves_named_gesture_and_corrections(self) -> None:
        import robot_sim

        notes = (
            "# lookbook\n\n## Named gestures\n\n- hello: wave\n- my sit: sit\n\n## Taught notes\n\n- 2026-09-05: wave too high\n"
        )
        gestures = d.parse_named_gestures(notes)
        self.assertEqual(gestures["hello"], "wave")
        self.assertEqual(gestures["my sit"], "sit")
        self.assertEqual(
            robot_sim.command_from_named_gesture("do my hello", gestures)["pose"],
            "wave",
        )
        self.assertIsNone(robot_sim.command_from_named_gesture("hello", gestures))
        self.assertTrue(robot_sim.looks_like_motor("do my hello", gestures=gestures))
        self.assertFalse(d.wants_full_agent("that's my hello"))
        self.assertFalse(d.wants_full_agent("your wave looks too high"))

        self.bot.robot_state = {"pose": "wave", "motion": "waving", "joints": dict(robot_sim.POSES["wave"])}
        d.ensure_body_md(self.bot.workspace)
        saved = d.learn_from_user(self.bot, "that's my hello")
        self.assertEqual(saved["kind"], "gesture")
        self.assertEqual(saved["pose"], "wave")
        body = (self.bot.workspace / "BODY.md").read_text(encoding="utf-8")
        self.assertIn("- hello: wave", body)
        facts = d.recall_body_facts(self.bot, "hello gesture")
        self.assertTrue(any("hello" in f and "wave" in f for f in facts))
        corr = d.learn_from_user(self.bot, "your wave looks too high")
        self.assertEqual(corr["kind"], "correction")
        body2 = (self.bot.workspace / "BODY.md").read_text(encoding="utf-8")
        self.assertIn("too high", body2)
        pref = d.extract_body_lesson("left means the arm on the left of the screen")
        self.assertEqual(pref["kind"], "preference")
        self.assertIsNone(robot_sim.infer_command("your wave looks too high"))
        self.assertFalse(robot_sim.looks_like_motor("your wave looks too high"))
        block = d.body_memory_block(self.bot, "hello")
        self.assertIn("hello=wave", block)
        self.assertIn("Remembered about your body", block)
        msgs = d.build_fast_chat_messages(self.bot, "do my hello")
        self.assertIn("Named gestures", msgs[0]["content"])

    def test_screenshot_saves_and_posts_to_chat(self) -> None:
        self.bot.root = self.base / "bot-root"
        self.bot.root.mkdir()
        self.bot.ensure_home_dirs()
        saved = self.bot.save_desktop_screenshot(b"\xff\xd8\xfffakejpeg", seq=4)
        self.assertTrue(saved["path"].startswith("Pictures/"))
        self.assertIn("minios-desktop", saved["path"])
        self.assertIn(datetime.now().strftime("%Y"), saved["path"])
        self.assertTrue(saved["path"].endswith(".jpg"))
        self.assertTrue((self.workspace / saved["path"]).is_file())
        self.assertEqual(saved["seq"], 4)
        self.bot.post_chat_image(saved["path"], "image/jpeg", "Desktop screenshot")
        last = self.bot.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertEqual(last["images"][0]["path"], saved["path"])
        self.assertIn("Desktop screenshot", last["text"])

    def test_system_zip_export_includes_soul_body_and_minios(self) -> None:
        import io
        import zipfile
        import conv_io

        self.bot.root = self.base / "bot-root"
        self.bot.desk = self.base / "desk"
        self.bot.workspace = self.workspace
        self.bot.root.mkdir()
        (self.bot.root / "SOUL.md").write_text("# soul\n", encoding="utf-8")
        (self.workspace / "BODY.md").write_text("# lookbook\n", encoding="utf-8")
        raw, filename, mime = conv_io.export_payload(self.bot, "system")
        self.assertTrue(filename.endswith(".zip"))
        self.assertEqual(mime, "application/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
        self.assertIn("manifest.json", names)
        self.assertIn("README.md", names)
        self.assertIn("bot/SOUL.md", names)
        self.assertIn("workspace/BODY.md", names)
        self.assertTrue(any(n.startswith("minios/deskd/robot_sim.py") for n in names))
        self.assertTrue(any(n.startswith("minios/ui/robot-simulator.html") for n in names))
        self.assertFalse(any("auth.json" in n for n in names))


class MiniOSFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        cls.hermesbot_css = (ROOT / "ui" / "hermesbot.css").read_text(encoding="utf-8")
        cls.desktop_mcp = (ROOT / "deskd" / "desktop_mcp.py").read_text(encoding="utf-8")
        cls.deskd = (ROOT / "deskd" / "deskd.py").read_text(encoding="utf-8")

    def test_voice_chat_hands_free(self) -> None:
        self.assertIn("async function toggleVoiceChat", self.app)
        self.assertIn("async function enterVoiceChat", self.app)
        self.assertIn("echoCancellation: true", self.app)
        self.assertIn("noiseSuppression: false", self.app)
        self.assertIn("function encodeWav", self.app)
        self.assertIn("function flushVoiceSend", self.app)
        self.assertIn("function interruptTeela", self.app)
        self.assertIn("beginVoiceUtterance(now, { barge: true })", self.app)
        self.assertIn("stopSpeak()", self.app)
        self.assertIn("function micSpeechFrame", self.app)
        self.assertIn("function loadMicSettings", self.app)
        self.assertIn("mic-analyzer", self.app)
        self.assertIn("MIC_EQ_HZ", self.app)
        self.assertIn("function micLogBins", self.app)
        self.assertIn("barge: 15", self.app)
        self.assertIn("hangMs: 1200", self.app)
        self.assertIn("speechMs >= 12000", self.app)
        self.assertIn("silenceMs >= hang", self.app)
        self.assertIn("beginVoiceUtterance(now, { barge: true })", self.app)
        self.assertNotIn("speechMs >= 45000", self.app)
        self.assertIn("$(\"composer\")?.requestSubmit()", self.app)
        self.assertIn("Voice chat — tap once, then just talk. Pause to send.", self.index)
        self.assertNotIn("voice-stage", self.index)
        self.assertNotIn("voice-live", self.index)
        self.assertNotIn("function syncVoiceLiveOverlay", self.app)
        self.assertNotIn("function settleVoiceBubbles", self.app)
        self.assertNotIn("function paintSpectrum", self.app)
        self.assertNotIn("async function toggleMic", self.app)
        self.assertIn("voice_chat: voiceChat.on", self.app)
        self.assertIn(".mic-btn.ready", self.css)
        self.assertIn(".mic-btn.processing", self.css)
        self.assertIn(".mic-btn.unavailable", self.css)
        self.assertIn("icon-mic-stop", self.index)
        self.assertIn("icon-speaker-on", self.index)
        self.assertIn("icon-speaker-off", self.index)
        self.assertNotIn(">🔇</button>", self.index)
        self.assertNotIn(">🎤</button>", self.index)
        self.assertNotIn("voice-status", self.index)
        self.assertNotIn("playbackRate.value", self.app)

    def test_voice_starts_muted(self) -> None:
        self.assertIn("voice: false,", self.app)
        self.assertNotIn("voice: true,", self.app)
        self.assertIn("Voice muted. Tap to enable.", self.index)
        self.assertIn("icon-speaker-off", self.index)
        self.assertIn('Default muted', self.deskd)
        self.assertIn('return False if v is None else bool(v)', self.deskd)

    def test_long_lived_ui_token_not_embedded_in_runtime_urls(self) -> None:
        self.assertNotIn("/v1/events?token=", self.app)
        self.assertNotIn("browser/frame?token=", self.app)
        self.assertNotIn("&token=${encodeURIComponent(state.token)}", self.app)

    def test_generated_html_previews_are_sandboxed(self) -> None:
        self.assertIn('iframe.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-downloads")', self.app)
        self.assertIn('id="app-preview-frame"', self.index)
        self.assertIn('sandbox="allow-scripts allow-forms allow-modals allow-downloads"', self.index)
        self.assertNotIn("allow-same-origin", self.index)

    def test_app_preview_defaults_to_robot_simulator(self) -> None:
        self.assertIn("Robot Simulator", self.index)
        self.assertIn("/ui/robot-simulator.html", self.index)
        self.assertIn("Teela Robot Body Simulator", self.index)
        self.assertIn('class="desktop-window app-window maximized-window focused-window" data-window-app="preview"', self.index)
        self.assertIn('data-window-app="preview" data-surface="preview" data-open="true"', self.index)
        self.assertIn('class="dock-app dock-btn running active" data-desktop-app="preview"', self.index)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("function showRobotOnAgentDesktop", ui)
        self.assertIn("showRobotOnAgentDesktop()", ui)
        self.assertIn('ensureMaximizedDesktopWindow(win)', ui.split("function showRobotOnAgentDesktop", 1)[1].split("function ", 1)[0])
        self.assertIn("if (!selectedHasRobotSimulator()) return;", ui.split("function showRobotOnAgentDesktop", 1)[1].split("function ", 1)[0])
        self.assertIn("function syncRobotSimulatorForBot", self.app)
        self.assertIn("minios-no-robot", self.app)
        self.assertIn('surface: "preview"', self.app)
        self.assertIn("function loadRobotSimulator", self.app)
        self.assertIn('id="preview-robot"', self.index)
        self.assertIn("function hydrateRobotIframe", self.app)
        self.assertIn("def inject_live_body", self.deskd)
        self.assertIn("def with_live_body", self.deskd)
        self.assertIn("continuous proprioception", self.deskd)
        self.assertIn("function postRobotCommand", self.app)
        self.assertIn("robot-command", self.app)
        self.assertIn('...msg, source: "hermes-desk", type: "robot-command"', self.app)
        robot_branch = self.app.split('if (msg.action === "robot")', 1)[1].split("if (msg.action ===", 1)[0]
        self.assertNotIn("moveCursorToElement", robot_branch)
        self.assertNotIn("ensureMaximized", robot_branch)
        self.assertNotIn("openWindow", robot_branch)
        post_robot = self.app.split("function postRobotCommand", 1)[1].split("function ", 1)[0]
        self.assertNotIn("openWindow", post_robot)
        dock_css = self.hermesbot_css.split(".ubuntu-dock {", 1)[1].split("}", 1)[0]
        self.assertIn("z-index:7", dock_css)
        self.assertIn("pointer-events:none;\n  z-index:10;", self.hermesbot_css)
        self.assertNotIn("inset:0 !important", self.hermesbot_css.split(".desktop-fullscreen-overlay .app-window.maximized-window", 1)[1].split("}", 1)[0])
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("if (desktopZCounter > 80) desktopZCounter = 31", ui)
        self.assertNotIn('type: "robot-command", source: "hermes-desk", ...msg', self.app)
        self.assertIn('tool("robot_status"', self.desktop_mcp)
        self.assertIn("commanded/live/delta", self.desktop_mcp)
        self.assertIn("temp/load/fall_flag stay null", self.desktop_mcp)
        self.assertIn('tool("robot_joint"', self.desktop_mcp)
        self.assertIn('tool("robot_pose"', self.desktop_mcp)
        self.assertIn('tool("robot_motion"', self.desktop_mcp)
        self.assertIn('"steps"', self.desktop_mcp)
        self.assertIn("Do not drag joint sliders", self.deskd)
        robot = (ROOT / "ui" / "robot-simulator.html").read_text(encoding="utf-8")
        self.assertIn("function command(msg)", robot)
        self.assertIn("robot-command", robot)
        self.assertIn("robot-simulator.html?v=141", self.app)
        self.assertIn("robot-simulator.html?v=141", self.index)
        begin = robot.split("function beginWalk", 1)[1].split("function startStepTurn", 1)[0]
        self.assertIn("stopWaving()", begin)
        self.assertIn("Hearing", robot)
        self.assertIn("micAnalyzer", robot)
        self.assertIn("micEq", robot)
        self.assertIn("mic-analysis", robot)
        self.assertIn("Sensitivity EQ", robot)
        self.assertIn("Sound analyzer", robot)
        self.assertIn("function applyMicAnalyzer", robot)
        self.assertIn("function ensureMicEq", robot)
        self.assertIn('row.className = "slider-row"', robot)
        self.assertNotIn("slider-vertical", robot)
        self.assertIn("Reset hearing", robot)
        self.assertIn('data-fmt="system"', self.index)
        self.assertIn("bot-system.zip", self.app)
        self.assertIn("/v1/local-llm/control", self.app)
        self.assertIn("function controlLocalLlm", self.app)
        self.assertIn("function selectPickerModel", self.app)
        self.assertIn("model-power", self.app)
        self.assertIn("MODEL_ICON_PLAY", self.app)
        self.assertIn("is-start", self.app)
        self.assertIn("model-select-btn", self.index)
        self.assertNotIn("id=\"model-stop-btn\"", self.index)
        self.assertIn('<div class="model-select-wrap">', self.index)
        self.assertNotIn('<label class="model-select-wrap">', self.index)
        self.assertIn("function closeModelMenu", self.app)
        self.assertIn("function closeFloatingMenus", self.app)
        self.assertIn('addEventListener("pointerdown"', self.app)
        self.assertIn('if (menu && !menu.hidden) placeModelMenu();', self.app)
        self.assertGreaterEqual(self.app.count("if (menu && !menu.hidden) placeModelMenu();"), 3)
        self.assertIn('e.key !== "Escape"', self.app)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn('getElementById("model-menu")?.setAttribute("hidden"', ui)
        self.assertIn("function isLocalGpuModel", self.app)
        self.assertIn("function mergePickerModels", self.app)
        self.assertIn("function isCloudPickerRow", self.app)
        self.assertIn("b.models = mergePickerModels(msg.models, b.models)", self.app)
        self.assertIn("b.models = mergePickerModels(st.models, prev)", self.app)
        self.assertIn("function runningLocalFamily", self.app)
        self.assertIn("function gpuHeld", self.app)
        self.assertIn("isExclusiveVllmModel(m) && occupying", self.app)
        self.assertIn("isLocalOccupying(m, list)", self.app)
        self.assertIn("Switch both GPUs to this model", self.app)
        self.assertIn("function localEngineReady", self.app)
        self.assertIn("function localTeelaBrainTaken", self.hermesbot_ui if hasattr(self, "hermesbot_ui") else (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8"))
        self.assertIn("Only one Teela Brain per computer", self.index)
        self.assertIn("card.hidden = Boolean(taken && isTeela && !editing)", (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8"))
        self.assertIn("teela_brain_taken", self.deskd)
        self.assertIn("def ensure_single_teela_brain", self.deskd)
        self.assertIn("Local model will start, then this message sends", self.app)
        self.assertIn('local === "wave" && (localSrc === "UI" || localSrc === "hermes-desk")', self.app)
        self.assertIn("suppressWaveEchoUntil", self.app)
        self.assertIn("superseded by a later robot command", self.app)
        self.assertIn("leftWaveLocally", self.app)
        self.assertIn("persistRobotEditGen", self.app)
        self.assertIn("incomingSeq <= localSeq", self.app)
        self.assertIn('cmd === "stop" || cmd === "stop_demo" || cmd === "neutral"', self.app)
        self.assertNotIn('if(cmd === "pose") waveIntent = true', robot)
        self.assertIn('cmd === "status" || cmd === "state"', self.app)
        self.assertIn("let waveIntent = false", robot)
        self.assertIn("function stopWaving", robot)
        self.assertIn("stale-wave", robot)
        self.assertIn("let waving = false", robot)
        self.assertIn("function composeLockedPose", robot)
        self.assertIn("while unlocked, the mesh is the Setup sliders", robot)
        self.assertIn("function completeJoints", robot)
        self.assertIn("function setGesture", robot)
        self.assertIn("function startWaveRight", robot)
        self.assertIn("function gestureOffsetsFromHold", robot)
        self.assertIn("return startWaveRight(source)", robot)
        self.assertIn("freshAgentWave", robot)
        self.assertIn("function returnNeutral", robot)
        self.assertIn("cmd === \"neutral\"", robot)
        self.assertIn("returnNeutral()", robot)
        self.assertIn("function restArms", robot)
        self.assertIn("ARM_KIND_RECIPE", robot)
        self.assertIn("function finishWave", robot)
        self.assertIn("WAVE_MS", robot)
        self.assertIn("userCalibrating", robot)
        self.assertIn("ignored:\"unlocked\"", robot)
        self.assertIn("applyWaveFrame", robot)
        self.assertIn("-(j.right_shoulder_out||0)*DEG", robot)
        self.assertIn("(j.left_shoulder_out||0)*DEG", robot)
        self.assertIn("function nudgeJoint", robot)
        self.assertIn('id="partControls"', robot)
        self.assertIn("DIR_SIGN", robot)
        self.assertIn("function cameraForView", robot)
        self.assertIn("function isLocomoting", robot)
        self.assertIn("function tickStepTurn", robot)
        self.assertIn("function startStepTurn", robot)
        self.assertIn("function stopWalkSmooth", robot)
        self.assertIn("function nearHeelStrike", robot)
        self.assertIn("function desiredFaceYaw", robot)
        self.assertIn("function faceHeadingOf", robot)
        self.assertIn('if(normalizeWalkDir(dir) === "back") return 0', robot)
        self.assertIn('if(h === "east") return Math.PI / 2', robot)
        self.assertIn('if(h === "west") return -Math.PI / 2', robot)
        self.assertIn('if(h === "north") return Math.PI', robot)
        self.assertIn('walkDir === "back" ? -1 : 1', robot)
        self.assertIn("function headingOf", robot)
        self.assertIn('id="walkBack"', robot)
        self.assertIn('id="compass"', robot)
        self.assertIn("floorScrollX", robot)
        self.assertIn("function wrapShift", robot)
        self.assertIn("function headingOf", robot)
        self.assertIn("gaitBlend", robot)
        self.assertIn("function humanLegCycle", robot)
        self.assertIn("function applyLegSafe", robot)
        self.assertIn("function needsPlantBefore", robot)
        self.assertIn("function startLegOut", robot)
        self.assertIn("function startLegRaise", robot)
        self.assertIn("function startKneel", robot)
        self.assertIn("function isKneeling", robot)
        self.assertIn("function riseFromKneelSteps", robot)
        self.assertIn("plant the front foot first while the kneeling knee stays", robot)
        self.assertIn("slowRise ? 1.85 : 6", robot)
        self.assertIn("cur.planted", robot)
        self.assertIn("current !== knee", robot)
        self.assertIn("concat(kneelSteps)", robot)
        self.assertIn("function liftKneelOutOfGround", robot)
        self.assertIn("function supportClearanceY", robot)
        self.assertIn("function needsPlantBeforeRaise", robot)
        self.assertIn('id="kneelRight"', robot)
        self.assertIn('id="kneelLeft"', robot)
        self.assertIn('id="kneelBoth"', robot)
        self.assertIn("function startKneelBoth", robot)
        self.assertIn("retract the raised knee", robot)
        self.assertIn("function kneelKneeClearanceY", robot)
        self.assertNotIn("More Actions", robot)
        self.assertIn("function startGestureSteps", robot)
        self.assertIn("function tickGestureSteps", robot)
        self.assertIn("Load/stance foot plants first", robot)
        self.assertIn("Stance foot plants first", robot)
        self.assertIn("stanceNeedsPlant", robot)
        self.assertIn("gestureSteps.pose === pose", robot)
        self.assertIn('pose === "left_leg_out"', self.app)
        self.assertIn('pose === "left_leg_raise"', self.app)
        self.assertIn("function ensureLocked", robot)
        self.assertIn("function applyWaveFrame", robot)
        self.assertIn("function applyWalkFrame", robot)
        self.assertIn("function persistLiveIfChanged", robot)
        self.assertIn('cmd: "live"', robot)
        self.assertIn("robot-live-nack", robot)
        self.assertIn("robot-live-nack", self.app)
        self.assertIn("observerBotId && !liveOnly", self.app)
        self.assertIn("function drawSkeleton", robot)
        self.assertIn('id="waveRight"', robot)
        self.assertIn('id="tPose"', robot)
        self.assertIn('id="lockPose"', robot)
        self.assertIn("right_wrist = 0", robot)
        self.assertIn('type: "robot-edit"', robot)
        self.assertIn("function uiHeld", robot)
        self.assertIn('state.motion === "waving"', robot)
        self.assertNotIn('class="badges"', robot)
        self.assertNotIn("approved wave + straight wrist", robot)
        self.assertNotIn("world-sim", robot)
        self.assertNotIn("RobotWorld", robot)
        self.assertNotIn("setWorldVisible", robot)
        self.assertNotIn("TeelaKimodo", robot)
        self.assertIn('st?.motion === "walking"', self.app)
        self.assertIn('cmd: "walk"', self.app)
        self.assertIn('"seq": result.get("seq")', self.deskd)
        self.assertIn('"motion": result.get("motion")', self.deskd)
        self.assertIn('ignored:"stale"', robot)
        self.assertIn('ignored:"busy"', robot)
        self.assertIn("seq <= appliedSeq", robot)
        self.assertIn("function isLocomoting", robot)
        self.assertIn('source === "hydrate" && isLocomoting()', robot)
        self.assertIn("RobotSim.uiHeld() || RobotSim.busy()", robot)
        self.assertIn("function initGL", robot)
        self.assertIn("function startWalk", robot)
        self.assertIn("gait keeps the knee", robot)
        self.assertIn("las-catrina.bin.gz", robot)
        self.assertIn("function loadCatrina", robot)
        self.assertIn('id="frontView"', robot)
        self.assertIn("function animateCamera", robot)

    def test_workspace_sharing_ui_and_tools_exist(self) -> None:
        self.assertIn('id="workspace-share-block"', self.index)
        self.assertIn("fillWorkspaceShareList", (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8"))
        mcp = (ROOT / "deskd" / "desk_mcp.py").read_text(encoding="utf-8")
        self.assertIn("list_shared_desks", mcp)
        self.assertIn("read_shared_file", mcp)
        self.assertIn("request_workspace_share", mcp)
        self.assertIn("workspace-shares", self.deskd)
        robot = (ROOT / "ui" / "robot-simulator.html").read_text(encoding="utf-8")
        self.assertIn("<h1>TEELA</h1>", robot)
        self.assertNotIn("Calibrate, Lock & Natural Walk", robot)
        self.assertIn("const RobotSim", robot)
        self.assertIn('class="app"', robot)
        self.assertNotIn('id="dock"', robot)
        self.assertNotIn('id="topbar"', robot)
        self.assertNotIn("createWindow", robot)
        self.assertIn("robot-simulator.html", self.deskd)
        self.assertIn("script-src 'unsafe-inline'", self.deskd)

    def test_minios_developer_apps_exist(self) -> None:
        for app in ("editor", "preview", "dev"):
            self.assertIn(f'data-desktop-app="{app}"', self.index)

    def test_long_horizon_task_bar_exists(self) -> None:
        self.assertIn('id="horizon-bar"', self.index)
        self.assertIn('id="horizon-list"', self.index)
        self.assertIn("function renderHorizon", self.app)
        self.assertIn("function planEntriesFromUpdate", self.app)
        self.assertIn('kind === "plan"', self.app)
        self.assertIn("Long Horizon Task", self.index)
        self.assertIn(".horizon-item.is-done", self.hermesbot_css)
        self.assertIn("function applySessionTurn", self.app)

    def test_compact_header_has_plugins_and_settings_icons(self) -> None:
        self.assertIn('id="menu-plugins"', self.index)
        self.assertIn('id="menu-settings"', self.index)
        self.assertIn('id="more-btn"', self.index)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn('$("menu-settings")?.addEventListener("click", openUserSettings)', ui)
        self.assertIn('$("menu-plugins")?.addEventListener("click", openPlugins)', ui)
        self.assertIn("function syncMoreMenu", self.app)

    def test_chat_fills_space_when_sidebars_collapse(self) -> None:
        self.assertIn("--left-w: 0px !important", self.hermesbot_css)
        self.assertIn("--right-w: 0px !important", self.hermesbot_css)
        self.assertIn("--left-resizer: 0px", self.hermesbot_css)
        self.assertIn("--right-resizer: 0px", self.hermesbot_css)
        self.assertIn("minmax(0, 1fr)", self.hermesbot_css)
        self.assertIn("--right-col: 50%", self.hermesbot_css)
        self.assertIn("--right-col: 0px !important", self.hermesbot_css)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("COLLAPSE_DRAG_PX", ui)
        self.assertIn('RIGHT_SPLIT_KEY = "hermes-desk-right-split"', ui)
        self.assertIn("function rightPaneMax", ui)
        self.assertIn("allowCollapse", ui)

    def test_half_window_splits_chat_and_agent_desktop(self) -> None:
        css = self.hermesbot_css
        self.assertIn("--compact-rail-w: 68px", css)
        self.assertIn("--compact-split-w: min(50vw, calc((100% - var(--compact-rail-w)) / 2))", css)
        self.assertIn("right: var(--compact-split-w) !important", css)
        self.assertIn("width: var(--compact-split-w) !important", css)
        self.assertIn("inset: 0 0 0 var(--compact-rail-w) !important", css)
        self.assertNotIn("min(360px, 38vw)", css)
        self.assertNotIn("Very tight half-screens", css)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertNotIn("window.innerWidth < 900", ui)
        self.assertIn('if (lastLayoutMode !== "compact")', ui)
        self.assertIn("closeCompactDrawers", ui)

    def test_live_desktop_expands_to_half_screen(self) -> None:
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("LIVE_DESKTOP_RATIO = 0.5", ui)
        self.assertIn("total * LIVE_DESKTOP_RATIO", ui)
        self.assertIn('min(${width}px, 50%)', ui)
        self.assertIn("function applyLiveDesktopWidth", ui)
        self.assertIn("function sizeLiveDesktopArea", ui)
        self.assertIn("--desktop-res-w", ui)
        self.assertIn("dataset.resolution", ui)
        self.assertIn("availH", ui)
        self.assertIn("@media (max-height: 700px)", self.hermesbot_css)
        self.assertIn("preLiveRightW", ui)
        self.assertIn("AGENT DESKTOP", self.index)
        self.assertNotIn("LIVE DESKTOP", self.index)
        self.assertNotIn("desktop-reconnect", self.index)
        self.assertNotIn("desktop-status-text", self.index)
        self.assertIn("body.live-desktop-entered .ubuntu-desktop-area", self.hermesbot_css)
        self.assertIn("body.live-desktop-entered .desktop-fullscreen-overlay", self.hermesbot_css)

    def test_workspace_desktop_uses_dock_not_wallpaper_icons(self) -> None:
        self.assertNotIn('class="desktop-icon"', self.index)
        self.assertIn('id="dock"', self.index)
        self.assertIn('class="dock-app', self.index)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("function wireDockMagnify", ui)
        self.assertIn("function wireDockSnap", ui)
        self.assertIn("dock-left", self.hermesbot_css)
        self.assertIn("data-dock-hot", self.hermesbot_css)
        self.assertIn("--dock-icon", self.hermesbot_css)
        self.assertIn("lastDesktopW", ui)
        self.assertIn("/%|calc\\(/", ui)
        self.assertIn("areaPoint", ui)

    def test_minios_has_ubuntu_os_chrome(self) -> None:
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn('id="os-boot"', self.index)
        self.assertIn('id="os-login"', self.index)
        self.assertIn('id="os-power-btn"', self.index)
        self.assertIn('class="win-btn close"', self.index)
        self.assertIn('data-window-action="close"', self.index)
        self.assertIn('class="ubuntu-dock dock-left"', self.index)
        self.assertIn("function wireOsSession", ui)
        self.assertIn("minios-dock-edge", ui)
        self.assertNotIn("settings-minios-password", self.index)
        self.assertIn("os-settings-password", self.index)
        self.assertIn('data-window-app="settings"', self.index)
        self.assertIn('data-desktop-app="settings"', self.index)
        self.assertIn(".cluster-field input", self.hermesbot_css)
        self.assertIn(".secret-input-row input", self.hermesbot_css)
        self.assertIn("refreshOsLock", ui)
        self.assertIn("minios-lock-password", ui)
        self.assertIn("function wireOsSettings", ui)
        self.assertIn("pass.hidden = !locked", ui)
        self.assertIn('id="os-login-pass"', self.index)
        self.assertIn("os-swatch", self.index)
        self.assertIn('id="desktop-trash"', self.index)
        self.assertIn("def trash_item", self.deskd)
        self.assertIn("/trash", self.deskd)
        self.assertIn("empty_trash", self.deskd)
        self.assertIn("/trash/empty", self.deskd)
        self.assertIn("id=\"empty-trash-btn\"", self.index)
        self.assertIn("showDeskMenu", self.app)
        self.assertIn("Move to Trash", self.app)
        self.assertIn("Empty Trash", self.app)
        self.assertIn('id="browser-hit"', self.index)
        self.assertNotIn('class="traffic"', self.index)

    def test_files_app_uses_ubuntu_home_layout(self) -> None:
        for place in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
            self.assertIn(f'data-file-place="{place}"', self.index)
        self.assertIn("function filesInFolder", self.app)
        self.assertIn("window.deskBrowseFiles", self.app)
        self.assertIn('class="files-grid"', self.index)
        self.assertIn("def ensure_home_dirs", self.deskd)

    def test_minios_has_notepad_text_editor(self) -> None:
        self.assertIn('data-window-app="notepad"', self.index)
        self.assertIn('data-desktop-app="notepad"', self.index)
        self.assertIn('id="notepad-editor"', self.index)
        self.assertIn('data-notepad-cmd="saveAs"', self.index)
        self.assertIn('id="notepad-dialog-places"', self.index)
        self.assertIn("function openNotepadFile", self.app)
        self.assertIn("function wireNotepad", self.app)
        self.assertIn("notepadCandidate", self.app)
        self.assertIn("function notepadGoDir", self.app)
        self.assertIn("fromDialog", self.app)
        self.assertIn("function currentBot", self.app)
        self.assertIn("hermes-desk-selected-bot", self.app)
        self.assertIn("Documents/Untitled.txt", self.app)
        self.assertIn('"notepad"', self.deskd)
        self.assertIn("app_notepad", self.deskd)
        self.assertIn("app_notepad", self.desktop_mcp)

    def test_persistent_visual_observer_tools_exist(self) -> None:
        self.assertIn('"name": "desktop_observe"', self.desktop_mcp)
        self.assertIn('"name": "desktop_watch"', self.desktop_mcp)
        self.assertIn('"name": "desktop_screenshot"', self.desktop_mcp)
        self.assertIn("action == \"screenshot\"", self.deskd)
        self.assertIn("screenshot_minios_desktop", self.deskd)
        self.assertIn("Never the host/user monitor", self.desktop_mcp)
        self.assertNotIn("Never screenshot the host/user monitor", d.agent_md_for_kind("hermes"))
        self.assertNotIn("Show the workspace desktop (files", self.deskd)
        self.assertIn("desktop.capture-request", self.deskd)
        self.assertIn("function miniosViewState", self.app)
        self.assertIn("function postMiniosView", self.app)
        self.assertIn("dock: dockEdge", self.app)
        self.assertIn("minios-dock-edge", (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8"))
        self.assertIn("dock-'+edge", (ROOT / "deskd" / "surfaces.py").read_text(encoding="utf-8"))
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn('document.body.classList.contains("observer-mode")', ui)
        self.assertIn("ensureMaximizedDesktopWindow", ui)
        self.assertIn("ensureMaximized", ui)
        self.assertIn("images: msg.images", self.app)
        self.assertIn('"type": "image"', self.desktop_mcp)
        self.assertIn('"mimeType": "image/jpeg"', self.desktop_mcp)

    def run_frontend_node(self, scenario: str) -> None:
        """Execute production handlers, replacing only browser/network boundaries."""
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
function load(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `Missing source boundary: ${start} -> ${end}`);
  vm.runInThisContext(source.slice(a, b));
}
function element() {
  const classes = new Set();
  return {value: '', style: {}, listeners: {},
    classList: {add: x => classes.add(x), remove: x => classes.delete(x),
      contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute(key, value) { this[key] = value; }, removeAttribute() {},
    querySelector() { return null; }};
}
const elements = Object.fromEntries(['composer', 'message', 'send', 'undo',
  'tps-counter', 'context-usage', 'context-stat'].map(x => [x, element()]));
const tpsStat = element();
Object.assign(globalThis, {
  $: id => elements[id] || null,
  document: {querySelector: s => s === '.tps-stat' ? tpsStat : s === '.context-stat' ? elements['context-stat'] : null},
  window: {}, state: {selected: 'bot', bots: [{id: 'bot', messages: [], status: 'Ready'}],
    working: {}, stopped: {}, pendingImages: []},
  voiceChat: {on: false, pendingSend: ''},
  localEngineReady: () => true, modelsForBot: () => [],
  paintMic() {}, flushVoiceSend() {}, isTtsPlaying: () => false,
  autosizeComposer() {}, renderPasteTray() {}, clearHorizon() {},
  renderConversation() {}, renderModelMenu() {}, requestAnimationFrame() {},
  syncMobileComposerPad() {}, slashMenuKey: () => false,
  stopSpeak() {}, PAGE_ID: 'test-page', chatStickBottom: false,
  skipCheckConfirm: false, checkScopeToSend: '',
  maybeConfirmSystemCheck: async () => false,
});
const event = (key) => ({key, defaultPrevented: false,
  preventDefault() { this.defaultPrevented = true; }});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
'''
        result = subprocess.run(
            ["node", "-e", harness + "\n(async () => {\n" + scenario
             + "\nconsole.log('frontend scenario completed');\n})().catch(err => { console.error(err); process.exitCode = 1; });"],
            input=json.dumps(self.app), text=True, capture_output=True, timeout=20,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frontend scenario completed", result.stdout,
                      "Node exited with an unresolved promise before finishing assertions")

    def test_composer_sends_one_turn_at_a_time(self) -> None:
        self.run_frontend_node(r'''
load('function isWorking(', 'function formatWorked(');
load('$("send")?.addEventListener("click"', '// ----');
load('$("composer").addEventListener("submit"', '$("message").addEventListener("paste"');
const submit = () => elements.composer.listeners.submit(event());
elements.composer.requestSubmit = submit;
const requests = [];
let response = deferred();
globalThis.api = (url, options) => {
  requests.push({url, body: JSON.parse(options.body)});
  if (url.endsWith('/prompt')) {
    assert.equal(isWorking(), true, 'lock must precede network dispatch');
    return response.promise;
  }
  return Promise.resolve({});
};
// Busy submissions preserve drafts/images; voice additionally holds the transcript.
for (const voiceOn of [false, true]) {
  voiceChat.on = voiceOn;
  state.working.bot = true;
  elements.message.value = ' next turn ';
  const image = {mime: 'image/png', data: 'abc'};
  state.pendingImages = [image];
  await submit();
  assert.equal(requests.length, 0);
  assert.equal(elements.message.value, ' next turn ');
  assert.deepEqual(state.pendingImages, [image]);
  assert.equal(voiceChat.pendingSend, voiceOn ? 'next turn' : '');
  updateSendButton();
  assert.equal(elements.send.classList.contains('is-stop'), true);
  assert.equal(elements.send['aria-label'], 'Stop');
  assert.equal(elements.send.title, 'Stop generating — send after this turn finishes');
  const enter = event('Enter');
  elements.message.listeners.keydown(enter);
  assert.equal(enter.defaultPrevented, true);
  assert.equal(requests.length, 0, 'Enter while busy must not send');
}
voiceChat.on = false;
voiceChat.pendingSend = '';
state.pendingImages = [];
const click = event();
elements.send.listeners.click(click);
await new Promise(setImmediate);
assert.equal(click.defaultPrevented, true);
assert.equal(requests.at(-1).url, '/v1/agent/bot/cancel');
assert.equal(state.stopped.bot, true);
assert.equal(isWorking(), false);
assert.equal(elements.send.classList.contains('is-stop'), false);
requests.length = 0;
// Two submissions before the async confirmation returns must still send once.
const check = deferred();
globalThis.maybeConfirmSystemCheck = () => check.promise;
elements.message.value = 'first';
const first = submit();
const duplicate = submit();
check.resolve(false);
await new Promise(setImmediate);
assert.equal(requests.length, 1, 'concurrent preflight must not dispatch two turns');
assert.equal(requests[0].body.text, 'first');
assert.equal(state.stopped.bot, undefined, 'a new turn clears the stop override');
assert.equal(state.bots[0].messages.filter(m => m.role === 'user').length, 1);
elements.message.value = 'second';
await submit();
assert.equal(requests.length, 1);
assert.equal(elements.message.value, 'second');
response.resolve({});
await Promise.all([first, duplicate]);
await submit();
assert.equal(requests.length, 1, 'HTTP acceptance is not turn completion');
setWorking('bot', false);
response = deferred();
const second = submit();
await new Promise(setImmediate);
assert.equal(requests.length, 2, 'completion unlocks the next turn');
response.reject(new Error('network failed'));
await second;
assert.equal(isWorking(), false, 'request failure releases the working lock');
// Confirmation early-return and errors must not leave the submit lock stuck.
globalThis.maybeConfirmSystemCheck = async () => true;
elements.message.value = 'needs confirmation';
await submit();
assert.equal(requests.length, 2);
assert.equal(elements.message.value, 'needs confirmation');
globalThis.maybeConfirmSystemCheck = async () => { throw new Error('check failed'); };
await assert.rejects(submit(), /check failed/);
globalThis.maybeConfirmSystemCheck = async () => false;
response = deferred();
const retry = submit();
await new Promise(setImmediate);
assert.equal(requests.length, 3, 'early returns and errors release the submit lock');
response.resolve({});
await retry;
''')
        self.assertIn("if (!state.stopped[b.id]) setWorking(b.id, true)", self.app)

    def test_hermes_slash_menu_in_composer(self) -> None:
        self.assertIn('id="slash-menu"', self.index)
        self.assertIn("const AGENT_SLASH", self.app)
        self.assertIn("function syncSlashMenu", self.app)
        self.assertIn("function handleSlash", self.app)
        self.assertIn("function slashSubfields", self.app)
        self.assertIn('cmd: "/compact"', self.app)
        self.assertIn('cmd: "/plan"', self.app)
        self.assertIn('cmd: "/imagine"', self.app)
        self.assertIn('fieldSource: "models"', self.app)
        self.assertIn('fieldSource: "effort"', self.app)
        self.assertIn("function currentEffort", self.app)
        self.assertIn("function normalizeEffort", self.app)
        self.assertIn("function withOffLevel", self.app)
        self.assertIn("function thinkingFieldRows", self.app)
        self.assertIn("thinking ${effort}", self.app)
        self.assertIn('kind: "heading"', self.app)
        self.assertIn('heading: "thinking"', self.app)
        self.assertIn("is-current", self.app)
        self.assertIn("thinking ${currentEffort(b)}", self.app)
        models_src = self.app.split('if (source === "models")', 1)[1].split('if (source === "effort")', 1)[0]
        self.assertIn("thinkingFieldRows(", models_src)
        self.assertIn("more: hasThink", models_src)
        self.assertNotIn("kind: \"heading\"", models_src.split("return models.map", 1)[-1])
        self.assertIn(".slash-item.is-current", self.hermesbot_css)
        self.assertNotIn("Use /model <id> [effort] to switch.", self.app)
        self.assertIn('fieldSource: "workflows"', self.app)
        self.assertIn('fieldSource: "memory"', self.app)
        self.assertIn('"off", "low", "medium", "high", "xhigh"', self.app)
        self.assertIn("No thinking", self.app)
        self.assertIn('value: "runs"', self.app)
        self.assertIn('value: "pause"', self.app)
        self.assertIn('kind === "field"', self.app)
        self.assertNotIn("/\\s/.test(typed)", self.app)
        self.assertIn('type / for commands', self.app)
        self.assertIn("available_commands_update", self.deskd)
        self.assertIn("session.commands", self.deskd)
        self.assertIn('entry["input"]', self.deskd)
        self.assertIn(".slash-menu", self.hermesbot_css)
        self.assertIn(".slash-item .slash-arg", self.hermesbot_css)
        self.assertIn("openUserSettings", (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8"))
        handle = self.app.split("async function handleSlash", 1)[1].split("async function undoLastTurn", 1)[0]
        self.assertIn('if (agentBuild) return false;', handle)
        self.assertIn('if (arg && agentBuild) return false;', handle)
        self.assertIn("return false;", handle)
        self.assertNotIn("if (effort && agentBuild) return false", handle)
        self.assertIn("await applyBotModel(b, modelId, effort || undefined)", handle)
        self.assertIn("await applyBotModel(b, b.model, level)", handle)
        self.assertIn("row.setting", self.app)
        pick = self.app.split("async function applySlashPick", 1)[1].split("function nextSlashIndex", 1)[0]
        self.assertIn("if (row.setting)", pick)
        self.assertIn("await handleSlash(String(next).trim(), b)", pick)

    def test_grok_build_chat_matches_tui_tool_blocks(self) -> None:
        self.assertIn("function toolOutputFromUpdate", self.app)
        self.assertIn("function unpackPackedMarkdownTables", self.app)
        self.assertIn("function unpackPackedMarkdownLists", self.app)
        chatter = self.app.split("function stripChatterboxTags", 1)[1].split("function keepChatterboxTags", 1)[0]
        self.assertNotIn(".trim()", chatter)
        self.assertIn("function stripThinkTags", self.app)
        self.assertIn("think\\b", self.app)
        self.assertIn('if (text && last && last.role === "assistant")', self.app)
        self.assertIn("function prettyToolOutput", self.app)
        self.assertIn("function toolCommandFromUpdate", self.app)
        self.assertIn('if (kind === "execute"', self.app)
        self.assertIn("slice(0, TOOL_OUTPUT_MAX)", self.app)
        self.assertNotIn("slice(0, 1200)", self.app)
        self.assertIn("el.open = !!m.expanded;", self.app)
        self.assertIn("if (running && m.expanded !== false) el.open = true;", self.app)
        self.assertIn("function bindFold", self.app)
        self.assertNotIn("el.open = running;", self.app)
        self.assertNotIn("el.open = running || failed;", self.app)
        self.assertNotIn('if (el.classList.contains("is-running")) return;', self.app)
        self.assertIn("<details class=\"code-block", self.app)
        self.assertNotIn("gb-fold-btn", self.app)
        self.assertNotIn(".gb-fold-btn", self.css)
        self.assertIn(".gb-tool.is-failed", self.css)
        self.assertIn("Same tools, answers, and transcript as a regular Hermes TUI session", self.index)

    def test_grok_build_tui_parity_progress_todos_workflow_effort(self) -> None:
        # TUI-style transient progress line: the local-model prefill wait is a
        # dim progress line that updates in place, never a "Thought 1ms" block.
        self.assertIn("function renderProgressLine", self.app)
        self.assertIn('role === "progress"', self.app)
        self.assertIn('"agent_progress"', self.app)
        self.assertIn('if (b.messages[i].role === "progress") b.messages.splice(i, 1);', self.app)
        self.assertIn("def emit_local_progress", self.deskd)
        self.assertIn('"sessionUpdate": "agent_progress"', self.deskd)
        prefill = self.deskd.split("def start_local_prefill_progress", 1)[1].split("threading.Thread(target=tick", 1)[0]
        self.assertIn("bot_kind_is_agent(bot)", prefill)
        self.assertIn("emit_local_progress", prefill)
        self.assertIn('emit_local_activity(bot, "Thinking…")', prefill)
        self.assertIn("Reading the local-model prompt", prefill)
        self.assertNotIn("agent_thought_chunk", prefill)
        self.assertIn("emit_local_progress(bot, \"\")", self.deskd)
        # TUI-style live task list for todo_write.
        self.assertIn("function todosFromUpdate", self.app)
        self.assertIn("function renderTodoBlock", self.app)
        self.assertIn("found.isTodos = true", self.app)
        self.assertIn("m.isTodos && (m.todos || []).length", self.app)
        self.assertIn(".gb-todo", self.hermesbot_css)
        self.assertIn(".todo-row", self.hermesbot_css)
        self.assertIn(".todo-row.st-in_progress", self.hermesbot_css)
        # Subagent / workflow progress.
        self.assertIn('if (/spawn_subagent/.test(name)) return "Subagent";', self.app)
        self.assertIn('if (name === "workflow") return "Workflow";', self.app)
        self.assertIn("function trackWorkflowRun", self.app)
        self.assertIn("b.workflow_runs = b.workflow_runs || []", self.app)
        # TUI status line: reasoning effort shown for Hermes Agent bots only.
        self.assertIn('id="effort-chip"', self.index)
        self.assertIn('b.kind === "hermes" ? currentEffort(b) : ""', self.app)
        self.assertIn(".effort-chip[hidden]", self.hermesbot_css)
        self.assertIn(".gb-progress", self.hermesbot_css)

    def test_chat_stays_pinned_to_latest(self) -> None:
        self.assertIn("function stickTranscript", self.app)
        self.assertIn("chatStickBottom", self.app)
        self.assertIn("margin-top: auto", self.css)

    def test_assistant_stream_restart_does_not_duplicate(self) -> None:
        self.assertIn("function collapseRestartedAssistant", self.app)
        self.assertIn("STREAM_RESTART_HEAD", self.app)
        self.assertIn("def collapse_restarted_assistant", self.deskd)
        self.run_frontend_node(r'''
load('const STREAM_RESTART_HEAD', 'function mdInline(');
const head = "Here's the honest, system-specific breakdown — what each one actually buys you on hermes-desk, and where the real value (and risk) is.\n";
const truncated = head + "Chrome DevTools — adopt now.\nthen copy the good ones into the";
const full = truncated + " skills directory and watch. Telescope first.";
const merged = mergeAssistantStream(truncated, "\n" + full);
assert.equal((merged.match(/Here's the honest/g) || []).length, 1);
assert.ok(merged.includes("Telescope first."));
const glued = truncated + "\n" + full;
assert.equal(collapseRestartedAssistant(glued), full);
''')

    def test_grok_build_tui_parity_behavior(self) -> None:
        self.run_frontend_node(r'''
load('function escapeHtml(', 'function mergeAssistantStream(');
load('function mergeAssistantStream(', 'function mdInline(');
load('const TOOL_OUTPUT_MAX', 'function toolLabel(');
load('function bindFold(', 'function renderWorked(');
load('function renderProgressLine(', 'function planEntriesFromUpdate(');
load('function planEntriesFromUpdate(', 'function renderHorizon(');
globalThis.setWorking = (id, on) => { state.working[id] = !!on; };
const makeEl = () => {
  const classes = new Set();
  return { value: '', style: {}, dataset: {}, listeners: {},
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
    setAttribute() {}, addEventListener() {}, open: false };
};
document.createElement = makeEl;
const b = { id: 'gb1', kind: 'hermes', messages: [] };
// Prefill wait: one progress line that updates in place, cleared when tokens start.
applySessionTurn(b, { sessionUpdate: 'agent_progress', content: { text: 'Reading the local-model prompt (23,425 tokens). First token waits on GPU prefill…' } });
assert.equal(b.messages.length, 1);
assert.equal(b.messages[0].role, 'progress');
assert.ok(!b.messages.some((m) => m.role === 'thought'), 'prefill wait must not create a Thought block');
applySessionTurn(b, { sessionUpdate: 'agent_progress', content: { text: 'Reading the local-model prompt (23,425 tokens)… 42%' } });
assert.equal(b.messages.length, 1, 'progress line updates in place');
assert.match(b.messages[0].text, /42%/);
const line = renderProgressLine(b.messages[0]);
assert.equal(line.className, 'gb-progress');
assert.match(line.textContent, /23,425/);
applySessionTurn(b, { sessionUpdate: 'agent_progress', content: { text: '' } });
assert.equal(b.messages.length, 0, 'first token clears the progress line');
// todo_write: TUI-style live task list.
applySessionTurn(b, {
  sessionUpdate: 'tool_call', toolCallId: 'todo-1', title: 'todo_write', status: 'in_progress',
  _meta: { 'x.ai/tool': { name: 'todo_write', label: 'Todo', kind: 'other', input: { todos: [
    { id: 'a', content: 'Study code', status: 'completed' },
    { id: 'b', content: 'Implement', status: 'in_progress' },
    { id: 'c', content: 'Verify', status: 'pending' } ] } } },
});
const t = b.messages[b.messages.length - 1];
assert.equal(t.role, 'tool');
assert.equal(t.isTodos, true);
assert.equal(t.todos.length, 3);
const block = renderTodoBlock(t, 0);
assert.match(block.innerHTML, /Tasks/);
assert.match(block.innerHTML, /1\/3 done/);
assert.ok(block.innerHTML.includes('✓') && block.innerHTML.includes('▶') && block.innerHTML.includes('Verify'));
applySessionTurn(b, {
  sessionUpdate: 'tool_call_update', toolCallId: 'todo-1', status: 'completed',
  _meta: { 'x.ai/tool': { name: 'todo_write', input: { todos: [
    { id: 'a', content: 'Study code', status: 'completed' },
    { id: 'b', content: 'Implement', status: 'completed' },
    { id: 'c', content: 'Verify', status: 'in_progress' } ] } } },
});
assert.equal(t.status, 'completed');
assert.equal(t.todos[1].status, 'completed');
assert.match(renderTodoBlock(t, 0).innerHTML, /2\/3 done/);
// workflow: session runs tracked for /workflow runs.
applySessionTurn(b, {
  sessionUpdate: 'tool_call', toolCallId: 'wf-1', title: 'workflow', status: 'in_progress',
  _meta: { 'x.ai/tool': { name: 'workflow', label: 'Workflow', input: { source: { type: 'name', name: 'deep-research' } } } },
});
assert.equal(b.workflow_runs.length, 1);
assert.equal(b.workflow_runs[0].name, 'deep-research');
assert.equal(b.workflow_runs[0].phase, 'running');
// Turn completion clears any leftover progress line.
applySessionTurn(b, { sessionUpdate: 'agent_progress', content: { text: 'prefill 10%' } });
applySessionTurn(b, { sessionUpdate: 'turn_completed', stop_reason: 'end_turn', elapsed_ms: 1234 });
assert.ok(!b.messages.some((m) => m.role === 'progress'));
''')

    def test_pasted_chat_images_are_not_duplicated(self) -> None:
        self.assertIn("function mergeChatImages", self.app)
        self.assertIn("optimisticPaste", self.app)
        self.assertIn("uniqueChatImages", self.app)
        self.assertIn("openChatLightbox", self.app)
        self.assertIn("dated_media_relpath", self.deskd)

    def test_user_bubble_hides_saved_to_media_paths(self) -> None:
        self.assertIn("def visible_user_text", self.deskd)
        self.assertNotIn("Saved to {s['path']}", self.deskd)
        self.assertIn('prompt_text = text or ("Look at the attached media." if saved else "")', self.deskd)
        self.assertIn("queue_prompt", self.deskd)
        self.assertIn("take_prompt_turn", self.deskd)
        self.assertNotIn("Queue until the current turn finishes", self.app)
        self.assertIn("function visibleUserText", self.app)
        self.assertIn("visibleUserText(msg.text)", self.app)
        self.assertEqual(
            d.visible_user_text("look at this\nSaved to Pictures/2026/September/2026-09-03_14-30-52-chat.jpg"),
            "look at this",
        )
        self.assertEqual(
            d.visible_user_text("Saved to Videos/2026/September/2026-09-03_08-01-00-video.mp4"),
            "",
        )

    def test_phone_can_attach_camera_roll_images(self) -> None:
        self.assertIn('id="file-input"', self.index)
        self.assertNotIn('id="file-input" type="file" accept="image/*,video/*" multiple hidden', self.index)
        self.assertIn('class="round-btn attach-wrap"', self.index)
        self.assertIn(".heic", self.index)
        self.assertIn("function mediaFilesFromClipboard", self.app)
        self.assertIn("function guessMediaMime", self.app)
        self.assertIn("function prepareChatImage", self.app)
        self.assertIn("CHAT_IMAGE_MAX_EDGE", self.app)
        self.assertIn("ingestClipboardEvent", self.app)
        self.assertNotIn('$("attach-btn")?.addEventListener("click", () => $("file-input")?.click())', self.app)
        self.assertIn("body.mobile-chat #file-input", self.hermesbot_css)
        self.assertIn("opacity: 0", self.css)

    def test_observer_mode_renders_minios_only(self) -> None:
        self.assertIn('new URLSearchParams(window.location.search).get("observe")', self.app)
        self.assertIn('window.deskObserverReady = true', self.app)
        self.assertIn('body.observer-mode #ubuntuDesktopViewer', self.css)
        self.assertNotIn('\\nbody.observer-mode', self.css)
        self.assertIn('body.observer-mode .ubuntu-desktop-area', self.css)
        self.assertIn('.ubuntu-desktop-area', (ROOT / "deskd" / "surfaces.py").read_text(encoding="utf-8"))
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn('get("observe")', ui)
        self.assertIn("body.observer-mode #os-login", self.hermesbot_css)
        self.assertIn('document.body.classList.add("observer-mode")', self.index)

    def test_desktop_observer_endpoints_exist(self) -> None:
        self.assertIn('/desktop/observe', self.deskd)
        self.assertIn('/desktop/frame', self.deskd)
        self.assertIn('def ensure_observer', self.deskd)
        self.assertIn('def observer_state', self.deskd)

    def test_agent_prompt_requires_visual_verification(self) -> None:
        hermes = d.agent_md_for_kind("hermes")
        teela = d.agent_md_for_kind("teela-brain")
        self.assertNotIn("persistent visual mirror", hermes.lower())
        self.assertNotIn("desktop_observe", hermes)
        self.assertIn("desktop_observe", self.desktop_mcp)
        self.assertIn("desktop_watch", self.desktop_mcp)
        self.assertIn("You have a body", teela)
        self.assertIn("robot_status.spoken is the same live feel", teela)
        self.assertIn("bot_desktop__teela_system_check", teela)
        self.assertNotIn("--language-model-only", self.deskd)

    def test_standard_arrow_cursor_and_click_tools_exist(self) -> None:
        self.assertIn('class="agent-cursor-arrow"', self.index)
        self.assertIn('fill:#fff; stroke:#111', self.hermesbot_css)
        self.assertIn('"name": "desktop_click"', self.desktop_mcp)
        self.assertIn('"name": "desktop_double_click"', self.desktop_mcp)
        self.assertIn('function agentBrowserClick', self.app)
        self.assertIn('type: "mousePressed"', self.app)
        self.assertIn('type: "mouseReleased"', self.app)
        self.assertIn('id="browser-hit"', self.index)
        self.assertNotIn("minmax(0,1fr) 30px", self.hermesbot_css)
        self.assertIn("28px 28px minmax(0,1fr)", self.hermesbot_css)
        self.assertNotIn("auto 28px 28px minmax(0,1fr)", self.hermesbot_css)
        self.assertIn('enqueueBrowser({ type: "mouseMoved", x, y, modifiers: mods })', self.app)
        self.assertIn("def _focus_point", (ROOT / "deskd" / "surfaces.py").read_text(encoding="utf-8"))

    def test_browser_page_body_can_receive_typed_keys(self) -> None:
        surfaces = (ROOT / "deskd" / "surfaces.py").read_text(encoding="utf-8")
        keys_css = self.css.split("\n#browser-keys {", 1)[1].split("}", 1)[0]
        self.assertIn('id="browser-keys"', self.index)
        self.assertIn("inset: 0", keys_css)
        self.assertNotIn("width: 8px", keys_css)
        self.assertIn("pointer-events: auto", keys_css)
        self.assertIn("function grabScreenKeys", self.app)
        self.assertNotIn('else if (msg.surface === "browser")', self.app)
        self.assertIn("observerBotId && msg.surface", self.app)
        self.assertIn("function stopDrivingScreen", self.app)
        self.assertIn('enqueueBrowser({ type: "insertText", text: e.key })', self.app)
        self.assertIn('type: "rawKeyDown"', self.app)
        self.assertNotIn('if (!driveScreen || state.surface !== "browser") return', self.app)
        self.assertNotIn('t.id === "url-bar" || t.id === "message"', self.app)
        self.assertIn("def _dom_insert_text", surfaces)
        self.assertIn("def _focus_page", surfaces)
        self.assertIn("Input.insertText", surfaces)
        self.assertIn("def _cast_watch", surfaces)
        self.assertIn("def _kick_frame", surfaces)

    def test_browser_screencast_matches_viewport(self) -> None:
        surfaces = (ROOT / "deskd" / "surfaces.py").read_text(encoding="utf-8")
        self.assertIn('"maxWidth": int(self.view_w or 1280)', surfaces)
        self.assertIn('"maxHeight": int(self.view_h or 800)', surfaces)
        self.assertIn("Do not copy screencast JPEG size into view_w/view_h", surfaces)
        self.assertIn("if not self._casting:", surfaces)
        self.assertIn("self._start_cast(restart=True)", surfaces)
        self.assertIn("object-fit: contain", self.css.split("#browser-frame {", 1)[1].split("}", 1)[0])
        self.assertIn("ready.onload", self.app)
        self.assertIn("stale_on_timeout=False", surfaces)
        self.assertNotIn("--disable-gpu", surfaces)
        self.assertIn("last.type === \"insertText\"", self.app)

    def test_bot_cursor_hides_when_idle(self) -> None:
        self.assertIn("function hideAgentDesktopCursor", self.app)
        self.assertIn("scheduleAgentCursorHide", self.app)
        self.assertIn("AGENT_CURSOR_IDLE_MS", self.app)
        self.assertIn("moveAgentDesktopCursor(b.desktop_cursor?.x ?? 500, b.desktop_cursor?.y ?? 500, false, false)", self.app)
        self.assertIn(".agent-desktop-cursor.is-active", self.hermesbot_css)

    def test_cursor_position_is_persisted_per_bot(self) -> None:
        self.assertIn('self.desktop_cursor = {"x": 500.0, "y": 500.0}', self.deskd)
        self.assertIn('"desktop_cursor": dict(self.desktop_cursor)', self.deskd)
        self.assertIn('bot.desktop_cursor = {"x": x, "y": y}', self.deskd)

    def test_ui_does_not_estimate_tps_from_characters(self) -> None:
        self.run_frontend_node(r'''
load('const CHATTERBOX_TURBO_TAGS', 'function mdInline(');
load('function fmtNum(', 'function modelLabel(');
load('function renderMeta(', 'const MODEL_ICON_STOP');
load('let eventSource = null;', '(async function init()');
let stream;
globalThis.EventSource = class { constructor() { stream = this; } };
// Render the real metadata after real streaming/usage handlers update the bot.
globalThis.renderConversation = b => renderMeta(b);
connectEvents();
const bot = state.bots[0];
const emit = msg => stream.onmessage({data: JSON.stringify({bot_id: bot.id, ...msg})});
const chunk = text => emit({type: 'session.update',
  update: {sessionUpdate: 'agent_message_chunk', content: {text}}});
renderMeta(bot);
assert.equal(elements['tps-counter'].textContent, '0.0');
for (const text of ['hello', ' a much longer chunk '.repeat(500), '日本語🙂'.repeat(200)]) {
  chunk(text);
  assert.equal(elements['tps-counter'].textContent, '0.0', 'text alone cannot establish TPS');
  assert.equal(bot.tps, undefined);
}
assert.ok(bot.messages[0].text.includes('日本語'), 'stream chunks must actually reach the transcript');
emit({type: 'usage', tps: 42.125, used: 12500, window: 1000000,
  speed_source: 'provider-timing', token_source: 'provider-usage'});
assert.equal(bot.tps, 42.125);
assert.equal(elements['tps-counter'].textContent, '42.1');
assert.equal(tpsStat.title, '42.13 tok/s · provider-timing · provider-usage');
assert.equal(elements['context-usage'].textContent, '13k / 1M');
assert.equal(elements['context-stat']['data-pressure'], 'GREEN');
chunk(' more output '.repeat(1000));
assert.equal(bot.tps, 42.125, 'character counts must not overwrite provider speed');
assert.equal(elements['tps-counter'].textContent, '42.1');
for (const tps of [undefined, null, -1, '999', 'NaN']) {
  emit({type: 'usage', tps});
  assert.equal(bot.tps, 42.125, 'invalid/missing usage preserves measured speed');
}
emit({type: 'usage', tps: 0});
assert.equal(bot.tps, 0, 'zero is a valid measurement');
assert.equal(elements['tps-counter'].textContent, '0.0');
for (const invalid of [undefined, NaN, Infinity, -1]) {
  bot.tps = invalid;
  renderMeta(bot);
  assert.equal(elements['tps-counter'].textContent, '0.0', 'never render invalid speed');
}
''')
        # Keep the historical forbidden heuristics as a supplementary guard.
        self.assertNotIn("/ 3.7", self.app)
        self.assertNotIn("_tpsChars", self.app)
        # Cache-busting versions may change independently of TPS behavior.
        self.assertRegex(self.index, r'<script\b[^>]*\bsrc="/app\.js\?v=[^"\s]+"')


class BrowserDiscoveryTests(unittest.TestCase):
    def test_browser_resolver_returns_a_candidate(self) -> None:
        self.assertTrue(resolve_chrome_bin())


if __name__ == "__main__":
    unittest.main()
