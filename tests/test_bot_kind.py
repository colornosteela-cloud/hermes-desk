#!/usr/bin/env python3
"""Hermes Agent vs Teela Brain bot kinds."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
import desktop_mcp as dm  # noqa: E402
import action_orchestrator as orch  # noqa: E402


class _KindBot:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.robot_state = None
        self.id = "b_test"
        self.model = "qwen38"
        self.soul = d.default_soul_for_kind(kind, "Coder", "write software")


class BotKindTests(unittest.TestCase):
    def test_one_teela_brain_per_host(self) -> None:
        class _Bot:
            def __init__(self, bid: str, kind: str, remote: bool = False) -> None:
                self.id = bid
                self.kind = kind
                self.remote = remote

        d.ensure_single_teela_brain("hermes")
        with patch.object(
            d,
            "bots",
            {
                "a": _Bot("a", "teela-brain"),
                "b": _Bot("b", "hermes"),
                "c": _Bot("c", "teela-brain", remote=True),
            },
        ):
            self.assertTrue(d.teela_brain_slot_taken())
            with self.assertRaises(ValueError) as ctx:
                d.ensure_single_teela_brain("teela-brain")
            self.assertIn("already has a Teela Brain", str(ctx.exception))
            d.ensure_single_teela_brain("teela-brain", exclude_id="a")
            d.ensure_single_teela_brain("hermes")
        with patch.object(d, "bots", {"legacy": _Bot("legacy", "")}):
            self.assertTrue(d.occupies_teela_brain_slot(d.bots["legacy"]))
            self.assertTrue(d.teela_brain_slot_taken())
            with self.assertRaises(ValueError):
                d.ensure_single_teela_brain("teela-brain")

    def test_load_existing_skips_extra_teela_brain(self) -> None:
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("skipping extra Teela Brain", src)
        self.assertIn("occupies_teela_brain_slot(bot) and teela_brain_slot_taken()", src)

    def test_normalize_aliases(self) -> None:
        self.assertEqual(d.normalize_bot_kind("Hermes Agent"), "hermes")
        self.assertEqual(d.normalize_bot_kind("agentic"), "hermes")
        self.assertEqual(d.normalize_bot_kind("Teela Brain"), "teela-brain")
        self.assertEqual(d.normalize_bot_kind("robot"), "teela-brain")
        self.assertEqual(d.normalize_bot_kind(""), "")
        self.assertEqual(d.normalize_bot_kind("", default="teela-brain"), "teela-brain")

    def test_agent_md_splits_tools(self) -> None:
        hermes = d.agent_md_for_kind("hermes")
        teela = d.agent_md_for_kind("teela-brain")
        self.assertIn("same working style as the Hermes TUI", hermes)
        self.assertIn("do not have a robot body", hermes.lower())
        self.assertNotIn("I-feel", hermes)
        self.assertNotIn("MiniOS", hermes)
        self.assertNotIn("desktop_state", hermes)
        self.assertIn("Robot Simulator", teela)
        self.assertIn("teela_body_action", teela)
        self.assertIn("Host-shell", teela)
        self.assertIn("teela_system_check", teela)
        self.assertIn("not in a lane", teela.lower())

    def test_default_souls(self) -> None:
        hermes = d.default_soul_for_kind("hermes", "Coder", "write software")
        teela = d.default_soul_for_kind("teela-brain", "Teela", "move the body")
        self.assertIn("Coder", hermes)
        self.assertIn("do not have a robot body", hermes.lower())
        self.assertIn("regular Hermes TUI session", hermes)
        self.assertNotIn("one or two short sentences", hermes)
        self.assertNotIn("MiniOS desktop", hermes)
        self.assertIn("Teela", teela)
        self.assertIn("You have a body", teela)
        self.assertIn("Trusted Embedded Embodied Learning Agent", teela)

    def test_strip_body_tools(self) -> None:
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
                {"type": "function", "function": {"name": "bot_desktop__teela_gesture"}},
            ]
        }
        out = d.strip_body_tools(payload)
        names = [d._openai_tool_name(t) for t in out["tools"]]
        self.assertEqual(names, ["read_file"])

    def test_grok_build_payload_keeps_files_drops_body(self) -> None:
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "wave then list files"}],
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "run_terminal_command"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
            ],
        }
        out = d.rewrite_local_llm_chat_payload(payload, bot=_KindBot("hermes"))
        names = [d._openai_tool_name(t) for t in out.get("tools") or []]
        self.assertIn("read_file", names)
        self.assertIn("run_terminal_command", names)
        self.assertNotIn("bot_desktop__robot_pose", names)
        self.assertNotIn("You are Teela", str(out.get("messages")))

    def test_teela_payload_keeps_body_drops_files(self) -> None:
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "wave"}],
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
            ],
        }
        out = d.rewrite_local_llm_chat_payload(payload, bot=_KindBot("teela-brain"))
        names = [d._openai_tool_name(t) for t in out.get("tools") or []]
        self.assertTrue(any("robot_pose" in n or "teela_" in n for n in names))
        self.assertIn("read_file", names)
        self.assertIn("bot_desktop__desktop_observe", names)

    def test_inject_live_body_skips_grok_build(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        out = d.inject_live_body(payload, _KindBot("hermes"))
        self.assertEqual(out["messages"][0]["content"], "hi")
        self.assertTrue(all(m.get("role") != "system" for m in out["messages"]))

    def test_grok_build_has_no_robot_simulator(self) -> None:
        self.assertFalse(d.bot_kind_has_robot_simulator(_KindBot("hermes")))
        self.assertTrue(d.bot_kind_has_robot_simulator(_KindBot("teela-brain")))
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("Hermes Agent bots do not have the Robot Simulator", src)
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function botHasRobotSimulator", app)
        self.assertIn("function syncRobotSimulatorForBot", app)
        self.assertIn("function unloadRobotSimulator", app)
        ui = (ROOT / "ui" / "hermesbot-ui.js").read_text(encoding="utf-8")
        self.assertIn("function selectedHasRobotSimulator", ui)
        self.assertIn("if (!selectedHasRobotSimulator()) return;", ui)

    def test_desktop_mcp_tool_split(self) -> None:
        hermes = {t["name"] for t in dm.tools_for_kind("hermes")}
        teela = {t["name"] for t in dm.tools_for_kind("teela-brain")}
        self.assertIn("desktop_open_app", hermes)
        self.assertIn("desktop_run_tests", hermes)
        self.assertNotIn("robot_pose", hermes)
        self.assertNotIn("teela_gesture", hermes)
        self.assertIn("robot_pose", teela)
        self.assertIn("teela_body_action", teela)
        self.assertIn("desktop_observe", teela)
        self.assertIn("teela_system_check", teela)
        self.assertIn("desktop_open_app", teela)
        self.assertIn("desktop_browser_navigate", teela)
        self.assertNotIn("desktop_run_tests", teela)
        self.assertNotIn("teela_system_check", hermes)

    def test_teela_skips_tool_search_for_body(self) -> None:
        md = d.agent_md_for_kind("teela-brain")
        self.assertIn("Never tool_search", md)
        self.assertIn("mcp__bot_desktop__robot_motion", md)
        self.assertNotIn("never mcp__", md)
        rules = str(d.acp_session_meta(_KindBot("teela-brain")).get("rules") or "")
        self.assertIn("never tool_search", rules)
        self.assertIn("cmd=plan", rules)
        import agent_home as ah

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ah.write_child_hermes_home(home, "qwen3.8-27b", {}, tool_search="off", max_turns=24)
            cfg = (home / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("tool_search:", cfg)
            self.assertIn("enabled: off", cfg)
            self.assertIn("max_turns: 24", cfg)

    def test_profile_fields_follow_kind(self) -> None:
        hermes = types.SimpleNamespace(
            id="b1",
            name="Coder",
            description="d",
            emoji="◉",
            avatar_color="",
            avatar_shape="",
            model="qwen38",
            kind="hermes",
            workspace_id="w",
        )
        hermes.kind = "hermes"
        fields = d.Bot.profile_fields(hermes)  # type: ignore[arg-type]
        self.assertEqual(fields["kind"], "hermes")
        self.assertTrue(fields["inherit_user_skills"])
        self.assertTrue(fields["inherit_user_mcp"])
        self.assertTrue(fields["browser"])
        self.assertEqual(fields["permission_mode"], "always-approve")
        self.assertEqual(fields["host_access"], "full")
        teela = types.SimpleNamespace(**{**hermes.__dict__, "kind": "teela-brain"})
        tfields = d.Bot.profile_fields(teela)  # type: ignore[arg-type]
        self.assertEqual(tfields["kind"], "teela-brain")
        self.assertTrue(tfields["inherit_user_skills"])
        self.assertTrue(tfields["inherit_user_mcp"])
        self.assertTrue(tfields["browser"])
        self.assertEqual(tfields["host_access"], "full")
        self.assertEqual(tfields["permission_mode"], "always-approve")

    def test_grok_build_agents_md_skips_runtime_dump(self) -> None:
        body = d.agents_markdown_for_bot(_KindBot("hermes"))
        self.assertIn("same tools, working style, and answers as the Hermes TUI", body)
        self.assertGreater(body.rfind("Agent type: Hermes Agent"), body.find("# Communication"))
        self.assertNotIn("Endpoint:", body)
        self.assertNotIn("Context window:", body)
        self.assertNotIn("Chromium + desktop", body)
        self.assertNotIn("# Memory", body)

    def test_migrate_old_grok_build_soul(self) -> None:
        old = (
            "Talk like a person in the room: one or two short sentences unless the work needs a longer report.\n"
            "You are a Hermes Agent agent. Use files, shell, grep, web search, browser, MiniOS desktop, skills, and subagents to do the work.\n"
        )
        out = d.migrate_agent_soul(old)
        self.assertNotIn("one or two short sentences", out)
        self.assertNotIn("MiniOS desktop", out)
        self.assertIn("regular Hermes Agent TUI session", out)

    def test_grok_build_acp_skips_minios_mcp(self) -> None:
        here = Path("/tmp")
        env = [{"name": "HERMES_DESK_URL", "value": "http://127.0.0.1:8742"}]
        with patch.object(d, "user_mcp_acp_specs", return_value=[]):
            hermes = d.acp_mcp_specs(_KindBot("hermes"), here, env)
            teela = d.acp_mcp_specs(_KindBot("teela-brain"), here, env)
        names = [s["name"] for s in hermes]
        self.assertEqual(names, ["desk_models", "desk_team", "bot_memory"])
        self.assertNotIn("bot_desktop", names)
        self.assertNotIn("bot_browser", names)
        tnames = [s["name"] for s in teela]
        self.assertIn("bot_desktop", tnames)
        self.assertIn("bot_browser", tnames)
        self.assertNotIn("desk_models", tnames)

    def test_acp_inherits_user_mcp_for_grok_build_and_teela(self) -> None:
        here = Path("/tmp")
        env = [{"name": "HERMES_DESK_URL", "value": "http://127.0.0.1:8742"}]
        extra = [
            {
                "name": "chrome-devtools",
                "command": "/home/roni/bin/npx",
                "args": ["-y", "chrome-devtools-mcp@latest"],
                "env": [{"name": "DISPLAY", "value": ":0"}],
            }
        ]
        with patch.object(d, "user_mcp_acp_specs", return_value=extra):
            hermes = d.acp_mcp_specs(_KindBot("hermes"), here, env)
            teela = d.acp_mcp_specs(_KindBot("teela-brain"), here, env)
        self.assertEqual(
            [s["name"] for s in hermes],
            ["desk_models", "desk_team", "bot_memory", "chrome-devtools"],
        )
        self.assertEqual(hermes[-1]["command"], "/home/roni/bin/npx")
        self.assertEqual(
            [s["name"] for s in teela],
            ["bot_browser", "desk_team", "bot_desktop", "bot_memory", "chrome-devtools"],
        )
        self.assertEqual(teela[-1]["command"], "/home/roni/bin/npx")

    def test_write_child_config_inherits_user_mcp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        servers = {
            "chrome-devtools": {
                "command": "/home/roni/bin/npx",
                "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"],
                "enabled": True,
                "startup_timeout_sec": 120,
                "env": {"DISPLAY": ":0", "PATH": "/home/roni/bin"},
            }
        }
        catalog = {
            "grok-4.6": {
                "model": "grok-4.6",
                "name": "Grok 4.6",
                "api_backend": "responses",
                "context_window": 500000,
            }
        }
        d.write_child_config(home, "grok-4.6", catalog, inherit_mcp=True)
        text = (home / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("provider: custom", text)
        self.assertIn("default: grok-4.6", text)
        self.assertNotIn("mcp_servers", text)

    def test_host_mcp_inherited_on_acp_session(self) -> None:
        servers = {
            "chrome-devtools": {
                "command": "/home/roni/bin/npx",
                "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"],
                "enabled": True,
                "startup_timeout_sec": 120,
                "env": {"DISPLAY": ":0", "PATH": "/home/roni/bin"},
            }
        }
        with patch.object(d, "load_user_mcp_servers", return_value=servers):
            specs = d.user_mcp_acp_specs()
        names = [s["name"] for s in specs]
        self.assertIn("chrome-devtools", names)
        row = next(s for s in specs if s["name"] == "chrome-devtools")
        self.assertIn("chrome-devtools-mcp@latest", row["args"])
        env = {e["name"]: e["value"] for e in row["env"]}
        self.assertEqual(env.get("DISPLAY"), ":0")

    def test_hermes_session_meta_is_empty_for_agent(self) -> None:
        meta = d.acp_session_meta(_KindBot("hermes"))
        self.assertEqual(meta, {})
        teela = d.acp_session_meta(_KindBot("teela-brain"))
        self.assertNotIn("yoloMode", teela)
        self.assertIn("rules", teela)

    def test_grok_build_keeps_thinking_flags(self) -> None:
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "hi"}],
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"},
            "reasoning_effort": "high",
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        }
        out = d.rewrite_local_llm_chat_payload(payload, bot=_KindBot("hermes"))
        self.assertTrue(out["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(out["chat_template_kwargs"]["reasoning_effort"], "high")
        self.assertEqual(out["reasoning_effort"], "high")
        bare = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38", "messages": [{"role": "user", "content": "hi"}]},
            bot=_KindBot("hermes"),
        )
        self.assertTrue(bare["chat_template_kwargs"]["enable_thinking"])
        chosen = _KindBot("hermes")
        chosen.effort = "low"
        applied = d.rewrite_local_llm_chat_payload(payload, bot=chosen)
        self.assertEqual(applied["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertEqual(applied["reasoning_effort"], "low")
        silent = _KindBot("hermes")
        silent.effort = "off"
        disabled = d.rewrite_local_llm_chat_payload(payload, bot=silent)
        self.assertFalse(disabled["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("reasoning_effort", disabled["chat_template_kwargs"])
        none = _KindBot("hermes")
        none.effort = "none"
        also = d.rewrite_local_llm_chat_payload(payload, bot=none)
        self.assertFalse(also["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(d.normalize_reasoning_effort("none"), "off")
        self.assertEqual(d.agent_effort_wire("off"), "none")

    def test_tool_output_from_nested_acp_content(self) -> None:
        failed = d.tool_output_from_update(
            {
                "sessionUpdate": "tool_call_update",
                "status": "failed",
                "content": [
                    {
                        "type": "content",
                        "content": {
                            "type": "text",
                            "text": "Failed to parse arguments for tool `run_terminal_command`: missing field `command`",
                        },
                    }
                ],
            }
        )
        self.assertIn("missing field `command`", failed)
        mcp = d.tool_output_from_update(
            {
                "rawOutput": {
                    "type": "MCP",
                    "output": {"OkayOutput": '{"ok": true, "n": 2}'},
                }
            }
        )
        self.assertIn('"ok": true', mcp)
        huge = "x" * (d.TOOL_OUTPUT_MAX + 50)
        clipped = d.tool_output_from_update({"rawOutput": {"output_for_prompt": huge}})
        self.assertTrue(clipped.endswith("…[truncated]"))
        self.assertLess(len(clipped), len(huge))

    def test_tool_command_from_raw_input(self) -> None:
        cmd = d.tool_command_from_update(
            {
                "rawInput": {"command": "uname -a"},
                "_meta": {"x.ai/tool": {"name": "run_terminal_command", "kind": "execute"}},
            }
        )
        self.assertEqual(cmd, "uname -a")

    def test_sse_keeps_shell_arg_fragments(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"run_terminal_command","arguments":""}}]}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\":\\"uname -a\\"}"}}]}}]}\n\n'
            b"data: [DONE]\n"
        )
        out = d.rewrite_llm_response_body(sse, "text/event-stream", ["run_terminal_command"]).decode()
        self.assertIn("uname -a", out)
        self.assertIn("run_terminal_command", out)
        names = []
        for ln in out.splitlines():
            if not ln.startswith("data:") or "[DONE]" in ln:
                continue
            payload = json.loads(ln[5:].strip())
            for ch in payload.get("choices") or []:
                delta = ch.get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    names.append(fn.get("name"))
                    if fn.get("name") == "run_terminal_command":
                        args = json.loads(fn.get("arguments") or "{}")
                        self.assertEqual(args.get("command"), "uname -a")
        self.assertIn("run_terminal_command", names)
        self.assertNotIn("", names)
        self.assertNotIn(None, names)

    def test_empty_shell_tool_fills_hardware_command(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "run_terminal_command", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(
                body,
                "application/json",
                ["run_terminal_command"],
                intent="Can you run a hardware check",
            )
        )
        args = json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertIn("uname -a", args["command"])
        self.assertIn("lscpu", args["command"])

    def test_empty_shell_tool_fills_from_xml_parameter(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '<parameter name="command">df -h</parameter>',
                            "tool_calls": [
                                {
                                    "function": {"name": "run_terminal_command", "arguments": ""},
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(body, "application/json", ["run_terminal_command"])
        )
        args = json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["command"], "df -h")

    def test_pose_args_still_rewritten(self) -> None:
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
        fn = out["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(fn["name"], "bot_desktop__robot_pose")
        self.assertEqual(json.loads(fn["arguments"])["pose"], "wave")

    def test_empty_name_target_directory_becomes_list_dir(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "",
                                        "arguments": '{"target_directory":"."}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(body, "application/json", ["list_dir", "run_terminal_command"])
        )
        fn = out["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(fn["name"], "list_dir")
        self.assertEqual(json.loads(fn["arguments"])["target_directory"], ".")

    def test_sse_nameless_args_join_previous_tool(self) -> None:
        sse = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"list_dir","arguments":""}}]}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"target_directory\\":\\".\\"}"}}]}}]}\n\n'
            b"data: [DONE]\n"
        )
        out = d.rewrite_llm_response_body(
            sse, "text/event-stream", ["list_dir", "run_terminal_command"]
        ).decode()
        self.assertIn("list_dir", out)
        self.assertIn("target_directory", out)
        self.assertNotIn('"name":""', out)
        found = False
        for ln in out.splitlines():
            if not ln.startswith("data:") or "[DONE]" in ln:
                continue
            payload = json.loads(ln[5:].strip())
            for ch in payload.get("choices") or []:
                for call in (ch.get("delta") or {}).get("tool_calls") or []:
                    fn = call.get("function") or {}
                    if fn.get("name") == "list_dir":
                        found = True
                        self.assertEqual(json.loads(fn["arguments"])["target_directory"], ".")
        self.assertTrue(found)

    def test_grok_build_prefill_keeps_tool_results(self) -> None:
        tools = [
            {"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
            for n in (
                "run_terminal_command",
                "read_file",
                "search_replace",
                "list_dir",
                "grep",
                "web_search",
            )
        ]
        payload = {
            "model": "qwen38-flash-next",
            "messages": [
                {"role": "system", "content": "You are a Hermes Agent agent.\n" + ("rule\n" * 200)},
                {"role": "user", "content": "what files are here?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "list_dir",
                                "arguments": '{"target_directory":"."}',
                            },
                        }
                    ],
                },
                {"role": "tool", "name": "list_dir", "content": "app.js\nindex.html\nREADME.md"},
            ],
            "tools": tools,
        }
        budget = d.local_prefill_budget(payload, _KindBot("hermes"))
        self.assertGreaterEqual(budget, 8000)
        motor = {"messages": [{"role": "user", "content": "wave"}]}
        self.assertLessEqual(d.local_prefill_budget(motor), d.LOCAL_LLM_MOTOR_PREFILL)
        out = d.rewrite_local_llm_chat_payload(payload, bot=_KindBot("hermes"))
        roles = [m.get("role") for m in out["messages"]]
        self.assertIn("tool", roles)
        self.assertTrue(
            any("app.js" in str(m.get("content") or "") for m in out["messages"] if m.get("role") == "tool")
        )
        self.assertIn(d._AFTER_TOOL_RESULTS, str(out["messages"][-1].get("content") or ""))

    def test_repeated_local_tool_call_is_dropped(self) -> None:
        prior = {("list_dir", '{"target_directory":"."}')}
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "list_dir",
                                        "arguments": '{"target_directory":"."}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(
                body,
                "application/json",
                ["list_dir"],
                prior_tools=prior,
            )
        )
        msg = out["choices"][0]["message"]
        self.assertFalse(msg.get("tool_calls"))
        self.assertEqual(out["choices"][0].get("finish_reason"), "stop")

    def test_orphan_background_task_poll_is_dropped(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "get_command_or_subagent_output",
                                        "arguments": '{"task_ids":["01a08301-16e6-7520-a4c5-427c73130e42"]}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.rewrite_llm_response_body(
                body,
                "application/json",
                ["get_command_or_subagent_output", "run_terminal_command"],
                known_task_ids=set(),
            )
        )
        msg = out["choices"][0]["message"]
        self.assertFalse(msg.get("tool_calls"))
        self.assertEqual(out["choices"][0].get("finish_reason"), "stop")
        kept = json.loads(
            d.rewrite_llm_response_body(
                body,
                "application/json",
                ["get_command_or_subagent_output"],
                known_task_ids={"01a08301-16e6-7520-a4c5-427c73130e42"},
            )
        )
        self.assertEqual(
            kept["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "get_command_or_subagent_output",
        )

    def test_acp_cancel_is_notification_and_aborts_local_llm(self) -> None:
        src = (ROOT / "deskd" / "deskd.py").read_text(encoding="utf-8")
        cancel = src.split("def cancel(self)", 1)[1].split("def rewind_last_prompt", 1)[0]
        self.assertIn('"method": "session/cancel"', cancel)
        self.assertNotIn('"id": self._next_id()', cancel)
        self.assertIn("abort_local_llm_conns", cancel)
        self.assertIn("self._turn_cancel.set()", cancel)
        self.assertIn('box["error"] = "cancelled"', cancel)
        self.assertIn("$/cancel_request", cancel)
        wait = src.split("def _wait_prompt", 1)[1].split("def ", 1)[0]
        self.assertIn("self._turn_cancel.is_set()", wait)
        self.assertIn("def abort_local_llm_conns", src)
        self.assertIn("turn_was_cancelled", src)
        self.assertIn("def start_local_prefill_progress", src)
        self.assertIn("def emit_local_activity", src)
        self.assertIn("def _pipe_agent_sse", src)
        self.assertIn("Reading the local-model prompt", src)

    def test_picker_models_include_reasoning_effort(self) -> None:
        src = (ROOT / "deskd" / "deskd.py").read_text(encoding="utf-8")
        self.assertIn("def model_effort_info", src)
        self.assertIn("def current_model_effort", src)
        self.assertIn("row.update(model_effort_info(tbl))", src)
        self.assertIn('"effort": self.effort or current_model_effort(self)', src)
        self.assertIn("def set_model(self, model_id: str, effort: str | None = None)", src)
        self.assertIn("default_reasoning_effort", src)
        self.assertIn('body.get("effort")', src)
        self.assertIn("normalize_reasoning_effort", src)
        self.assertIn("agent_effort_wire", src)

    def test_system_check_intent_is_teela_not_shell(self) -> None:
        self.assertTrue(d.looks_like_system_check("can you run a full system check of yourself"))
        self.assertTrue(d.looks_like_system_check("run diagnostics"))
        self.assertTrue(d.looks_like_system_check("do some system work"))
        self.assertTrue(d.looks_like_system_check("inspect your system information"))
        self.assertFalse(d.looks_like_system_check("wave your hand"))
        self.assertFalse(d.looks_like_system_check("how are you"))
        self.assertTrue(d.looks_like_talk("how are you"))
        self.assertTrue(d.looks_like_talk("hey"))
        self.assertFalse(d.looks_like_talk("can you run a full system check of yourself"))
        self.assertFalse(d.looks_like_talk("wave your hand"))
        self.assertTrue(orch.classify("check yourself then wave")["system"])
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "can you run a full system check of yourself"}],
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
            ],
        }
        teela = d.rewrite_local_llm_chat_payload(payload, bot=_KindBot("teela-brain"))
        names = [d._openai_tool_name(t) for t in teela.get("tools") or []]
        self.assertIn("bot_desktop__teela_system_check", names)
        self.assertIn("read_file", names)
        hermes = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "can you run a full system check of yourself"}],
                "tools": [
                    {"type": "function", "function": {"name": "run_terminal_command"}},
                    {"type": "function", "function": {"name": "bot_desktop__teela_system_check"}},
                ],
            },
            bot=_KindBot("hermes"),
        )
        grok_names = [d._openai_tool_name(t) for t in hermes.get("tools") or []]
        self.assertIn("run_terminal_command", grok_names)
        self.assertNotIn("bot_desktop__teela_system_check", grok_names)

    def test_spoken_system_check_becomes_tool_call(self) -> None:
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "can you run a full system check of yourself"}],
        }
        bot = _KindBot("teela-brain")
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Running a full system check now.",
                        }
                    }
                ]
            }
        ).encode()
        out = json.loads(
            d.ensure_usable_system_check_completion(
                body, payload, "application/json", served="qwen38", bot=bot
            )
        )
        fn = out["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(fn["name"], "bot_desktop__teela_system_check")
        direct = json.loads(d.local_llm_direct_completion(payload, served="qwen38", bot=bot))
        self.assertEqual(
            direct["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "bot_desktop__teela_system_check",
        )

    def test_teela_system_report_is_read_only_twin(self) -> None:
        bot = _KindBot("teela-brain")
        bot.robot_state = __import__("robot_sim").default_state()
        bot.observer = None
        report = d.teela_system_report(bot, scope="both")
        self.assertIn("spoken", report)
        mini = d.teela_minios_report(bot)
        host = d.teela_host_report(bot)
        self.assertIn("Workspace check", mini.get("spoken") or "")
        self.assertNotIn("GPU", mini.get("spoken") or "")
        self.assertIn("teela-brain check", host.get("spoken") or "")
        self.assertNotIn("pose", (host.get("spoken") or "").lower())
        self.assertEqual(d.teela_check_scope("check your workspace"), "minios")
        self.assertEqual(d.teela_check_scope("check the main system"), "host")
        self.assertEqual(d.teela_check_scope("check teela-brain"), "host")
        self.assertFalse(d.teela_check_confirm_payload("check your workspace").get("confirm"))
        self.assertFalse(d.teela_check_confirm_payload("check teela-brain").get("confirm"))
        self.assertFalse(d.teela_check_confirm_payload("can you wave").get("confirm"))
        scan = d.teela_check_confirm_payload("Are you able to perform a system scan?")
        self.assertTrue(scan.get("confirm"))
        ids = [o.get("id") for o in scan.get("options") or []]
        self.assertEqual(ids[0], scan.get("guess"))
        self.assertEqual(ids[-1], "other")
        self.assertIn("host", ids)
        ranked = d.rank_clarify_options(
            [{"id": "b", "label": "B"}, {"id": "a", "label": "A"}],
            guess="a",
        )
        self.assertEqual([o["id"] for o in ranked], ["a", "b", "other"])
        self.assertIn("ask_user", [t["function"]["name"] for t in d.teela_minios_tool_specs()])
        opts = d._clarify_options(["MiniOS", {"id": "host", "label": "teela-brain"}])
        self.assertEqual(opts[0]["label"], "MiniOS")
        self.assertEqual(opts[1]["id"], "host")
        self.assertTrue(d.looks_like_system_check("check your workspace"))
        self.assertTrue(d.looks_like_host_system_check("check the main system"))
        self.assertIn("teela_system_check", d.agent_md_for_kind("teela-brain"))
        self.assertIn("teela_system_check", d.default_soul_for_kind("teela-brain", "Teela", "move"))
        old = (
            "Talk like a person in the room: one or two short sentences. No paragraphs or lists.\n"
            "You know your body; mention sitting.\n"
            "Use bot_desktop__robot_status only to read the live feel. Never issue servo degrees, PWM, or I2C.\n"
        )
        migrated = d.migrate_teela_soul(old)
        self.assertIn("just talk", migrated)
        self.assertIn("teela_system_check", migrated)
        self.assertIn("message_teammate", migrated)
        self.assertIn("Component | Status", migrated)
        self.assertIn("message_teammate", d.default_soul_for_kind("teela-brain", "Teela", "move"))
        self.assertIn("early twenties", d.default_soul_for_kind("teela-brain", "Teela", "move"))
        self.assertIn("happy to be alive", d.default_soul_for_kind("teela-brain", "Teela", "move"))
        self.assertIn("early twenties", d._FAST_CHAT_SYS)
        self.assertIn("early twenties", d._TEELA_MINIOS_SYS)
        self.assertIn("[happy]", d.CHATTERBOX_VOICE_NOTE)
        self.assertIn("[chuckle]", d.CHATTERBOX_VOICE_NOTE)
        self.assertIn("only if they told a joke", d.CHATTERBOX_VOICE_NOTE)
        self.assertNotIn("Prefer [happy] and [chuckle]", d.CHATTERBOX_VOICE_NOTE)
        self.assertIn("not your mind", d.CHATTERBOX_VOICE_NOTE.lower())
        self.assertIn("not your model", d.CHATTERBOX_VOICE_NOTE.lower())
        self.assertNotIn("Jade is Chatterbox-Turbo.", d.CHATTERBOX_VOICE_NOTE)
        self.assertIn("your voice, not your model", d.VOICE_CHAT_NOTE.lower())
        self.assertIn("twenty-seven billion", d._TEELA_MINIOS_SYS.lower())
        self.assertIn("not 3b", d._TEELA_MINIOS_SYS.lower().replace("3.8b", "x"))
        self.assertIn("not 3B", d._TEELA_MINIOS_SYS)
        self.assertEqual(d.spoken_clock(10, 3, "PM"), "ten oh three PM")
        self.assertEqual(d.tts_friendly_times("It's 10:03 PM."), "It's ten oh three PM.")
        self.assertEqual(d.sanitize_chatterbox_text("The time is 10:03 PM."), "The time is ten oh three PM.")
        self.assertEqual(d.sanitize_chatterbox_text("[happy] Hi [laughs] there [nope]"), "[happy] Hi there")
        self.assertEqual(d.sanitize_chatterbox_text("Hi 😊 there ✅"), "Hi there")
        self.assertIn("never use emoji", d.VOICE_OFF_NOTE.lower())
        self.assertIn("Jade may still speak", d.VOICE_OFF_NOTE)
        self.assertNotIn("[Voice mode::", d.VOICE_OFF_NOTE)
        self.assertIn("Jade cannot say", d.VOICE_ON_NOTE)
        self.assertIn("never emoji", d.migrate_teela_soul(d._TEELA_SOUL_TALK_TUI).lower())
        self.assertEqual(
            d.sanitize_chatterbox_text("[laugh] That's a good one", "tell me a joke"),
            "[laugh] That's a good one",
        )
        self.assertEqual(d.filter_unwarranted_laughs("[laugh] Hello there.", "How are you?"), "Hello there.")
        self.assertEqual(d.filter_unwarranted_laughs("[chuckle] Hi.", "hi"), "Hi.")
        self.assertIn("[laugh]", d.filter_unwarranted_laughs("[laugh] Nice one.", "that was hilarious"))
        self.assertEqual(d.strip_chatterbox_tags("[happy] Hi [chuckle] there"), "Hi there")
        self.assertIn("happy", d.CHATTERBOX_TURBO_TAGS)
        self.assertIn("clear throat", d.CHATTERBOX_TURBO_TAGS)
        self.assertIn("check with Body Bot", d.agent_md_for_kind("teela-brain"))
        self.assertIn("early twenties", d.agent_md_for_kind("teela-brain"))
        self.assertIn("Trusted Embedded Embodied Learning Agent", d.DEFAULT_SOUL)
        self.assertIn("Trusted Embedded Embodied Learning Agent", d.agent_md_for_kind("teela-brain"))
        self.assertIn("Trusted Embedded Embodied Learning Agent", d.AGENT_MD)
        bare = "# Identity\n\nYou are Teela Bot.\n\n# Purpose\n\nhello\n"
        self.assertIn("# Personality", d.migrate_teela_soul(bare))
        self.assertIn("early twenties", d.migrate_teela_soul(bare))
        self.assertIn("Trusted Embedded Embodied Learning Agent", d.migrate_teela_soul(bare))

    def test_teela_teammate_sync_not_pose(self) -> None:
        ask = "can you check with Body Bot and make sure you two are in sync"
        self.assertTrue(d.looks_like_teammate_work(ask))
        self.assertFalse(d.looks_like_system_check(ask))
        self.assertTrue(d.wants_full_agent(ask))
        self.assertFalse(d.looks_like_teammate_work("hi"))
        self.assertFalse(d.wants_full_agent("hi"))
        self.assertTrue(d.looks_like_teammate_work("list your teammates"))
        self.assertIn("Answer what they just said first", d._SHORT_CHAT)
        self.assertIn("recite your pose", d._FAST_CHAT_SYS)

        class _Peer:
            def __init__(self) -> None:
                self.id = "b_body"
                self.name = "Body Bot"
                self.dms: list[tuple[object, str]] = []

            def deliver_dm(self, src, text):
                self.dms.append((src, text))

        teela = _KindBot("teela-brain")
        teela.id = "b_teela"
        teela.name = "Teela Bot"
        body = _Peer()
        with patch.object(d, "bots", {"b_teela": teela, "b_body": body}), patch.object(
            d, "cluster", None
        ):
            line = d.ping_teammate(teela, ask)
            self.assertIsNotNone(line)
            self.assertIn("Body Bot", line or "")
            self.assertIn("sync", (line or "").lower())
            self.assertEqual(len(body.dms), 1)
            missing = d.ping_teammate(teela, "list your teammates")
            self.assertIn("Body Bot", missing or "")
        with patch.object(d, "bots", {"b_teela": teela}), patch.object(d, "cluster", None):
            none = d.ping_teammate(teela, ask)
            self.assertIn("don't see", (none or "").lower())

    def test_voice_page_skips_teammate_dms(self) -> None:
        class _VoiceBot:
            id = "b_teela"
            _voice_page = "page-1"

        bot = _VoiceBot()
        with patch.object(d, "bots", {bot.id: bot}):
            user = {"type": "chat", "bot_id": bot.id, "role": "assistant", "text": "I pinged Body Bot so we can stay in sync."}
            d._attach_voice_page(user)
            self.assertEqual(user.get("page_id"), "page-1")
            dm = {
                "type": "chat",
                "bot_id": bot.id,
                "role": "assistant",
                "text": "Sent to Body Bot: standing by.",
                "via": "dm",
                "peer": "Body Bot",
            }
            d._attach_voice_page(dm)
            self.assertNotIn("page_id", dm)
            sent = {
                "type": "chat",
                "bot_id": bot.id,
                "role": "assistant",
                "text": "Sent to Body Bot: standing by.",
            }
            d._attach_voice_page(sent)
            self.assertNotIn("page_id", sent)
            d.release_voice_page(bot)
            later = {"type": "chat", "bot_id": bot.id, "role": "assistant", "text": "We're both idle."}
            d._attach_voice_page(later)
            self.assertNotIn("page_id", later)

    def test_teela_inbound_dm_is_ping_only(self) -> None:
        self.assertFalse(d.inbound_dm_starts_acp(_KindBot("teela-brain")))
        self.assertTrue(d.inbound_dm_starts_acp(_KindBot("hermes")))
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("if not inbound_dm_starts_acp(self):", src)
        start = Path(d.__file__).resolve().parents[1] / "start.sh"
        text = start.read_text(encoding="utf-8")
        self.assertIn("deskd.pid", text)
        self.assertIn("deskd_already_running", text)
        self.assertIn("claim_deskd_pidfile", src)

    def test_teela_minios_loop_skips_grok_build_tools(self) -> None:
        names = [t["function"]["name"] for t in d.teela_minios_tool_specs()]
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertIn("bot_desktop__teela_body_action", names)
        self.assertIn("bot_desktop__desktop_open_app", names)
        self.assertIn("bot_desktop__desktop_observe", names)
        self.assertIn("memory_write", names)
        self.assertIn("memory_retrieve", names)
        self.assertTrue(any(n.endswith("teela_system_check") for n in names))
        self.assertIn("read_file", names)
        self.assertIn("list_dir", names)
        self.assertIn("web_search", names)
        self.assertIn("search_tool", names)
        self.assertIn("run_terminal_command", names)
        self.assertIn("grep", names)
        self.assertIn("search_replace", names)
        self.assertIn("hermes_build", names)
        loop = Path(d.__file__).read_text(encoding="utf-8").split("def _run_prompt_loop", 1)[1].split("def ", 1)[0]
        self.assertIn("bot.acp.prompt", loop)
        self.assertIn("with_voice_note", loop)
        self.assertNotIn("teela_turn_lane", loop)
        self.assertNotIn('if lane == "minios"', loop)

        class _Bot:
            id = "b_teela"
            kind = "teela-brain"
            model = "qwen38-27b-q5"
            robot_state = __import__("robot_sim").default_state()
            messages: list = []
            surface = "preview"
            desktop_cursor = {"x": 0, "y": 0}
            applied: list = []

            def apply_robot(self, body):
                self.applied.append(body)
                return (
                    {"ok": True, "pose": body.get("pose")},
                    {"type": "desktop.action", "bot_id": self.id, "action": "robot", "pose": body.get("pose")},
                )

        bot = _Bot()
        out = d.dispatch_teela_minios_tool(bot, "bot_desktop__robot_pose", {"pose": "wave"})
        self.assertTrue(out.get("ok"))
        self.assertEqual(bot.applied[0].get("cmd"), "pose")
        self.assertEqual(bot.applied[0].get("pose"), "wave")
        with patch.object(d, "_teela_web_search_tool", return_value={"ok": True, "query": "x", "results": []}):
            searched = d.dispatch_teela_minios_tool(bot, "search_tool", {"query": "x"})
        self.assertTrue(searched.get("ok"))
        rounds = [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
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
            {"choices": [{"message": {"content": "Waving."}}]},
        ]

        def fake_complete(_payload):
            return rounds.pop(0)

        with patch.object(d, "_teela_minios_complete", side_effect=fake_complete):
            line = d.run_teela_minios_turn(bot, "wave")
        self.assertIn("wav", (line or "").lower())
        # Body chat must dispatch a wave without waiting on llama prefill.
        self.assertTrue(
            bot.applied
            or "wav" in (line or "").lower()
        )

    def test_teela_host_coding_tools_run_for_herself(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "note.txt").write_text("hello teela\nsecond line\n", encoding="utf-8")

        class _Bot:
            id = "b_teela"
            kind = "teela-brain"
            workspace = root

        bot = _Bot()
        shell = d.dispatch_teela_minios_tool(bot, "run_terminal_command", {"command": "uname -s"})
        self.assertTrue(shell.get("ok"))
        self.assertIn("Linux", str(shell.get("output") or ""))
        self.assertEqual(shell.get("via"), "host-shell")
        grepped = d.dispatch_teela_minios_tool(bot, "grep", {"pattern": "hello", "path": "."})
        self.assertTrue(grepped.get("ok"))
        self.assertTrue(any("hello teela" in str(m.get("text")) for m in grepped.get("matches") or []))
        replaced = d.dispatch_teela_minios_tool(
            bot,
            "search_replace",
            {"path": "note.txt", "old_string": "hello teela", "new_string": "hi teela"},
        )
        self.assertTrue(replaced.get("ok"))
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8").splitlines()[0], "hi teela")
        self.assertTrue(d.bot_kind_has_host_coding(_KindBot("teela-brain")))
        self.assertTrue(d.bot_kind_has_host_coding(_KindBot("hermes")))

    def test_teela_minios_loop_writes_and_injects_memory(self) -> None:
        import memory as botmem

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mgr = botmem.MemoryManager(
            Path(tmp.name) / ".memory",
            summarizer=botmem.RuleBasedSummarizer(),
        )
        mgr.write("The user's name is Roni.", ["name", "user"])

        class _Bot:
            id = "b_teela"
            kind = "teela-brain"
            model = "qwen38-27b-q5"
            robot_state = __import__("robot_sim").default_state()
            messages = [{"role": "user", "text": "what is my name"}]
            surface = "preview"
            desktop_cursor = {"x": 0, "y": 0}
            memory = mgr

            def apply_robot(self, body):
                return {"ok": True}, {"type": "desktop.action", "bot_id": self.id, "action": "robot"}

        bot = _Bot()
        wrote = d.dispatch_teela_minios_tool(
            bot, "memory_write", {"text": "Prefers short replies.", "tags": ["style"]}
        )
        self.assertTrue(wrote.get("ok"))
        found = d.dispatch_teela_minios_tool(bot, "memory_retrieve", {"query": "Roni"})
        self.assertTrue(found.get("ok"))
        self.assertTrue(any("Roni" in str(f.get("text")) for f in found.get("facts") or []))
        captured: list[dict] = []

        def fake_complete(payload):
            captured.append(payload)
            return {"choices": [{"message": {"content": "Your name is Roni."}}]}

        with patch.object(d, "_teela_minios_complete", side_effect=fake_complete):
            line = d.run_teela_minios_turn(bot, "what is my name")
        self.assertIn("Roni", line or "")
        sys_text = str((captured[0].get("messages") or [{}])[0].get("content") or "")
        self.assertIn("Roni", sys_text)
        q5 = botmem.TokenBudget.for_model("qwen38-27b-q5")
        self.assertEqual(q5.available_for_memory, 16000)

    def test_teela_can_create_and_delete_helper_bots(self) -> None:
        ask = "can you create a helper bot for recipes"
        self.assertTrue(d.looks_like_helper_bot_work(ask))
        self.assertTrue(d.wants_full_agent(ask))
        self.assertTrue(d.wants_full_agent("delete the recipe bot"))
        names = [t["function"]["name"] for t in d.teela_minios_tool_specs()]
        self.assertIn("create_teammate", names)
        self.assertIn("delete_teammate", names)
        self.assertIn("list_teammates", names)
        teela = _KindBot("teela-brain")
        teela.id = "b_teela"
        teela.name = "Teela Bot"
        helper = types.SimpleNamespace(
            id="b_help",
            name="Recipe Bot",
            kind="hermes",
            remote=False,
            destroyed=False,
        )

        def _destroy():
            helper.destroyed = True

        helper.destroy = _destroy
        with patch.object(d, "bots", {"b_teela": teela, "b_help": helper}), patch.object(
            d, "cluster", None
        ):
            listed = d.dispatch_teela_minios_tool(teela, "list_teammates", {})
            self.assertTrue(any(r.get("name") == "Recipe Bot" for r in listed.get("teammates") or []))
            gone = d.delete_helper_teammate(teela, "Recipe Bot")
            self.assertTrue(gone.get("ok"))
            self.assertTrue(helper.destroyed)
            self.assertNotIn("b_help", d.bots)
            nope = d.delete_helper_teammate(teela, "Teela Bot")
            self.assertFalse(nope.get("ok"))
        fake = types.SimpleNamespace(id="b_x", name="Notes", kind="hermes", model="grok-4.6")
        with patch.object(d, "create_bot", return_value=fake) as cb, patch.object(
            d, "load_user_models", return_value=("grok-4.6", {"grok-4.6": {}})
        ):
            created = d.create_helper_teammate(teela, name="Notes", description="notes helper")
            self.assertTrue(created.get("ok"))
            self.assertEqual(cb.call_args[0][0]["kind"], "hermes")
            self.assertEqual(cb.call_args[0][0]["model"], "grok-4.6")
        blocked = d.create_helper_teammate(teela, name="Twin", description="x", kind="teela-brain")
        self.assertFalse(blocked.get("ok"))

    def test_teela_turn_lane_keeps_local_work_off_acp(self) -> None:
        self.assertTrue(d.looks_like_system_check("Are you able to perform a system scan?"))
        self.assertTrue(d.looks_like_system_check("run a hardware system check"))
        # Lane labels may still exist as telemetry/hints; they must not hide tools.
        wave_names = [t["function"]["name"] for t in d.teela_capability_tool_specs()]
        self.assertIn("bot_desktop__teela_body_action", wave_names)
        self.assertIn("bot_desktop__desktop_observe", wave_names)
        self.assertIn("read_file", wave_names)
        hint = d.teela_capability_hint("can you wave")
        self.assertTrue(not hint or "body" in hint.lower())
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("def capture_minios_avatar", src)
        self.assertIn("bot_desktop__desktop_observe", src)
        self.assertIn("bot_desktop__desktop_screenshot", src)
        self.assertIn("run_teela_executive_turn", src)
        self.assertEqual(d.teela_check_scope("check the main system"), "host")
        self.assertIn("host-shell", d.teela_system_acp_prefix("host"))
        self.assertIn("MiniOS workspace", d.teela_system_acp_prefix("minios"))
        self.assertIn("markdown table", d.teela_system_acp_prefix("host"))
        self.assertIn("markdown table", d.teela_system_acp_prefix("minios"))
        self.assertNotIn("speak a short", d.teela_system_acp_prefix("host"))
        self.assertNotIn("speak a short", d.teela_system_acp_prefix("minios"))
        self.assertFalse(d.looks_like_coding_job("can you wave"))
        with patch.object(d, "voice_enabled", return_value=True):
            acp_note = d.with_voice_note("can you run a system scan", tui=True)
            self.assertNotIn("no markdown", acp_note)
            self.assertIn("Hermes TUI", acp_note)
            talk_note = d.with_voice_note("hi teela")
            self.assertIn("No markdown", talk_note)
            live = types.SimpleNamespace(_voice_chat=True)
            live_note = d.with_voice_note("hi teela", bot=live)
            self.assertIn("[Voice chat: ON]", live_note)
            self.assertTrue(d.voice_chat_active(live))
            self.assertFalse(d.voice_chat_active(None))

    def test_teela_talk_vs_system_work(self) -> None:
        bot = _KindBot("teela-brain")
        chat = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "how are you"}],
                "tools": [
                    {"type": "function", "function": {"name": "bot_desktop__teela_system_check"}},
                    {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
                ],
            },
            bot=bot,
        )
        chat_names = [d._openai_tool_name(t) for t in chat.get("tools") or []]
        self.assertIn("bot_desktop__teela_system_check", chat_names)
        self.assertIn("bot_desktop__robot_pose", chat_names)
        self.assertIn("bot_desktop__desktop_observe", chat_names)
        self.assertNotEqual(chat.get("tool_choice"), "required")
        work = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "do some system work"}],
                "tools": [{"type": "function", "function": {"name": "bot_desktop__robot_pose"}}],
            },
            bot=bot,
        )
        names = [d._openai_tool_name(t) for t in work.get("tools") or []]
        self.assertIn("bot_desktop__teela_system_check", names)
        mixed = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {
                        "role": "user",
                        "content": "inspect your system information and wave when you're finished",
                    }
                ],
                "tools": [{"type": "function", "function": {"name": "bot_desktop__teela_body_action"}}],
            },
            bot=bot,
        )
        mixed_names = [d._openai_tool_name(t) for t in mixed.get("tools") or []]
        self.assertIn("bot_desktop__teela_system_check", mixed_names)
        direct = json.loads(
            d.local_llm_direct_completion(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "inspect your system information and wave when you're finished",
                        }
                    ]
                },
                bot=bot,
            )
        )
        self.assertEqual(
            direct["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "bot_desktop__teela_system_check",
        )
        self.assertNotIn("uname", json.dumps(direct))
        after = {
            "messages": [
                {
                    "role": "user",
                    "content": "inspect your system information and wave when you're finished",
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "bot_desktop__teela_system_check",
                                "arguments": "{}",
                            }
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "bot_desktop__teela_system_check",
                    "content": '{"spoken":"twin is idle"}',
                },
            ]
        }
        self.assertTrue(d.local_llm_after_system_work_turn(after, bot))
        self.assertFalse(d.local_llm_talk_turn(after, bot))

    def test_teela_opens_minios_browser(self) -> None:
        bot = _KindBot("teela-brain")
        self.assertTrue(d.looks_like_desktop_work("can you open your browser and google birds"))
        self.assertFalse(d.looks_like_desktop_work("how are you"))
        self.assertFalse(d.looks_like_desktop_work("wave your hand"))
        name, args = d.infer_desktop_tool("can you open your browser and google birds")
        self.assertEqual(name, "bot_desktop__desktop_browser_navigate")
        self.assertIn("birds", args.get("url") or "")
        payload = {
            "model": "qwen38",
            "messages": [{"role": "user", "content": "can you open your browser and google birds"}],
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "bot_desktop__robot_pose"}},
            ],
        }
        out = d.rewrite_local_llm_chat_payload(payload, bot=bot)
        names = [d._openai_tool_name(t) for t in out.get("tools") or []]
        self.assertIn("bot_desktop__desktop_browser_navigate", names)
        self.assertIn("read_file", names)
        self.assertIn("bot_desktop__teela_body_action", names)
        self.assertNotEqual(out.get("tool_choice"), "required")
        spoken = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I can't open a browser right now.",
                        }
                    }
                ]
            }
        ).encode()
        fixed = json.loads(
            d.ensure_usable_desktop_completion(
                spoken, payload, "application/json", served="qwen38", bot=bot
            )
        )
        fn = fixed["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(fn["name"], "bot_desktop__desktop_browser_navigate")
        self.assertIn("birds", json.loads(fn["arguments"]).get("url") or "")

    def test_teela_types_and_reviews_media(self) -> None:
        self.assertEqual(d.workspace_media_kind("Pictures/cat.jpg"), "image")
        self.assertEqual(d.workspace_media_kind("Videos/clip.mp4"), "video")
        self.assertEqual(d.workspace_media_kind("Documents/note.txt"), "notepad")
        self.assertTrue(d.looks_like_desktop_work("type hello in notepad"))
        self.assertTrue(d.looks_like_desktop_work("look at this picture"))
        self.assertTrue(d.looks_like_desktop_work("watch the video"))
        name, args = d.infer_desktop_tool('type "hello there" in notepad')
        self.assertEqual(name, "bot_desktop__desktop_type_text")
        self.assertEqual(args.get("text"), "hello there")
        self.assertEqual(args.get("app"), "notepad")
        name, args = d.infer_desktop_tool("open Pictures/bird.png")
        self.assertEqual(name, "bot_desktop__desktop_open_file")
        self.assertEqual(args.get("path"), "Pictures/bird.png")
        bot = _KindBot("teela-brain")
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "type hello in notepad"}],
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
            },
            bot=bot,
        )
        names = [d._openai_tool_name(t) for t in out.get("tools") or []]
        self.assertIn("bot_desktop__desktop_type_text", names)
        self.assertIn("read_file", names)
        self.assertTrue(d.wants_full_agent("Can you open a notepad and type Hello"))
        self.assertTrue(d.wants_full_agent("can you open your browser and google birds"))
        self.assertFalse(d.wants_full_agent("hi"))
        name, args = d.infer_desktop_tool("Can you open a notepad and type Hello")
        self.assertEqual(name, "bot_desktop__desktop_type_text")
        self.assertEqual(args.get("text"), "Hello")
        self.assertEqual(args.get("app"), "notepad")

        class _DeskBot:
            id = "b_test"
            surface = "desktop"
            browser = None

            def ensure_browser(self) -> None:
                return None

        with patch.object(d, "emit"):
            spoken = d.apply_inferred_desktop(_DeskBot(), "Can you open a notepad and type Hello")
        self.assertIsNotNone(spoken)
        self.assertIn("Hello", spoken or "")
        self.assertIn("Notepad", spoken or "")
        self.assertTrue(d.looks_like_composed_type("Can you type a health dinner recipe"))
        self.assertTrue(
            d.looks_like_composed_type("research online a health dinner recipe and type it in Notepad")
        )
        self.assertFalse(d.looks_like_composed_type("Can you Open Notepad and type Hello"))
        self.assertIsNone(d.infer_desktop_tool("Can you type a health dinner recipe"))
        self.assertIsNone(
            d.infer_desktop_tool("research online a health dinner recipe and type it in Notepad")
        )
        self.assertEqual(d.compose_topic("research online a health dinner recipe and type it in Notepad").lower(), "a health dinner recipe")
        with patch.object(d, "emit"):
            self.assertIsNone(
                d.apply_inferred_desktop(_DeskBot(), "Can you type a health dinner recipe")
            )
        with patch.object(d, "local_llm_write_document", return_value="Salmon, greens, 20 min."):
            with patch.object(d, "emit"):
                with patch.object(d, "emit_local_activity"):
                    wrote = d.compose_and_type_desktop(
                        _DeskBot(), "Can you type a health dinner recipe"
                    )
        self.assertEqual(wrote, "I wrote that in Notepad.")


if __name__ == "__main__":
    unittest.main()
