"""Conversation must not authorize embodied skill retrieval or learning."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deskd"))

from teela_cl.interpreter import is_performance_request, record_backed_interpreter
from teela_cl.resolver import assess_capability
from teela_cl.self_model import snapshot_self_model
from teela_cl.skill_store import SkillStore


class ConversationIntentRegressionTests(unittest.TestCase):
    def test_speech_requests_are_not_performance_requests(self):
        for request in (
            "Can you tell me a joke?", "Could you tell me a story",
            "Please explain how you walk", "Tell me about your balance",
            "Would you describe a wave?", "Can you say hello?",
            "What is a wave?", "How do you walk", "You are doing a wave",
            "That sounds nice.", "Nice.", "Okay.",
        ):
            with self.subTest(request=request):
                self.assertFalse(is_performance_request(request))
                intent = record_backed_interpreter(request, SkillStore().all(), snapshot_self_model())
                self.assertNotEqual(intent.domain, "embodied")
                result = assess_capability(request)
                self.assertNotIn(result.decision, {"learn", "compose", "practice"})
                self.assertEqual(result.matched_skills, [])

    def test_talk_intent_cannot_leak_semantically_matched_body_skill(self):
        from teela_cl.records import InterpretedIntent

        result = assess_capability(
            "Explain a wave",
            interpreter=lambda *args: InterpretedIntent(
                goal="explain", required_capabilities=[], domain="talk",
                expected_outcome="explain a wave", confidence=1.0,
            ),
        )
        self.assertEqual(result.decision, "execute")
        self.assertEqual(result.matched_skills, [])
        self.assertIsNone(result.capability)

    def test_direct_learning_of_conversation_never_calls_executor(self):
        from teela_cl.learning import LearnContext, learn_goal
        from teela_cl.memory_kinds import TypedMemory

        calls = []
        ctx = LearnContext(
            skills=SkillStore(), memory=TypedMemory(), self_model=snapshot_self_model(),
            executor=lambda plan: calls.append(plan) or {"ok": True, "ran": True},
        )
        result = learn_goal("Can you tell me a joke?", ctx)
        self.assertEqual(calls, [])
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.plan, [])
        self.assertIsNone(result.skill)
        self.assertFalse(result.success)

    def test_unspecified_plan_does_not_invent_locomotion(self):
        from teela_cl.learning import compose_plan

        self.assertEqual(compose_plan("spoken reply", []), [])
        self.assertEqual(compose_plan("unknown outcome", [], prior_plan=[
            {"capability": "stand", "action": "apply"}
        ]), [])

    def test_actual_body_requests_remain_supported(self):
        for request in ("Can you wave at me", "walk left", "say hello with your hand",
                        "greet that person physically", "You can put your leg down",
                        "try this unseen dance sequence", "moonwalk"):
            with self.subTest(request=request):
                self.assertTrue(is_performance_request(request))
                self.assertIn(assess_capability(request).decision, {"execute", "learn", "compose", "practice"})

    def test_system_and_grok_build_commands_are_software_not_body_learning(self):
        from teela_cl.learning import LearnContext, compose_plan, learn_goal
        from teela_cl.memory_kinds import TypedMemory

        for request in (
            "Can you run uname -s",
            "Can you use hermes to run system info",
            "check system info",
            "run uname -s",
        ):
            with self.subTest(request=request):
                intent = record_backed_interpreter(request, SkillStore(seed=True).all(), snapshot_self_model())
                self.assertEqual(intent.domain, "software")
                result = assess_capability(request)
                self.assertNotEqual(result.decision, "learn")
                self.assertNotIn("stand", result.required_capabilities)
                self.assertNotIn("step", result.required_capabilities)
                calls = []
                ctx = LearnContext(
                    skills=SkillStore(),
                    memory=TypedMemory(),
                    self_model=snapshot_self_model(),
                    executor=lambda plan: calls.append(plan) or {"ok": True, "ran": True},
                )
                learned = learn_goal(request, ctx)
                self.assertEqual(calls, [])
                self.assertFalse(any(
                    str(s.get("capability")) in {"stand", "step"} for s in (learned.plan or []) if isinstance(s, dict)
                ))
        self.assertEqual(compose_plan("run uname -s", []), [])


if __name__ == "__main__":
    unittest.main()
