"""Context-window and generation-speed telemetry for Hermes Desk.

Hermes Agent / ACP is the source of truth. This module only interprets payloads
already emitted by the runtime; it does not invent billed usage.
"""

from __future__ import annotations

import re
from typing import Any

# Built-in Hermes Agent defaults. Used only when ACP / user model config omit a window.
KNOWN_CONTEXT_WINDOWS = {
    "grok-4.6": 500000,
    "grok-4.5": 256000,
    "qwen38-27b": 32768,
    "qwen38-27b-q4": 262144,
    "qwen38-27b-q5": 262144,
    "qwen3-vl-8b": 32768,
    "muse-glimmer": 131072,
}

_LEDGER_KEYS = {
    "modelCalls",
    "model_calls",
    "apiDurationMs",
    "api_duration_ms",
    "numTurns",
    "num_turns",
    "costUsdTicks",
    "reasoningTokens",
    "reasoning_tokens",
    "cachedReadTokens",
    "cached_read_tokens",
    "cacheCreationTokens",
    "cache_creation_tokens",
}

# GPT-style word/punct split used only when Hermes does not report output tokens
# for the current generation. Not the official Hermes vocabulary.
_TOKEN_RE = re.compile(
    r"'(?:[sS]|[mM]|[dD]|ll|ve|re)|[A-Za-z]+|\d{1,3}|[^\sA-Za-z0-9]+|\s+",
)


def resolve_context_window(
    model: str,
    *candidates: Any,
) -> int:
    """Pick the first positive window. Prefer runtime/user values over builtins."""
    for value in candidates:
        n = _as_int(value)
        if n and n > 0:
            return n
    return int(KNOWN_CONTEXT_WINDOWS.get(model, 0) or 0)


def context_percentage(used: int | float, window: int | float) -> float:
    try:
        u = float(used)
        w = float(window)
    except (TypeError, ValueError):
        return 0.0
    if w <= 0 or u < 0:
        return 0.0
    return max(0.0, min(100.0, (u / w) * 100.0))


def is_usage_ledger(blob: Any) -> bool:
    """True for turn-level billed usage, not a live window occupancy sample."""
    if not isinstance(blob, dict) or not blob:
        return False
    keys = set(blob)
    if keys & _LEDGER_KEYS:
        return True
    has_in = bool(keys & {"inputTokens", "input_tokens"})
    has_out = bool(keys & {"outputTokens", "output_tokens"})
    return has_in and has_out


def unwrap_usage(blob: Any) -> dict[str, Any]:
    if not isinstance(blob, dict):
        return {}
    inner = blob.get("usage")
    if isinstance(inner, dict) and inner:
        return inner
    return blob


def context_tokens_from_payload(blob: Any) -> tuple[int | None, str]:
    """Current context occupancy from a Hermes Agent payload.

    Prefers explicit context fields, then `_meta.totalTokens` (live window).
    Turn ledgers with modelCalls > 1 sum every tool-loop prompt and must not
    be used as the current window. A single-call ledger's totalTokens is the
    prompt+completion of that call, which *is* the window after the turn.
    """
    if not isinstance(blob, dict) or not blob:
        return None, ""
    inner = unwrap_usage(blob)
    for src in (inner, blob):
        n = _as_int(
            src.get("contextTokensUsed")
            or src.get("context_tokens")
            or src.get("contextTokens")
            or src.get("context_window_tokens_used")
        )
        if n is not None:
            return n, "agent_runtime"
    # Billed turn ledgers (input+output, possibly summed across tool-loop
    # modelCalls) are not the live window. Hermes's occupancy is _meta.totalTokens
    # / contextTokensUsed, which matches signals.json.
    if is_usage_ledger(inner) or is_usage_ledger(blob):
        return None, ""
    # ACP session/update _meta carries live occupancy as totalTokens *during a
    # stream* (streamStartMs present). That value matches signals.json
    # contextTokensUsed. eventId alone is not enough: prompt_complete/_meta
    # copies billed totals under the same key.
    prompt = _as_int(inner.get("prompt_tokens") or inner.get("promptTokens") or blob.get("prompt_tokens"))
    completion = _as_int(
        inner.get("completion_tokens") or inner.get("completionTokens") or blob.get("completion_tokens")
    )
    if prompt is not None or completion is not None:
        total = _as_int(inner.get("total_tokens") or inner.get("totalTokens") or blob.get("total_tokens"))
        if total is None:
            total = (prompt or 0) + (completion or 0)
        if total:
            return total, "local_runtime"
    n = _as_int(blob.get("totalTokens") or blob.get("total_tokens"))
    # Live occupancy is on streamed content _meta (chunkId present, matching
    # signals.contextTokensUsed). Later updates reuse streamStartMs but stamp
    # billed usage.totalTokens into the same field.
    chunk_like = blob.get("chunkId") is not None or str(blob.get("updateType") or "") in {
        "agent_message_chunk",
        "agent_thought_chunk",
        "user_message_chunk",
        "AgentMessageChunk",
        "AgentThoughtChunk",
        "UserMessageChunk",
        "MessageChunk",
        "ThoughtChunk",
    }
    if n is not None and blob.get("streamStartMs") is not None and chunk_like:
        return n, "agent_runtime"
    return None, ""


