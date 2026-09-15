"""Authoritative TTS/STT upstream URLs for Hermes Desk.

Production teela-brain talks to teela-body over LAN. Localhost remains the
dev fallback. Env aliases: TEELA_TTS_URL / TEELA_STT_URL and HERMES_DESK_TTS /
HERMES_DESK_STT. The teela-body IP lives only here.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REMOTE_TTS = "http://10.0.0.118:8090"
REMOTE_STT = "http://10.0.0.118:8091"
LOCAL_TTS = "http://127.0.0.1:8090"
LOCAL_STT = "http://127.0.0.1:8091"

TTS_TIMEOUT_S = 25.0
STT_TIMEOUT_S = 30.0
HEALTH_TIMEOUT_S = 2.0

_BODY_HOSTS = {"10.0.0.118", "teela-body"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _strip(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _env_url(*names: str) -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw and str(raw).strip():
            return _strip(str(raw))
    return ""


def _voice_local_override() -> bool:
    return os.environ.get("TEELA_VOICE_LOCAL", "").strip().lower() in {"1", "true", "yes"}


def is_teela_brain_host() -> bool:
    if _voice_local_override():
        return False
    if socket.gethostname().strip().lower() == "teela-brain":
        return True
    try:
        home = Path(os.environ.get("HERMES_DESK_HOME", str(Path.home() / ".hermes")))
        rec = json.loads((home / "desk.json").read_text(encoding="utf-8"))
        return str(rec.get("node_name") or "").strip().lower() == "teela-brain"
    except Exception:
        return False


def tts_url() -> str:
    return _env_url("TEELA_TTS_URL", "HERMES_DESK_TTS") or (
        REMOTE_TTS if is_teela_brain_host() else LOCAL_TTS
    )


def stt_url() -> str:
    return _env_url("TEELA_STT_URL", "HERMES_DESK_STT") or (
        REMOTE_STT if is_teela_brain_host() else LOCAL_STT
    )


def host_label(url: str) -> str:
    host = (urlparse(url).hostname or "").strip().lower()
    if host in _BODY_HOSTS:
        return "teela-body"
    if host in _LOCAL_HOSTS:
        return "localhost"
    return host or "unknown"


def fetch(
    method: str,
    url: str,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout: float | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout if timeout is not None else HEALTH_TIMEOUT_S)) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(e.code), body
    except Exception as e:
        return 502, json.dumps({"error": f"upstream unreachable: {e}"}).encode()


def parse_health_payload(code: int, raw: bytes) -> dict[str, Any]:
    if code == 200:
        try:
            rec = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            rec = {"ready": False, "error": "bad health payload"}
        if not isinstance(rec, dict):
            rec = {"ready": False, "error": "bad health payload"}
        return rec
    try:
        rec = json.loads(raw or b"{}")
        err = rec.get("error", "unreachable") if isinstance(rec, dict) else "unreachable"
    except json.JSONDecodeError:
        err = "unreachable"
    return {"ready": False, "error": str(err)}


def service_block(kind: str, code: int, raw: bytes) -> dict[str, Any]:
    url = tts_url() if kind == "tts" else stt_url()
    rec = parse_health_payload(code, raw)
    ready = bool(code == 200 and rec.get("ready") is True)
    out = dict(rec)
    out["host"] = host_label(url)
    out["status"] = "ready" if ready else "unavailable"
    out["ready"] = ready
    return out


def health_snapshot() -> dict[str, Any]:
    tts_code, tts_raw = fetch("GET", f"{tts_url()}/health", timeout=HEALTH_TIMEOUT_S)
    stt_code, stt_raw = fetch("GET", f"{stt_url()}/health", timeout=HEALTH_TIMEOUT_S)
    return {
        "tts": service_block("tts", tts_code, tts_raw),
        "stt": service_block("stt", stt_code, stt_raw),
    }
