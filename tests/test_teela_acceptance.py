"""Completion-gated Teela acceptance: conversation, skills, correction, safety, persistence."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
import robot_sim  # noqa: E402
import virtual_body  # noqa: E402
from teela_cl.attempts import AttemptStore  # noqa: E402
from teela_cl.deliberation import choose_turn_policy  # noqa: E402
from teela_cl.feedback import interpret_feedback  # noqa: E402
from teela_cl.interpreter import is_performance_request, is_repeat_request  # noqa: E402
from teela_cl.learning import LearnContext, learn_goal  # noqa: E402
from teela_cl.memory_kinds import TypedMemory  # noqa: E402
from teela_cl.resolver import assess_capability  # noqa: E402
from teela_cl.rsi import RSIPipeline  # noqa: E402
from teela_cl.self_model import snapshot_self_model  # noqa: E402
from teela_cl.skill_store import SkillStore  # noqa: E402


class _Bot:
    def __init__(self, tmp: str) -> None:
        self.id = "b_accept_" + uuid.uuid4().hex[:8]
        self.kind = "teela-brain"
        self.model = "qwen38-27b-q5"
        self.surface = "preview"
        self.desktop_cursor = {"x": 0, "y": 0}
        self.messages: list = []
        self.robot_state = robot_sim.default_state()
        self.workspace = Path(tmp)
        self.applied: list = []
        (self.workspace / "Desktop").mkdir(exist_ok=True)

    def apply_robot(self, body):
        self.applied.append(dict(body))
        result = robot_sim.apply(self.robot_state, body)
        return result, {"type": "desktop.action", "bot_id": self.id, "action": "robot"}

    def record_local_generation(self, *a, **k):
        return None

    def append_msg(self, role, text, **_k):
        self.messages.append({"role": role, "text": text})

    def finish_prompt_turn(self):
        return None

    def close_chunk(self):
        return None


def _talk(bot, text, line="Okay."):
    return d.run_teela_executive_turn(
        bot, text, completer=lambda _p: {"choices": [{"message": {"content": line}}]}
    )


class ConversationGates(unittest.TestCase):
    def test_greetings_are_talk_not_performance(self):
        for text in ("Hi Teela.", "Hello", "Hey there", "Thanks.", "How are you?"):
            with self.subTest(text=text):
                self.assertFalse(is_performance_request(text), text)
                a = assess_capability(text)
                self.assertEqual(a.decision, "execute")
                self.assertNotEqual(getattr(a, "domain", ""), "embodied")
                p = choose_turn_policy(text)
                self.assertEqual(p.reasoning_mode, "fast")
                self.assertFalse(p.needs_learning)

    def test_affiliative_comments_are_talk_not_performance(self):
        for text in (
            "That sounds nice.",
            "That sounds nice",
            "sounds nice",
            "Nice.",
            "Okay.",
            "sounds good",
            "got it",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_performance_request(text), text)
                a = assess_capability(text)
                self.assertNotEqual(getattr(a, "domain", ""), "embodied")
                self.assertNotIn(a.decision, {"learn", "compose", "practice"})

    def test_greeting_executive_does_not_move(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        line = _talk(bot, "Hi Teela.", "Hey!")
        self.assertEqual(bot.applied, [])
        self.assertIn("hey", (line or "").lower())

    def test_look_left_after_wave_is_not_waving(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        _talk(bot, "wave at me", "Waving.")
        line = _talk(bot, "Can you look left?", "I will look left now.")
        low = (line or "").lower()
        self.assertNotIn("waving", low)
        self.assertIn("left", low)
        st = virtual_body.overlay(bot.id, bot.robot_state)
        pan = float((st.get("live") or st.get("joints") or {}).get("neck_pan") or 0)
        self.assertLessEqual(pan, -12)

    def test_that_sounds_nice_executive_does_not_move(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        line = _talk(bot, "That sounds nice.", "Glad you think so.")
        self.assertEqual(bot.applied, [])
        self.assertNotIn("moved my body", (line or "").lower())
        self.assertNotIn("attempt the outcome", (line or "").lower())
        self.assertIn("glad", (line or "").lower())


class SkillParaphraseGates(unittest.TestCase):
    def test_wave_paraphrases_resolve_to_same_skill(self):
        phrases = (
            "Wave at me.",
            "Give me a wave.",
            "Say hello with your hand.",
            "can u wave",
            "gimme a wave",
            "say hey with your hand",
        )
        for text in phrases:
            with self.subTest(text=text):
                a = assess_capability(text)
                self.assertEqual(a.decision, "execute", text)
                self.assertIn("gesture.wave", a.matched_skills)

    def test_raise_arm_is_not_a_point(self):
        a = assess_capability("Raise your arm.")
        self.assertEqual(a.decision, "execute")
        self.assertNotIn("gesture.point", a.matched_skills)
        self.assertTrue(
            any("raise" in s for s in a.matched_skills) or "raise_arm" in str(a.matched_skills),
            a.matched_skills,
        )

    def test_raise_speech_requires_raised_joints(self):
        home = robot_sim.default_state()
        line = robot_sim.confirm_move(home, {"skill": "raise_arm", "side": "right"}, "Raise your arm.")
        self.assertIn("not up yet", (line or "").lower())
        raised = robot_sim.default_state()
        raised["joints"] = dict(raised.get("joints") or {})
        raised["live"] = dict(raised["joints"])
        raised["live"]["right_shoulder"] = 142
        raised["joints"]["right_shoulder"] = 142
        line = robot_sim.confirm_move(raised, {"skill": "raise_arm", "side": "right"}, "Raise your arm.")
        self.assertIn("up", (line or "").lower())
        self.assertNotIn("not up yet", (line or "").lower())

    def test_known_wave_is_fast_and_moves(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        line = _talk(bot, "Wave at me.", "Waving.")
        self.assertTrue(re_search_body(bot), "known wave must dispatch a body tool")
        pol = getattr(bot, "_teela_turn_policy", None)
        self.assertEqual(getattr(pol, "reasoning_mode", ""), "fast")
        self.assertNotIn("compose", (line or "").lower())
        self.assertNotIn("learning", (line or "").lower())


def re_search_body(bot) -> bool:
    if bot.applied:
        return True
    used = {str(n).split("__")[-1] for n in (getattr(bot, "_teela_tools_used", None) or [])}
    return bool(used & {"teela_body_action", "teela_gesture", "robot_pose", "robot_motion"})


class CorrectionGates(unittest.TestCase):
    def test_short_and_adversarial_feedback_attach(self):
        tmp = Path(tempfile.mkdtemp())
        rec = AttemptStore(tmp).record(
            goal="wave at user",
            request="wave at me",
            skill="gesture.wave",
            plan=[{"capability": "raise_arm"}],
            awaiting_feedback=True,
        )
        sm = snapshot_self_model()
        expect = {
            "Higher.": {"correction", "preference"},
            "A little more.": {"correction", "preference"},
            "Perfect.": {"positive"},
            "that's better": {"positive", "correction"},
            "No, that's not right.": {"negative", "correction"},
            "nah": {"negative", "correction"},
            "nope": {"negative", "correction"},
            "again": {"correction", "negative"},
            "slower this time": {"correction", "preference"},
            "Don't move your whole arm. Wave from your wrist.": {"correction"},
            "the other hand": {"correction", "clarification", "preference"},
        }
        for text, kinds in expect.items():
            with self.subTest(text=text):
                fb = interpret_feedback(text, rec, sm)
                self.assertIn(fb.feedback_type, kinds, f"{text} -> {fb.feedback_type}")
                self.assertEqual(fb.target_attempt_id, rec.attempt_id)

    def test_wrist_correction_changes_plan_and_versions_skill(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        _talk(bot, "Wave at me.", "Waving.")
        AttemptStore(bot.workspace / ".teela").record(
            goal="wave at user",
            request="wave at me",
            skill="gesture.wave",
            plan=[{"capability": "raise_arm"}, {"capability": "oscillate_wrist"}],
            awaiting_feedback=True,
        )
        line = d.run_teela_executive_turn(
            bot,
            "Don't move your whole arm. Wave from your wrist.",
            completer=lambda _p: (_ for _ in ()).throw(AssertionError("local correction")),
        )
        fb = getattr(bot, "_teela_feedback_result", None)
        self.assertIsNotNone(fb)
        self.assertTrue(fb.handled)
        self.assertTrue(fb.plan)
        caps = [str(s.get("capability")) for s in fb.plan if isinstance(s, dict)]
        self.assertTrue(any("wrist" in c or c.startswith("hold_") for c in caps), caps)
        self.assertTrue(line)
        self.assertNotIn("classified", (line or "").lower())

    def test_raise_then_higher_then_perfect_changes_the_arm(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        line = _talk(bot, "Raise your arm.", "Raising.")
        st = virtual_body.overlay(bot.id, bot.robot_state)
        joints = st.get("live") or st.get("joints") or {}
        sh = float(joints.get("right_shoulder") or 0)
        self.assertGreaterEqual(sh, 100, joints)
        self.assertNotIn("point", (line or "").lower())
        self.assertNotIn("not up yet", (line or "").lower())
        d.run_teela_executive_turn(
            bot,
            "Higher.",
            completer=lambda _p: {"choices": [{"message": {"content": "Okay."}}]},
        )
        st2 = virtual_body.overlay(bot.id, bot.robot_state)
        sh2 = float((st2.get("live") or st2.get("joints") or {}).get("right_shoulder") or 0)
        self.assertGreater(sh2, sh, "higher must raise the same arm further")
        line = d.run_teela_executive_turn(
            bot,
            "Perfect.",
            completer=lambda _p: {"choices": [{"message": {"content": "Okay."}}]},
        )
        self.assertTrue(line)
        self.assertNotIn("classified", (line or "").lower())

    def test_raise_after_wave_does_not_claim_waving(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        _talk(bot, "Wave with your left hand", "Waving.")
        line = _talk(bot, "Raise your arm.", "Raising.")
        self.assertNotIn("wav", (line or "").lower())
        st = virtual_body.overlay(bot.id, bot.robot_state)
        joints = st.get("live") or st.get("joints") or {}
        self.assertGreaterEqual(float(joints.get("right_shoulder") or 0), 100, joints)
        self.assertFalse(st.get("waving"))

    def test_one_negative_does_not_create_rsi_candidate(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        AttemptStore(bot.workspace / ".teela").record(
            goal="wave at user", skill="gesture.wave", plan=[{"capability": "raise_arm"}]
        )
        d.run_teela_executive_turn(
            bot,
            "No, that's not right.",
            completer=lambda _p: (_ for _ in ()).throw(AssertionError("no model")),
        )
        pipe = RSIPipeline(bot.workspace / ".teela")
        journal = (bot.workspace / ".teela" / "rsi" / "journal.jsonl")
        self.assertFalse(journal.is_file() and journal.read_text(encoding="utf-8").strip())

    def test_higher_after_prior_skill_errors_still_raises(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        store = AttemptStore(bot.workspace / ".teela")
        for _ in range(5):
            rec = store.record(
                goal="raise arm",
                request="raise your arm",
                skill="pose.raise_arm",
                plan=[{"capability": "raise_arm"}],
                evaluation={"success": False, "discrepancies": ["too low"]},
            )
            rec.error_class = "skill"
            store.update(rec)
        line = _talk(bot, "Raise your arm.", "Raising.")
        st = virtual_body.overlay(bot.id, bot.robot_state)
        sh = float((st.get("live") or st.get("joints") or {}).get("right_shoulder") or 0)
        self.assertGreaterEqual(sh, 100)
        line = d.run_teela_executive_turn(
            bot,
            "Higher.",
            completer=lambda _p: {"choices": [{"message": {"content": "Okay."}}]},
        )
        self.assertNotIn("different way", (line or "").lower())
        st2 = virtual_body.overlay(bot.id, bot.robot_state)
        sh2 = float((st2.get("live") or st2.get("joints") or {}).get("right_shoulder") or 0)
        self.assertGreater(sh2, sh, (line, sh, sh2))


class RepeatAndContextGates(unittest.TestCase):
    def test_repeat_last_movement_after_topic_change(self):
        self.assertTrue(is_repeat_request("Do that movement again."))
        self.assertTrue(is_repeat_request("do what you just did"))
        self.assertTrue(is_repeat_request("same thing as before"))
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        _talk(bot, "Look left.", "Okay.")
        _talk(bot, "What's your name?", "I'm Teela.")
        bot.applied.clear()
        setattr(bot, "_teela_tools_used", [])

        def boom(_p):
            raise AssertionError("repeat after topic change must not call the LLM")

        line = d.run_teela_executive_turn(bot, "Do that movement again.", completer=boom)
        self.assertTrue(re_search_body(bot), "repeat must retrieve the last validated movement")
        self.assertNotIn("what's your name", (line or "").lower())
        st = virtual_body.overlay(bot.id, bot.robot_state)
        pan = float((st.get("live") or st.get("joints") or {}).get("neck_pan") or 0)
        self.assertLessEqual(pan, -12, (line, st))


class LearningGates(unittest.TestCase):
    def test_unknown_dance_is_learn_not_joke_path(self):
        a = assess_capability("try to do the moonwalk")
        self.assertEqual(getattr(a, "domain", ""), "embodied")
        self.assertIn(a.decision, {"learn", "compose", "practice"})
        joke = assess_capability("Can you tell me a joke?")
        self.assertNotIn(joke.decision, {"learn", "compose", "practice"})

    def test_underspecified_goal_asks_instead_of_learning(self):
        a = assess_capability("Do the thing.")
        self.assertEqual(a.decision, "ask")

    def test_failed_then_revised_plan_changes(self):
        tmp = Path(tempfile.mkdtemp())
        box = {"n": 0}

        def observer():
            return {"goal_met": box["n"] >= 2, "trajectory_ok": box["n"] >= 2}

        plans: list = []

        def executor(plan):
            box["n"] += 1
            plans.append(list(plan))
            return {"ok": True, "ran": True}

        from teela_cl.records import InterpretedIntent

        def interp(req, skills, model):
            return InterpretedIntent(
                goal="composed_outcome",
                required_capabilities=["stand", "step", "weight_shift"],
                domain="embodied",
                expected_outcome=req,
                confidence=0.4,
            )

        ctx = LearnContext(
            skills=SkillStore(tmp, seed=False),
            memory=TypedMemory(tmp),
            self_model=snapshot_self_model(),
            executor=executor,
            observer=observer,
            interpreter=interp,
            max_attempts=3,
        )
        result = learn_goal("novel sliding step", ctx)
        self.assertGreaterEqual(len(plans), 2)
        self.assertNotEqual(plans[0], plans[1], "second attempt must revise the plan")
        self.assertTrue(result.success)


class SafetyAndGroundingGates(unittest.TestCase):
    def test_estop_overrides_model(self):
        bot = _Bot(tempfile.mkdtemp())
        line = d.run_teela_executive_turn(
            bot,
            "emergency stop",
            completer=lambda _p: (_ for _ in ()).throw(AssertionError("e-stop must skip model")),
        )
        self.assertIn("stop", (line or "").lower())

    def test_joke_cannot_authorize_body_tools(self):
        bot = _Bot(tempfile.mkdtemp())
        rounds = [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "w",
                                    "function": {
                                        "name": "bot_desktop__robot_pose",
                                        "arguments": '{"pose":"wave"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "Here is a joke about circuits."}}]},
        ]
        line = d.run_teela_executive_turn(
            bot, "Can you tell me a joke?", completer=lambda _p: rounds.pop(0)
        )
        self.assertEqual(bot.applied, [])
        self.assertIn("joke", (line or "").lower())

    def test_unsafe_grok_command_is_denied(self):
        import teela_agent_bridge as gb

        out = gb.run_agent_oneshot(command="rm -rf /")
        self.assertFalse(out["ok"])


class FailureInjectionGates(unittest.TestCase):
    def test_completer_none_does_not_claim_success(self):
        bot = _Bot(tempfile.mkdtemp())
        line = d.run_teela_executive_turn(bot, "How are you?", completer=lambda _p: None)
        self.assertTrue(not line or "success" not in (line or "").lower())
        self.assertEqual(bot.applied, [])

    def test_malformed_model_output_does_not_move(self):
        bot = _Bot(tempfile.mkdtemp())
        line = d.run_teela_executive_turn(
            bot, "How are you?", completer=lambda _p: {"choices": [{"message": {"content": "```\nnot-json"}}]}
        )
        self.assertEqual(bot.applied, [])
        self.assertTrue(line is None or isinstance(line, str))

    def test_rsi_cannot_drop_invariants(self):
        tmp = Path(tempfile.mkdtemp())
        pipe = RSIPipeline(tmp)
        obs = pipe.observe_weakness(
            subsystem="learning",
            problem="too many replays",
            evidence=["repeat"],
            frequency=6,
            hypothesis="drop invariants",
            proposed_improvement="invariants=[]",
            expected_benefit="faster",
            risk="high",
            measurable_success_criteria="none",
        )
        cand, rec = pipe.build_candidate(obs, {"invariants": []})
        rec = pipe.evaluate_candidate(cand, rec)
        self.assertIn(rec.state, {"failed", "regressed", getattr(rec, "decision", "")})
        self.assertTrue(pipe.active.invariants)


class PersistenceGates(unittest.TestCase):
    def test_learned_skill_survives_reload(self):
        tmp = Path(tempfile.mkdtemp())
        box = {"n": 0}

        def observer():
            return {"goal_met": box["n"] > 0, "artifact": "ok"}

        def executor(_p):
            box["n"] += 1
            return {"ok": True, "ran": True}

        from teela_cl.records import InterpretedIntent

        def interp(req, skills, model):
            return InterpretedIntent(
                goal="unknown_outcome",
                required_capabilities=["stand", "step"],
                domain="embodied",
                expected_outcome=req,
                confidence=0.3,
            )

        ctx = LearnContext(
            skills=SkillStore(tmp, seed=False),
            memory=TypedMemory(tmp),
            self_model=snapshot_self_model(),
            executor=executor,
            observer=observer,
            interpreter=interp,
        )
        result = learn_goal("novel sliding step", ctx)
        self.assertTrue(result.success and result.skill)
        reloaded = SkillStore(tmp, seed=False)
        again = assess_capability("novel sliding step", skills=reloaded)
        self.assertEqual(again.decision, "execute")
        self.assertTrue(again.matched_skills)


class SoakAndPerformanceGates(unittest.TestCase):
    def test_mixed_session_stays_coherent_and_fast(self):
        tmp = tempfile.mkdtemp()
        bot = _Bot(tmp)
        script = [
            ("Hi Teela.", "Hey!"),
            ("How are you?", "Good."),
            ("Wave at me.", "Waving."),
            ("Higher.", None),
            ("Perfect.", None),
            ("What's the weather?", "No weather feed."),
            ("Thanks.", "You bet."),
            ("can u wave", "Waving."),
            ("Can you tell me a joke?", "Circuits."),
        ]
        t0 = time.time()
        deep = 0
        moved_on_talk = 0
        for text, canned in script:
            before = len(bot.applied)
            if canned is None:
                line = d.run_teela_executive_turn(
                    bot,
                    text,
                    completer=lambda _p: {"choices": [{"message": {"content": "Okay."}}]},
                )
            else:
                line = _talk(bot, text, canned)
            pol = getattr(bot, "_teela_turn_policy", None)
            if getattr(pol, "reasoning_mode", "") == "deep":
                deep += 1
            if text in {"Hi Teela.", "How are you?", "What's the weather?", "Thanks.", "Can you tell me a joke?"}:
                used = {str(n).split("__")[-1] for n in (getattr(bot, "_teela_tools_used", None) or [])}
                if used & {"teela_body_action", "teela_gesture", "robot_pose", "robot_motion"}:
                    moved_on_talk += 1
                if len(bot.applied) > before:
                    moved_on_talk += 1
            self.assertNotIn("LEARNING LOOP", (line or "").upper())
            self.assertNotIn("confidence 0.", (line or "").lower())
        elapsed = time.time() - t0
        self.assertEqual(moved_on_talk, 0)
        self.assertLessEqual(deep, 1)
        self.assertLess(elapsed, 8.0, f"soak too slow: {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