def ledger_stats(blob: Any) -> dict[str, Any]:
    inner = unwrap_usage(blob)
    if not inner:
        return {}
    out = {
        "input_tokens": _as_int(
            inner.get("inputTokens")
            or inner.get("input_tokens")
            or inner.get("prompt_tokens")
            or inner.get("promptTokens")
        ),
        "output_tokens": _as_int(
            inner.get("outputTokens")
            or inner.get("output_tokens")
            or inner.get("completion_tokens")
            or inner.get("completionTokens")
        ),
        "reasoning_tokens": _as_int(inner.get("reasoningTokens") or inner.get("reasoning_tokens")),
        "total_tokens": _as_int(inner.get("totalTokens") or inner.get("total_tokens")),
        "model_calls": _as_int(inner.get("modelCalls") or inner.get("model_calls")),
        "api_duration_ms": _as_float(inner.get("apiDurationMs") or inner.get("api_duration_ms")),
        "cached_read_tokens": _as_int(inner.get("cachedReadTokens") or inner.get("cached_read_tokens") or inner.get("cache_read_input_tokens")),
    }
    if not any(v is not None for v in out.values()):
        return {}
    return out


def visible_output_tokens(ledger: dict[str, Any] | None) -> tuple[int | None, str]:
    """Model output tokens for the tok/s meter.

    Prefer Hermes's outputTokens. Reasoning is reported separately; subtract it
    so the meter tracks visible completion rather than thought tokens.
    """
    if not ledger:
        return None, ""
    out = ledger.get("output_tokens")
    if out is None:
        return None, ""
    reason = ledger.get("reasoning_tokens") or 0
    visible = int(out)
    if reason and 0 < int(reason) < visible:
        visible = visible - int(reason)
    return max(0, visible), "agent_runtime"


