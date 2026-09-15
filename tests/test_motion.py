#!/usr/bin/env python3
"""STATIC vs DYNAMIC motion routing and see-then-move."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
import motion  # noqa: E402
import robot_sim  # noqa: E402


class MotionRouterTests(unittest.TestCase):
    def test_roles(self) -> None:
        self.assertEqual(motion.node_role("teela-brain"), "brain")
        self.assertEqual(motion.node_role("teela-body"), "body")
        self.assertEqual(motion.node_role("teela-jetson"), "jetson")

    def test_balance_mode(self) -> None:
        self.assertEqual(motion.balance_mode("wave"), "static")
        self.assertEqual(motion.balance_mode("pose"), "static")
        self.assertEqual(motion.balance_mode("walk"), "dynamic")
        self.assertEqual(motion.balance_mode("estop_on"), "safety")
        self.assertEqual(motion.balance_mode("joint", "pick that up from the floor"), "dynamic")

    def test_needs_vision(self) -> None:
        self.assertTrue(motion.needs_vision("point at the cup"))
        self.assertTrue(motion.needs_vision("look at the person and wave"))
        self.assertFalse(motion.needs_vision("wave"))
        self.assertFalse(motion.needs_vision("look left"))

    def test_dispatch_skips_without_cluster(self) -> None:
        out = motion.dispatch(None, {"cmd": "wave"})
        self.assertEqual(out["mode"], "static")
        self.assertFalse(out["hardware"])
        self.assertTrue((out["jetson"] or {}).get("skipped"))

    def test_probe_mesh_does_not_dispatch_motion(self) -> None:
        out = motion.probe_mesh(None)
        self.assertEqual(out["mode"], "probe")
        self.assertFalse(out["hardware"])
        self.assertTrue((out["jetson"] or {}).get("skipped"))
        self.assertTrue((out["wbc"] or {}).get("skipped"))
        self.assertIn("MiniOS twin only", out.get("reason") or "")

    def test_walk_stays_sim_until_wbc(self) -> None:
        with patch.dict("os.environ", {"HERMES_DESK_WBC": ""}, clear=False):
            out = motion.dispatch(None, {"cmd": "walk"})
        self.assertEqual(out["mode"], "dynamic")
        self.assertFalse(out["hardware"])
        self.assertIn("wbc not loaded", out.get("reason") or "")

    def test_brain_rejects_wbc(self) -> None:
        hit = motion.handle_wbc("teela-brain", {"cmd": "walk"})
        self.assertFalse(hit["accepted"])
        self.assertEqual(hit["backend"], "wrong-node")

    def test_jetson_rejects_walk(self) -> None:
        hit = motion.handle_execute("teela-jetson", {"cmd": "walk"})
        self.assertFalse(hit["accepted"])

    def test_body_wbc_stub(self) -> None:
        hit = motion.handle_wbc("teela-body", {"cmd": "walk"})
        self.assertFalse(hit["accepted"])
        self.assertEqual(hit["backend"], "none")

    def test_walk_left_is_dynamic(self) -> None:
        self.assertEqual(motion.balance_mode("walk_left"), "dynamic")
        out = motion.dispatch(None, {"cmd": "walk_left"})
        self.assertEqual(out["mode"], "dynamic")

    def test_apply_walk_left_starts_walk(self) -> None:
        st = robot_sim.default_state()
        out = robot_sim.apply(st, {"cmd": "walk_left"})
        self.assertNotIn("Unknown robot command", str(out.get("error") or ""))
        self.assertEqual(out.get("motion") or st.get("motion"), "walking")
        self.assertEqual(out.get("walk_direction") or st.get("walk_direction"), "left")

    def test_qwen_tool_names_map_onto_grok_mcp(self) -> None:
        allowed = [
            "bot_desktop__robot_pose",
            "bot_desktop__robot_joint",
            "bot_desktop__robot_status",
            "bot_desktop__desktop_state",
        ]
        self.assertEqual(
            d.canonicalize_tool_name("mcp__bot_desktop__robot_pose", allowed),
            "bot_desktop__robot_pose",
        )
        self.assertEqual(d.canonicalize_tool_name("robot_pose", allowed), "bot_desktop__robot_pose")
        self.assertEqual(d.canonicalize_tool_name("robot_status", allowed), "bot_desktop__robot_status")
        self.assertEqual(d.canonicalize_tool_name("desktop_state", allowed), "bot_desktop__desktop_state")
        self.assertEqual(
            d.canonicalize_tool_name("bot_desktop__robot_pose", allowed),
            "bot_desktop__robot_pose",
        )
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "mcp__bot_desktop__robot_pose",
                                        "arguments": '{"pose":"wave"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.rewrite_llm_response_body(body, "application/json", allowed))
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "bot_desktop__robot_pose")

    def test_chat_log_wave_names_map_even_when_allowed_is_mcp(self) -> None:
        """Exact names from the failed MiniOS 'Can you wave' turn."""
        self.assertEqual(
            d.canonicalize_tool_name("mcp__bot_desktop__robot_pose", ["mcp__bot_desktop__robot_pose"]),
            "bot_desktop__robot_pose",
        )
        self.assertEqual(d.canonicalize_tool_name("mcp__bot_desktop__robot_pose", None), "bot_desktop__robot_pose")
        self.assertEqual(d.canonicalize_tool_name("robot_pose", None), "bot_desktop__robot_pose")
        self.assertEqual(d.canonicalize_tool_name("desktop_state", None), "bot_desktop__desktop_state")
        self.assertEqual(d.canonicalize_tool_name("robot_status", None), "bot_desktop__robot_status")
        self.assertEqual(
            d.canonicalize_tool_name("mcp__bot_desktop__search_tool", ["search_tool"]),
            "search_tool",
        )
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "mcp__bot_desktop__robot_pose",
                                        "arguments": '{"pose":"wave"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.rewrite_llm_response_body(body, "application/json", None))
        self.assertEqual(
            out["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "bot_desktop__robot_pose",
        )
        sse = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"mcp__bot_desktop__robot_pose"}}]}}]}\n'
            b"data: [DONE]\n"
        )
        sse_out = d.rewrite_llm_response_body(sse, "text/event-stream", None).decode()
        self.assertIn("bot_desktop__robot_pose", sse_out)
        self.assertNotIn("mcp__bot_desktop__robot_pose", sse_out)

    def test_use_tool_wrapper_unwraps_to_robot_pose(self) -> None:
        allowed = ["bot_desktop__robot_pose", "bot_desktop__robot_motion"]
        name, args = d.unwrap_misdirected_tool_call(
            "use_tool",
            {
                "name": "search_tool",
                "args": json.dumps({"server": "bot_desktop", "tool": "robot_pose", "pose": "wave"}),
            },
            allowed,
        )
        self.assertEqual(name, "bot_desktop__robot_pose")
        self.assertEqual(args.get("pose"), "wave")
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "use_tool",
                                        "arguments": json.dumps(
                                            {
                                                "tool_name": "search_tool",
                                                "args": {
                                                    "server": "bot_desktop",
                                                    "tool": "robot_motion",
                                                },
                                            }
                                        ),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.rewrite_llm_response_body(body, "application/json", allowed))
        call = out["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        self.assertNotEqual(call["name"], "use_tool")

    def test_pose_json_content_becomes_tool_call(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"pose":"wave"}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()
        out = json.loads(d.rewrite_llm_response_body(body, "application/json", None))
        msg = out["choices"][0]["message"]
        call = msg["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_pose")
        self.assertEqual(json.loads(call["arguments"]), {"pose": "wave"})
        self.assertFalse(msg.get("content"))
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")

    def test_sse_pose_json_content_becomes_tool_call(self) -> None:
        sse = (
            b'data: {"id":"cmpl-1","model":"qwen38","choices":[{"delta":{"content":"{\\"pose\\":\\"wave\\"}"}}]}\n'
            b"data: [DONE]\n"
        )
        out = d.rewrite_llm_response_body(sse, "text/event-stream", None).decode()
        self.assertIn("bot_desktop__robot_pose", out)
        self.assertIn("tool_calls", out)
        # content-only JSON must not remain the assistant text
        chunks = []
        for line in out.splitlines():
            if line.startswith("data:") and "[DONE]" not in line:
                payload = line[5:].strip()
                if payload:
                    chunks.append(json.loads(payload))
        names = []
        for obj in chunks:
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    names.append((call.get("function") or {}).get("name"))
        self.assertIn("bot_desktop__robot_pose", names)

    def test_motor_followup_does_not_call_pose_again(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {"role": "user", "content": "can you wave"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_pose",
                                    "arguments": '{"pose":"wave"}',
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "bot_desktop__robot_pose",
                        "content": '{"ok":true,"pose":"wave"}',
                    },
                ],
                "tools": [{"type": "function", "function": {"name": "bot_desktop__robot_pose"}}],
            }
        )
        self.assertTrue(d.payload_already_moved(out) or d.payload_already_moved(
            {
                "messages": [
                    {"role": "user", "content": "can you wave"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "bot_desktop__robot_pose", "arguments": '{"pose":"wave"}'}}
                        ],
                    },
                ]
            }
        ))
        self.assertNotIn("tools", out)
        self.assertIsNone(out.get("tool_choice"))
        user = out["messages"][0]["content"]
        self.assertIn("already moved", user.lower())
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("already moved; speaking so the turn can end", src)
        self.assertIn("ACP {err}; retrying prompt", src)
        self.assertNotIn("Wave = bot_desktop__robot_pose", user)

    def test_speech_completion_streams_as_sse(self) -> None:
        raw = d.fallback_moved_speech(
            {
                "messages": [
                    {"role": "user", "content": "Can you wave"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_pose",
                                    "arguments": '{"pose":"wave"}',
                                }
                            }
                        ],
                    },
                ]
            }
        )
        sse = d.openai_completion_to_sse(raw).decode()
        self.assertIn("data: ", sse)
        self.assertIn("I'm waving.", sse)
        self.assertIn('"finish_reason":"stop"', sse)
        self.assertIn("[DONE]", sse)
        self.assertNotIn("tool_calls", sse)

    def test_already_moved_json_becomes_speech_not_tool(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": '{"pose":"wave"}'}}]}
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(body, "application/json", None, promote_json=False)
        )
        msg = out["choices"][0]["message"]
        self.assertNotIn("tool_calls", msg)
        self.assertIn("waving", (msg.get("content") or "").lower())

    def test_intent_strips_motor_prefix_so_stop_still_infers(self) -> None:
        wrapped = d._MOTOR_FIRST_TOOL + "\n\nstop waving"
        self.assertEqual(d.user_intent_text(wrapped), "stop waving")
        out = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38", "messages": [{"role": "user", "content": "stop waving"}]}
        )
        raw = d.fallback_motor_completion(out)
        self.assertIsNotNone(raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        self.assertEqual(json.loads(call["arguments"])["cmd"], "stop")

    def test_stop_waving_fallback_emits_robot_motion(self) -> None:
        raw = d.fallback_motor_completion(
            {"messages": [{"role": "user", "content": "stop waving"}]}
        )
        self.assertIsNotNone(raw)
        obj = json.loads(raw)
        call = obj["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        self.assertEqual(json.loads(call["arguments"])["cmd"], "stop")
        moved = d.fallback_motor_completion(
            {
                "messages": [
                    {"role": "user", "content": "Can you wave"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_pose",
                                    "arguments": '{"pose":"wave"}',
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "bot_desktop__robot_pose",
                        "content": '{"ok":true,"pose":"wave"}',
                    },
                ]
            }
        )
        speech = json.loads(moved)
        self.assertEqual(speech["choices"][0]["finish_reason"], "stop")
        self.assertIn("waving", speech["choices"][0]["message"]["content"].lower())
        self.assertNotIn("tool_calls", speech["choices"][0]["message"])

    def test_you_can_stop_forces_robot_motion(self) -> None:
        self.assertTrue(
            d.local_llm_motor_turn({"messages": [{"role": "user", "content": "you can stop"}]})
        )
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "you can stop"}],
                "tools": [
                    {"type": "function", "function": {"name": "use_tool"}},
                    {"type": "function", "function": {"name": "run_terminal_command"}},
                ],
            }
        )
        names = [t["function"]["name"] for t in out.get("tools") or []]
        self.assertIn("bot_desktop__robot_motion", names)
        self.assertNotIn("use_tool", names)
        self.assertEqual(out.get("tool_choice"), "required")

    def test_empty_tool_name_is_dropped(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "", "arguments": "{}{}{}{}"}},
                            ]
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.rewrite_llm_response_body(body, "application/json", None))
        msg = out["choices"][0]["message"]
        self.assertFalse(msg.get("tool_calls"))

    def test_wave_payload_requires_robot_tool(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "Can you wave at me"}],
                "tools": [{"type": "function", "function": {"name": "use_tool"}}],
            }
        )
        names = [t["function"]["name"] for t in out["tools"]]
        self.assertEqual(out.get("tool_choice"), "required")
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertNotIn("use_tool", names)
        self.assertIn("never call use_tool", out["messages"][0]["content"].lower())

    def test_payload_tools_drop_mcp_prefix(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "wave"}],
                "tools": [
                    {"type": "function", "function": {"name": "mcp__bot_desktop__robot_pose"}},
                    {"type": "function", "function": {"name": "search_tool"}},
                ],
            }
        )
        names = [t["function"]["name"] for t in out["tools"]]
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertNotIn("search_tool", names)
        self.assertNotIn("mcp__bot_desktop__robot_pose", names)
        self.assertEqual(out.get("tool_choice"), "required")

    def test_see_then_move_keeps_observe_tool(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "search_tool"}},
            {"type": "function", "function": {"name": "bot_desktop__desktop_observe"}},
            {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
        ]
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38-hybrid",
                "messages": [{"role": "user", "content": "point at the cup"}],
                "tools": tools,
            }
        )
        names = [t["function"]["name"] for t in out["tools"]]
        self.assertIn("bot_desktop__desktop_observe", names)
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertNotIn("search_tool", names)
        self.assertIn("visual motor request", out["messages"][0]["content"])

    def test_motor_followup_is_first_person(self) -> None:
        text = d.motor_followup_prompt(
            "raise your right hand",
            {"cmd": "joint", "joints": {"right_shoulder": 145}},
            {"ok": True, "pose": "custom", "joints": {"right_shoulder": 145, "right_elbow": 20}},
        )
        self.assertIn("right arm is raised", text)
        self.assertIn("Talk like a person in the room", text)
        self.assertNotIn("Do not volunteer your pose", text)
        self.assertNotIn("right_shoulder", text)
        self.assertNotIn("145", text)
        self.assertIn("[[minios-body-applied]]", text)
        self.assertIn("raise your right hand", text)
        self.assertIn("Present:", text)
        self.assertIn("Past:", text)
        self.assertIn("Future:", text)

    def test_fast_chat_sees_history_not_work_tasks(self) -> None:
        self.assertFalse(d.wants_full_agent("hi"))
        src = Path(d.__file__).read_text(encoding="utf-8")
        loop = src.split("def _run_prompt_loop", 1)[1].split("def do_PUT", 1)[0]
        self.assertIn("run_teela_executive_turn", loop)
        self.assertNotIn("teela_turn_lane", loop)
        self.assertNotIn("resolve_motor_command(", loop)
        self.assertNotIn("chat_acting(", loop)
        self.assertTrue(d.wants_full_agent("can you bow"))
        self.assertTrue(d.wants_full_agent("wave"))
        self.assertTrue(d.wants_full_agent("what are you doing"))
        self.assertTrue(d.wants_full_agent("How do you feel? Anything that can be fixed or updated?"))
        self.assertTrue(d.robot_sim.looks_like_body_query("How do you feel?"))
        self.assertTrue(
            d.robot_sim.looks_like_body_query("How do you feel? Anything that can be fixed or updated?")
        )
        self.assertTrue(d.local_llm_body_query_turn(
            {"messages": [{"role": "user", "content": "How do you feel?"}]}
        ))
        query_out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "what are you doing"}],
                "tools": [
                    {"type": "function", "function": {"name": "search_tool"}},
                    {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
                ],
            }
        )
        self.assertNotIn("tools", query_out)
        self.assertIsNone(query_out.get("tool_choice"))
        self.assertIn("do not call tools", query_out["messages"][0]["content"].lower())
        self.assertFalse(d.local_llm_motor_turn(query_out))
        self.assertTrue(d.wants_full_agent("write a python script"))
        self.assertTrue(d.wants_full_agent("debug this traceback"))
        self.assertTrue(d.wants_full_agent("can you check with Body Bot and make sure you two are in sync"))
        self.assertFalse(d.wants_full_agent("your wave looks too high"))
        self.assertTrue(d.looks_like_body_teach("your wave looks too high"))
        self.assertTrue(d.looks_like_body_teach("remember how that wave should look"))
        self.assertTrue(d.wants_full_agent("mimic this"))
        self.assertTrue(d.looks_like_body_teach_acp("update BODY.md"))
        self.assertTrue(motion.needs_vision("mimic this"))

        class _Bot:
            messages = [
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "Hey."},
                {"role": "user", "text": "bow"},
            ]
            robot_state = {"pose": "bow", "motion": "idle", "joints": {}}

        msgs = d.build_fast_chat_messages(_Bot(), "bow")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("[[minios-body-sense]]", msgs[0]["content"])
        self.assertIn("I feel right now", msgs[0]["content"])
        self.assertIn("proprioception", msgs[0]["content"].lower())
        self.assertIn("do not describe your pose", msgs[0]["content"].lower())
        self.assertIn("BODY.md", msgs[0]["content"])
        self.assertIn("Stay in the conversation", msgs[0]["content"])
        self.assertIn("bowing", msgs[0]["content"].lower())
        wave_bot = _Bot()
        wave_bot.robot_state = {
            "pose": "wave",
            "motion": "waving",
            "joints": dict(d.robot_sim.POSES["wave"]),
        }
        sense = d.proprioception_block(wave_bot.robot_state)
        self.assertIn("neither hand is raised overhead", sense)
        self.assertIn("in front of my chest", sense)
        self.assertEqual(msgs[1], {"role": "user", "content": "hi"})
        self.assertEqual(msgs[2], {"role": "assistant", "content": "Hey."})
        self.assertEqual(msgs[-1], {"role": "user", "content": "bow"})
        self.assertNotIn("private body now", msgs[-1]["content"])
        did = d.build_fast_chat_messages(_Bot(), "now forward", just_did="The arm on your left is reaching forward.")
        self.assertIn("already moved this turn", did[0]["content"])
        self.assertIn("reaching forward", did[0]["content"])
        self.assertIn("Recent conversation", d._motor_nl_user("now forward", {"pose": "custom", "joints": {"right_shoulder": 145}}, "raise your left arm", "User: raise your left arm\nYou: Okay."))
        self.assertIn("ordinary words", d._motor_nl_user("now forward", {"pose": "home", "joints": {}}, None, None))
        conv = d.format_conversation(_Bot())
        self.assertIn("User: hi", conv)
        self.assertIn("You: Hey.", conv)
        self.assertNotIn("User: bow", conv)
        self.assertTrue(d.robot_sim.looks_like_body_query("what are you doing"))
        self.assertTrue(d.robot_sim.looks_like_body_query("are you sitting"))
        self.assertFalse(d.robot_sim.looks_like_body_query("can you sit"))

    def test_proprioception_merges_into_single_system_turn(self) -> None:
        class _Bot:
            robot_state = {
                "pose": "wave",
                "motion": "waving",
                "joints": dict(d.robot_sim.POSES["wave"]),
            }
            _prompt_queue = None
            _prompt_lock = None

        payload = {
            "model": "qwen38",
            "messages": [
                {"role": "system", "content": "You are Teela.\n\n[[minios-body-sense]]\nI feel right now: stale standing."},
                {"role": "user", "content": "hi\n\n[[minios-body-live]] old dump"},
                {"role": "assistant", "content": "Hey."},
                {"role": "user", "content": "what are you doing"},
            ],
        }
        out = d.inject_live_body(payload, _Bot())
        roles = [m["role"] for m in out["messages"]]
        self.assertEqual(roles.count("system"), 1)
        self.assertEqual(out["messages"][0]["role"], "system")
        sys = out["messages"][0]["content"]
        self.assertIn("[[minios-body-sense]]", sys)
        self.assertIn("I feel right now", sys)
        self.assertIn("waving", sys.lower())
        self.assertNotIn("stale standing", sys)
        self.assertEqual(sys.count("[[minios-body-sense]]"), 1)
        self.assertEqual(out["messages"][1]["content"], "hi")
        self.assertEqual(out["messages"][-1]["content"], "what are you doing")

    def test_live_body_context_does_not_retrigger_motor(self) -> None:
        wrapped = d.with_live_body("hello", {"pose": "bow", "joints": {"upper_back_pitch": 26, "left_hip": 42}})
        self.assertIn("[[minios-body-live]]", wrapped)
        self.assertEqual(d.user_intent_text(wrapped), "hello")
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": wrapped}],
            "tools": [
                {"type": "function", "function": {"name": "search_tool"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
            ],
        }
        out = d.rewrite_local_llm_chat_payload(payload)
        names = [t["function"]["name"] for t in out.get("tools") or []]
        self.assertIn("search_tool", names)
        self.assertFalse(d.local_llm_motor_turn(payload))

    def test_unapplied_motor_keeps_robot_tools(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "wave"}],
                "tools": [
                    {"type": "function", "function": {"name": "search_tool"}},
                    {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
                ],
            }
        )
        names = [t["function"]["name"] for t in out["tools"]]
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertIn("bot_desktop__robot_joint", names)
        self.assertIn("bot_desktop__robot_motion", names)
        self.assertNotIn("search_tool", names)
        self.assertEqual(out.get("tool_choice"), "required")

    def test_applied_motor_omits_tools_and_keeps_user_words(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {
                        "role": "user",
                        "content": d.motor_followup_prompt(
                            "raise your right hand and say hi",
                            {"cmd": "joint"},
                            {"pose": "custom", "joints": {"right_shoulder": 145}},
                        ),
                    }
                ],
                "tools": [
                    {"type": "function", "function": {"name": "search_tool"}},
                    {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
                ],
            }
        )
        self.assertNotIn("tools", out)
        self.assertIn("raise your right hand and say hi", out["messages"][0]["content"])
        self.assertNotIn("FIRST tool call", out["messages"][0]["content"])

    def test_compound_stop_and_walk_right_is_a_plan_tool(self) -> None:
        intent = "can you stop and walk right"
        cmd = robot_sim.infer_command(intent)
        self.assertEqual(cmd["cmd"], "plan")
        self.assertEqual(cmd["steps"][0]["cmd"], "stop")
        self.assertEqual(cmd["steps"][1]["cmd"], "walk")
        self.assertEqual(cmd["steps"][1]["direction"], "right")
        hit = d.motor_cmd_to_openai_tool(cmd)
        self.assertIsNotNone(hit)
        name, args = hit
        self.assertEqual(name, "bot_desktop__robot_motion")
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(len(args["steps"]), 2)
        self.assertEqual(args["steps"][0]["cmd"], "stop")
        self.assertEqual(args["steps"][1]["direction"], "right")
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": intent}],
                "tools": [{"type": "function", "function": {"name": "use_tool"}}],
            }
        )
        self.assertEqual(out.get("tool_choice"), "required")
        raw = d.fallback_motor_completion(
            {"messages": [{"role": "user", "content": intent}]}
        )
        self.assertIsNotNone(raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        args = json.loads(call["arguments"])
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(args["steps"][1]["direction"], "right")

    def test_wave_then_bow_is_a_plan_tool(self) -> None:
        cmd = robot_sim.infer_command("wave and bow")
        self.assertEqual(cmd["cmd"], "plan")
        name, args = d.motor_cmd_to_openai_tool(cmd)
        self.assertEqual(name, "bot_desktop__robot_motion")
        self.assertEqual([s.get("pose") for s in args["steps"]], ["wave", "bow"])

    def test_dumped_tool_array_promotes_then_expands_to_plan(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '[{"name":"bot_desktop__robot_motion","parameters":{"cmd":"stop"}}]',
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()
        promoted = json.loads(d.rewrite_llm_response_body(body, "application/json", None))
        call = promoted["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        self.assertEqual(json.loads(call["arguments"])["cmd"], "stop")
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "can you stop and walk right"}],
        }
        promoted_bytes = json.dumps(promoted).encode()
        self.assertIsNone(d.prefer_inferred_plan_completion(promoted_bytes, payload, "application/json"))
        empty = json.dumps({"choices": [{"message": {"role": "assistant", "content": ""}}]}).encode()
        filled = d.prefer_inferred_plan_completion(empty, payload, "application/json")
        self.assertIsNotNone(filled)
        plan_call = json.loads(filled)["choices"][0]["message"]["tool_calls"][0]["function"]
        args = json.loads(plan_call["arguments"])
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(args["steps"][1]["direction"], "right")

    def test_walk_speech_uses_direction_not_walk_cycle(self) -> None:
        text = d.speech_after_motor(
            {
                "messages": [
                    {"role": "user", "content": "can you walk left"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_motion",
                                    "arguments": '{"cmd":"walk","direction":"left"}',
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "bot_desktop__robot_motion",
                        "content": '{"ok":true,"pose":"walk-cycle","motion":"walking","walk_direction":"left"}',
                    },
                ]
            }
        )
        self.assertEqual(text, "I'm walking left.")
        plan_text = d.speech_after_motor(
            {
                "messages": [
                    {"role": "user", "content": "can you stop and walk right"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_motion",
                                    "arguments": json.dumps(
                                        {
                                            "cmd": "plan",
                                            "why": "I'll stop, then walk right.",
                                            "steps": [
                                                {"cmd": "stop"},
                                                {"cmd": "walk", "direction": "right"},
                                            ],
                                        }
                                    ),
                                }
                            }
                        ],
                    },
                ]
            }
        )
        self.assertIn("walking right", plan_text.lower())

    def test_put_leg_down_replaces_stale_raise_plan(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                '[{"name":"bot_desktop__robot_motion",'
                                '"parameters":{"cmd":"plan","steps":['
                                '{"cmd":"stop"},{"cmd":"pose","pose":"right_leg_raise"}]}}]'
                            ),
                        }
                    }
                ]
            }
        ).encode()
        payload = {"messages": [{"role": "user", "content": "ok, put your leg down"}]}
        rewritten = d.rewrite_llm_response_body(body, "application/json", None)
        self.assertTrue(d.response_has_robot_tool_call(rewritten))
        self.assertIsNone(d.prefer_inferred_plan_completion(rewritten, payload, "application/json"))
        empty = json.dumps({"choices": [{"message": {"role": "assistant", "content": ""}}]}).encode()
        filled = d.prefer_inferred_plan_completion(empty, payload, "application/json")
        self.assertIsNotNone(filled)
        call = json.loads(filled)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_pose")
        self.assertEqual(json.loads(call["arguments"])["pose"], "home")
        self.assertEqual(d.speech_after_motor(payload), "I've put my leg down.")

    def test_raise_leg_speech_follows_the_request(self) -> None:
        self.assertEqual(
            d.speech_after_motor({"messages": [{"role": "user", "content": "raise your leg"}]}),
            "I'm raising my leg.",
        )
        self.assertIn(
            "raise",
            d.speech_after_motor(
                {"messages": [{"role": "user", "content": "stop and then raise your leg"}]}
            ).lower(),
        )
        wrapped = d._MOTOR_FIRST_TOOL + "\n\nstop and wave\n\n[[minios-body-live]] leftover"
        self.assertEqual(d.user_intent_text(wrapped), "stop and wave")
        raw = d.fallback_motor_completion(
            {"messages": [{"role": "user", "content": "stop and wave"}]}
        )
        args = json.loads(json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(args["steps"][1].get("pose"), "wave")

    def test_empty_completion_may_use_fallback_but_model_tools_are_kept(self) -> None:
        raw = d.fallback_motor_completion(
            {"messages": [{"role": "user", "content": "can you stop and raise your leg"}]}
        )
        self.assertIsNotNone(raw)
        args = json.loads(json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(args["steps"][1].get("pose"), "right_leg_raise")
        self.assertIsNone(
            d.local_llm_direct_completion(
                {"messages": [{"role": "user", "content": "How do you feel?"}]}
            )
        )

    def test_followup_speech_ignores_prior_plan_why(self) -> None:
        text = d.speech_after_motor(
            {
                "messages": [
                    {"role": "user", "content": "can you stop and walk right"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_motion",
                                    "arguments": json.dumps(
                                        {
                                            "cmd": "plan",
                                            "why": "I'll stop, then walk right.",
                                            "steps": [
                                                {"cmd": "stop"},
                                                {"cmd": "walk", "direction": "right"},
                                            ],
                                        }
                                    ),
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "bot_desktop__robot_motion",
                        "content": json.dumps(
                            {
                                "ok": True,
                                "pose": "walk-cycle",
                                "plan": {"why": "I'll stop, then walk right."},
                            }
                        ),
                    },
                    {"role": "user", "content": "wave at me"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "bot_desktop__robot_pose",
                                    "arguments": '{"pose":"wave"}',
                                }
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "bot_desktop__robot_pose",
                        "content": '{"ok":true,"pose":"wave","motion":"waving"}',
                    },
                ]
            }
        )
        self.assertEqual(text, "I'm waving.")
        self.assertNotIn("walk right", text.lower())

    def test_stop_infers_through_instruction_wrapper(self) -> None:
        wrapped = d._MOTOR_FIRST_TOOL + "\n\n" + d._MOTOR_FIRST_TOOL + "\n\nstop"
        self.assertEqual(d.user_intent_text(wrapped), "stop")
        tagged = "<user_query>stop</user_query>\nDo not claim you already moved."
        self.assertEqual(d.user_intent_text(tagged), "stop")
        raw = d.fallback_motor_completion(
            {"messages": [{"role": "user", "content": wrapped}]}
        )
        self.assertIsNotNone(raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(json.loads(call["arguments"])["cmd"], "stop")
        down = d.fallback_engine_down_completion(
            {"messages": [{"role": "user", "content": "stop"}]}
        )
        call = json.loads(down)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(json.loads(call["arguments"])["cmd"], "stop")
        empty = json.loads(d.fallback_engine_down_completion({"messages": [{"role": "user", "content": "hi"}]}))
        self.assertTrue((empty["choices"][0]["message"].get("content") or "").strip())
        self.assertNotIn("tool_calls", empty["choices"][0]["message"])
        feel = json.loads(
            d.fallback_engine_down_completion(
                {"messages": [{"role": "user", "content": "How do you feel? Anything that can be fixed or updated?"}]}
            )
        )
        spoken = feel["choices"][0]["message"].get("content") or ""
        self.assertTrue(spoken.strip())
        self.assertNotIn("tool_calls", feel["choices"][0]["message"])

    def test_prose_mentioning_tools_is_not_a_robot_call(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Sure — watching my right arm come up now. "
                                "I will call bot_desktop__robot_pose after tool_calls return."
                            ),
                        }
                    }
                ]
            }
        ).encode()
        self.assertFalse(d.response_has_robot_tool_call(body))

    def test_empty_robot_pose_is_not_a_usable_call(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Sure — watching my right arm come up now.",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "bot_desktop__robot_pose",
                                        "arguments": "{}",
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        self.assertFalse(d.response_has_robot_tool_call(body))
        self.assertFalse(
            d.payload_already_moved(
                {
                    "messages": [
                        {"role": "user", "content": "Can you wave"},
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {"function": {"name": "bot_desktop__robot_pose", "arguments": "{}"}}
                            ],
                        },
                        {
                            "role": "tool",
                            "name": "bot_desktop__robot_pose",
                            "content": '{"ok":false,"error":"Unknown pose \'\'","pose":"home","seq":0}',
                        },
                    ]
                }
            )
        )

    def test_wave_prose_becomes_pose_after_the_model_processes_chat(self) -> None:
        payload = {"messages": [{"role": "user", "content": "Can you wave"}]}
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Sure — watching my right arm come up now.",
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.ensure_usable_motor_completion(body, payload, "application/json"))
        msg = out["choices"][0]["message"]
        self.assertFalse(msg.get("content"))
        call = msg["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_pose")
        self.assertEqual(json.loads(call["arguments"])["pose"], "wave")

    def test_empty_pose_tool_is_filled_from_user_intent(self) -> None:
        payload = {"messages": [{"role": "user", "content": "Can you wave"}]}
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Sure — watching my right arm come up now.",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "bot_desktop__robot_pose",
                                        "arguments": "{}",
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(d.ensure_usable_motor_completion(body, payload, "application/json"))
        msg = out["choices"][0]["message"]
        self.assertFalse(msg.get("content"))
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"])["pose"], "wave")

    def test_qwen_xml_pose_promotes_to_tool_call(self) -> None:
        xml = (
            "<tool_call>\n"
            "<function=bot_desktop__robot_pose>\n"
            "<parameter=pose>wave</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )
        call = d.promote_text_to_tool_call(xml, None)
        self.assertIsNotNone(call)
        self.assertEqual(call["function"]["name"], "bot_desktop__robot_pose")
        self.assertEqual(json.loads(call["function"]["arguments"])["pose"], "wave")

    def test_failed_empty_pose_retries_motor_not_false_speech(self) -> None:
        payload = {
            "messages": [
                {"role": "user", "content": "Can you wave"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "bot_desktop__robot_pose", "arguments": "{}"}}
                    ],
                },
                {
                    "role": "tool",
                    "name": "bot_desktop__robot_pose",
                    "content": '{"ok":false,"error":"Unknown pose \'\'","pose":"home","seq":0}',
                },
            ]
        }
        self.assertTrue(d.robot_tool_failed_this_turn(payload))
        self.assertFalse(d.payload_already_moved(payload))
        raw = d.fallback_motor_completion(payload)
        self.assertIsNotNone(raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(json.loads(call["arguments"])["pose"], "wave")

    def test_empty_pose_action_fills_from_processed_chat(self) -> None:
        class _Bot:
            _motor_user = "Can you wave"
            robot_state = robot_sim.default_state()
            messages = [{"role": "user", "text": "Can you wave"}]

        filled = d.fill_robot_action_from_intent(_Bot(), {"action": "robot", "cmd": "pose"})
        self.assertEqual(filled["pose"], "wave")
        live = d.fill_robot_action_from_intent(
            _Bot(), {"action": "robot", "cmd": "joints", "joints": {"neck_pan": 0}}
        )
        self.assertNotIn("pose", live)
        self.assertEqual(live["cmd"], "joints")
        st = robot_sim.default_state()
        out = robot_sim.apply(st, {"cmd": "pose", "pose": filled["pose"]})
        self.assertTrue(out.get("ok"))
        self.assertEqual(st.get("pose") or out.get("pose"), "wave")
        self.assertGreater(int(st.get("seq") or out.get("seq") or 0), 0)

    def test_clear_motor_skips_llama_prefill(self) -> None:
        raw = d.local_llm_direct_completion(
            {"messages": [{"role": "user", "content": "can you stop and walk left"}]}
        )
        self.assertIsNotNone(raw)
        call = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "bot_desktop__robot_motion")
        args = json.loads(call["arguments"])
        self.assertEqual(args["cmd"], "plan")
        self.assertEqual(args["steps"][0].get("cmd"), "stop")
        self.assertEqual(args["steps"][1].get("direction"), "left")
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("motor direct after intent", src)
        self.assertIn("skip llama prefill", src)

    def test_prefill_budget_trims_grok_acp_dump(self) -> None:
        huge = "You are a Hermes coding agent.\n" + ("tool dump line\n" * 4000)
        huge += "You have a body. Call bot_desktop__robot_pose to wave.\n"
        payload = {
            "model": "qwen38",
            "messages": [
                {"role": "system", "content": huge},
                {"role": "user", "content": "hello there"},
            ],
        }
        self.assertGreater(d.estimate_local_prompt_tokens(payload), 8000)
        out = d.trim_local_llm_payload(payload, max_prompt=1536)
        self.assertLessEqual(d.estimate_local_prompt_tokens(out), 1600)
        self.assertIn("You have a body", out["messages"][0]["content"])
        motor = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38", "messages": [{"role": "user", "content": "wave"}]}
        )
        self.assertLessEqual(motor.get("max_tokens") or 999, d.LOCAL_LLM_MOTOR_MAX_TOKENS)
        self.assertLessEqual(d.local_prefill_budget(motor), d.LOCAL_LLM_MOTOR_PREFILL)

    def test_tiny_grok_max_tokens_is_raised(self) -> None:
        out = d.apply_local_token_budget(
            {
                "model": "qwen38",
                "max_tokens": 56,
                "messages": [{"role": "user", "content": "hello there"}],
            }
        )
        self.assertGreaterEqual(int(out["max_tokens"]), d.LOCAL_LLM_MIN_COMPLETION)
        motor = d.apply_local_token_budget(
            {
                "model": "qwen38",
                "max_tokens": 56,
                "messages": [{"role": "user", "content": "Can you wave"}],
            }
        )
        self.assertGreaterEqual(int(motor["max_tokens"]), d.LOCAL_LLM_MIN_COMPLETION)
        self.assertLessEqual(int(motor["max_tokens"]), d.LOCAL_LLM_MOTOR_MAX_TOKENS)

    def test_length_finish_is_rewritten_so_grok_does_not_error(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "I'm here."},
                    }
                ]
            }
        ).encode()
        self.assertTrue(d.completion_truncated_by_max_tokens(body))
        out = json.loads(d.rewrite_length_finish_to_stop(body))
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        sse = (
            b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":"length"}]}\n'
            b"data: [DONE]\n"
        )
        sse_out = d.rewrite_length_finish_to_stop(sse).decode()
        self.assertIn('"finish_reason":"stop"', sse_out)
        self.assertNotIn('"finish_reason":"length"', sse_out)
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("finish_reason=length rewritten to stop", src)
        self.assertIn("truncated motor reply; using inferred tool", src)

    def test_greeting_cannot_claim_a_wave(self) -> None:
        waved = {
            "pose": "wave",
            "motion": "waving",
            "joints": dict(robot_sim.POSES["wave"]),
        }
        self.assertEqual(
            d.ground_chat_speech(
                "Hey there! I'm waving hello with my right hand right now.",
                "Hi",
                waved,
            ),
            "Hey — I'm here.",
        )
        self.assertIn(
            "waving",
            d.ground_chat_speech("I'm waving.", "Can you wave", waved, acted=True).lower(),
        )
        sense = d.proprioception_block(waved, greet=True)
        self.assertNotIn("I feel right now: I'm waving", sense)
        self.assertIn("greeting is not a body action", sense.lower())
        posture = d._as_current_posture(robot_sim.describe_body(waved))
        self.assertIn("held in a wave", posture.lower())
        self.assertNotIn("I'm waving", posture)

    def test_already_waving_followup_speaks_instead_of_llama(self) -> None:
        class _Bot:
            robot_state = {
                "pose": "wave",
                "motion": "waving",
                "joints": dict(robot_sim.POSES["wave"]),
            }

        payload = {"messages": [{"role": "user", "content": "Can you wave"}]}
        # Wave always restarts through the executive loop; leftover pose is not skip-llama.
        self.assertFalse(d.motor_already_satisfied(payload, _Bot()))
        wrapped = {
            "messages": [
                {"role": "user", "content": "Can you wave"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "bot_desktop__robot_pose", "arguments": '{"pose":"wave"}'}}
                    ],
                },
                {
                    "role": "tool",
                    "name": "bot_desktop__robot_pose",
                    "content": json.dumps(
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps({"ok": True, "pose": "wave", "motion": "waving"}),
                                }
                            ]
                        }
                    ),
                },
            ]
        }
        self.assertTrue(d.payload_already_moved(wrapped))
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("motor follow-up skip llama; speaking", src)


if __name__ == "__main__":
    unittest.main()
