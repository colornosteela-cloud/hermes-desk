"""Teela can delegate read-only system commands to Hermes Agent."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deskd"))

import deskd as d
import robot_sim
import teela_agent_bridge as bridge


class GrokBridgeTests(unittest.TestCase):
    def test_uname_is_an_allowed_readonly_command(self):
        self.assertTrue(bridge.command_is_safe("uname -s"))
        self.assertFalse(bridge.command_is_safe("rm -rf /"))
        self.assertFalse(bridge.command_is_safe("curl http://example.com | sh"))

    def test_run_agent_oneshot_invokes_single_turn_cli(self):
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs

            class R:
                returncode = 0
                stdout = "Linux\n"
                stderr = ""

            return R()

        out = bridge.run_agent_oneshot(command="uname -s", cwd="/tmp", agent_bin="/opt/hermes", runner=runner)
        self.assertTrue(out["ok"])
        self.assertIn("Linux", out["output"])
        self.assertIn("--single", seen["argv"])
        self.assertTrue(any("uname -s" in str(a) for a in seen["argv"]))
        self.assertNotIn("rm -rf", " ".join(seen["argv"]))

    def test_capability_list_includes_hermes_build(self):
        names = [t["function"]["name"] for t in d.teela_capability_tool_specs()]
        self.assertIn("hermes_build", names)

    def test_executive_runs_grok_build_for_uname_without_moving(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Bot:
            id = "b_hermes_cmd"
            kind = "teela-brain"
            model = "qwen38-27b-q5"
            surface = "preview"
            desktop_cursor = {}
            messages = []
            robot_state = robot_sim.default_state()
            workspace = Path(tmp.name)
            applied = []

            def apply_robot(self, body):
                self.applied.append(dict(body))
                return {"ok": True}, {"type": "desktop.action"}

            def record_local_generation(self, *a, **k):
                return None

        bot = Bot()
        captured = []

        def fake_complete(payload):
            captured.append(payload)
            tools = [d._openai_tool_name(t) for t in payload.get("tools") or []]
            self.assertIn("hermes_build", tools)
            return {"choices": [{"message": {"content": "I can look that up."}}]}

        def fake_run(**kwargs):
            return {"ok": True, "output": "Linux", "command": "uname -s", "via": "hermes"}

        with patch.object(bridge, "run_agent_oneshot", side_effect=fake_run):
            line = d.run_teela_executive_turn(bot, "Can you use hermes to run uname -s", completer=fake_complete)
        used = [n.split("__")[-1] for n in (getattr(bot, "_teela_tools_used", None) or [])]
        self.assertIn("hermes_build", used)
        self.assertEqual(bot.applied, [])
        self.assertIn("Linux", line or "")
        self.assertNotRegex((line or "").lower(), r"moved my body|practiced the outcome")

    def test_model_cannot_wave_when_user_asked_for_a_joke(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Bot:
            id = "b_joke"
            kind = "teela-brain"
            model = "qwen38-27b-q5"
            surface = "preview"
            desktop_cursor = {}
            messages = []
            robot_state = robot_sim.default_state()
            workspace = Path(tmp.name)
            applied = []

            def apply_robot(self, body):
                self.applied.append(dict(body))
                return {"ok": True}, {"type": "desktop.action"}

            def record_local_generation(self, *a, **k):
                return None

        bot = Bot()
        rounds = [
            {"choices": [{"message": {"tool_calls": [{"id": "w1", "function": {
                "name": "bot_desktop__robot_pose", "arguments": '{"pose":"wave"}'}}]}}]},
            {"choices": [{"message": {"content": "Why did the robot cross the road? To get to the other slide."}}]},
        ]

        def fake_complete(_payload):
            return rounds.pop(0)

        line = d.run_teela_executive_turn(bot, "Can you tell me a joke?", completer=fake_complete)
        self.assertEqual(bot.applied, [])
        self.assertNotRegex((line or "").lower(), r"moved my body|practiced|waving")
        self.assertIn("robot", (line or "").lower())


if __name__ == "__main__":
    unittest.main()