def count_tokens_local(text: str) -> int:
    """Fallback token count when Hermes Agent does not report output tokens."""
    s = text or ""
    if not s:
        return 0
    n = 0
    for part in _TOKEN_RE.findall(s):
        if not part or part.isspace():
            if "\n" in part:
                n += part.count("\n")
            continue
        if part.isalpha() and len(part) > 8:
            n += max(1, (len(part) + 3) // 4)
        elif _is_cjk_run(part):
            n += max(1, len(part))
        else:
            n += 1
    extra = sum(1 for ch in s if _is_cjk_char(ch))
    return max(1, n + extra) if s.strip() else 0


def generation_metrics(
    *,
    output_tokens: int,
    token_source: str,
    first_out_ms: float | None,
    last_out_ms: float | None,
    stream_start_ms: float | None,
    turn_start_ms: float | None,
    api_duration_ms: float | None = None,
    model_calls: int | None = None,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    ttft_ms = None
    if first_out_ms is not None and stream_start_ms is not None:
        ttft_ms = max(0.0, float(first_out_ms) - float(stream_start_ms))
    elif first_out_ms is not None and turn_start_ms is not None:
        ttft_ms = max(0.0, float(first_out_ms) - float(turn_start_ms))

    chunk_ms: float | None = None
    if first_out_ms is not None and last_out_ms is not None and (last_out_ms - first_out_ms) >= 50:
        chunk_ms = float(last_out_ms) - float(first_out_ms)

    wall_ms: float | None = None
    if elapsed_ms and float(elapsed_ms) > 0:
        wall_ms = float(elapsed_ms)
    elif api_duration_ms and (model_calls is None or int(model_calls) <= 1) and float(api_duration_ms) > 0:
        wall_ms = float(api_duration_ms)

    # Decode speed is first visible token → last visible token.
    # Full wall includes prefill/thinking and under-reports tok/s on local Qwen.
    # ACP sometimes dumps the whole completion in one short burst after a long wait;
    # treat a short-span burst as implausible GPU decode and fall back to time after
    # first token. Long fast streams (>= 1s) are plausible and keep their rate.
    stream_tps = None
    if chunk_ms and chunk_ms > 0 and output_tokens:
        stream_tps = float(output_tokens) / (chunk_ms / 1000.0)
    implausible_burst = bool(
        stream_tps is not None and stream_tps > 150 and (chunk_ms or 0.0) < 1000.0
    )

    gen_ms: float | None = None
    speed_source = ""
    decode_ms = None
    if wall_ms is not None and ttft_ms is not None and wall_ms > ttft_ms + 80:
        decode_ms = float(wall_ms) - float(ttft_ms)
    if chunk_ms is not None and not implausible_burst:
        gen_ms = chunk_ms
        speed_source = "stream_measurement"
    elif decode_ms is not None:
        gen_ms = decode_ms
        speed_source = "stream_measurement"
    elif wall_ms is not None:
        gen_ms = wall_ms
        speed_source = "agent_runtime"
    elif stream_start_ms is not None and last_out_ms is not None and last_out_ms > stream_start_ms:
        gen_ms = float(last_out_ms) - float(stream_start_ms)
        speed_source = "stream_measurement"

    tps = 0.0
    if output_tokens and gen_ms and gen_ms > 0:
        tps = float(output_tokens) / (gen_ms / 1000.0)
    if tps < 0 or tps != tps or tps == float("inf"):  # noqa: PLR0124
        tps = 0.0
    return {
        "output_tokens": int(output_tokens or 0),
        "token_source": token_source or "",
        "first_token_at": first_out_ms,
        "last_token_at": last_out_ms,
        "ttft_ms": ttft_ms,
        "generation_ms": gen_ms,
        "generation_tok_s": tps,
        "speed_source": speed_source,
        "stream_start_ms": stream_start_ms,
        "turn_start_ms": turn_start_ms,
        "api_duration_ms": api_duration_ms,
        "model_calls": model_calls,
    }


class LocalStreamSpeed:
    """Decode speed for local-engine SSE streams (real per-token timing).

    ACP chunk timing for local models is batched (one snapshot per model call),
    so tok/s must come from the proxy path that sees every token delta.
    """

    MIN_SPAN_S = 0.4
    LIVE_MIN_SPAN_S = 1.0
    LIVE_PERIOD_S = 1.0

    def __init__(self) -> None:
        self.first_t: float | None = None
        self.last_t: float | None = None
        self.tokens = 0
        self._last_live_t: float | None = None

    def record(self, token_count: int, t: float) -> float | None:
        """Note a token delta at wall time t. Returns a live tok/s when due."""
        n = int(token_count or 0)
        if n <= 0:
            return None
        if self.first_t is None:
            self.first_t = t
        self.last_t = t
        self.tokens += n
        span = t - self.first_t
        if span < self.LIVE_MIN_SPAN_S:
            return None
        if self._last_live_t is not None and (t - self._last_live_t) < self.LIVE_PERIOD_S:
            return None
        self._last_live_t = t
        return float(self.tokens) / span

    def final(self) -> float | None:
        """Decode tok/s over first → last token, or None when the span is too short."""
        if self.first_t is None or self.last_t is None or self.tokens < 2:
            return None
        span = float(self.last_t) - float(self.first_t)
        if span < self.MIN_SPAN_S:
            return None
        return float(self.tokens) / span


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_cjk_char(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF
        or 0x3400 <= o <= 0x4DBF
        or 0x4E00 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xAC00 <= o <= 0xD7AF
    )


def _is_cjk_run(part: str) -> bool:
    return bool(part) and all(_is_cjk_char(ch) for ch in part)
