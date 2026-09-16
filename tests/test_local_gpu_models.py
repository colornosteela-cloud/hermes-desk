#!/usr/bin/env python3
"""Qwen and Muse share the Arc GPUs — only the live vLLM family is selectable."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402

TEELA_START = next(
    (
        p
        for p in (
            ROOT / "teela" / "start.sh.real",
            Path("/home/roni/teela/start.sh.real"),
        )
        if p.is_file()
    ),
    ROOT / "teela" / "start.sh.real",
)

CATALOG = {
    "qwen38-27b": {
        "model": "qwen38",
        "name": "Qwen 3.8 27B Local",
        "base_url": "http://127.0.0.1:8000/v1",
        "context_window": 262144,
    },
    "muse-glimmer": {
        "model": "muse-glimmer",
        "name": "Muse Glimmer 30B Local",
        "base_url": "http://127.0.0.1:8000/v1",
        "context_window": 65536,
    },
    "grok-4.6": {"model": "grok-4.6", "name": "Grok 4.6", "context_window": 500000},
}

PICKER = [
    {"id": "qwen38-27b", "name": "Qwen 3.8 27B Local", "context_window": 262144},
    {"id": "muse-glimmer", "name": "Muse Glimmer 30B Local", "context_window": 65536},
    {"id": "grok-4.6", "name": "Grok 4.6", "context_window": 500000},
]


class LocalGpuModelTests(unittest.TestCase):
    def setUp(self) -> None:
        d.reset_local_llm_probe()
        with d._LLM_JOB_LOCK:
            d._LLM_JOB.update({"action": None, "target": None, "family": None, "started": 0.0, "error": ""})

    def test_qwen_live_grays_muse(self) -> None:
        rows = d.annotate_model_availability(PICKER, CATALOG, live_ids=("qwen38",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-27b"]["available"])
        self.assertTrue(by_id["qwen38-27b"]["running"])
        self.assertFalse(by_id["qwen38-27b"]["startable"])
        self.assertFalse(by_id["muse-glimmer"]["available"])
        self.assertFalse(by_id["muse-glimmer"]["startable"])
        self.assertIn("Qwen", by_id["muse-glimmer"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])
        self.assertFalse(by_id["grok-4.6"]["local"])

    def test_muse_live_grays_qwen(self) -> None:
        rows = d.annotate_model_availability(PICKER, CATALOG, live_ids=("muse-glimmer",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["muse-glimmer"]["available"])
        self.assertFalse(by_id["qwen38-27b"]["available"])
        self.assertIn("Muse", by_id["qwen38-27b"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])

    def test_vllm_down_grays_both_local(self) -> None:
        rows = d.annotate_model_availability(PICKER, CATALOG, live_ids=())
        by_id = {r["id"]: r for r in rows}
        self.assertFalse(by_id["qwen38-27b"]["available"])
        self.assertTrue(by_id["qwen38-27b"]["startable"])
        self.assertFalse(by_id["muse-glimmer"]["available"])
        self.assertTrue(by_id["muse-glimmer"]["startable"])
        self.assertIn("not running", by_id["muse-glimmer"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])

    def test_start_target_maps_picker_ids(self) -> None:
        self.assertEqual(d.local_llm_start_target("muse-glimmer", CATALOG), "muse")
        self.assertEqual(d.local_llm_start_target("qwen38-27b", CATALOG), "qwen")
        self.assertEqual(d.local_llm_start_target("qwen38-hybrid", {"qwen38-hybrid": {"model": "qwen38-hybrid", "name": "Qwen3-VL-8B / 27B Local"}}), "hybrid")
        self.assertEqual(
            d.local_llm_start_target(
                "qwen3-vl-8b",
                {"qwen3-vl-8b": {"model": "qwen3-vl-8b", "name": "Qwen3-VL-8B Local"}},
            ),
            "qwen3-vl-8b",
        )
        self.assertEqual(
            d.local_llm_start_target(
                "llama-70b",
                {"llama-70b": {"model": "llama70", "base_url": "http://127.0.0.1:8002/v1", "weights": "Llama-70B"}},
            ),
            "serve",
        )

    def test_new_and_cloud_models_are_classified(self) -> None:
        llama = {
            "model": "llama70",
            "name": "Llama 70B Local",
            "base_url": "http://127.0.0.1:8002/v1",
            "weights": "Llama-70B",
        }
        hermes = {"model": "grok-4.6", "name": "Grok 4.6", "api_backend": "responses"}
        self.assertTrue(d.is_local_gpu_model("llama-70b", llama))
        self.assertFalse(d.is_local_gpu_model("grok-4.6", hermes))

        class _Local:
            model = "qwen38-27b"

        class _Cloud:
            model = "grok-4.6"

        catalog = {**CATALOG, "llama-70b": llama, "grok-4.6": hermes}
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", catalog)):
            self.assertTrue(d.uses_short_local_chat(_Local()))
            self.assertFalse(d.uses_short_local_chat(_Cloud()))
            self.assertTrue(d.uses_local_text_llm("llama-70b"))
            self.assertFalse(d.uses_local_text_llm("grok-4.6"))
        live = ("llama70",)
        live_map = {"llama70": "http://127.0.0.1:8002/v1"}
        with patch.object(d, "load_user_models", return_value=("llama-70b", {"llama-70b": llama})):
            url, served = d.pick_local_llm_route(
                {"model": "llama-70b", "messages": [{"role": "user", "content": "hi"}]},
                live_ids=live,
                requested_model="llama-70b",
                live_map=live_map,
            )
        self.assertEqual(served, "llama70")
        self.assertIn(":8002", url)

    def test_hybrid_live_grays_full_27b(self) -> None:
        catalog = {
            **CATALOG,
            "qwen38-hybrid": {
                "model": "qwen38-hybrid",
                "name": "Qwen3-VL-8B / 27B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        }
        picker = [
            {"id": "qwen38-hybrid", "name": "Qwen3-VL-8B / 27B Local"},
            *PICKER,
        ]
        rows = d.annotate_model_availability(picker, catalog, live_ids=("qwen38", "qwen3-vl-8b"))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-hybrid"]["running"])
        self.assertFalse(by_id["qwen38-27b"]["available"])
        self.assertFalse(by_id["qwen38-27b"]["startable"])
        self.assertIn("occupying", by_id["qwen38-27b"]["unavailable_reason"])

    def test_hybrid_live_grays_exclusive_vl8(self) -> None:
        catalog = {
            **CATALOG,
            "qwen38-hybrid": {
                "model": "qwen38-hybrid",
                "name": "Qwen3-VL-8B / 27B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
            "qwen3-vl-8b": {
                "model": "qwen3-vl-8b",
                "name": "Qwen3-VL-8B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        }
        picker = [
            {"id": "qwen38-hybrid", "name": "Qwen3-VL-8B / 27B Local"},
            {"id": "qwen3-vl-8b", "name": "Qwen3-VL-8B Local"},
            *PICKER,
        ]
        rows = d.annotate_model_availability(picker, catalog, live_ids=("qwen38", "qwen3-vl-8b"))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-hybrid"]["running"])
        self.assertFalse(by_id["qwen3-vl-8b"]["available"])
        self.assertFalse(by_id["qwen3-vl-8b"]["startable"])
        self.assertIn("occupying", by_id["qwen3-vl-8b"]["unavailable_reason"])

    def test_exclusive_vl8_live_grays_hybrid_and_27b(self) -> None:
        catalog = {
            **CATALOG,
            "qwen38-hybrid": {
                "model": "qwen38-hybrid",
                "name": "Qwen3-VL-8B / 27B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
            "qwen3-vl-8b": {
                "model": "qwen3-vl-8b",
                "name": "Qwen3-VL-8B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        }
        picker = [
            {"id": "qwen38-hybrid", "name": "Qwen3-VL-8B / 27B Local"},
            {"id": "qwen3-vl-8b", "name": "Qwen3-VL-8B Local"},
            *PICKER,
        ]
        rows = d.annotate_model_availability(picker, catalog, live_ids=("qwen3-vl-8b",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen3-vl-8b"]["running"])
        self.assertFalse(by_id["qwen38-hybrid"]["available"])
        self.assertFalse(by_id["qwen38-27b"]["available"])
        self.assertFalse(by_id["muse-glimmer"]["available"])

    def test_full_27b_live_grays_hybrid(self) -> None:
        catalog = {
            **CATALOG,
            "qwen38-hybrid": {
                "model": "qwen38-hybrid",
                "name": "Qwen3-VL-8B / 27B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        }
        picker = [
            {"id": "qwen38-hybrid", "name": "Qwen3-VL-8B / 27B Local"},
            *PICKER,
        ]
        rows = d.annotate_model_availability(picker, catalog, live_ids=("qwen38",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-27b"]["running"])
        self.assertFalse(by_id["qwen38-hybrid"]["available"])
        self.assertFalse(by_id["qwen38-hybrid"]["startable"])
        self.assertIn("occupying", by_id["qwen38-hybrid"]["unavailable_reason"])

    def test_start_replaces_other_family(self) -> None:
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", CATALOG)):
            with patch.object(d, "probe_local_llm_ids", return_value=("muse-glimmer",)):
                with patch.object(d, "_run_teela_script") as run:
                    run.return_value = None
                    out = d.local_llm_start("qwen38-27b")
        self.assertTrue(out.get("ok"))
        run.assert_called()
        args = run.call_args[0]
        self.assertEqual(args[1], ["qwen"])

    def test_control_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            d.local_llm_control({"action": "reboot"})

    def test_ensure_rejects_muse_while_qwen_serves(self) -> None:
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", CATALOG)):
            with patch.object(d, "probe_local_llm_ids", return_value=("qwen38",)):
                d.ensure_model_runnable("qwen38-27b")
                with self.assertRaises(ValueError) as ctx:
                    d.ensure_model_runnable("muse-glimmer")
        self.assertIn("occupying the GPUs", str(ctx.exception))

    def test_cloud_models_never_gated(self) -> None:
        d.ensure_model_runnable("grok-4.6")
        d.ensure_model_runnable("grok-4.5")

    def test_rewrite_drops_auto_tool_choice(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38-27b",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
                "tools": [{"type": "function", "function": {"name": "shell"}}],
            }
        )
        self.assertNotIn("tool_choice", out)
        self.assertTrue(out.get("tools"))

    def test_qwen_start_script_keeps_tool_parser_on_same_command(self) -> None:
        text = TEELA_START.read_text(encoding="utf-8")
        self.assertIn("--enable-auto-tool-choice", text)
        self.assertNotIn("--kv-cache-dtype fp8 \\\\\n${HYBRID_MM}\n", text)

    def test_start_script_has_nvidia_5060_fallback(self) -> None:
        text = TEELA_START.read_text(encoding="utf-8")
        self.assertIn("TEELA_BACKEND", text)
        self.assertIn("vllm/vllm-openai:v0.28.0", text)
        self.assertIn("8GB-class GPU", text)
        self.assertIn("Qwen3-VL-8B", text)
        self.assertIn("--gpus all", text)
        self.assertIn("detect_backend", text)
        self.assertIn("rocm", text)
        self.assertIn("vllm/vllm-openai-rocm", text)
        self.assertIn("apply_fit_policy", text)
        self.assertIn("/dev/kfd", text)
        self.assertIn("serve /path/to/weights", text)
        self.assertIn("inner_tp_boot", text)
        self.assertIn("visible GPUs=", text)
        self.assertIn("single GPU: max-model-len", text)
        self.assertIn("vllm_init_failed", text)
        self.assertIn("Engine core initialization failed", text)
        self.assertIn("No available memory for the cache blocks", text)

    def test_stale_device_lost_is_starting_after_new_init(self) -> None:
        stale = (
            "UR_RESULT_ERROR_DEVICE_LOST\n"
            "[teela] vLLM exited 0; cooling Intel GPUs 30s before restart\n"
            "[teela] visible GPUs=1 < TP=2; using tensor-parallel-size 1\n"
            "Initializing a V1 LLM engine (v0.21.1)\n"
        )
        self.assertEqual(d._vllm_failure_from_log_text(stale), "")
        dead = (
            "Initializing a V1 LLM engine (v0.21.1)\n"
            "Engine core initialization failed\n"
        )
        self.assertIn("failed to start", d._vllm_failure_from_log_text(dead).lower())

    def test_engine_up_is_port_not_models_list(self) -> None:
        src = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("def local_llm_engine_up", src)
        self.assertIn("def local_llm_engine_state", src)
        self.assertIn("def reap_local_llm_job", src)
        self.assertIn('engine in ("up", "starting")', src)
        self.assertNotIn(
            "The Arc GPUs likely reset",
            src,
        )

    def test_crash_loop_clears_stuck_starting_job(self) -> None:
        with d._LLM_JOB_LOCK:
            prev = dict(d._LLM_JOB)
            d._LLM_JOB.update(
                {"action": "starting", "target": "qwen", "family": "qwen", "started": time.time() - 30, "error": ""}
            )
        try:
            with patch.object(d, "_vllm_start_failure_from_logs", return_value="Local model failed to start."):
                d.reap_local_llm_job()
                snap = d.local_llm_job_snapshot()
            self.assertIsNone(snap.get("action"))
            self.assertIn("failed", str(snap.get("error") or "").lower())
            with patch.object(d, "local_llm_engine_up", return_value=False):
                with patch.object(d, "local_vllm_container_running", return_value=True):
                    with patch.object(d, "_vllm_start_failure_from_logs", return_value="device index"):
                        with patch.object(
                            d,
                            "local_llm_job_snapshot",
                            return_value={"action": None, "error": "device index"},
                        ):
                            self.assertEqual(d.local_llm_engine_state(), "down")
        finally:
            with d._LLM_JOB_LOCK:
                d._LLM_JOB.clear()
                d._LLM_JOB.update(prev)

    def test_catalog_add_and_delete_weights(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        weights = home / "models"
        (weights / "NewNet").mkdir(parents=True)
        (weights / "NewNet" / "config.json").write_text("{}", encoding="utf-8")
        orig_home, orig_models, orig_desk, orig_legacy = (
            d.USER_AGENT_HOME,
            d.MODELS_DIR,
            os.environ.get("HERMES_DESK_HOME"),
            os.environ.get("HERMES_DESK_LEGACY_TOML"),
        )
        d.USER_AGENT_HOME = home
        d.MODELS_DIR = weights
        # The shared catalog path (agent_home.models_catalog_path) is derived
        # from HERMES_DESK_HOME — without this the test writes the real
        # ~/.hermes/models.json and can clobber the live model picker. The
        # legacy TOML is isolated too so the catalog self-heal cannot
        # re-import the host's real model rows mid-test.
        os.environ["HERMES_DESK_HOME"] = str(home)
        os.environ["HERMES_DESK_LEGACY_TOML"] = str(home / "no-such-legacy.toml")
        try:
            with patch.object(d, "bots", {}):
                with patch.object(d, "emit"):
                    saved = d.save_user_model_catalog(
                        [
                            {
                                "key": "grok-4.6",
                                "model": "grok-4.6",
                                "name": "Grok 4.6",
                                "apiBackend": "responses",
                            },
                            {
                                "key": "newnet",
                                "model": "newnet",
                                "name": "New Net",
                                "baseUrl": "http://127.0.0.1:8000/v1",
                                "weights": "NewNet",
                            },
                        ],
                        "newnet",
                    )
                    self.assertEqual(saved["default"], "newnet")
                    default, cat = d.load_user_models()
                    self.assertEqual(default, "newnet")
                    self.assertEqual(cat["newnet"]["weights"], "NewNet")
                    gone = d.delete_user_model("newnet", delete_weights=True)
                    self.assertEqual(gone["deleted"], "newnet")
                    self.assertIn("NewNet", gone["deleted_weights"])
                    self.assertFalse((weights / "NewNet").exists())
                    _, cat2 = d.load_user_models()
                    self.assertNotIn("newnet", cat2)
                    with self.assertRaises(ValueError):
                        d.delete_user_model("grok-4.6")
        finally:
            d.USER_AGENT_HOME = orig_home
            d.MODELS_DIR = orig_models
            if orig_desk is None:
                os.environ.pop("HERMES_DESK_HOME", None)
            else:
                os.environ["HERMES_DESK_HOME"] = orig_desk
            if orig_legacy is None:
                os.environ.pop("HERMES_DESK_LEGACY_TOML", None)
            else:
                os.environ["HERMES_DESK_LEGACY_TOML"] = orig_legacy
            tmp.cleanup()

    def test_qwen_start_cools_gpus_after_crash(self) -> None:
        text = TEELA_START.read_text(encoding="utf-8")
        self.assertIn("cooling Intel GPUs 30s before restart", text)
        self.assertIn("trap term TERM INT", text)
        self.assertIn("GPU_UTIL=0.75", text)
        self.assertIn("MAX_SEQS=1", text)
        self.assertNotIn("GPU_UTIL=0.82", text)

    def test_local_llm_proxy_waits_out_xpu_prefill(self) -> None:
        text = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("sock_timeout = ACP_PROMPT_MAX_SEC if \"chat/completions\" in rest else 10", text)
        self.assertNotIn("HTTPConnection(host, port, timeout=90)", text)

    def test_local_proxy_caps_huge_max_tokens(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38-27b", "max_tokens": 247302, "messages": []}
        )
        self.assertEqual(out["model"], "qwen38")
        self.assertEqual(out["max_tokens"], d.LOCAL_LLM_MAX_COMPLETION)
        self.assertFalse(out["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(out["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertEqual(out["reasoning_effort"], "low")

    def test_local_proxy_maps_grok_high_effort_to_qwen_low(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [],
                "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "high"},
            }
        )
        self.assertFalse(out["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(out["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertEqual(out["reasoning_effort"], "low")

    def test_local_proxy_default_max_tokens(self) -> None:
        out = d.rewrite_local_llm_chat_payload({"model": "qwen38", "messages": []})
        self.assertEqual(out["max_tokens"], d.LOCAL_LLM_MAX_COMPLETION)
        self.assertEqual(out["reasoning_effort"], "low")

    def test_local_proxy_fits_max_tokens_into_32768_window(self) -> None:
        err = (
            "This model's maximum context length is 32768 tokens. However, you requested "
            "8192 output tokens and your prompt contains at least 24577 input tokens, "
            "for a total of at least 32769 tokens."
        )
        self.assertEqual(d.parse_context_overflow(err), 32768 - 24577 - 8)
        big = "alpha " * 12000
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": big}],
            }
        )
        prompt = d.estimate_local_prompt_tokens(out)
        self.assertLessEqual(out["max_tokens"] + prompt, d.LOCAL_LLM_MAX_MODEL_LEN)
        self.assertLess(out["max_tokens"], 8192)

    def test_local_proxy_trims_prompt_when_input_fills_window(self) -> None:
        err = (
            "This model's maximum context length is 32768 tokens. However, you requested "
            "1 output tokens and your prompt contains at least 32768 input tokens, "
            "for a total of at least 32769 tokens."
        )
        self.assertIsNone(d.parse_context_overflow(err))
        details = d.parse_context_overflow_details(err)
        self.assertEqual(details, (32768, 1, 32768))
        cli = "You are Hermes released by xAI.\n" + ("policy line\n" * 200)
        body = "You have a body. Call bot_desktop__robot_pose to wave.\n"
        sys_text = cli + "Follow AGENTS.md\n" + body + ("lookbook row\n" * 8000)
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "max_tokens": 8192,
                "messages": [
                    {"role": "system", "content": sys_text},
                    {"role": "assistant", "content": "Hi there!"},
                    {"role": "user", "content": "Can you wave"},
                ],
            }
        )
        prompt = d.estimate_local_prompt_tokens(out)
        self.assertLessEqual(prompt + int(out["max_tokens"]), d.LOCAL_LLM_MAX_MODEL_LEN)
        last = out["messages"][-1]["content"]
        self.assertIn("wave", last.lower())
        sys_out = d._flatten_message_text(out["messages"][0])
        self.assertNotIn("You are Hermes released by xAI", sys_out)
        self.assertIn("body", sys_out.lower())

    def test_fast_local_chat_does_not_cap_completion(self) -> None:
        src = d.fast_local_chat.__code__.co_consts
        self.assertNotIn(96, src)
        text = Path(d.__file__).read_text(encoding="utf-8")
        fn = text.split("def fast_local_chat", 1)[1].split("def ", 1)[0]
        self.assertNotIn('"max_tokens"', fn)
        self.assertNotIn("len(line) > 220", fn)

    def test_motor_turn_disables_thinking(self) -> None:
        wave = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38", "messages": [{"role": "user", "content": "wave"}]}
        )
        self.assertFalse(wave["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("robot_pose", wave["messages"][0]["content"])
        arm = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "raise your right arm"}],
            }
        )
        self.assertFalse(arm["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("robot_joint", arm["messages"][0]["content"])
        loop = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": "robot_pose", "arguments": '{"pose":"wave"}'},
                            }
                        ],
                    },
                    {"role": "tool", "name": "robot_pose", "content": '{"ok":true}'},
                ],
            }
        )
        self.assertFalse(loop["chat_template_kwargs"]["enable_thinking"])
        tools = [
            {"type": "function", "function": {"name": "search_tool"}},
            {"type": "function", "function": {"name": "bot_desktop__robot_joint"}},
            {"type": "function", "function": {"name": "shell"}},
        ]
        motor_tools = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "raise your right hand"}],
                "tools": tools,
            }
        )
        names = [t["function"]["name"] for t in motor_tools["tools"]]
        self.assertIn("bot_desktop__robot_joint", names)
        self.assertIn("bot_desktop__robot_pose", names)
        self.assertNotIn("search_tool", names)
        self.assertNotIn("shell", names)
        dropped = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [{"role": "user", "content": "tilt your body right"}],
                "tools": [
                    {"type": "function", "function": {"name": "shell"}},
                    {"type": "function", "function": {"name": "search_tool"}},
                ],
            }
        )
        dropped_names = [t["function"]["name"] for t in dropped.get("tools") or []]
        self.assertIn("bot_desktop__robot_pose", dropped_names)
        self.assertNotIn("shell", dropped_names)
        self.assertNotIn("search_tool", dropped_names)

    def test_probe_cache_is_id_to_url_map(self) -> None:
        d.reset_local_llm_probe()
        ts, cached = d._LLM_PROBE_CACHE
        self.assertEqual(cached, {})
        self.assertEqual(ts, 0.0)

    def test_hybrid_routes_normal_to_9b_and_hard_to_27b(self) -> None:
        live = ("qwen38", "qwen38-9b")
        wave = {"model": "qwen38-hybrid", "messages": [{"role": "user", "content": "wave"}]}
        url, served = d.pick_local_llm_route(wave, live_ids=live)
        self.assertEqual(served, "qwen38-9b")
        self.assertIn(":8001", url)
        live_vl = ("qwen38", "qwen3-vl-8b")
        wave_vl = d.pick_local_llm_route(wave, live_ids=live_vl)
        self.assertEqual(wave_vl[1], "qwen3-vl-8b")
        self.assertIn(":8001", wave_vl[0])
        hard = {
            "model": "qwen38-hybrid",
            "messages": [{"role": "user", "content": "debug this TypeError and implement a fix"}],
        }
        url, served = d.pick_local_llm_route(hard, live_ids=live)
        self.assertEqual(served, "qwen38")
        self.assertIn(":8000", url)
        pin = d.pick_local_llm_route(
            {"model": "qwen38-27b", "messages": [{"role": "user", "content": "wave"}]},
            live_ids=live,
        )
        self.assertEqual(pin[1], "qwen38")
        self.assertIn(":8000", pin[0])

    def test_hybrid_falls_back_when_9b_down(self) -> None:
        url, served = d.pick_local_llm_route(
            {"model": "qwen38", "messages": [{"role": "user", "content": "wave"}]},
            live_ids=("qwen38",),
        )
        self.assertEqual(served, "qwen38")
        self.assertIn(":8000", url)

    def test_chat_uses_vl_on_8001_when_27b_down(self) -> None:
        live = ("qwen3-vl-8b",)
        live_map = {"qwen3-vl-8b": d.LOCAL_LLM_FAST_UPSTREAM}
        for mid in ("qwen38", "qwen38-hybrid", "qwen38-27b", "qwen3-vl-8b"):
            url, served = d.pick_local_llm_route(
                {"model": mid, "messages": [{"role": "user", "content": "hi"}]},
                live_ids=live,
                live_map=live_map,
            )
            self.assertIn(":8001", url, mid)
            self.assertEqual(served, "qwen3-vl-8b", mid)

    def test_hybrid_pin_does_not_stick_to_dead_27b(self) -> None:
        class _Bot:
            llm_pin_key = "hi"
            llm_pin_url = d.LOCAL_LLM_UPSTREAM
            llm_pin_served = "qwen38"

        bot = _Bot()
        url, served = d.pick_local_llm_route(
            {"model": "qwen38-hybrid", "messages": [{"role": "user", "content": "hi"}]},
            live_ids=("qwen3-vl-8b",),
            live_map={"qwen3-vl-8b": d.LOCAL_LLM_FAST_UPSTREAM},
            bot=bot,
        )
        self.assertIn(":8001", url)
        self.assertEqual(served, "qwen3-vl-8b")

    def test_hybrid_routes_images_to_vl8(self) -> None:
        live = ("qwen38", "qwen3-vl-8b")
        look = {
            "model": "qwen38-hybrid",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aaaa"},
                        },
                    ],
                }
            ],
        }
        url, served = d.pick_local_llm_route(look, live_ids=live)
        self.assertEqual(served, "qwen3-vl-8b")
        self.assertIn(":8001", url)
        wave = d.pick_local_llm_route(
            {"model": "qwen38-hybrid", "messages": [{"role": "user", "content": "wave"}]},
            live_ids=live,
        )
        self.assertEqual(wave[1], "qwen3-vl-8b")
        pin = d.pick_local_llm_route(
            {
                "model": "qwen38-9b-distill",
                "messages": look["messages"],
            },
            live_ids=live,
        )
        self.assertEqual(pin[1], "qwen3-vl-8b")
        pin_vl = d.pick_local_llm_route(
            {"model": "qwen3-vl-8b", "messages": look["messages"]},
            live_ids=live,
        )
        self.assertEqual(pin_vl[1], "qwen3-vl-8b")
        self.assertIn(":8001", pin_vl[0])
        exclusive = d.pick_local_llm_route(
            {"model": "qwen3-vl-8b", "messages": look["messages"]},
            live_ids=("qwen3-vl-8b",),
        )
        self.assertEqual(exclusive[1], "qwen3-vl-8b")
        self.assertIn(":8000", exclusive[0])

    def test_hybrid_pins_turn_to_9b_unless_escalated(self) -> None:
        live = ("qwen38", "qwen38-9b")

        class _Bot:
            llm_pin_key = ""
            llm_pin_url = ""
            llm_pin_served = ""

        bot = _Bot()
        look = {
            "model": "qwen38-hybrid",
            "messages": [{"role": "user", "content": "what is this?"}],
        }
        url, served = d.pick_local_llm_route(look, live_ids=live, bot=bot)
        self.assertEqual(served, "qwen38-9b")
        follow = {
            "model": "qwen38-hybrid",
            "messages": [
                {"role": "user", "content": "what is this?"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_tool"}}]},
                {"role": "tool", "content": "debug this TypeError and implement a fix"},
            ],
        }
        url, served = d.pick_local_llm_route(follow, live_ids=live, bot=bot)
        self.assertEqual(served, "qwen38-9b")
        self.assertIn(":8001", url)
        pictured = {
            "model": "qwen38-hybrid",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaaa"}},
                    ],
                }
            ],
        }
        url, served = d.pick_local_llm_route(pictured, live_ids=live, bot=bot)
        self.assertEqual(served, "qwen38-9b")
        self.assertIn(":8001", url)
        hard = {
            "model": "qwen38-hybrid",
            "messages": [{"role": "user", "content": "debug this TypeError and implement a fix"}],
        }
        url, served = d.pick_local_llm_route(hard, live_ids=live, bot=bot)
        self.assertEqual(served, "qwen38")
        self.assertIn(":8000", url)

    def test_non_motor_keeps_thinking_off_without_motor_prefix(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {"model": "qwen38", "messages": [{"role": "user", "content": "what is 2+2?"}]}
        )
        self.assertFalse(out["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(out["messages"][0]["content"], "what is 2+2?")
        later = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {"role": "user", "content": "wave"},
                    {
                        "role": "assistant",
                        "tool_calls": [{"function": {"name": "robot_pose", "arguments": "{}"}}],
                    },
                    {"role": "tool", "name": "robot_pose", "content": "{}"},
                    {"role": "user", "content": "what is 2+2?"},
                ],
            }
        )
        self.assertFalse(later["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(later["messages"][-1]["content"], "what is 2+2?")

    def test_local_prompt_blocks_are_text_only(self) -> None:
        blocks = d.local_llm_prompt_blocks(
            "what is this?",
            [{"path": "Desktop/paste-1.png", "mime": "image/png"}],
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("what is this?", blocks[0]["text"])
        self.assertIn("Desktop/paste-1.png", blocks[0]["text"])
        self.assertNotIn("cannot view pixels", blocks[0]["text"])
        self.assertNotIn("image_url", blocks[0]["text"])
        self.assertIn("local VL path", blocks[0]["text"])

    def test_local_proxy_keeps_image_parts(self) -> None:
        out = d.rewrite_local_llm_chat_payload(
            {
                "model": "qwen38",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is on screen?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aaaa"},
                            },
                        ],
                    }
                ],
            }
        )
        content = out["messages"][0]["content"]
        self.assertIsInstance(content, list)
        types = [p.get("type") for p in content if isinstance(p, dict)]
        self.assertIn("image_url", types)
        self.assertIn("data:image/png;base64,aaaa", json.dumps(out["messages"]))

    def test_hydrate_workspace_image_to_data_url(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        pic = root / "Pictures" / "shot.png"
        pic.parent.mkdir(parents=True)
        pic.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
            b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        {"type": "image_url", "image_url": {"url": "Pictures/shot.png"}},
                    ],
                }
            ]
        }
        out = d.hydrate_local_mm_parts(payload, root)
        url = out["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        tmp.cleanup()

    def test_hydrate_mcp_image_part_to_data_url(self) -> None:
        out = d.hydrate_local_mm_parts(
            {
                "messages": [
                    {
                        "role": "tool",
                        "content": [
                            {"type": "text", "text": "frame"},
                            {
                                "type": "image",
                                "mimeType": "image/jpeg",
                                "data": "abc123",
                            },
                        ],
                    }
                ]
            },
            None,
        )
        part = out["messages"][0]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertEqual(part["image_url"]["url"], "data:image/jpeg;base64,abc123")

    def test_attach_recent_chat_images_from_bot_messages(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        pic = root / "Pictures" / "paste.png"
        pic.parent.mkdir(parents=True)
        pic.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
            b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        class _Bot:
            workspace = root
            messages = [
                {
                    "role": "user",
                    "text": "what is this?",
                    "images": [{"path": "Pictures/paste.png", "mime": "image/png"}],
                }
            ]

        payload = {
            "messages": [{"role": "user", "content": "what is this?"}],
        }
        out = d.attach_recent_chat_images(payload, _Bot())
        content = out["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "what is this?")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        tmp.cleanup()

    def test_engine_up_sees_llama_cpp_8081(self) -> None:
        urls = []

        def port_open(url: str, timeout: float = 0.25) -> bool:
            urls.append(str(url))
            return ":8081" in str(url)

        with patch.object(d, "local_llm_port_open", side_effect=port_open):
            self.assertTrue(d.local_llm_engine_up())
        joined = " ".join(urls)
        self.assertIn("8081", joined)
        self.assertIn("def wait_for_local_model", Path(d.__file__).read_text(encoding="utf-8"))

    def test_acp_timeout_names_dead_local_engine(self) -> None:
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", CATALOG)):
            with patch.object(d, "local_llm_engine_state", return_value="down"):
                err = d.acp_prompt_timeout_error("qwen38-27b", local_live=False)
            live = d.acp_prompt_timeout_error("qwen38-27b", local_live=True)
            cloud = d.acp_prompt_timeout_error("grok-4.6", local_live=False)
        self.assertIsInstance(err, TimeoutError)
        self.assertIn("Local model is not running", str(err))
        self.assertNotIn("vLLM", str(err))
        self.assertNotIn("llama.cpp", str(err))
        flash = {
            **CATALOG,
            "qwen38-flash-next": {
                "model": "Qwen3.8-Flash-Next",
                "name": "Qwen 3.8 Flash-Next",
                "base_url": "http://127.0.0.1:8080/v1",
                "weights": "/home/roni/models/Qwen3.8-Flash-Next",
            },
        }
        with patch.object(d, "load_user_models", return_value=("qwen38-flash-next", flash)):
            llama_err = d.acp_prompt_timeout_error("qwen38-flash-next", local_live=False)
        self.assertIn("Local model is not running", str(llama_err))
        self.assertNotIn("vLLM", str(llama_err))
        self.assertNotIn("llama.cpp", str(llama_err))
        self.assertEqual(str(live), "ACP session/prompt timed out")
        self.assertEqual(str(cloud), "ACP session/prompt timed out")

    def test_local_model_serving_needs_matching_family(self) -> None:
        hybrid = {
            **CATALOG,
            "qwen38-hybrid": {
                "model": "qwen38-hybrid",
                "name": "Qwen3-VL-8B / 27B Local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        }
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", CATALOG)):
            self.assertTrue(d.local_model_serving("qwen38-27b", live_ids=("qwen38",)))
            self.assertTrue(d.local_model_serving("qwen38-27b", live_ids=("qwen38", "qwen3-vl-8b")))
            self.assertFalse(d.local_model_serving("qwen38-27b", live_ids=("qwen3-vl-8b",)))
            self.assertFalse(d.local_model_serving("qwen38-27b", live_ids=()))
            self.assertTrue(d.local_model_serving("grok-4.6", live_ids=()))
        with patch.object(d, "load_user_models", return_value=("qwen38-hybrid", hybrid)):
            self.assertTrue(d.local_model_serving("qwen38-hybrid", live_ids=("qwen38",)))
            self.assertTrue(d.local_model_serving("qwen38-hybrid", live_ids=("qwen3-vl-8b",)))
            self.assertFalse(d.local_model_serving("qwen38-hybrid", live_ids=()))

    def test_local_prompt_waits_600s_not_hard_90s(self) -> None:
        text = Path(d.__file__).read_text(encoding="utf-8")
        self.assertIn("prompt_timeout = ACP_PROMPT_MAX_SEC", text)
        self.assertNotIn("90 if uses_local_text_llm", text)
        self.assertEqual(d.ACP_PROMPT_MAX_SEC, 600)
        self.assertEqual(d.ACP_LOCAL_DOWN_GRACE_SEC, 12)
        self.assertIn("def _wait_prompt", text)
        wait = text.split("def _wait_prompt", 1)[1].split("def ", 1)[0]
        self.assertIn("idle_limit", wait)
        self.assertNotIn("deadline = time.time() + max(1.0, float(timeout))", wait)

    def _bare_acp(self, model: str = "grok-4.6") -> d.AcpClient:
        bot = types.SimpleNamespace(model=model, id="b_test")
        acp = d.AcpClient.__new__(d.AcpClient)
        acp.bot = bot
        acp._pending = {}
        acp._turn_cancel = threading.Event()
        acp._last_acp_event = time.time()
        acp._prompt_rid = None
        acp.abandoned: list[int] = []

        def abandon(rid: int) -> None:
            acp.abandoned.append(rid)
            acp._pending.pop(rid, None)

        acp._abandon_prompt = abandon  # type: ignore[method-assign]
        return acp

    def test_wait_prompt_activity_extends_idle_cap(self) -> None:
        acp = self._bare_acp("grok-4.6")
        rid = 1
        ev = threading.Event()
        acp._pending[rid] = (ev, {})
        acp._last_acp_event = time.time()

        def keep_alive() -> None:
            for _ in range(8):
                time.sleep(0.12)
                acp._last_acp_event = time.time()
            ev.set()

        t = threading.Thread(target=keep_alive, daemon=True)
        t.start()
        acp._wait_prompt(rid, ev, timeout=0.35)
        t.join(2)
        self.assertEqual(acp.abandoned, [])

    def test_wait_prompt_idle_still_times_out(self) -> None:
        acp = self._bare_acp("grok-4.6")
        rid = 2
        ev = threading.Event()
        acp._pending[rid] = (ev, {})
        acp._last_acp_event = time.time() - 5
        with self.assertRaises(TimeoutError) as ctx:
            acp._wait_prompt(rid, ev, timeout=0.3)
        self.assertEqual(str(ctx.exception), "ACP session/prompt timed out")
        self.assertEqual(acp.abandoned, [2])

    def test_wait_prompt_local_down_fast_fail(self) -> None:
        acp = self._bare_acp("qwen38-27b")
        rid = 3
        ev = threading.Event()
        acp._pending[rid] = (ev, {})
        acp._last_acp_event = time.time() - 20
        with patch.object(d, "uses_local_text_llm", return_value=True):
            with patch.object(d, "local_model_serving", return_value=False):
                with patch.object(d, "local_llm_engine_state", return_value="down"):
                    with patch.object(d, "load_user_models", return_value=("qwen38-27b", CATALOG)):
                        t0 = time.time()
                        with self.assertRaises(TimeoutError) as ctx:
                            acp._wait_prompt(rid, ev, timeout=30)
                        elapsed = time.time() - t0
        self.assertIn("Local model is not running", str(ctx.exception))
        self.assertLess(elapsed, 2.0)
        self.assertEqual(acp.abandoned, [3])

    def test_emit_local_activity_refreshes_acp_idle(self) -> None:
        bot = types.SimpleNamespace(
            model="qwen38-27b",
            id="b_test",
            status="",
            surface="chat",
            control="agent_controlled",
            acp=types.SimpleNamespace(_last_acp_event=0.0, _turn_cancel=threading.Event()),
            append_thought=lambda _t: None,
        )
        with patch.object(d, "emit"):
            d.emit_local_activity(bot, "Thinking…")
        self.assertGreater(bot.acp._last_acp_event, 0.0)

    def test_reclaim_unsticks_thinking_when_turn_is_idle(self) -> None:
        bot = types.SimpleNamespace(
            id="b_stuck",
            status="Thinking…",
            surface="chat",
            control="agent_controlled",
            _prompt_busy=False,
            _prefill_stop=None,
            acp=types.SimpleNamespace(_last_acp_event=0.0),
            _prompt_lock=threading.Lock(),
        )
        with patch.object(d, "emit"):
            self.assertTrue(d.reclaim_stale_busy_status(bot))
        self.assertEqual(bot.status, "Ready")
        bot.status = "Thinking…"
        bot._prompt_busy = True
        bot.acp._last_acp_event = time.time()
        with patch.object(d, "emit"):
            self.assertFalse(d.reclaim_stale_busy_status(bot))
        self.assertEqual(bot.status, "Thinking…")


LOCAL_ONLY = {
    "qwen38-27b": CATALOG["qwen38-27b"],
    "muse-glimmer": CATALOG["muse-glimmer"],
}

BODY_CATALOG = {
    "llama-local": {
        "model": "llama-70b",
        "name": "Llama 70B Local",
        "base_url": "http://127.0.0.1:8000/v1",
        "context_window": 32768,
    },
    "grok-4.6": {"model": "grok-4.6", "name": "Grok 4.6", "context_window": 500000},
}


class HostCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        d.reset_local_llm_probe()

    def test_grok_zero_max_completion_tokens_is_rejected(self) -> None:
        self.assertEqual(d.coerce_max_completion_tokens("grok-4.6", {"max_completion_tokens": 0}), 65536)
        self.assertEqual(d.coerce_max_completion_tokens("grok-4.5", {}), 65536)
        self.assertEqual(d.coerce_max_completion_tokens("qwen3-8-27b", {"max_completion_tokens": 0}), None)
        self.assertEqual(d.coerce_max_completion_tokens("qwen3-8-27b", {"max_completion_tokens": 32768}), 32768)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        d.write_child_config(
            home,
            "grok-4.6",
            {
                "grok-4.6": {
                    "model": "grok-4.6",
                    "name": "Grok 4.6",
                    "api_backend": "responses",
                    "context_window": 500000,
                    "max_completion_tokens": 0,
                }
            },
        )
        text = (home / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("provider: custom", text)
        self.assertIn("default: grok-4.6", text)

    def test_toml_model_keys_quote_dots(self) -> None:
        self.assertEqual(d.toml_key("qwen38-27b"), "qwen38-27b")
        self.assertEqual(d.toml_key("grok-4.6"), '"grok-4.6"')
        nested = {
            "qwen38-27b": LOCAL_ONLY["qwen38-27b"],
            "grok-4": {"6": {"model": "grok-4.6", "name": "Grok 4.6", "context_window": 500000}},
        }
        flat = d.flatten_model_tables(nested)
        self.assertIn("qwen38-27b", flat)
        self.assertIn("grok-4.6", flat)
        self.assertEqual(flat["grok-4.6"]["name"], "Grok 4.6")

    def test_host_picker_is_config_only(self) -> None:
        default, rows = d.host_picker_models(LOCAL_ONLY, "qwen38-27b", live_ids=("qwen38",))
        ids = [r["id"] for r in rows]
        self.assertEqual(default, "qwen38-27b")
        self.assertEqual(ids[:2], ["qwen38-27b", "muse-glimmer"])
        self.assertIn("grok-4.6", ids)
        self.assertIn("grok-4.5", ids)
        self.assertTrue(rows[0]["available"])
        self.assertFalse(rows[1]["available"])
        hermes = next(r for r in rows if r["id"] == "grok-4.6")
        self.assertTrue(hermes["available"])
        self.assertFalse(hermes["local"])

    def test_host_picker_keeps_config_cloud_grok(self) -> None:
        catalog = {
            **LOCAL_ONLY,
            "grok-4.6": {"model": "grok-4.6", "name": "Grok 4.6", "api_backend": "responses", "context_window": 500000},
            "grok-4.5": {"model": "grok-4.5", "name": "Grok 4.5", "api_backend": "responses", "context_window": 256000},
        }
        default, rows = d.host_picker_models(catalog, "grok-4.6", live_ids=("qwen38",))
        ids = [r["id"] for r in rows]
        self.assertEqual(default, "grok-4.6")
        self.assertIn("qwen38-27b", ids)
        self.assertIn("grok-4.6", ids)
        self.assertIn("grok-4.5", ids)
        hermes = next(r for r in rows if r["id"] == "grok-4.6")
        self.assertTrue(hermes["available"])
        self.assertFalse(hermes["local"])

    def test_host_picker_hides_distill_alias(self) -> None:
        catalog = {
            "qwen38-hybrid": {"name": "Qwen3-VL-8B / 27B Local", "base_url": "http://127.0.0.1:8000/v1"},
            "qwen38-9b-distill": {"name": "Qwen3-VL-8B Local", "base_url": "http://127.0.0.1:8001/v1"},
            "qwen3-vl-8b": {"name": "Qwen3-VL-8B Local", "base_url": "http://127.0.0.1:8001/v1"},
        }
        default, rows = d.host_picker_models(
            catalog, "qwen38-hybrid", live_ids=("qwen38", "qwen3-vl-8b"), extra_ids=["qwen38-9b-distill"]
        )
        ids = [r["id"] for r in rows]
        names = [r["name"] for r in rows]
        self.assertEqual(default, "qwen38-hybrid")
        self.assertIn("qwen3-vl-8b", ids)
        self.assertNotIn("qwen38-9b-distill", ids)
        self.assertEqual(names.count("Qwen3-VL-8B Local"), 1)

    def test_host_picker_keeps_stale_current(self) -> None:
        default, rows = d.host_picker_models(
            LOCAL_ONLY, "qwen38-27b", live_ids=("qwen38",), extra_ids=["grok-4.6"]
        )
        ids = [r["id"] for r in rows]
        self.assertIn("grok-4.6", ids)
        self.assertIn("grok-4.5", ids)
        hermes = next(r for r in rows if r["id"] == "grok-4.6")
        self.assertTrue(hermes["available"])

    def test_apply_models_drops_acp_cloud_ids(self) -> None:
        bot = d.Bot("b_picker00001", "P", "", "", "qwen38-27b", "x")
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", LOCAL_ONLY)):
            with patch.object(d, "probe_local_llm_ids", return_value=("qwen38",)):
                bot.apply_models(
                    {
                        "currentModelId": "grok-4.6",
                        "availableModels": [
                            {"modelId": "grok-4.6", "name": "Grok 4.6"},
                            {"modelId": "grok-4.5", "name": "Grok 4.5"},
                            {"modelId": "grok-code-fast-1", "name": "Hermes Code Fast 1"},
                        ],
                    }
                )
        ids = [m["id"] for m in bot.models]
        self.assertEqual(bot.model, "qwen38-27b")
        self.assertIn("qwen38-27b", ids)
        self.assertIn("muse-glimmer", ids)
        self.assertIn("grok-4.6", ids)
        self.assertIn("grok-4.5", ids)
        self.assertNotIn("grok-code-fast-1", ids)

    def test_ensure_model_on_host_rejects_foreign(self) -> None:
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", LOCAL_ONLY)):
            with patch.object(d, "probe_local_llm_ids", return_value=("qwen38",)):
                d.ensure_model_on_host("qwen38-27b")
                d.ensure_model_on_host("grok-4.6")
                d.ensure_model_on_host("grok-4.5")
                with self.assertRaises(ValueError) as ctx:
                    d.ensure_model_on_host("not-a-real-model")
                self.assertIn("unknown model", str(ctx.exception))

    def test_body_loopback_occupancy_does_not_assume_qwen(self) -> None:
        picker = [
            {"id": "llama-local", "name": "Llama 70B Local", "context_window": 32768},
            {"id": "grok-4.6", "name": "Grok 4.6", "context_window": 500000},
        ]
        live = d.annotate_model_availability(picker, BODY_CATALOG, live_ids=("llama-70b",))
        by_id = {r["id"]: r for r in live}
        self.assertTrue(by_id["llama-local"]["available"])
        self.assertTrue(by_id["grok-4.6"]["available"])
        busy = d.annotate_model_availability(picker, BODY_CATALOG, live_ids=("other-weights",))
        by_id = {r["id"]: r for r in busy}
        self.assertFalse(by_id["llama-local"]["available"])
        self.assertIn("occupying", by_id["llama-local"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])
        down = d.annotate_model_availability(picker, BODY_CATALOG, live_ids=())
        by_id = {r["id"]: r for r in down}
        self.assertFalse(by_id["llama-local"]["available"])
        self.assertIn("not running", by_id["llama-local"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])

    def test_llama_cpp_models_exclusive_on_gpus(self) -> None:
        catalog = {
            "qwen38-flash-next": {
                "model": "Qwen3.8-Flash-Next",
                "name": "Qwen 3.8 Flash-Next",
                "base_url": "http://127.0.0.1:8080/v1",
                "weights": "/home/roni/models/Qwen3.8-Flash-Next",
            },
            "qwen38-27b-q4": {
                "model": "Qwen3.8-27B",
                "name": "Qwen 3.8 27B",
                "base_url": "http://127.0.0.1:8081/v1",
                "weights": "/home/roni/models/Qwen3.8-27B",
            },
            "grok-4.6": CATALOG["grok-4.6"],
        }
        picker = [
            {"id": "qwen38-flash-next", "name": "Qwen 3.8 Flash-Next"},
            {"id": "qwen38-27b-q4", "name": "Qwen 3.8 27B"},
            {"id": "grok-4.6", "name": "Grok 4.6"},
        ]

        def port_open(url: str, timeout: float = 0.35) -> bool:
            return "8080" in str(url)

        with patch.object(d, "local_llm_port_open", side_effect=port_open):
            rows = d.annotate_model_availability(
                picker, catalog, live_ids=("Qwen3.8-Flash-Next",)
            )
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-flash-next"]["running"])
        self.assertTrue(by_id["qwen38-flash-next"]["available"])
        self.assertFalse(by_id["qwen38-27b-q4"]["available"])
        self.assertFalse(by_id["qwen38-27b-q4"]["running"])
        self.assertFalse(by_id["qwen38-27b-q4"]["startable"])
        self.assertTrue(by_id["qwen38-27b-q4"]["occupying"])
        self.assertIn("occupying the GPUs", by_id["qwen38-27b-q4"]["unavailable_reason"])
        self.assertTrue(by_id["grok-4.6"]["available"])
        self.assertFalse(by_id["grok-4.6"]["local"])

        with patch.object(d, "local_llm_port_open", return_value=False):
            idle = d.annotate_model_availability(picker, catalog, live_ids=())
        idle_id = {r["id"]: r for r in idle}
        self.assertFalse(idle_id["qwen38-flash-next"]["running"])
        self.assertTrue(idle_id["qwen38-flash-next"]["startable"])
        self.assertTrue(idle_id["qwen38-27b-q4"]["startable"])
        self.assertFalse(idle_id["qwen38-27b-q4"]["occupying"])

    def test_gguf_27b_q4_is_llamacpp(self) -> None:
        tbl = {
            "model": "Qwen3.8-27B",
            "name": "Qwen 3.8 27B",
            "base_url": "http://127.0.0.1:8081/v1",
            "weights": "/home/roni/models/Qwen3.8-27B",
        }
        self.assertEqual(d._picker_gpu_family("qwen38-27b-q4", tbl), "llamacpp")
        self.assertTrue(d.is_llama_cpp_model("qwen38-27b-q4", tbl))
        self.assertFalse(d.is_exclusive_vllm_model("qwen38-27b-q4", tbl))
        self.assertEqual(d.LOCAL_LLM_ALIASES.get("qwen38-27b-q4"), "Qwen3.8-27B")
        self.assertIn(8081, d._LLAMA_CPP_PORTS)

    def test_peer_llm_backup_does_not_occupy_or_start(self) -> None:
        tbl = {
            "model": "Qwen3.8-27B",
            "name": "Qwen 3.8 27B Q5 (brain backup)",
            "base_url": "http://127.0.0.1:8081/v1",
            "peer": "teela-brain",
        }
        self.assertTrue(d.is_peer_llm_backup(tbl))
        catalog = {"qwen38-27b-q5": tbl, "grok-4.6": CATALOG["grok-4.6"]}
        picker = [
            {"id": "qwen38-27b-q5", "name": tbl["name"]},
            {"id": "grok-4.6", "name": "Grok 4.6"},
        ]
        with patch.object(d, "local_llm_port_open", return_value=True):
            rows = d.annotate_model_availability(picker, catalog, live_ids=("Qwen3.8-27B",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-27b-q5"]["available"])
        self.assertTrue(by_id["qwen38-27b-q5"]["running"])
        self.assertFalse(by_id["qwen38-27b-q5"]["startable"])
        self.assertFalse(by_id["qwen38-27b-q5"]["occupying"])
        self.assertFalse(by_id["qwen38-27b-q5"]["local"])
        self.assertEqual(by_id["qwen38-27b-q5"]["peer"], "teela-brain")
        self.assertTrue(by_id["grok-4.6"]["available"])
        with patch.object(d, "local_llm_port_open", return_value=False):
            down = d.annotate_model_availability(picker, catalog, live_ids=())
        down_id = {r["id"]: r for r in down}
        self.assertFalse(down_id["qwen38-27b-q5"]["available"])
        self.assertIn("tunnel is down", down_id["qwen38-27b-q5"]["unavailable_reason"])
        with patch.object(d, "local_llm_port_open", return_value=True):
            d._start_independent_local("qwen38-27b-q5", tbl)
        with self.assertRaises(ValueError) as ctx:
            with patch.object(d, "local_llm_port_open", return_value=False):
                d._start_independent_local("qwen38-27b-q5", tbl)
        self.assertIn("tunnel is down", str(ctx.exception))

    def test_flash_next_independent_of_vllm_occupancy(self) -> None:
        catalog = {
            "qwen38-flash-next": {
                "model": "Qwen3.8-Flash-Next",
                "name": "Qwen 3.8 Flash-Next",
                "base_url": "http://127.0.0.1:8080/v1",
                "weights": "/home/roni/models/Qwen3.8-Flash-Next",
            },
            "qwen38-27b": CATALOG["qwen38-27b"],
            "grok-4.6": CATALOG["grok-4.6"],
        }
        picker = [
            {"id": "qwen38-flash-next", "name": "Qwen 3.8 Flash-Next"},
            {"id": "qwen38-27b", "name": "Qwen 3.8 27B Local"},
            {"id": "grok-4.6", "name": "Grok 4.6"},
        ]
        with patch.object(d, "local_llm_port_open", return_value=True):
            rows = d.annotate_model_availability(picker, catalog, live_ids=("qwen38",))
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["qwen38-flash-next"]["available"])
        self.assertEqual(by_id["qwen38-flash-next"]["family"], "llamacpp")
        self.assertTrue(by_id["qwen38-27b"]["available"])
        self.assertTrue(by_id["grok-4.6"]["available"])

    def test_grok_build_apply_models_keeps_cloud(self) -> None:
        bot = d.Bot("b_picker00002", "P", "", "", "grok-4.6", "x", kind="hermes")
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", LOCAL_ONLY)):
            with patch.object(d, "probe_local_llm_ids", return_value=("qwen38",)):
                bot.apply_models(
                    {
                        "currentModelId": "grok-4.6",
                        "availableModels": [
                            {"modelId": "grok-4.6", "name": "Grok 4.6"},
                            {"modelId": "grok-4.5", "name": "Grok 4.5"},
                            {"modelId": "grok-code-fast-1", "name": "Hermes Code Fast 1"},
                        ],
                    }
                )
        ids = [m["id"] for m in bot.models]
        self.assertIn("qwen38-27b", ids)
        self.assertIn("muse-glimmer", ids)
        self.assertIn("grok-4.6", ids)
        self.assertIn("grok-4.5", ids)
        self.assertIn("grok-code-fast-1", ids)

    def test_grok_build_can_select_cloud_outside_config(self) -> None:
        bot = d.Bot("b_picker00003", "P", "", "", "qwen38-27b", "x", kind="hermes")
        with patch.object(d, "load_user_models", return_value=("qwen38-27b", LOCAL_ONLY)):
            d.ensure_model_on_host("grok-code-fast-1", bot=bot)
            d.ensure_model_on_host("grok-4.5", bot=bot)
            d.ensure_model_on_host("grok-4.6", bot=bot)

    def test_grok_build_can_select_flash_next_when_down(self) -> None:
        bot = d.Bot("b_picker00004", "P", "", "", "grok-4.6", "x", kind="hermes")
        catalog = {
            "qwen38-flash-next": {
                "base_url": "http://127.0.0.1:8080/v1",
                "weights": "/models/Qwen3.8-Flash-Next",
            },
            "grok-4.6": {"model": "grok-4.6", "api_backend": "responses"},
        }
        with patch.object(d, "load_user_models", return_value=("grok-4.6", catalog)):
            with patch.object(d, "local_llm_port_open", return_value=False):
                d.ensure_model_on_host("qwen38-flash-next", bot=bot)
                d.ensure_model_on_host("grok-4.6", bot=bot)

    def test_register_user_model_adds_picker_row(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        models = home / "models"
        weights = models / "tiny"
        weights.mkdir(parents=True)
        (weights / "model.gguf").write_bytes(b"gguf")
        orig_home, orig_models, orig_desk = d.USER_AGENT_HOME, d.MODELS_DIR, os.environ.get("HERMES_DESK_HOME")
        d.USER_AGENT_HOME = home
        d.MODELS_DIR = models
        os.environ["HERMES_DESK_HOME"] = str(home)
        try:
            (home / "config.toml").write_text(
                '[models]\ndefault = "grok-4.6"\n\n[model."grok-4.6"]\nmodel = "grok-4.6"\nname = "Grok 4.6"\n',
                encoding="utf-8",
            )
            with patch.object(d, "refresh_host_model_catalog", return_value=("tiny-model", [{"id": "tiny-model"}])):
                out = d.register_user_model({"id": "Tiny Model", "weights": str(weights), "name": "Tiny"})
            self.assertEqual(out["id"], "tiny-model")
            text = (home / "models.json").read_text(encoding="utf-8")
            self.assertIn("tiny-model", text)
            self.assertIn("127.0.0.1:808", text)
            self.assertIn("weights", text)
        finally:
            d.USER_AGENT_HOME = orig_home
            d.MODELS_DIR = orig_models
            if orig_desk is None:
                os.environ.pop("HERMES_DESK_HOME", None)
            else:
                os.environ["HERMES_DESK_HOME"] = orig_desk
            tmp.cleanup()

    def test_stop_flash_next_is_independent(self) -> None:
        catalog = {
            "qwen38-flash-next": {
                "base_url": "http://127.0.0.1:8080/v1",
                "weights": "/models/Qwen3.8-Flash-Next",
            }
        }
        with patch.object(d, "load_user_models", return_value=("qwen38-flash-next", catalog)):
            with patch.object(d, "_stop_independent_local", return_value={"ok": True}) as indep:
                with patch.object(d, "_stop_exclusive_gpu") as excl:
                    d.local_llm_stop("qwen38-flash-next")
                    indep.assert_called_once()
                    excl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
