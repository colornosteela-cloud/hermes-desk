#!/usr/bin/env python3
"""Context meter and tok/s telemetry — occupancy vs billed usage, speed formula."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import telemetry as t  # noqa: E402


class ContextPayloadTests(unittest.TestCase):
    def test_meta_total_tokens_is_live_window(self) -> None:
        n, src = t.context_tokens_from_payload({"totalTokens": 21502, "streamStartMs": 1, "chunkId": "c1"})
        self.assertEqual(n, 21502)
        self.assertEqual(src, "agent_runtime")

    def test_explicit_context_tokens_used(self) -> None:
        n, src = t.context_tokens_from_payload({"contextTokensUsed": 79854, "contextWindowTokens": 500000})
        self.assertEqual(n, 79854)
        self.assertEqual(src, "agent_runtime")

    def test_multi_call_ledger_is_not_current_context(self) -> None:
        n, src = t.context_tokens_from_payload(
            {
                "inputTokens": 67135,
                "outputTokens": 760,
                "totalTokens": 67895,
                "modelCalls": 4,
                "apiDurationMs": 23103,
            }
        )
        self.assertIsNone(n)
        self.assertEqual(src, "")

    def test_single_call_ledger_is_not_current_context(self) -> None:
        n, src = t.context_tokens_from_payload(
            {
                "inputTokens": 15408,
                "outputTokens": 72,
                "totalTokens": 15480,
                "modelCalls": 1,
                "apiDurationMs": 17262,
            }
        )
        self.assertIsNone(n)
        self.assertEqual(src, "")

    def test_llama_openai_usage_is_local_occupancy(self) -> None:
        n, src = t.context_tokens_from_payload(
            {"prompt_tokens": 812, "completion_tokens": 44, "total_tokens": 856}
        )
        self.assertEqual(n, 856)
        self.assertEqual(src, "local_runtime")
        ledger = t.ledger_stats({"prompt_tokens": 812, "completion_tokens": 44, "total_tokens": 856})
        self.assertEqual(ledger.get("input_tokens"), 812)
        self.assertEqual(ledger.get("output_tokens"), 44)

    def test_bare_total_tokens_without_meta_is_ignored(self) -> None:
        n, src = t.context_tokens_from_payload({"totalTokens": 5366})
        self.assertIsNone(n)
        self.assertEqual(src, "")

    def test_eventid_only_total_tokens_is_ignored(self) -> None:
        n, src = t.context_tokens_from_payload({"totalTokens": 15543, "eventId": "prompt-complete"})
        self.assertIsNone(n)
        self.assertEqual(src, "")

    def test_post_turn_meta_with_stream_but_no_chunk_is_ignored(self) -> None:
        n, src = t.context_tokens_from_payload(
            {
                "totalTokens": 15431,
                "streamStartMs": 1,
                "updateType": "ToolCallUpdate",
                "updateParams": {},
                "eventId": "e",
            }
        )
        self.assertIsNone(n)
        self.assertEqual(src, "")

    def test_compaction_may_decrease(self) -> None:
        first, _ = t.context_tokens_from_payload({"totalTokens": 80000, "streamStartMs": 1, "chunkId": "a"})
        later, _ = t.context_tokens_from_payload({"totalTokens": 12000, "streamStartMs": 2, "chunkId": "b"})
        self.assertEqual(first, 80000)
        self.assertEqual(later, 12000)
        self.assertLess(later, first)


class WindowAndBarTests(unittest.TestCase):
    def test_known_grok_46_window(self) -> None:
        self.assertEqual(t.resolve_context_window("grok-4.6"), 500000)

    def test_runtime_window_wins_over_builtin(self) -> None:
        self.assertEqual(t.resolve_context_window("grok-4.6", 256000), 256000)

    def test_percentage_example(self) -> None:
        pct = t.context_percentage(79854, 500000)
        self.assertAlmostEqual(pct, 15.9708, places=4)

    def test_percentage_clamped(self) -> None:
        self.assertEqual(t.context_percentage(-10, 100), 0.0)
        self.assertEqual(t.context_percentage(200, 100), 100.0)
        self.assertEqual(t.context_percentage(50, 0), 0.0)


class GenerationSpeedTests(unittest.TestCase):
    def test_first_to_last_chunk_excludes_ttft(self) -> None:
        m = t.generation_metrics(
            output_tokens=742,
            token_source="agent_runtime",
            first_out_ms=1000,
            last_out_ms=1000 + 9405,
            stream_start_ms=387,
            turn_start_ms=0,
        )
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["ttft_ms"], 613.0)
        self.assertAlmostEqual(m["generation_ms"], 9405.0)
        self.assertAlmostEqual(m["generation_tok_s"], 742 / 9.405, places=2)

    def test_oneshot_chunk_uses_stream_start(self) -> None:
        m = t.generation_metrics(
            output_tokens=14,
            token_source="tokenizer",
            first_out_ms=1705610,
            last_out_ms=1705610,
            stream_start_ms=1705452,
            turn_start_ms=1699715,
        )
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["generation_ms"], 158.0)
        self.assertGreater(m["generation_tok_s"], 0)

    def test_coalesced_chunks_use_time_after_first_token(self) -> None:
        # ACP dumped 40 tokens in 229ms after a 32s wait. Burst tok/s is not GPU decode.
        # Meter should be time after first token, not the whole wait and not the 229ms dump.
        m = t.generation_metrics(
            output_tokens=40,
            token_source="agent_runtime",
            first_out_ms=32000,
            last_out_ms=32229,
            stream_start_ms=0,
            turn_start_ms=0,
            api_duration_ms=32408,
            elapsed_ms=32458,
        )
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["generation_ms"], 458.0)
        self.assertGreater(m["generation_tok_s"], 50.0)
        self.assertLess(m["generation_tok_s"], 120.0)

    def test_local_prefill_does_not_dilute_decode_tps(self) -> None:
        m = t.generation_metrics(
            output_tokens=50,
            token_source="local_runtime",
            first_out_ms=8000,
            last_out_ms=9200,
            stream_start_ms=0,
            turn_start_ms=0,
            elapsed_ms=9300,
        )
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["generation_ms"], 1200.0)
        self.assertAlmostEqual(m["generation_tok_s"], 50 / 1.2, places=2)

    def test_rejects_nan_inf(self) -> None:
        m = t.generation_metrics(
            output_tokens=10,
            token_source="tokenizer",
            first_out_ms=None,
            last_out_ms=None,
            stream_start_ms=None,
            turn_start_ms=None,
        )
        self.assertEqual(m["generation_tok_s"], 0.0)

    def test_fast_long_stream_is_not_a_burst(self) -> None:
        # 200 tok/s over 2s of real decode is plausible; the >150 rule must only
        # reject short dump bursts, not sustained fast streams.
        m = t.generation_metrics(
            output_tokens=400,
            token_source="agent_runtime",
            first_out_ms=1000,
            last_out_ms=3000,
            stream_start_ms=0,
            turn_start_ms=0,
        )
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["generation_ms"], 2000.0)
        self.assertAlmostEqual(m["generation_tok_s"], 200.0, places=1)

    def test_short_dump_burst_still_falls_back(self) -> None:
        m = t.generation_metrics(
            output_tokens=3000,
            token_source="agent_runtime",
            first_out_ms=9000,
            last_out_ms=9100,
            stream_start_ms=0,
            turn_start_ms=0,
            elapsed_ms=9600,
        )
        # 30k tok/s over 100ms is a coalesced dump, not GPU decode.
        self.assertEqual(m["speed_source"], "stream_measurement")
        self.assertAlmostEqual(m["generation_ms"], 600.0)
        self.assertAlmostEqual(m["generation_tok_s"], 3000 / 0.6, places=1)


class LocalStreamSpeedTests(unittest.TestCase):
    def test_live_rate_appears_after_one_second(self) -> None:
        m = t.LocalStreamSpeed()
        self.assertIsNone(m.record(5, 0.0))
        self.assertIsNone(m.record(5, 0.5))
        self.assertAlmostEqual(m.record(10, 1.0), 20.0)

    def test_live_rate_is_throttled(self) -> None:
        m = t.LocalStreamSpeed()
        m.record(10, 0.0)
        m.record(10, 1.0)
        self.assertIsNone(m.record(5, 1.4))
        self.assertAlmostEqual(m.record(5, 2.2), 30 / 2.2, places=2)

    def test_final_needs_span_and_tokens(self) -> None:
        m = t.LocalStreamSpeed()
        self.assertIsNone(m.final())
        m.record(1, 0.0)
        m.record(1, 0.3)
        self.assertIsNone(m.final())  # 0.3s < 0.4s minimum span
        m.record(1, 0.6)
        self.assertAlmostEqual(m.final(), 3 / 0.6, places=2)

    def test_zero_count_ignored(self) -> None:
        m = t.LocalStreamSpeed()
        self.assertIsNone(m.record(0, 0.0))
        self.assertIsNone(m.final())

    def test_visible_output_subtracts_reasoning(self) -> None:
        n, src = t.visible_output_tokens({"output_tokens": 49, "reasoning_tokens": 35})
        self.assertEqual(n, 14)
        self.assertEqual(src, "agent_runtime")

    def test_local_tokenizer_is_not_char_div_four(self) -> None:
        text = "Hi. What would you like to work on?"
        n = t.count_tokens_local(text)
        self.assertGreater(n, 4)
        self.assertLess(n, len(text) / 2)
        self.assertNotEqual(n, len(text) // 4)


class BotIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        import deskd as d
        self.bot = d.Bot("b_tel", "Tel", "", "", "grok-4.6", "🤖")

    def test_default_window_is_grok_46(self) -> None:
        self.assertEqual(self.bot.context_window, 500000)

    def test_multi_call_ledger_does_not_replace_live_window(self) -> None:
        self.bot.ingest_usage({"totalTokens": 21502, "streamStartMs": 1, "chunkId": "c1"})
        self.assertEqual(self.bot.context_used, 21502)
        self.bot.ingest_usage(
            {
                "inputTokens": 67135,
                "outputTokens": 760,
                "totalTokens": 67895,
                "modelCalls": 4,
                "apiDurationMs": 23103,
            }
        )
        self.assertEqual(self.bot.context_used, 21502)

    def test_compaction_updates_down(self) -> None:
        self.bot.ingest_usage({"totalTokens": 80000, "streamStartMs": 1, "chunkId": "a"})
        self.bot.ingest_usage({"totalTokens": 12000, "streamStartMs": 2, "chunkId": "b"})
        self.assertEqual(self.bot.context_used, 12000)

    def test_single_call_ledger_does_not_replace_live_window(self) -> None:
        self.bot.ingest_usage({"totalTokens": 2534, "streamStartMs": 1, "chunkId": "c"})
        self.bot.ingest_usage(
            {
                "inputTokens": 15408,
                "outputTokens": 72,
                "totalTokens": 15480,
                "modelCalls": 1,
                "apiDurationMs": 17262,
            }
        )
        self.assertEqual(self.bot.context_used, 2534)

    def test_record_local_generation_fills_meters(self) -> None:
        self.bot.record_local_generation(
            "Hello there, I'm Teela.",
            {"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420},
            started_ms=1000.0,
            ended_ms=2000.0,
        )
        self.assertEqual(self.bot.context_used, 420)
        self.assertEqual(self.bot.context_source, "local_runtime")
        self.assertGreater(self.bot.tps, 0)
        self.assertEqual(self.bot.speed_source, "local_runtime")

    def test_llama_timings_set_tok_s_and_survive_finish(self) -> None:
        import deskd as d

        d.apply_llama_generation_speed(
            self.bot,
            {
                "usage": {"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420},
                "timings": {"predicted_n": 20, "predicted_ms": 250.0, "predicted_per_second": 80.0},
            },
            elapsed_ms=10.0,
        )
        self.assertEqual(self.bot.tps, 80.0)
        self.assertEqual(self.bot.speed_source, "local_runtime")
        self.bot.record_local_generation(
            "Hello there, I'm Teela.",
            {"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420},
            started_ms=1000.0,
            ended_ms=2000.0,
        )
        self.assertEqual(self.bot.tps, 80.0)
        self.assertEqual(self.bot.speed_source, "local_runtime")

    def test_canned_reply_does_not_shrink_context(self) -> None:
        self.bot.record_local_generation(
            "A longer local reply that filled the window.",
            {"prompt_tokens": 1500, "completion_tokens": 40, "total_tokens": 1540},
            started_ms=0.0,
            ended_ms=2000.0,
        )
        self.assertEqual(self.bot.context_used, 1540)
        prev_tps = self.bot.tps
        self.bot.record_local_generation("I'm waving.", started_ms=5000.0, ended_ms=5010.0)
        self.assertEqual(self.bot.context_used, 1540)
        self.assertEqual(self.bot.tps, prev_tps)

    def test_oneshot_generation_speed(self) -> None:
        self.bot.note_generation_chunk(
            "Hi. What would you like to work on?",
            {"streamStartMs": 1000, "agentTimestampMs": 1158, "turnStartMs": 0, "totalTokens": 2154},
        )
        self.assertGreater(self.bot.tps, 0)
        self.assertEqual(self.bot.speed_source, "stream_measurement")
        snap = self.bot.telemetry_snapshot()
        self.assertIn(snap["token_source"], ("tokenizer", "agent_runtime"))
        self.assertAlmostEqual(snap["ttft_ms"], 158.0)


class _SeedFakeBot:
    """Minimal stand-in so _seed_usage_from_disk can run against a temp tree."""

    def __init__(self, home: Path, sid: str | None) -> None:
        self.agent_home = home
        self.acp = type("Acp", (), {"session_id": sid})()
        self.context_used = 0
        self.context_window = 500000
        self.context_source = ""
        self.emitted: list[int] = []

    def _emit_usage(self, log: bool = False) -> None:
        self.emitted.append(self.context_used)

    def ingest_usage(self, blob: dict) -> None:
        n, src = t.context_tokens_from_payload(blob)
        if n is not None:
            self.context_used = n
            self.context_source = src


class SeedUsageTests(unittest.TestCase):
    def _write_signals(self, home: Path, sid: str, used: int, mtime: float) -> Path:
        import sqlite3

        # Hermes persists turns to <HERMES_HOME>/state.db (sessions rows carry
        # token counts); the seed reads that table.
        db = home / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "create table if not exists sessions ("
            "id text primary key, last_activity_at real, input_tokens integer, "
            "output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,"
            " reasoning_tokens integer, model text)"
        )
        con.execute(
            "insert or replace into sessions (id, last_activity_at, input_tokens) "
            "values (?,?,?)",
            (sid, mtime, used),
        )
        con.commit()
        con.close()
        return db

    def test_fresh_session_falls_back_to_newest_runtime_value(self) -> None:
        import deskd as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._write_signals(home, "old-sid", 48275, 1000.0)
            fake = _SeedFakeBot(home, "new-sid")
            d.Bot._seed_usage_from_disk(fake)
            self.assertEqual(fake.context_used, 48275)
            self.assertEqual(fake.context_source, "agent_runtime")
            self.assertEqual(fake.emitted, [48275])

    def test_sid_match_wins_over_newer_foreign_session(self) -> None:
        import deskd as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._write_signals(home, "sid-a", 30000, 2000.0)  # newer mtime
            self._write_signals(home, "sid-b", 48275, 1000.0)
            fake = _SeedFakeBot(home, "sid-b")
            d.Bot._seed_usage_from_disk(fake)
            # sid-b match wins over the newer foreign sid-a.
            self.assertEqual(fake.context_used, 48275)

    def test_no_sid_still_seeds_from_newest(self) -> None:
        import deskd as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._write_signals(home, "old-sid", 22313, 1000.0)
            fake = _SeedFakeBot(home, None)
            d.Bot._seed_usage_from_disk(fake)
            self.assertEqual(fake.context_used, 22313)

    def test_no_files_keeps_prior_value(self) -> None:
        import deskd as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake = _SeedFakeBot(home, "new-sid")
            d.Bot._seed_usage_from_disk(fake)
            self.assertEqual(fake.context_used, 0)
            self.assertEqual(fake.emitted, [])

    def test_tokenless_newest_session_keeps_zero(self) -> None:
        import deskd as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self._write_signals(home, "old-sid", 0, 1000.0)  # zero-token session
            fake = _SeedFakeBot(home, "new-sid")
            d.Bot._seed_usage_from_disk(fake)
            self.assertEqual(fake.context_used, 0)
            self.assertEqual(fake.emitted, [])


class MeterRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        import deskd as d
        self.bot = d.Bot("b_tel", "Tel", "", "", "grok-4.6", "🤖")

    def test_reset_keeps_last_speed_and_context(self) -> None:
        self.bot.context_used = 33734
        self.bot.context_source = "agent_runtime"
        self.bot.tps = 42.0
        self.bot.speed_source = "local_stream"
        self.bot.token_source = "tokenizer"
        self.bot._gen = {"stream_start_ms": 1}
        self.bot.telemetry = {"x": 1}
        self.bot.reset_telemetry()
        self.assertEqual(self.bot.context_used, 33734)
        self.assertEqual(self.bot.tps, 42.0)
        self.assertEqual(self.bot.speed_source, "local_stream")
        self.assertEqual(self.bot.token_source, "tokenizer")
        self.assertEqual(self.bot._gen, {})
        self.assertEqual(self.bot.telemetry, {})

    def test_empty_stream_final_keeps_last_speed(self) -> None:
        # A thought-only stream (no visible tokens) must not zero the meter.
        self.bot.tps = 33.0
        self.bot.speed_source = "local_stream"
        self.bot.note_generation_chunk(
            "",
            {"streamStartMs": 1000, "agentTimestampMs": 1200, "turnStartMs": 0},
            timing_only=True,
        )
        self.bot.finish_generation(None, None, None)
        self.assertEqual(self.bot.tps, 33.0)

    def test_local_engine_acp_publish_does_not_touch_tps(self) -> None:
        import deskd as d

        _, catalog = d.load_user_models()
        local_id = next(
            (mid for mid, raw in catalog.items() if d.is_local_gpu_model(mid, raw)),
            None,
        )
        if not local_id:
            self.skipTest("no local engine in catalog")
        self.bot.model = local_id
        self.bot.note_generation_chunk(
            "Hello world, this is a test of the speed latch behavior.",
            {"streamStartMs": 1000, "agentTimestampMs": 1158, "turnStartMs": 0},
        )
        self.assertEqual(self.bot.tps, 0.0)
        self.assertEqual(self.bot.speed_source, "")

        # Cloud model: the same ACP measurement latches as before.
        self.bot.model = "grok-4.6"
        self.bot.note_generation_chunk(
            "Hello world, this is a test of the speed latch behavior.",
            {"streamStartMs": 2000, "agentTimestampMs": 2158, "turnStartMs": 0},
        )
        self.assertGreater(self.bot.tps, 0)
        self.assertEqual(self.bot.speed_source, "stream_measurement")

    def test_note_local_stream_speed_latches(self) -> None:
        self.bot.note_local_stream_speed(33.3, 100)
        self.assertAlmostEqual(self.bot.tps, 33.3)
        self.assertEqual(self.bot.speed_source, "local_stream")
        self.assertEqual(self.bot.token_source, "tokenizer")
        for bad in (0.0, -5.0, float("nan"), 99999.0):
            self.bot.note_local_stream_speed(bad, 10)
        self.assertAlmostEqual(self.bot.tps, 33.3)


class FrontendFormulaTests(unittest.TestCase):
    def test_ui_no_longer_uses_char_div_37(self) -> None:
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/ 3.7", app)
        self.assertNotIn("/3.7", app)


if __name__ == "__main__":
    unittest.main()
