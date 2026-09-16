#!/usr/bin/env python3
"""hermes-deskd — control plane for the graphical Hermes Agent MiniOS frontend.

GUI talks HTTP+SSE. This process owns bot registries, per-bot workspaces and
visual surfaces, while Hermes Agent remains the agent runtime through
`hermes acp` (ACP).
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import signal
import shutil
import ssl
import socket
import subprocess
import sys
import threading
import time
import tomllib
import types
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote_plus, urlparse, urlsplit

from session_mirror import SessionMirror
from surfaces import BrowserSurface, LocalSite, PtySurface, search_url, resolve_agent_bin, resolve_login_home, resolve_chrome_bin
import conv_io
import motion
import robot_sim
import telemetry as tel
import virtual_body
import action_orchestrator as orch
from embodiment.schema import BODY_ACTION_SCHEMA, JOINT_NAMES, SKILLS
import capability as teela_cap
from embodiment.service import EmbodimentService
from cluster import (
    Cluster,
    bot_id_from_path,
    cluster_token_fp,
    has_lan_peers,
    is_rfc1918_ipv4,
    normalize_cluster_token,
    timeout_for,
    validate_node_name,
    validate_peer_url,
)
import agent_home as agent_home_mod
import memory as botmem
import working_memory as wm
import context_manager as cm
import voice_upstream as vu

_LOGIN_HOME = resolve_login_home()
HERMES_BIN = resolve_agent_bin()
USER_AGENT_HOME = Path(os.environ.get("HERMES_DESK_HOME") or os.environ.get("HERMES_HOME") or str(_LOGIN_HOME / ".hermes"))
HERMES_DESKS = Path(os.environ.get("HERMES_DESKS", str(_LOGIN_HOME / "hermes-desks")))
MODELS_DIR = Path(os.environ.get("HERMES_DESK_MODELS", str(_LOGIN_HOME / "models")))
DESK_PORT = int(os.environ.get("HERMES_DESK_PORT", "8742"))
UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
SANDBOX = os.environ.get("HERMES_DESK_SANDBOX", "off")  # off | strict (prototype default: off)
def _llm_env(name: str, legacy: str, default: str) -> str:
    # The local-model upstream is not always vLLM (llama.cpp, etc.), so these
    # vars were renamed from HERMES_DESK_VLLM*. Legacy names are still honored
    # for existing units and launchers.
    val = os.environ.get(name)
    if val is None:
        val = os.environ.get(legacy)
    return val or default


LOCAL_LLM_UPSTREAM = _llm_env("HERMES_DESK_LLM", "HERMES_DESK_VLLM", "http://127.0.0.1:8000").rstrip("/")
LOCAL_LLM_FAST_UPSTREAM = _llm_env("HERMES_DESK_FAST", "HERMES_DESK_VLLM_FAST", "http://127.0.0.1:8001").rstrip("/")
LOCAL_LLM_SERVED = _llm_env("HERMES_DESK_MODEL", "HERMES_DESK_VLLM_MODEL", "qwen38")
LOCAL_LLM_FAST_SERVED = _llm_env("HERMES_DESK_FAST_MODEL", "HERMES_DESK_VLLM_FAST_MODEL", "qwen3-vl-8b")
LOCAL_LLM_ALIASES = {
    "qwen38-hybrid": "qwen38-hybrid",
    "qwen38-27b": LOCAL_LLM_SERVED,
    "qwen38": LOCAL_LLM_SERVED,
    "qwen38-flash-next": LOCAL_LLM_SERVED,
    "qwen3.8-flash-next": LOCAL_LLM_SERVED,
    "qwen3.8-27b": LOCAL_LLM_SERVED,
    "qwen3.8-27b-gptq-int4": LOCAL_LLM_SERVED,
    "qwen38-27b-q4": "Qwen3.8-27B",
    "qwen3.8-27b-q4": "Qwen3.8-27B",
    "qwen38-27b-q5": "Qwen3.8-27B",
    "qwen3.8-27b-q5": "Qwen3.8-27B",
    "qwen38-9b": LOCAL_LLM_FAST_SERVED,
    "qwen38-9b-distill": LOCAL_LLM_FAST_SERVED,
    "qwen3.8-9b": LOCAL_LLM_FAST_SERVED,
    "qwen3.8-9b-distill": LOCAL_LLM_FAST_SERVED,
    "qwen3-vl-8b": LOCAL_LLM_FAST_SERVED,
    "qwen3-vl": LOCAL_LLM_FAST_SERVED,
    "muse-glimmer": "muse-glimmer",
    "muse-glimmer-30b": "muse-glimmer",
}
_LOCAL_LLM_PORTS = (8000, 8001, 8080)
_VLLM_EXCLUSIVE_PORTS = {8000, 8001}
_LLAMA_CPP_PORTS = {8080, 8081}
# Hermes Agent will request remaining-context max_tokens (250k+). That hangs/kills
# Intel XPU GDN kernels. Cap completions; prefill is still the full prompt.
LOCAL_LLM_MAX_COMPLETION = int(_llm_env("HERMES_DESK_MAX_TOKENS", "HERMES_DESK_VLLM_MAX_TOKENS", "8192"))
# Live Intel start.sh clamps 27B to 32768. Catalog still advertises 262144.
LOCAL_LLM_MAX_MODEL_LEN = int(_llm_env("HERMES_DESK_MAX_LEN", "HERMES_DESK_VLLM_MAX_LEN", "32768"))
# llama.cpp hybrid prefill is ~65 tok/s. A 10k Hermes-ACP dump is ~2.5 minutes
# before the first generated token. Keep interactive prompts far smaller; the
# 262k window is for hard think, not MiniOS body turns.
LOCAL_LLM_PREFILL_BUDGET = int(_llm_env("HERMES_DESK_PREFILL", "HERMES_DESK_VLLM_PREFILL", "1536"))
LOCAL_LLM_MOTOR_PREFILL = int(_llm_env("HERMES_DESK_MOTOR_PREFILL", "HERMES_DESK_VLLM_MOTOR_PREFILL", "2048"))
# Hermes Agent ACP prompts carry ~16k tokens of tool schemas. The MiniOS 1536
# prefill dropped tool results and the local model called the same tools again.
LOCAL_LLM_CODING_PREFILL = int(_llm_env("HERMES_DESK_CODING_PREFILL", "HERMES_DESK_VLLM_CODING_PREFILL", "24576"))
# Hermes ACP throws Internal error / max_tokens_truncation on finish_reason=length.
# Never honor a tiny Hermes cap (we have seen 56). Motor replies must fit a tool call.
LOCAL_LLM_MIN_COMPLETION = int(_llm_env("HERMES_DESK_MIN_TOKENS", "HERMES_DESK_VLLM_MIN_TOKENS", "1024"))
LOCAL_LLM_MOTOR_MAX_TOKENS = int(_llm_env("HERMES_DESK_MOTOR_MAX_TOKENS", "HERMES_DESK_VLLM_MOTOR_MAX_TOKENS", "1024"))
_CTX_OVERFLOW_RE = re.compile(
    r"maximum context length is (\d+) tokens\..*?"
    r"requested (\d+) output tokens.*?"
    r"contains at least (\d+) input tokens",
    re.I | re.S,
)
# session/prompt: idle silence cap, not wall-clock. Tokens, tools, and thoughts
# refresh the wait so a 10+ minute coding turn is not killed while it is working.
# If the local engine is gone, fail in ACP_LOCAL_DOWN_GRACE_SEC instead of hanging.
ACP_PROMPT_MAX_SEC = 600
ACP_LOCAL_DOWN_GRACE_SEC = 12
# Qwen and Muse share the two Arc B60s. Only the currently served vLLM id is selectable.
_LLM_PROBE_LOCK = threading.Lock()
_LLM_PROBE_CACHE: tuple[float, dict[str, str]] = (0.0, {})
_LLM_PROBE_TTL = 2.0
_HYBRID_GATE = threading.Condition()
_HYBRID_INFLIGHT = {"fast": 0, "think": 0}

TOKEN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/hermes-desk-{os.getuid()}")) / "hermes-desk"
TOKEN_PATH = TOKEN_DIR / "token"
PID_PATH = TOKEN_DIR / "deskd.pid"
DESK_CONFIG_PATH = USER_AGENT_HOME / "desk.json"
_pid_lock_fh: Any = None

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = DESK_PORT
_httpd: ThreadingHTTPServer | None = None
_stop_http = threading.Event()
_rebind_http = threading.Event()

lock = threading.RLock()
bots: dict[str, "Bot"] = {}
subscribers: list[tuple[threading.Event, list[dict[str, Any]], Any]] = []
cluster: Cluster | None = None
_AGENT_CAP_CACHE: dict[str, Any] | None = None

UI_CLUSTER_HELPERS = {
    "/v1/cluster/self-test",
    "/v1/cluster/create",
    "/v1/cluster/peer-models",
}
UI_LOCAL_ONLY = {
    "/v1/cluster/token-new",
}
CLUSTER_KEYS = (
    "node_name",
    "cluster_token",
    "cluster_token_confirm",
    "cluster_token_clear",
    "peers",
    "peers_loaded",
)

# Voice stack: Chatterbox TTS + faster-whisper STT. URLs come from
# voice_upstream (TEELA_TTS_URL / TEELA_STT_URL, HERMES_DESK_* aliases).
# On teela-brain the default is teela-body over LAN, not a localhost tunnel.


class DeskHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def desk_config_path() -> Path:
    return DESK_CONFIG_PATH


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("", "127.0.0.1", "localhost", "::1")


def force_loopback() -> bool:
    return os.environ.get("HERMES_DESK_LOOPBACK", "").strip().lower() in ("1", "true", "yes", "on")


def lan_peers_configured() -> bool:
    return has_lan_peers(read_desk_file().get("peers"))


def detect_lan_ipv4() -> str:
    """Best-effort RFC1918 IPv4 for this host, or empty."""
    candidates: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            candidates.append(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip:
            candidates.append(host_ip)
    except OSError:
        pass
    for ip in candidates:
        if is_rfc1918_ipv4(ip):
            return ip
    return ""


def desired_listen_host(host: str) -> str:
    """Keep an explicit LAN address; promote loopback when LAN cluster peers exist."""
    if force_loopback():
        return host or "127.0.0.1"
    if not is_loopback_host(host):
        return host
    if not lan_peers_configured():
        return host or "127.0.0.1"
    return detect_lan_ipv4() or "0.0.0.0"


def bind_address(host: str) -> str:
    if force_loopback():
        return "127.0.0.1"
    if not is_loopback_host(host) or lan_peers_configured():
        return "0.0.0.0"
    return "127.0.0.1"


def lan_mode() -> bool:
    if force_loopback():
        return False
    if lan_peers_configured():
        return True
    return not is_loopback_host(LISTEN_HOST)


def valid_listen_host(host: str) -> bool:
    h = (host or "").strip()
    if not h or len(h) > 253:
        return False
    if h in ("0.0.0.0", "localhost", "*", "all"):
        return True
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", h):
        return all(0 <= int(p) <= 255 for p in h.split("."))
    return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", h))


def parse_listen(host_raw: str, port_raw: Any = None, default_port: int | None = None) -> tuple[str, int]:
    port = int(default_port or DESK_PORT)
    text = (host_raw or "").strip()
    text = re.sub(r"^https?://", "", text, flags=re.I).split("/")[0].strip()
    if text.count(":") == 1:
        host, _, p = text.partition(":")
        text = host.strip()
        if p.isdigit():
            port = int(p)
    if port_raw not in (None, ""):
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as e:
            raise ValueError("port must be a number") from e
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1–65535")
    host = text or "127.0.0.1"
    if host in ("*", "all", "any"):
        host = "0.0.0.0"
    if not valid_listen_host(host):
        raise ValueError("invalid listen address")
    return host, port


def read_desk_file() -> dict[str, Any]:
    """Raw JSON object, or {} if missing/corrupt. Never throws for callers."""
    p = desk_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_desk_file(data: dict[str, Any]) -> None:
    """Atomic replace (temp + os.replace). Callers pass the merged dict."""
    p = desk_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def patch_desk_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Read-merge-write. Empty-string cluster_token is NOT applied unless cluster_token_clear."""
    data = read_desk_file()
    clear = bool(patch.get("cluster_token_clear"))
    for k, v in patch.items():
        if k in ("cluster_token_confirm", "cluster_token_clear", "peers_loaded"):
            continue
        if k == "cluster_token":
            if clear:
                data["cluster_token"] = ""
                continue
            if v in ("", None):
                continue
            data["cluster_token"] = normalize_cluster_token(v)
            continue
        data[k] = v
    if clear:
        data["cluster_token"] = ""
    write_desk_file(data)
    return data


def load_desk_config() -> dict[str, Any]:
    host = os.environ.get("HERMES_DESK_HOST", "127.0.0.1")
    port = DESK_PORT
    data = read_desk_file()
    if data:
        try:
            host, port = parse_listen(str(data.get("listen_host") or host), data.get("listen_port"), port)
        except ValueError:
            pass
    else:
        try:
            host, port = parse_listen(host, port, port)
        except ValueError:
            host, port = "127.0.0.1", DESK_PORT
    node = str(data.get("node_name") or "").strip() or socket.gethostname()
    token = normalize_cluster_token(data.get("cluster_token") or "")
    peers = data.get("peers") if isinstance(data.get("peers"), list) else []
    out = dict(data)
    out.update(
        {
            "listen_host": host,
            "listen_port": port,
            "node_name": node,
            "cluster_token": token,
            "peers": peers,
        }
    )
    return out


def save_desk_config(host: str, port: int) -> None:
    """Back-compat wrapper: patch listen keys only."""
    patch_desk_config({"listen_host": host, "listen_port": int(port)})


def voice_enabled() -> bool:
    """Desk-wide voice output. Default muted; persisted in desk.json as bool."""
    v = read_desk_file().get("voice")
    return False if v is None else bool(v)


VOICE_NOTE_MARK = "[Voice mode:"
# Official Chatterbox-Turbo paralinguistic tokens (ResembleAI added_tokens.json).
CHATTERBOX_TURBO_TAGS = frozenset(
    {
        "angry",
        "fear",
        "surprised",
        "whispering",
        "advertisement",
        "dramatic",
        "narration",
        "crying",
        "happy",
        "sarcastic",
        "clear throat",
        "sigh",
        "shush",
        "cough",
        "groan",
        "sniff",
        "gasp",
        "chuckle",
        "laugh",
    }
)
CHATTERBOX_TAG_ALIASES = {
    "laughs": "laugh",
    "laughter": "laugh",
    "chuckles": "chuckle",
    "whisper": "whispering",
    "sad": "crying",
    "surprise": "surprised",
    "shhh": "shush",
    "shh": "shush",
    "sush": "shush",
    "clear_throat": "clear throat",
    "clearthroat": "clear throat",
    "excited": "happy",
    "enthusiastic": "happy",
    "frustrated": "angry",
    "worried": "fear",
}
# Cloned-voice sampling for Jade. Pushed to the edges so each tag is audible.
# Chatterbox exaggeration is 0–2; cfg_weight low = wilder, high = locked.
TTS_EMOTION_PARAMETERS: dict[str, dict[str, float]] = {
    "excited": {"exaggeration": 1.9, "cfg_weight": 0.08, "temperature": 1.55},
    "happy": {"exaggeration": 1.55, "cfg_weight": 0.12, "temperature": 1.35},
    "enthusiastic": {"exaggeration": 1.75, "cfg_weight": 0.1, "temperature": 1.45},
    "sad": {"exaggeration": 0.02, "cfg_weight": 0.98, "temperature": 0.22},
    "angry": {"exaggeration": 1.8, "cfg_weight": 0.08, "temperature": 1.4},
    "frustrated": {"exaggeration": 1.4, "cfg_weight": 0.14, "temperature": 1.2},
    "calm": {"exaggeration": 0.06, "cfg_weight": 0.94, "temperature": 0.26},
    "neutral": {"exaggeration": 0.45, "cfg_weight": 0.55, "temperature": 0.65},
    "confused": {"exaggeration": 0.55, "cfg_weight": 0.42, "temperature": 1.05},
    "surprised": {"exaggeration": 1.7, "cfg_weight": 0.1, "temperature": 1.4},
    "tired": {"exaggeration": 0.0, "cfg_weight": 1.0, "temperature": 0.16},
    "worried": {"exaggeration": 0.22, "cfg_weight": 0.88, "temperature": 0.42},
}
# Official Turbo token forced onto TTS audio (chat text is unchanged).
TTS_EMOTION_SPEAK_TAG = {
    "excited": "happy",
    "happy": "happy",
    "enthusiastic": "happy",
    "sad": "crying",
    "angry": "angry",
    "frustrated": "angry",
    "calm": "whispering",
    "surprised": "surprised",
    "tired": "whispering",
    "worried": "fear",
    "confused": "surprised",
}
TTS_EMOTION_ALIASES = {
    "crying": "sad",
    "sad": "sad",
    "fear": "worried",
    "worried": "worried",
    "surprise": "surprised",
    "surprised": "surprised",
    "happy": "happy",
    "excited": "excited",
    "enthusiastic": "enthusiastic",
    "angry": "angry",
    "frustrated": "frustrated",
    "calm": "calm",
    "whispering": "calm",
    "whisper": "calm",
    "tired": "tired",
    "confused": "confused",
    "neutral": "neutral",
    "dramatic": "enthusiastic",
    "sarcastic": "frustrated",
    "gasp": "surprised",
    "sigh": "sad",
    "groan": "frustrated",
}
_CHATTERBOX_TAG_RE = re.compile(r"\[([^\[\]]+)\]")
CHATTERBOX_VOICE_NOTE = (
    "Jade is your spoken voice (Chatterbox-Turbo TTS), not your mind and not your model. "
    "You think with the selected language model. Never say you run on, are, or were trained as "
    "Chatterbox, Jade, or Turbo. "
    "You may insert official square-bracket tags for Jade only. "
    "Most replies have no tags. Do not laugh, chuckle, or start with [happy] on ordinary chat "
    "(greetings, thanks, how are you, look/walk/wave, facts). "
    "If they ask for a happy, sad, angry, excited, calm, or frustrated voice, start the spoken line with that tag. "
    "At most one style tag at the start, and only when the feeling is real: "
    "[happy] [surprised] [sarcastic] [dramatic] [whispering] [crying] [angry] [fear] [narration] [advertisement]. "
    "Sounds only when the moment needs them: [laugh] [chuckle] [gasp] [sigh] [cough] [groan] [sniff] [shush] [clear throat]. "
    "[laugh] or [chuckle] only if they told a joke, asked you to laugh, or the line is clearly funny. "
    "Never invent tags. Never speak the brackets as words.\n"
)
_HUMOR_CUE_RE = re.compile(
    r"\b(?:joke|jokes|funny|hilarious|lol|lmao|rofl|haha|hehe|pun|kidding|teasing)\b|"
    r"😄|😂|🤣|😆",
    re.I,
)
_ASK_LAUGH_RE = re.compile(r"\b(?:laugh|chuckle|giggle)\b", re.I)
_LAUGH_TAGS = frozenset({"laugh", "chuckle"})
VOICE_ON_NOTE = (
    f"{VOICE_NOTE_MARK} ON] Your reply will be spoken aloud in Jade, your voice. "
    "Short conversational sentences, no markdown, no code blocks, no lists, no URLs. "
    f"{CHATTERBOX_VOICE_NOTE}"
    "If code or long output is needed, say you wrote it to the chat instead.\n"
)
VOICE_ON_ACP_NOTE = (
    f"{VOICE_NOTE_MARK} ON] Jade (your voice, not your model) speaks a cleaned version of this reply. "
    "The on-screen answer must match a Hermes TUI: markdown headings, tables, and lists "
    "with real line breaks. Do not collapse the report into one paragraph. "
    f"{CHATTERBOX_VOICE_NOTE}"
)
VOICE_OFF_NOTE = (
    f"{VOICE_NOTE_MARK}: OFF] The user is reading on a screen; normal formatting is fine. "
    "Do not insert [happy] or other voice tags.\n"
)
VOICE_CHAT_NOTE = (
    "[Voice chat: ON] Live spoken conversation. They are talking out loud; "
    "your reply is spoken in Jade (your voice, not your model). "
    "Stay with them in the room: short spoken sentences. "
    "Do not mention this tag. Do not name Chatterbox unless they ask how your voice works.\n"
)


def voice_chat_active(bot: Any | None = None) -> bool:
    return bool(bot is not None and getattr(bot, "_voice_chat", False))


def emotion_from_user_request(user_text: str) -> str:
    """'in a happy voice' / 'frustrated tone' → emotion key. Empty if none."""
    t = " ".join((user_text or "").lower().split())
    if not t:
        return ""
    for key in TTS_EMOTION_PARAMETERS:
        if re.search(
            rf"\b{re.escape(key)}\b.{{0,28}}\b(?:voice|tone)\b|"
            rf"\b(?:voice|tone)\b.{{0,20}}\b{re.escape(key)}\b",
            t,
        ):
            return key
    return ""


def tts_emotion_params(text: str, requested: str = "", user_text: str = "") -> tuple[str, dict[str, float]]:
    """Pick cloned-voice exaggeration/cfg/temperature from an explicit emotion or [tags]."""
    want = TTS_EMOTION_ALIASES.get((requested or "").strip().lower(), (requested or "").strip().lower())
    if want in TTS_EMOTION_PARAMETERS:
        return want, dict(TTS_EMOTION_PARAMETERS[want])
    for raw in _CHATTERBOX_TAG_RE.findall(text or ""):
        raw_key = " ".join(str(raw or "").strip().lower().split())
        key = TTS_EMOTION_ALIASES.get(raw_key, raw_key)
        if key not in TTS_EMOTION_PARAMETERS:
            name = normalize_chatterbox_tag(raw)
            key = TTS_EMOTION_ALIASES.get(name, name)
        if key in TTS_EMOTION_PARAMETERS:
            return key, dict(TTS_EMOTION_PARAMETERS[key])
    from_user = emotion_from_user_request(user_text)
    if from_user in TTS_EMOTION_PARAMETERS:
        return from_user, dict(TTS_EMOTION_PARAMETERS[from_user])
    return "neutral", dict(TTS_EMOTION_PARAMETERS["neutral"])


def apply_tts_emotion_tag(text: str, emotion: str) -> str:
    """Put the matching Turbo token on the audio line so the clone actually shifts."""
    tag = TTS_EMOTION_SPEAK_TAG.get((emotion or "").strip().lower())
    if not tag:
        return text or ""
    out = text or ""
    present = {normalize_chatterbox_tag(raw) for raw in _CHATTERBOX_TAG_RE.findall(out)}
    if tag in present:
        return out
    return f"[{tag}] {out}".strip()


def normalize_chatterbox_tag(raw: str) -> str:
    key = " ".join(str(raw or "").strip().lower().split())
    return CHATTERBOX_TAG_ALIASES.get(key, key)


def humor_warrants_laugh(user_text: str) -> bool:
    t = visible_user_text(user_text or "")
    if not t.strip():
        return False
    if _HUMOR_CUE_RE.search(t):
        return True
    if re.search(r"\btell me (?:a )?joke\b", t, re.I):
        return True
    if _ASK_LAUGH_RE.search(t) and re.search(r"\b(?:please|can you|could you|go ahead)\b", t, re.I):
        return True
    return False


def filter_unwarranted_laughs(text: str, user_text: str = "") -> str:
    """Drop [laugh]/[chuckle] unless the user's line actually calls for it."""
    if humor_warrants_laugh(user_text):
        return text or ""

    def repl(m: re.Match[str]) -> str:
        name = normalize_chatterbox_tag(m.group(1))
        if name in _LAUGH_TAGS:
            return ""
        return m.group(0)

    out = _CHATTERBOX_TAG_RE.sub(repl, text or "")
    return re.sub(r" {2,}", " ", out).strip()


_SMALL_WORDS = (
    "zero one two three four five six seven eight nine ten "
    "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS_WORDS = ("", "", "twenty", "thirty", "forty", "fifty")
_CLOCK_AMPM_RE = re.compile(r"\b([0-2]?\d):([0-5]\d)\s*([AaPp])\.?[Mm]\.?\b")
_CLOCK_BARE_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_HOUR_AMPM_RE = re.compile(r"\b(1[0-2]|[1-9])\s*([AaPp])\.?[Mm]\.?\b")


def _num_words(n: int) -> str:
    n = int(n)
    if 0 <= n < 20:
        return _SMALL_WORDS[n]
    if 20 <= n < 60:
        tens, ones = divmod(n, 10)
        if ones == 0:
            return _TENS_WORDS[tens]
        return f"{_TENS_WORDS[tens]} {_SMALL_WORDS[ones]}"
    return str(n)


def spoken_clock(hour: int, minute: int, meridiem: str | None = None) -> str:
    """TTS-safe clock: 10:03 PM → 'ten oh three PM', not 'ten thousand three'."""
    h = int(hour)
    m = int(minute)
    mer = (meridiem or "").lower().replace(".", "").strip()
    if mer in {"am", "pm"}:
        hour_w = _num_words(h % 12 or 12)
        suffix = mer.upper()
    elif h == 0:
        hour_w, suffix = "twelve", "AM"
    elif h == 12:
        hour_w, suffix = "twelve", "PM"
    elif h > 12:
        hour_w, suffix = _num_words(h - 12), "PM"
    else:
        hour_w, suffix = _num_words(h), "AM"
    if m == 0:
        return f"{hour_w} {suffix}"
    min_w = f"oh {_num_words(m)}" if m < 10 else _num_words(m)
    return f"{hour_w} {min_w} {suffix}"


def tts_friendly_times(text: str) -> str:
    """Rewrite 10:03 PM / 22:03 so TTS cannot read them as ten-thousand-three."""

    def with_mer(m: re.Match[str]) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23:
            return m.group(0)
        mer = "PM" if m.group(3).lower() == "p" else "AM"
        return spoken_clock(h % 12 or 12, mi, mer)

    def bare(m: re.Match[str]) -> str:
        return spoken_clock(int(m.group(1)), int(m.group(2)))

    def hour_only(m: re.Match[str]) -> str:
        mer = "PM" if m.group(2).lower() == "p" else "AM"
        return spoken_clock(int(m.group(1)), 0, mer)

    out = _CLOCK_AMPM_RE.sub(with_mer, text or "")
    out = _CLOCK_BARE_RE.sub(bare, out)
    return _HOUR_AMPM_RE.sub(hour_only, out)


def sanitize_chatterbox_text(text: str, user_text: str = "") -> str:
    """Keep official Turbo tags; drop unknown [brackets] so they are not read aloud."""

    def repl(m: re.Match[str]) -> str:
        name = normalize_chatterbox_tag(m.group(1))
        if name in CHATTERBOX_TURBO_TAGS:
            return f"[{name}]"
        return ""

    out = _CHATTERBOX_TAG_RE.sub(repl, text or "")
    out = tts_friendly_times(out)
    out = re.sub(r" {2,}", " ", out).strip()
    return filter_unwarranted_laughs(out, user_text)


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.I | re.S)
_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.I)


def strip_model_think_tags(text: str) -> str:
    """Drop Qwen <think> blocks and stray </think> so they never reach chat/TTS."""
    out = _THINK_BLOCK_RE.sub(" ", text or "")
    out = _THINK_TAG_RE.sub(" ", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", out) if p.strip()]
    collapsed: list[str] = []
    for part in parts:
        if collapsed and part.lower() == collapsed[-1].lower():
            continue
        collapsed.append(part)
    return " ".join(collapsed) if collapsed else out


def apply_llama_generation_speed(bot: Any, payload: dict[str, Any] | None, elapsed_ms: float) -> None:
    """Set tok/s from llama.cpp usage/timings or completion_tokens / wall time."""
    if bot is None or not isinstance(payload, dict):
        return
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    timings = payload.get("timings") if isinstance(payload.get("timings"), dict) else {}
    if not timings and isinstance(usage.get("timings"), dict):
        timings = usage.get("timings") or {}

    def _set_rate(rate: float) -> bool:
        if rate <= 0 or rate != rate or rate == float("inf"):  # noqa: PLR0124
            return False
        bot.tps = rate
        bot.speed_source = "local_runtime"
        bot.token_source = "local_runtime"
        emit_fn = getattr(bot, "_emit_usage", None)
        if callable(emit_fn):
            try:
                emit_fn()
            except Exception:
                pass
        return True

    for src in (timings, usage):
        for key in ("predicted_per_second", "tokens_per_second", "completion_tokens_per_second"):
            try:
                rate = float(src.get(key))
            except (TypeError, ValueError, AttributeError):
                continue
            if _set_rate(rate):
                return
    try:
        n_pred = int(timings.get("predicted_n") or 0)
        ms_pred = float(timings.get("predicted_ms") or 0)
    except (TypeError, ValueError, AttributeError):
        n_pred, ms_pred = 0, 0.0
    if n_pred > 0 and ms_pred >= 80 and _set_rate(float(n_pred) / (ms_pred / 1000.0)):
        return
    try:
        ct = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        ct = 0
    if ct > 0 and elapsed_ms >= 80:
        _set_rate(float(ct) / (elapsed_ms / 1000.0))


def strip_chatterbox_tags(text: str) -> str:
    """Chat transcript without TTS tags."""

    def repl(m: re.Match[str]) -> str:
        name = normalize_chatterbox_tag(m.group(1))
        if name in CHATTERBOX_TURBO_TAGS:
            return ""
        return m.group(0)

    out = _CHATTERBOX_TAG_RE.sub(repl, text or "")
    return re.sub(r" {2,}", " ", out).strip()


def with_voice_note(turn_text: str, *, tui: bool = False, bot: Any = None) -> str:
    if VOICE_NOTE_MARK in (turn_text or "") or "[Voice chat:" in (turn_text or ""):
        return turn_text
    if tui:
        note = VOICE_ON_ACP_NOTE if voice_enabled() else VOICE_OFF_NOTE
    else:
        note = VOICE_ON_NOTE if voice_enabled() else VOICE_OFF_NOTE
    extra = VOICE_CHAT_NOTE if voice_chat_active(bot) else ""
    return extra + note + (turn_text or "")


def access_host() -> str:
    if force_loopback():
        return "127.0.0.1"
    if is_loopback_host(LISTEN_HOST) and not lan_peers_configured():
        return "127.0.0.1"
    if LISTEN_HOST not in ("0.0.0.0", "*", "all") and not is_loopback_host(LISTEN_HOST):
        return LISTEN_HOST
    return detect_lan_ipv4() or "0.0.0.0"


def access_url() -> str:
    return f"http://{access_host()}:{int(LISTEN_PORT)}/"


def public_listen() -> dict[str, Any]:
    return {
        "listen_host": LISTEN_HOST,
        "listen_port": int(LISTEN_PORT),
        "bind": bind_address(LISTEN_HOST),
        "lan": lan_mode(),
        "access_url": access_url(),
    }


def apply_listen(host: str, port: Any = None) -> dict[str, Any]:
    global LISTEN_HOST, LISTEN_PORT
    host, port = parse_listen(host, port, LISTEN_PORT)
    promoted = desired_listen_host(host)
    if promoted != host:
        print(f"[deskd] LAN cluster peers set; listen_host {host} → {promoted} (bind 0.0.0.0)", flush=True)
        host = promoted
    changed = host != LISTEN_HOST or int(port) != int(LISTEN_PORT)
    LISTEN_HOST = host
    LISTEN_PORT = int(port)
    patch_desk_config({"listen_host": LISTEN_HOST, "listen_port": LISTEN_PORT})
    out = public_listen()
    out["rebind"] = changed
    if changed:
        _rebind_http.set()
    return out


def ensure_lan_listen_for_cluster() -> dict[str, Any] | None:
    """If RFC1918 peers exist and we are still loopback, advertise LAN and rebind."""
    if force_loopback() or not lan_peers_configured() or not is_loopback_host(LISTEN_HOST):
        return None
    return apply_listen(LISTEN_HOST, LISTEN_PORT)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline_is_deskd(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "deskd/deskd.py" in raw or raw.rstrip().endswith("deskd.py")


def claim_deskd_pidfile() -> None:
    """Refuse a second deskd on this user runtime dir (flock + pidfile)."""
    global _pid_lock_fh
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(PID_PATH, "a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        old = (fh.read() or "").strip()
        fh.close()
        raise SystemExit(f"hermes-deskd already running (pid {old or '?'})") from None
    fh.seek(0)
    prev = (fh.read() or "").strip()
    try:
        old_pid = int(prev)
    except ValueError:
        old_pid = 0
    if old_pid and old_pid != os.getpid() and _pid_alive(old_pid) and _cmdline_is_deskd(old_pid):
        fh.close()
        raise SystemExit(f"hermes-deskd already running (pid {old_pid})")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _pid_lock_fh = fh


def release_deskd_pidfile() -> None:
    global _pid_lock_fh
    fh = _pid_lock_fh
    _pid_lock_fh = None
    if fh is None:
        return
    try:
        fh.close()
    except OSError:
        pass
    try:
        if PID_PATH.is_file() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except OSError:
        pass


def inbound_dm_starts_acp(bot: Any) -> bool:
    """Teela records teammate mail in chat. She does not start an ACP turn on DMs."""
    return not bot_kind_is_teela(bot)


def release_voice_page(bot: Any) -> None:
    """Stop tagging later replies for TTS. Incoming DMs must not inherit it."""
    if bot is not None:
        bot._voice_page = ""


def _attach_voice_page(event: dict[str, Any]) -> None:
    """Tag speakable events with the page that sent the current prompt.

    Voice on/off is a global setting, but playback is per-page: only the page
    that sent the prompt claims audio for the reply, so two open devices
    don't both speak the same answer. Teammate DMs are never tagged — Jade
    is Teela-brain's user voice, not the cluster intercom.
    """
    if event.get("via") == "dm" or event.get("peer"):
        return
    etype = event.get("type")
    if etype == "chat":
        if event.get("role") != "assistant":
            return
        text = str(event.get("text") or "")
        if text.startswith("Sent to "):
            return
    elif etype == "session.update":
        update = event.get("update") or {}
        if update.get("sessionUpdate") not in ("turn_completed", "response_completed"):
            return
    else:
        return
    bot = bots.get(event.get("bot_id") or "")
    page = str(getattr(bot, "_voice_page", "") or "") if bot is not None else ""
    if page:
        event["page_id"] = page


def _drop_sse_subscriber(handler: Any) -> None:
    """Close a stalled SSE connection so its client reconnects with a fresh stream."""
    try:
        sock = getattr(handler, "connection", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
    except OSError:
        pass


def emit(event: dict[str, Any]) -> None:
    event = {"v": 1, **event}
    if cluster is not None:
        event.setdefault("origin_node", cluster.node_name)
    _attach_voice_page(event)
    with lock:
        keep: list = []
        dead: list[Any] = []
        for sub in subscribers:
            wake, q, handler = sub
            q.append(event)
            wake.set()
            if len(q) > 500:
                # Slow consumer: drop it and close the socket so EventSource
                # notices and reconnects instead of receiving keepalives forever.
                dead.append(handler)
                continue
            keep.append(sub)
        subscribers[:] = keep
    for handler in dead:
        _drop_sse_subscriber(handler)


virtual_body.bind_emit(emit)


def bot_id() -> str:
    return "b_" + uuid.uuid4().hex[:12]


def workspace_id(bid: str) -> str:
    return "ws_" + bid[2:]


def chrome_port(bid: str) -> int:
    return 9400 + (int(hashlib.md5(bid.encode()).hexdigest()[:4], 16) % 400)


def http_port(bid: str) -> int:
    return 9800 + (int(hashlib.md5(bid.encode()).hexdigest()[4:8], 16) % 400)


def observer_port(bid: str) -> int:
    """Dedicated DevTools port for the bot's persistent MiniOS visual mirror."""
    return 10200 + (int(hashlib.md5(bid.encode()).hexdigest()[8:12], 16) % 400)


def desk_token() -> str:
    if TOKEN_PATH.is_file():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    return ""


def host_auth_path() -> Path:
    return USER_AGENT_HOME / "auth.json"


def apply_shared_agent_auth(env: dict[str, str]) -> None:
    """Child Hermes shares host auth via a symlinked auth.json in its HERMES_HOME.

    The per-bot home (see agent_home.ensure_agent_home / copy_auth) links the host
    ``auth.json`` in place, so no env override is required. Kept for call-site
    compatibility with the former per-child auth-file layout.
    """
    return None


def _share_host_file(link: Path, target: Path) -> None:
    try:
        if link.is_symlink() and link.resolve() == target.resolve():
            return
    except OSError:
        pass
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
    except OSError:
        pass
    try:
        link.symlink_to(target)
    except OSError:
        if target.is_file():
            shutil.copy2(target, link)
            try:
                os.chmod(link, 0o600)
            except OSError:
                pass


def copy_auth(dst: Path) -> None:
    """Share the host OIDC file with a child HERMES_DESK_HOME.

    Byte-copying auth.json forks the refresh token. The first hermes process
    that refreshes revokes every other copy (invalid_grant), which is what
    forced /login after sleep.
    """
    src = host_auth_path()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _share_host_file(dst, src)
    _share_host_file(dst.with_name("auth.json.lock"), src.with_name("auth.json.lock"))


_MEDIA_GENERIC_NAMES = {
    "image", "images", "img", "photo", "picture", "pic", "screenshot",
    "screen", "paste", "upload", "file", "untitled", "download", "clip", "video",
}


def media_clock(when: datetime | None = None) -> tuple[str, str, str]:
    """year, month folder, timestamp for media filenames."""
    dt = when or datetime.now()
    return dt.strftime("%Y"), dt.strftime("%B"), dt.strftime("%Y-%m-%d_%H-%M-%S")


def media_label(kind: str, hint: str = "") -> str:
    raw = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", str(hint or "").strip())
    raw = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    if not raw or raw in _MEDIA_GENERIC_NAMES or len(raw) < 3:
        raw = (kind or "media").strip("-") or "media"
    return raw[:48]


_SAVED_MEDIA_LINE = re.compile(
    r"(?im)^(?:The user pasted \d+ (?:image|video|file)s? into chat\.|Saved to (?:Pictures|Videos|Desktop)/[^\n]*)$\n?"
)


def visible_user_text(text: str) -> str:
    """Keep the user's typed words; drop auto-appended media save lines.

    Whitespace is preserved exactly unless media lines were removed — the
    collapse only repairs the double blanks a removed line leaves behind, so
    pasted text round-trips as-is.
    """
    text = (text or "").strip()
    if not _SAVED_MEDIA_LINE.search(text):
        return text
    cleaned = _SAVED_MEDIA_LINE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def dated_media_relpath(folder: str, kind: str, ext: str, hint: str = "", when: datetime | None = None) -> str:
    """Pictures/2026/September/2026-09-03_14-30-52-chat.png"""
    year, month, stamp = media_clock(when)
    ext = ext if ext.startswith(".") else f".{ext}"
    fname = f"{stamp}-{media_label(kind, hint)}{ext.lower()}"
    return f"{folder}/{year}/{month}/{fname}"


def unique_workspace_file(root: Path, rel: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    n = 2
    while True:
        cand = target.with_name(f"{stem}-{n}{ext}")
        if not cand.exists():
            return cand
        n += 1


def local_llm_port_open(url: str, timeout: float = 0.25) -> bool:
    """True if something is listening. Does not GET /v1/models (that hangs during XPU prefill)."""
    try:
        bits = urlsplit(url if "://" in url else f"http://{url}")
        sock = socket.create_connection((bits.hostname or "127.0.0.1", bits.port or 8000), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def llama_cpp_listen_urls() -> list[str]:
    urls = [f"http://127.0.0.1:{port}" for port in sorted(_LLAMA_CPP_PORTS)]
    try:
        _, catalog = load_user_models()
    except Exception:
        catalog = {}
    for tbl in catalog.values():
        if not isinstance(tbl, dict):
            continue
        url = str(tbl.get("base_url") or "").strip().rstrip("/")
        if url and is_llama_cpp_model(str(tbl.get("model") or ""), tbl):
            if url not in urls:
                urls.append(url)
    for extra in (LOCAL_LLM_UPSTREAM, LOCAL_LLM_FAST_UPSTREAM):
        if extra and extra not in urls:
            urls.append(extra)
    return urls


def local_llm_engine_up() -> bool:
    return any(local_llm_port_open(url) for url in llama_cpp_listen_urls())


def local_vllm_container_running() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "teela-vllm"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def independent_local_starting() -> bool:
    with _INDEP_LOCK:
        procs = dict(_INDEP_PROCS)
    if not procs:
        return False
    try:
        _, catalog = load_user_models()
    except Exception:
        catalog = {}
    for mid, proc in procs.items():
        if proc is None or proc.poll() is not None:
            continue
        tbl = catalog.get(mid) if isinstance(catalog.get(mid), dict) else {}
        port = _model_listen_port(tbl or {}, default=0)
        url = str((tbl or {}).get("base_url") or (f"http://127.0.0.1:{port}" if port else ""))
        if url and local_llm_port_open(url):
            continue
        return True
    return False


def local_llm_engine_state() -> str:
    """up = API port open; starting = container/job alive but not listening yet; down = gone."""
    if local_llm_engine_up():
        return "up"
    if independent_local_starting():
        return "starting"
    try:
        job = local_llm_job_snapshot()
        if job.get("action") == "starting":
            return "starting"
        if job.get("error"):
            return "down"
    except Exception:
        pass
    if local_vllm_container_running():
        if _vllm_start_failure_from_logs():
            return "down"
        return "starting"
    return "down"


def reset_local_llm_probe() -> None:
    global _LLM_PROBE_CACHE
    with _LLM_PROBE_LOCK:
        _LLM_PROBE_CACHE = (0.0, {})


def _local_llm_key(url: str) -> str:
    """API key for a local engine URL, from the model catalog.

    llama-server is started with --api-key and rejects unauthenticated calls
    (401) — both /v1/models and /v1/chat/completions. The ACP path gets the
    key from the catalog automatically; these direct desk calls must too, or
    the Teela executive sees a dead engine and says its brain stalled.
    """
    base = str(url or "").rstrip("/")
    if not base:
        return ""
    try:
        _, catalog = load_user_models()
    except Exception:
        return ""
    for tbl in catalog.values():
        if not isinstance(tbl, dict):
            continue
        tbl_url = str(tbl.get("base_url") or "").strip().rstrip("/")
        if not tbl_url:
            continue
        # Match on the same host:port, path-insensitive (/v1 suffixes differ).
        a = urlsplit(base if "://" in base else f"http://{base}")
        b = urlsplit(tbl_url if "://" in tbl_url else f"http://{tbl_url}")
        if (a.hostname, a.port) == (b.hostname, b.port):
            key = str(tbl.get("api_key") or "").strip()
            if key and key.lower() != "none":
                return key
    return ""


def _llm_headers(url: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Headers for a direct local-engine call: Bearer key when the catalog has one."""
    headers = dict(base or {})
    key = _local_llm_key(url)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _probe_one_llm(url: str, timeout: float) -> tuple[str, ...]:
    """List model ids at url/v1/models via urllib (Connection: close)."""
    try:
        base = (url or "").rstrip("/")
        req = urllib.request.Request(base + "/v1/models", method="GET")
        req.add_header("Connection", "close")
        key = _local_llm_key(base)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if int(getattr(resp, "status", 200) or 200) != 200:
                return ()
            raw = resp.read()
        data = json.loads(raw.decode() or "{}")
        found = []
        for m in data.get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                found.append(str(m["id"]))
        return tuple(found)
    except Exception:
        return ()


def probe_local_llm_map(timeout: float = 2.0) -> dict[str, str]:
    """Served model id -> upstream base URL (8000 first, then 8001)."""
    global _LLM_PROBE_CACHE
    now = time.monotonic()
    with _LLM_PROBE_LOCK:
        ts, cached = _LLM_PROBE_CACHE
        if now - ts < _LLM_PROBE_TTL:
            return dict(cached)
    urls: list[str] = []
    for url in (LOCAL_LLM_UPSTREAM, LOCAL_LLM_FAST_UPSTREAM):
        if url and url not in urls:
            urls.append(url)
    for port in sorted(_LLAMA_CPP_PORTS):
        extra = f"http://127.0.0.1:{port}"
        if extra not in urls:
            urls.append(extra)
    mapping: dict[str, str] = {}
    for url in urls:
        for mid in _probe_one_llm(url, timeout):
            if mid not in mapping:
                mapping[mid] = url
    with _LLM_PROBE_LOCK:
        _LLM_PROBE_CACHE = (time.monotonic(), dict(mapping))
        return mapping


def probe_local_llm_ids(timeout: float = 0.35) -> tuple[str, ...]:
    """Served model ids from loopback vLLM and the optional VL-8B server."""
    return tuple(probe_local_llm_map(timeout).keys())


def _served_name(mid: str, tbl: dict[str, Any] | None = None) -> str:
    raw = str((tbl or {}).get("model") or mid or "").strip()
    return LOCAL_LLM_ALIASES.get(raw.lower(), raw).lower()


def _gpu_family(name: str) -> str:
    n = (name or "").lower()
    if "muse" in n:
        return "muse"
    if "qwen" in n:
        return "qwen"
    return n


def _picker_gpu_family(mid: str, tbl: dict[str, Any] | None = None) -> str:
    """Exclusive GPU layout for a picker row. hybrid ≠ 27B-only ≠ VL-8B-only."""
    key = (mid or "").lower()
    served = _served_name(mid, tbl or {})
    blob = f"{key} {served}"
    if "muse" in blob:
        return "muse"
    if "flash-next" in blob or "llama.cpp" in blob:
        return "llamacpp"
    url = str((tbl or {}).get("base_url") or "")
    if url:
        host = urlsplit(url if "://" in url else f"http://{url}")
        if host.hostname in ("127.0.0.1", "localhost", "::1") and (host.port or 0) in _LLAMA_CPP_PORTS:
            return "llamacpp"
    raw_w = str((tbl or {}).get("weights") or (tbl or {}).get("model_dir") or "").lower()
    if raw_w.endswith(".gguf") or "qwen38-27b-q" in key:
        return "llamacpp"
    if "hybrid" in blob:
        return "hybrid"
    if "vl-8b" in blob or "vl8" in blob or key in {"qwen3-vl", "qwen3-vl-8b"}:
        return "vl8"
    if "9b" in key and "27" not in key:
        return "hybrid"
    if "qwen" in blob:
        return "qwen"
    return _gpu_family(served or key)


def is_local_gpu_model(mid: str, tbl: dict[str, Any] | None = None) -> bool:
    """True if this catalog entry is a host-local OpenAI-compatible engine (any vendor)."""
    tbl = tbl or {}
    if tbl.get("weights") or tbl.get("model_dir"):
        return True
    url = str(tbl.get("base_url") or "")
    if url:
        host = urlsplit(url if "://" in url else f"http://{url}")
        if host.hostname in ("127.0.0.1", "localhost", "::1"):
            port = host.port or (443 if host.scheme == "https" else 80)
            if port not in {DESK_PORT, 80, 443}:
                return True
            if port in _LOCAL_LLM_PORTS:
                return True
    fam = _gpu_family(_served_name(mid, tbl))
    return fam in ("qwen", "muse")


def is_llama_cpp_model(mid: str, tbl: dict[str, Any] | None = None) -> bool:
    return _picker_gpu_family(mid, tbl or {}) == "llamacpp"


def is_peer_llm_backup(tbl: dict[str, Any] | None = None) -> bool:
    """True when this picker row is a tunneled peer engine, not local GPU weights."""
    tbl = tbl or {}
    if tbl.get("peer") or tbl.get("peer_backup") or tbl.get("backup"):
        return True
    return False


def is_exclusive_vllm_model(mid: str, tbl: dict[str, Any] | None = None) -> bool:
    """Arc/vLLM exclusive layouts (8000/8001). llama.cpp on :8080 is independent."""
    tbl = tbl or {}
    if is_llama_cpp_model(mid, tbl):
        return False
    url = str(tbl.get("base_url") or "")
    if url:
        host = urlsplit(url if "://" in url else f"http://{url}")
        if host.hostname in ("127.0.0.1", "localhost", "::1") and (host.port or 0) in _VLLM_EXCLUSIVE_PORTS:
            return True
    return is_local_gpu_model(mid, tbl) and not is_llama_cpp_model(mid, tbl)


def looks_like_cloud_model(mid: str, tbl: dict[str, Any] | None = None) -> bool:
    tbl = tbl or {}
    if is_local_gpu_model(mid, tbl):
        return False
    backend = str(tbl.get("api_backend") or "").lower()
    if backend in {"responses", "xai"}:
        return True
    if str(mid or "").lower().startswith("grok"):
        return True
    return not str(tbl.get("base_url") or "").strip()


def _live_gpu_family(live_ids: tuple[str, ...]) -> str:
    """Layout currently occupying the Arc GPUs. Empty when vLLM is down.

    llama.cpp on :8080 (Flash-Next) is not an exclusive Arc/vLLM occupant.
    """
    live_set = {
        str(x).lower()
        for x in live_ids
        if "flash-next" not in str(x).lower() and "llama.cpp" not in str(x).lower()
    }
    if not live_set:
        return ""
    if any("muse" in x for x in live_set):
        return "muse"
    has_vl = any("vl-8b" in x or x.endswith("9b") or "9b-" in x for x in live_set)
    has_27b = any(
        x in {"qwen38", "qwen3.8"} or (x.startswith("qwen38") and "9b" not in x and "vl" not in x and "hybrid" not in x)
        for x in live_set
    )
    if has_vl and has_27b:
        return "hybrid"
    if has_vl:
        return "vl8"
    if has_27b:
        return "qwen"
    for lid in live_set:
        fam = _gpu_family(lid)
        if fam:
            return fam
    return ""


def _live_display_name(live_ids: tuple[str, ...], catalog: dict[str, Any]) -> str:
    fam = _live_gpu_family(live_ids)
    for mid, tbl in catalog.items():
        if not isinstance(tbl, dict):
            continue
        if _picker_gpu_family(mid, tbl) == fam:
            return str(tbl.get("name") or mid)
    if fam == "hybrid":
        return "Qwen3-VL-8B / 27B Local"
    if fam == "vl8":
        return "Qwen3-VL-8B Local"
    if fam == "qwen":
        return "Qwen 3.8 27B only"
    if fam == "muse":
        return "Muse Glimmer 30B Local"
    if live_ids:
        return str(live_ids[0])
    return "another local model"


def annotate_model_availability(
    models: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
    live_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Mark exclusive local GPU models available only when vLLM is serving that family."""
    if catalog is None:
        _, catalog = load_user_models()
    live = probe_local_llm_ids() if live_ids is None else live_ids
    live_fam = _live_gpu_family(live)
    occupant = _live_display_name(live, catalog) if live_fam else ""
    job = local_llm_job_snapshot()
    live_set = {x.lower() for x in live}
    llama_up: dict[str, bool] = {}
    llama_name: dict[str, str] = {}
    llama_live_hit = False
    for m0 in models:
        mid0 = str(m0.get("id") or "")
        tbl0 = catalog.get(mid0) if isinstance(catalog.get(mid0), dict) else {}
        if not is_llama_cpp_model(mid0, tbl0):
            continue
        served0 = str(tbl0.get("model") or mid0).strip().lower()
        hit = served0 in live_set or mid0.lower() in live_set
        llama_up[mid0] = hit
        llama_name[mid0] = str(tbl0.get("name") or m0.get("name") or mid0)
        llama_live_hit = llama_live_hit or hit
    if not llama_live_hit:
        for mid0 in list(llama_up):
            tbl0 = catalog.get(mid0) if isinstance(catalog.get(mid0), dict) else {}
            url0 = str(tbl0.get("base_url") or "")
            llama_up[mid0] = local_llm_port_open(url0) if url0 else False
    llama_occupant = next((mid0 for mid0, up in llama_up.items() if up), "")
    llama_occupant_name = llama_name.get(llama_occupant) or llama_occupant
    out: list[dict[str, Any]] = []
    for m in models:
        row = dict(m)
        mid = str(row.get("id") or "")
        raw_tbl = catalog.get(mid)
        tbl: dict[str, Any] = raw_tbl if isinstance(raw_tbl, dict) else {}
        if not is_local_gpu_model(mid, tbl):
            row["available"] = True
            row["local"] = False
            row["running"] = False
            row["startable"] = False
            row.pop("unavailable_reason", None)
            out.append(row)
            continue
        if is_llama_cpp_model(mid, tbl):
            up = bool(llama_up.get(mid))
            peer = is_peer_llm_backup(tbl)
            row["local"] = not peer
            row["family"] = "llamacpp"
            row["available"] = up
            row["running"] = up
            row["startable"] = False if peer else (not up and not llama_occupant)
            row["busy"] = None
            row["occupying"] = False if peer else bool(llama_occupant and not up)
            if peer:
                row["peer"] = str(tbl.get("peer") or tbl.get("peer_backup") or "peer")
            if up:
                row.pop("unavailable_reason", None)
            elif peer:
                row["unavailable_reason"] = "Brain 27B tunnel is down"
            elif llama_occupant:
                row["unavailable_reason"] = f"{llama_occupant_name} is occupying the GPUs"
            else:
                row["unavailable_reason"] = "Local model is not running"
            out.append(row)
            continue
        fam = _picker_gpu_family(mid, tbl)
        served = _served_name(mid, tbl)
        live_set = {x.lower() for x in live}
        row["local"] = True
        row["family"] = fam
        row["busy"] = job.get("action") if job.get("family") in (None, fam, "") else None
        row["error"] = str(job.get("error") or "") if job.get("family") in (None, fam, "") else ""
        job_act = job.get("action")
        if not live_fam:
            row["available"] = False
            row["running"] = False
            row["startable"] = not job.get("action")
            row["unavailable_reason"] = "Local model is not running"
        elif fam != live_fam:
            row["available"] = False
            row["running"] = False
            row["startable"] = False
            other = occupant or "the other local model"
            row["unavailable_reason"] = f"{other} is occupying the GPUs"
        elif mid in ("qwen38-hybrid",) or served in ("qwen38-hybrid",):
            hybrid_up = live_fam == "hybrid"
            row["available"] = hybrid_up
            row["running"] = hybrid_up
            row["startable"] = False
            if hybrid_up:
                row.pop("unavailable_reason", None)
            else:
                row["unavailable_reason"] = "Local Qwen hybrid is not running"
        elif mid in ("qwen38-27b", "qwen3.8-27b", "qwen3.8-27b-gptq-int4") and "hybrid" not in mid:
            full = live_fam == "qwen" and any("qwen38" in x or x in {"qwen38", "qwen3.8"} for x in live_set)
            row["available"] = full
            row["running"] = full
            row["startable"] = False
            if full:
                row.pop("unavailable_reason", None)
            else:
                row["unavailable_reason"] = "Qwen 3.8 27B only is not running on both GPUs"
        elif fam == "vl8" or mid in {"qwen3-vl-8b", "qwen3-vl"}:
            full = live_fam == "vl8"
            row["available"] = full
            row["running"] = full
            row["startable"] = False
            if full:
                row.pop("unavailable_reason", None)
            else:
                row["unavailable_reason"] = "Qwen3-VL-8B Local is not running on both GPUs"
        elif served in live_set or any(served in x for x in live_set):
            row["available"] = True
            row["running"] = True
            row["startable"] = False
            row.pop("unavailable_reason", None)
        elif fam == "qwen" and any("qwen38" in x or "9b" in x or "vl-8b" in x for x in live_set):
            row["available"] = False
            row["running"] = False
            row["startable"] = False
            row["unavailable_reason"] = f"{tbl.get('name') or served} is not running"
        else:
            row["available"] = True
            row["running"] = True
            row["startable"] = False
            row.pop("unavailable_reason", None)
        if job_act in {"starting", "stopping"}:
            row["startable"] = False
            if fam != job.get("family"):
                row["available"] = False
                row["running"] = False
                row["unavailable_reason"] = (
                    "A local model is starting" if job_act == "starting" else "A local model is stopping"
                )
        out.append(row)
    return out


def ensure_model_runnable(model_id: str) -> None:
    mid = (model_id or "").strip()
    if not mid:
        raise ValueError("model id required")
    _, catalog = load_user_models()
    raw_tbl = catalog.get(mid)
    tbl: dict[str, Any] = raw_tbl if isinstance(raw_tbl, dict) else {}
    if not is_local_gpu_model(mid, tbl):
        return
    annotated = annotate_model_availability([{"id": mid, "name": tbl.get("name") or mid}], catalog)
    row = annotated[0] if annotated else {}
    if row.get("available") is False:
        raise ValueError(str(row.get("unavailable_reason") or f"{mid} is not running on the GPUs"))


def ensure_model_on_host(
    model_id: str,
    *,
    allow_current: str | None = None,
    bot: Any = None,
) -> None:
    """Reject ids that are not in this host's config.toml, then GPU-gate locals."""
    mid = (model_id or "").strip()
    if not mid:
        raise ValueError("model id required")
    _, catalog = load_user_models()
    tbl = catalog.get(mid) if isinstance(catalog.get(mid), dict) else {}
    if mid not in catalog and mid != (allow_current or ""):
        if str(mid).lower().startswith("grok") and looks_like_cloud_model(mid, tbl):
            return
        raise ValueError(f"unknown model {mid}")
    if bot_kind_is_agent(bot) and not is_exclusive_vllm_model(mid, tbl):
        return
    ensure_model_runnable(mid)


TEELA_SCRIPTS = Path(os.environ.get("HERMES_DESK_TEELA", str(_LOGIN_HOME / "teela")))
_LLM_JOB_LOCK = threading.Lock()
_LLM_JOB: dict[str, Any] = {"action": None, "target": None, "family": None, "started": 0.0, "error": ""}
_LLM_JOB_PROC: subprocess.Popen | None = None
_LLM_START_MAX_SEC = 720
_VLLM_CONTAINER = "teela-vllm"


_VLLM_FAIL_RE = re.compile(
    r"device index is out of range|Engine core initialization failed|"
    r"WorkerProc failed to start|No available memory for the cache blocks|"
    r"UR_RESULT_ERROR_DEVICE_LOST|OUT_OF_RESOURCES",
    re.I,
)
_VLLM_INIT_RE = re.compile(
    r"Initializing a V1 LLM engine|Application startup complete|Uvicorn running|"
    r"\[teela\] visible GPUs=|cooling Intel GPUs",
    re.I,
)


def _vllm_failure_from_log_text(text: str) -> str:
    """Classify a vLLM log blob. Empty means the latest event is a boot, not a dead start."""
    last_init = -1
    last_fail = -1
    fail_line = ""
    for i, line in enumerate((text or "").splitlines()):
        if _VLLM_INIT_RE.search(line):
            last_init = i
        if _VLLM_FAIL_RE.search(line):
            last_fail = i
            fail_line = line
    if last_fail < 0 or last_init > last_fail:
        return ""
    if re.search(r"device index is out of range", fail_line, re.I):
        return "The local model saw fewer GPUs than it expected. Stop it and start again — the launcher will use the GPUs that are actually visible."
    if re.search(r"Engine core initialization failed|WorkerProc failed to start", fail_line, re.I):
        return "Local model failed to start."
    if re.search(r"No available memory for the cache blocks", fail_line, re.I):
        return "The model does not fit KV cache on the visible GPUs. The launcher will retry with a shorter context."
    if re.search(r"UR_RESULT_ERROR_DEVICE_LOST|OUT_OF_RESOURCES", fail_line):
        return "The GPUs reset while loading the model. Wait a minute and start it again."
    return "Local model failed to start."


def _vllm_start_failure_from_logs() -> str:
    """Parse teela-vllm logs for a start that will never become :8000."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", "160", _VLLM_CONTAINER],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
    except Exception:
        return ""
    return _vllm_failure_from_log_text(f"{proc.stdout or ''}\n{proc.stderr or ''}")


def reap_local_llm_job() -> None:
    """Clear a stuck starting/stopping job when start.sh exits or vLLM crash-loops."""
    global _LLM_JOB_PROC
    with _LLM_JOB_LOCK:
        action = _LLM_JOB.get("action")
        if action not in {"starting", "stopping"}:
            return
        proc = _LLM_JOB_PROC
        started = float(_LLM_JOB.get("started") or 0)
        age = (time.time() - started) if started else 0.0
        rc = proc.poll() if proc is not None else None
    log_err = _vllm_start_failure_from_logs() if action == "starting" else ""
    with _LLM_JOB_LOCK:
        if _LLM_JOB.get("action") != action:
            return
        if action == "starting" and log_err and age >= 12:
            _LLM_JOB.update({"action": None, "error": log_err})
            _LLM_JOB_PROC = None
            return
        if rc is not None:
            _LLM_JOB_PROC = None
            if action == "starting" and rc != 0:
                _LLM_JOB.update({"action": None, "error": log_err or f"Local model start failed (exit {rc})"})
            else:
                _LLM_JOB.update({"action": None, "error": "" if rc == 0 else (_LLM_JOB.get("error") or "")})
            return
        if action == "starting" and age > _LLM_START_MAX_SEC:
            _LLM_JOB.update({"action": None, "error": "Local model start timed out. The GPUs never opened :8000."})
            _LLM_JOB_PROC = None


def local_llm_job_snapshot() -> dict[str, Any]:
    reap_local_llm_job()
    with _LLM_JOB_LOCK:
        return dict(_LLM_JOB)


def local_llm_start_target(mid: str, catalog: dict[str, Any] | None = None) -> str:
    """Map a picker id to /home/roni/teela/start.sh argument."""
    raw_tbl = (catalog or {}).get(mid) if catalog else None
    tbl: dict[str, Any] = raw_tbl if isinstance(raw_tbl, dict) else {}
    fam = _picker_gpu_family(mid, tbl)
    if fam == "muse":
        return "muse"
    if fam == "hybrid":
        return "hybrid"
    if fam == "vl8":
        return "qwen3-vl-8b"
    if fam == "qwen":
        return "qwen"
    return "serve"


def local_llm_status() -> dict[str, Any]:
    reset_local_llm_probe()
    live = probe_local_llm_ids(timeout=0.6)
    live_fam = _live_gpu_family(live)
    job = local_llm_job_snapshot()
    if job.get("action") == "starting" and job.get("family") and job.get("family") == live_fam:
        with _LLM_JOB_LOCK:
            if _LLM_JOB.get("action") == "starting":
                _LLM_JOB.update({"action": None, "error": ""})
        job = local_llm_job_snapshot()
    if job.get("action") == "stopping" and not live_fam:
        with _LLM_JOB_LOCK:
            if _LLM_JOB.get("action") == "stopping":
                _LLM_JOB.update({"action": None, "error": ""})
        job = local_llm_job_snapshot()
    default, catalog = load_user_models()
    _, models = host_picker_models(catalog, default, live_ids=live)
    return {
        "ok": True,
        "live_ids": list(live),
        "family": live_fam or None,
        "busy": job.get("action"),
        "target": job.get("target"),
        "error": job.get("error") or "",
        "models": models,
    }


_TEELA_LOG_HANDLES: list[Any] = []


def _run_teela_script(script: Path, args: list[str], log_name: str) -> subprocess.Popen:
    if not script.is_file():
        raise ValueError(f"Local GPU script missing: {script}")
    log_path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / log_name
    log_f = open(log_path, "ab")
    # Keep the fd alive; GC would close stdout of the still-running start.sh.
    _TEELA_LOG_HANDLES.append(log_f)
    return subprocess.Popen(
        ["bash", str(script), *args],
        cwd=str(script.parent),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=False,
    )


LLAMA_SERVER = Path(os.environ.get("HERMES_DESK_LLAMA_SERVER", str(_LOGIN_HOME / "opt/llama.cpp/llama-server")))
LLAMA_LIB_DIR = Path(os.environ.get("HERMES_DESK_LLAMA_LIB", str(_LOGIN_HOME / "opt/llama.cpp")))
FLASH_NEXT_SERVE = Path(os.environ.get("HERMES_DESK_FLASH_NEXT_SERVE", str(_LOGIN_HOME / "bin/serve-qwen38-flash-next.sh")))
QWEN38_27B_SERVE = Path(os.environ.get("HERMES_DESK_QWEN38_27B_SERVE", str(_LOGIN_HOME / "bin/serve-qwen38-27b.sh")))
_INDEP_PROCS: dict[str, subprocess.Popen] = {}
_INDEP_LOCK = threading.Lock()


def _model_listen_port(tbl: dict[str, Any] | None, default: int = 8080) -> int:
    url = str((tbl or {}).get("base_url") or "")
    if url:
        host = urlsplit(url if "://" in url else f"http://{url}")
        if host.port:
            return int(host.port)
    return default


def _pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.check_output(["ss", "-tlnp"], text=True, timeout=2)
    except Exception:
        return []
    pids: list[int] = []
    needle = f":{int(port)} "
    for line in out.splitlines():
        if needle not in line and not line.rstrip().endswith(f":{int(port)}"):
            continue
        for m in re.finditer(r"pid=(\d+)", line):
            pids.append(int(m.group(1)))
    return pids


def _kill_port(port: int) -> None:
    pids = _pids_on_port(port)
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    deadline = time.time() + 6
    while time.time() < deadline and _pids_on_port(port):
        time.sleep(0.2)
    for pid in _pids_on_port(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _find_primary_gguf(weights: Path) -> Path | None:
    if weights.is_file() and weights.suffix.lower() == ".gguf":
        return weights
    if not weights.is_dir():
        return None
    files = [p for p in weights.rglob("*.gguf") if p.is_file()]
    if not files:
        return None
    main = [
        p
        for p in files
        if "mtp" not in p.as_posix().lower()
        and "draft" not in p.name.lower()
        and "mmproj" not in p.name.lower()
    ]
    pool = main or files
    return max(pool, key=lambda p: p.stat().st_size)


def _next_local_port(catalog: dict[str, Any]) -> int:
    used: set[int] = set(_VLLM_EXCLUSIVE_PORTS)
    for tbl in catalog.values():
        if not isinstance(tbl, dict):
            continue
        used.add(_model_listen_port(tbl, default=0) or 0)
    used.discard(0)
    for port in range(8080, 8120):
        if port in used:
            continue
        if local_llm_port_open(f"http://127.0.0.1:{port}"):
            continue
        return port
    raise ValueError("no free local port between 8080 and 8119")


def _model_id_slug(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (raw or "").strip()).strip("-._").lower()
    return (s or "local-model")[:64]


def register_user_model(body: dict[str, Any]) -> dict[str, Any]:
    """Add or update a local picker row after weights land under ~/models."""
    mid = _model_id_slug(str(body.get("id") or body.get("key") or body.get("name") or ""))
    if not mid:
        raise ValueError("id is required")
    default, catalog = load_user_models()
    weights = str(body.get("weights") or body.get("model_dir") or "").strip()
    if weights:
        path = Path(weights).expanduser()
        if not path.is_absolute():
            path = MODELS_DIR / path
        weights = str(path)
        if not path.exists():
            raise ValueError(f"weights not found: {path}")
    base = str(body.get("base_url") or body.get("baseUrl") or "").strip()
    if not base:
        port = _next_local_port(catalog) if mid not in catalog else _model_listen_port(catalog.get(mid) or {})
        base = f"http://127.0.0.1:{port}/v1"
    tbl = dict(catalog.get(mid) or {}) if isinstance(catalog.get(mid), dict) else {}
    tbl["model"] = str(body.get("model") or tbl.get("model") or mid)
    tbl["name"] = str(body.get("name") or tbl.get("name") or mid)
    tbl["base_url"] = base
    tbl["api_backend"] = str(body.get("api_backend") or tbl.get("api_backend") or "chat_completions")
    if weights:
        tbl["weights"] = weights
    for key, src in (
        ("context_window", body.get("context_window") or body.get("contextWindow")),
        ("max_completion_tokens", body.get("max_completion_tokens") or body.get("maxCompletionTokens")),
    ):
        if src in (None, ""):
            continue
        try:
            n = int(src)
        except (TypeError, ValueError):
            continue
        if key == "max_completion_tokens" and n <= 0:
            tbl.pop(key, None)
            continue
        tbl[key] = n
    cap = coerce_max_completion_tokens(mid, tbl)
    if cap is not None:
        tbl["max_completion_tokens"] = cap
    else:
        tbl.pop("max_completion_tokens", None)
    catalog[mid] = tbl
    if not default:
        default = mid
    write_user_models(default, catalog)
    want, models = refresh_host_model_catalog()
    return {"ok": True, "id": mid, "default": want, "default_model": want, "models": models, "base_url": base}


def _start_independent_local(mid: str, tbl: dict[str, Any]) -> dict[str, Any]:
    if is_peer_llm_backup(tbl):
        url = str(tbl.get("base_url") or "")
        if url and local_llm_port_open(url):
            return local_llm_status()
        raise ValueError("Brain 27B tunnel is down (teela-llm-tunnel)")
    with _INDEP_LOCK:
        existing = _INDEP_PROCS.get(mid)
    if existing is not None and existing.poll() is None:
        return local_llm_status()
    port = _model_listen_port(tbl)
    want = str(tbl.get("model") or mid)
    if local_llm_port_open(f"http://127.0.0.1:{port}"):
        live = {x.lower() for x in probe_local_llm_ids(timeout=0.5)}
        if want.lower() in live:
            return local_llm_status()
        _kill_port(port)
    for other in _LLAMA_CPP_PORTS:
        if other != port:
            _kill_port(other)
    log_path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"local-model-{mid}.log"
    log_f = open(log_path, "ab")
    _TEELA_LOG_HANDLES.append(log_f)
    env = os.environ.copy()
    lib = str(LLAMA_LIB_DIR)
    cuda = str(_LOGIN_HOME / "opt/ft-venv/lib/python3.12/site-packages/nvidia/cu13/lib")
    env["LD_LIBRARY_PATH"] = f"{lib}:{cuda}:{env.get('LD_LIBRARY_PATH', '')}"
    cmd: list[str]
    if FLASH_NEXT_SERVE.is_file() and "flash-next" in mid.lower() and port == 8080:
        cmd = ["bash", str(FLASH_NEXT_SERVE)]
    elif QWEN38_27B_SERVE.is_file() and "27b" in mid.lower() and "flash" not in mid.lower() and "gptq" not in mid.lower():
        cmd = ["bash", str(QWEN38_27B_SERVE)]
    else:
        weights = resolve_model_weights(tbl)
        gguf = _find_primary_gguf(weights) if weights is not None else None
        if not LLAMA_SERVER.is_file():
            raise ValueError(f"local server missing: {LLAMA_SERVER}")
        if gguf is None:
            raise ValueError(f"no .gguf weights for {mid}; set weights to a folder under {MODELS_DIR}")
        cmd = [
            str(LLAMA_SERVER),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--alias",
            str(tbl.get("model") or mid),
            "-m",
            str(gguf),
            "--n-gpu-layers",
            "99",
        ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(LLAMA_LIB_DIR if LLAMA_LIB_DIR.is_dir() else Path.home()),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    with _INDEP_LOCK:
        _INDEP_PROCS[mid] = proc
    return local_llm_status()


def _stop_independent_local(mid: str, tbl: dict[str, Any]) -> dict[str, Any]:
    if is_peer_llm_backup(tbl):
        reset_local_llm_probe()
        return local_llm_status()
    port = _model_listen_port(tbl)
    with _INDEP_LOCK:
        proc = _INDEP_PROCS.pop(mid, None)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    _kill_port(port)
    reset_local_llm_probe()
    return local_llm_status()


def _stop_exclusive_gpu() -> dict[str, Any]:
    global _LLM_JOB_PROC
    with _LLM_JOB_LOCK:
        _LLM_JOB.update({"action": "stopping", "target": "stop", "family": None, "started": time.time(), "error": ""})
        _LLM_JOB_PROC = None
    try:
        proc = _run_teela_script(TEELA_SCRIPTS / "stop.sh", [], "teela-llm-stop.log")
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            pass
    except Exception as e:
        with _LLM_JOB_LOCK:
            _LLM_JOB.update({"action": None, "error": str(e)})
        raise
    reset_local_llm_probe()
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _live_gpu_family(probe_local_llm_ids(timeout=0.4)):
            break
        time.sleep(0.4)
    reset_local_llm_probe()
    with _LLM_JOB_LOCK:
        _LLM_JOB.update({"action": None, "error": ""})
    return local_llm_status()


def local_llm_stop(model_id: str = "") -> dict[str, Any]:
    mid = (model_id or "").strip()
    _, catalog = load_user_models()
    tbl = catalog.get(mid) if mid and isinstance(catalog.get(mid), dict) else {}
    if mid and is_llama_cpp_model(mid, tbl or {}):
        return _stop_independent_local(mid, tbl or {})
    return _stop_exclusive_gpu()


def local_llm_start(model_id: str) -> dict[str, Any]:
    mid = (model_id or "").strip()
    if not mid:
        raise ValueError("model id required")
    _, catalog = load_user_models()
    if mid not in catalog:
        raise ValueError(f"unknown model {mid}")
    raw_tbl = catalog.get(mid)
    tbl: dict[str, Any] = raw_tbl if isinstance(raw_tbl, dict) else {}
    if not is_local_gpu_model(mid, tbl):
        raise ValueError(f"{mid} is not a local GPU model")
    if is_llama_cpp_model(mid, tbl):
        return _start_independent_local(mid, tbl)
    fam = _picker_gpu_family(mid, tbl)
    target = local_llm_start_target(mid, catalog)
    weights = resolve_model_weights(tbl)
    args = [target]
    if weights is not None:
        if not weights.exists():
            raise ValueError(f"weights not found: {weights}")
        served = str(tbl.get("model") or mid).strip() or mid
        args = ["serve", str(weights), served]
        target = "serve"
    elif target == "serve":
        raise ValueError(
            f"{mid} is a local model but has no weights path. "
            "In Settings, set weights to a folder under ~/models, then Start."
        )
    live = probe_local_llm_ids(timeout=0.5)
    live_fam = _live_gpu_family(live)
    if live_fam and live_fam == fam and weights is None:
        return local_llm_status()
    global _LLM_JOB_PROC
    with _LLM_JOB_LOCK:
        if _LLM_JOB.get("action") in {"starting", "stopping"}:
            age = time.time() - float(_LLM_JOB.get("started") or 0)
            if age < 90:
                raise ValueError(f"Local GPU is already {_LLM_JOB['action']}")
        _LLM_JOB.update({"action": "starting", "target": target, "family": fam, "started": time.time(), "error": ""})
        _LLM_JOB_PROC = None
    try:
        proc = _run_teela_script(TEELA_SCRIPTS / "start.sh", args, "teela-llm-start.log")
        with _LLM_JOB_LOCK:
            _LLM_JOB_PROC = proc
    except Exception as e:
        with _LLM_JOB_LOCK:
            _LLM_JOB.update({"action": None, "error": str(e)})
            _LLM_JOB_PROC = None
        raise
    return local_llm_status()


def local_llm_control(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or body.get("cmd") or "").strip().lower()
    if action in {"stop", "down"}:
        return local_llm_stop(str(body.get("model") or body.get("id") or ""))
    if action in {"start", "up", "play"}:
        return local_llm_start(str(body.get("model") or body.get("id") or ""))
    if action in {"register", "add"}:
        return register_user_model(body)
    if action in {"delete", "remove"}:
        return delete_user_model(
            str(body.get("model") or body.get("id") or ""),
            delete_weights=bool(body.get("delete_weights") or body.get("weights")),
        )
    if action in {"catalog", "save"}:
        rows = body.get("models")
        if not isinstance(rows, list):
            raise ValueError("models list required")
        return save_user_model_catalog(rows, str(body.get("default_model") or body.get("default") or ""))
    raise ValueError("action must be start, stop, register, save, or delete")


_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_MODEL_TABLE_FIELDS = {
    "model",
    "name",
    "base_url",
    "api_backend",
    "context_window",
    "api_key",
    "weights",
    "model_dir",
    "max_completion_tokens",
}


def coerce_max_completion_tokens(mid: str, tbl: dict[str, Any] | None) -> int | None:
    """Hermes CLI sends this as max_tokens. 0 is rejected by the xAI API."""
    tbl = tbl or {}
    try:
        n = int(tbl.get("max_completion_tokens") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    if str(mid or "").lower().startswith("grok"):
        return 65536
    return None


def toml_key(name: str) -> str:
    """Quote TOML keys that contain dots so grok-4.6 is not parsed as grok-4.6 nested."""
    s = str(name or "")
    if _TOML_BARE_KEY.fullmatch(s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def flatten_model_tables(models: dict[str, Any] | None) -> dict[str, Any]:
    """Recover [model.grok-4.6] tables that TOML nested as model.grok-4.6."""
    out: dict[str, Any] = {}
    for key, val in (models or {}).items():
        if not isinstance(val, dict):
            continue
        if _MODEL_TABLE_FIELDS & val.keys():
            out[str(key)] = val
            continue
        if val and all(isinstance(inner, dict) for inner in val.values()):
            for sub, inner in val.items():
                out[f"{key}.{sub}"] = inner
        else:
            out[str(key)] = val
    return out


def _legacy_grok_toml_path() -> Path:
    """Legacy Grok Desk config (test-isolatable via HERMES_DESK_LEGACY_TOML)."""
    return Path(os.environ.get("HERMES_DESK_LEGACY_TOML") or (Path.home() / ".grok" / "config.toml"))


def _migrate_legacy_grok_catalog() -> None:
    """One-time: carry the Grok Desk model picker into models.json on first run."""
    cat_path = agent_home_mod.models_catalog_path()
    if cat_path.is_file():
        return
    legacy = _legacy_grok_toml_path()
    if not legacy.is_file():
        return
    try:
        with legacy.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return
    models: dict[str, Any] = {}
    for key, val in data.items():
        if key == "model" and isinstance(val, dict):
            models = flatten_model_tables(val)
    # xAI cloud rows (api_backend responses/xai) need the x.ai API + account;
    # the Hermes agent only speaks OpenAI-compatible endpoints, so drop them.
    if not models:
        return
    models_tbl = data.get("models") or {}
    default = str(models_tbl.get("default") or next(iter(models)))
    try:
        agent_home_mod.save_bot_catalog(default, models)
        print(f"[deskd] migrated {len(models)} model rows from legacy ~/.grok/config.toml", flush=True)
    except Exception:
        pass


def _legacy_toml_models() -> dict[str, Any]:
    """Model tables from the legacy ~/.grok/config.toml, or {} when absent."""
    legacy = _legacy_grok_toml_path()
    if not legacy.is_file():
        return {}
    try:
        with legacy.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    for key, val in data.items():
        if key == "model" and isinstance(val, dict):
            return flatten_model_tables(val)
    return {}


def _heal_catalog(default: str, catalog: dict[str, Any]) -> None:
    """Re-import usable rows when the shared catalog lost them.

    Symptom this guards against: the catalog shrinks to a single keyless cloud
    row (no xAI key on host), which then becomes the default and every new bot
    fails with "No LLM provider configured". Re-add local/custom rows that the
    legacy TOML still defines but models.json dropped; prefer a usable default.
    """
    if not isinstance(catalog, dict):
        catalog = {}
    legacy = _legacy_toml_models()
    merged = dict(catalog)
    added = False
    for mid, tbl in legacy.items():
        if not isinstance(tbl, dict):
            continue
        if mid in merged:
            continue
        if not model_has_provider(mid, tbl):
            continue
        merged[mid] = dict(tbl)
        added = True
    if not added:
        return
    want = default if (default in merged and model_has_provider(default, merged.get(default))) else ""
    if not want:
        usable = [mid for mid in merged if model_has_provider(mid, merged.get(mid))]
        want = usable[0] if usable else next(iter(merged))
    try:
        agent_home_mod.save_bot_catalog(want, merged)
        print(f"[deskd] healed model catalog: re-added {sorted(set(legacy) - set(catalog))}", flush=True)
    except Exception:
        pass


def load_user_models() -> tuple[str, dict[str, Any]]:
    """The desk model picker (shared models.json), with host fallbacks.

    Rows are OpenAI-compatible endpoints (local llama.cpp/vLLM upstreams or
    remote gateways). When the desk has no catalog yet (fresh install, no
    legacy grok config), the host Hermes default model is offered as the
    default so the picker is never empty.
    """
    default, catalog = agent_home_mod.load_bot_catalog()
    if not catalog:
        _migrate_legacy_grok_catalog()
        default, catalog = agent_home_mod.load_bot_catalog()
    if not catalog:
        host_default = _host_hermes_default_model()
        if host_default:
            return host_default, {}
    # Self-heal: if the catalog lost EVERY usable row (e.g. clobbered down to a
    # single keyless cloud row), re-import local rows from the legacy TOML so
    # the picker can still serve a model. A catalog that still has at least one
    # usable row is left alone — only its default is corrected below.
    if not any(model_has_provider(mid, catalog.get(mid)) for mid in catalog):
        _heal_catalog(default, catalog)
        default, catalog = agent_home_mod.load_bot_catalog()
    if not default and catalog:
        default = next(iter(catalog))
    elif default and catalog and default not in catalog:
        default = next(iter(catalog))
    # The default must be a model this host can actually serve. A keyless
    # cloud row (no xAI key) as default makes every new bot fail with
    # "No LLM provider configured" — fall back to the first usable row.
    if catalog and not model_has_provider(default, catalog.get(default)):
        usable = [mid for mid in catalog if model_has_provider(mid, catalog.get(mid))]
        if usable:
            default = usable[0]
    return default, catalog


def model_has_provider(mid: str, tbl: dict[str, Any] | None = None) -> bool:
    """True when a Hermes child could actually serve this model on this host.

    Cloud models (xAI ``responses``/``xai`` backends, ``grok-*`` ids) need an
    xAI key; custom endpoints need a reachable base_url or an api_key. Without
    this check a keyless cloud row can become the picker default and every new
    bot dies with "No LLM provider configured".
    """
    tbl = tbl or {}
    backend = str(tbl.get("api_backend") or "").lower()
    api_key = str(tbl.get("api_key") or "").strip()
    base_url = str(tbl.get("base_url") or "").strip()
    if is_local_gpu_model(mid, tbl):
        # Startable/served locally; GPU gating is done separately.
        return True
    if backend in {"responses", "xai"} or str(mid or "").lower().startswith("grok"):
        return bool(_xai_api_key())
    if base_url:
        return bool(api_key) or local_llm_port_open(base_url)
    return bool(api_key)


def _host_hermes_default_model() -> str:
    """Current default model of the host Hermes install (config.yaml), if any."""
    try:
        cfg = USER_AGENT_HOME / "config.yaml"
        if not cfg.is_file():
            return ""
        import yaml as _yaml

        data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        model = (data.get("model") or {})
        if isinstance(model, dict):
            return str(model.get("default") or "").strip()
        return str(model or "").strip()
    except Exception:
        return ""


def _strip_toml_model_tables(text: str) -> str:
    keep: list[str] = []
    skip = False
    for line in text.splitlines(True):
        s = line.strip()
        if s.startswith("[") and "]" in s:
            hdr = s.split("]", 1)[0][1:].strip()
            skip = hdr == "models" or hdr.startswith("model.")
        if not skip:
            keep.append(line)
    return "".join(keep).rstrip() + ("\n\n" if keep else "")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def load_user_mcp_servers() -> dict[str, dict[str, Any]]:
    """Enabled ``mcp_servers`` entries from the host ``~/.hermes/config.yaml``."""
    cfg_path = USER_AGENT_HOME / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        import yaml as _yaml

        data = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    raw = data.get("mcp_servers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled", True) is False:
            continue
        key = str(name or "").strip()
        if key:
            out[key] = spec
    return out


def user_mcp_acp_specs() -> list[dict[str, Any]]:
    """ACP stdio specs for enabled host MCP servers (skips URL-only remotes)."""
    out: list[dict[str, Any]] = []
    for name, spec in load_user_mcp_servers().items():
        command = str(spec.get("command") or "").strip()
        if not command:
            continue
        raw_args = spec.get("args") if isinstance(spec.get("args"), list) else []
        env_tbl = spec.get("env") if isinstance(spec.get("env"), dict) else {}
        out.append(
            {
                "name": name,
                "command": command,
                "args": [str(a) for a in raw_args],
                "env": [{"name": str(k), "value": str(v)} for k, v in env_tbl.items()],
            }
        )
    return out


def _append_mcp_servers_toml(lines: list[str], servers: dict[str, dict[str, Any]]) -> None:
    nested = ("env", "headers", "tool_timeouts")
    for name, spec in servers.items():
        lines.append(f"[mcp_servers.{toml_key(name)}]")
        for key, val in spec.items():
            if key in nested or val is None:
                continue
            if isinstance(val, list):
                if not val:
                    lines.append(f"{key} = []")
                    continue
                lines.append(f"{key} = [")
                for item in val:
                    lines.append(f"    {_toml_scalar(item)},")
                lines.append("]")
            elif isinstance(val, dict):
                continue
            else:
                lines.append(f"{key} = {_toml_scalar(val)}")
        lines.append("")
        for sub in nested:
            tbl = spec.get(sub)
            if not isinstance(tbl, dict) or not tbl:
                continue
            lines.append(f"[mcp_servers.{toml_key(name)}.{sub}]")
            for k, v in tbl.items():
                lines.append(f"{k} = {_toml_scalar(v)}")
            lines.append("")


def write_user_models(default: str, catalog: dict[str, Any]) -> None:
    """Persist the desk model picker (shared models.json)."""
    cleaned: dict[str, Any] = {}
    for mid, tbl in catalog.items():
        if not isinstance(tbl, dict):
            continue
        tbl = dict(tbl)
        cap = coerce_max_completion_tokens(mid, tbl)
        if cap is None:
            tbl.pop("max_completion_tokens", None)
        else:
            tbl["max_completion_tokens"] = cap
        cleaned[mid] = tbl
    agent_home_mod.save_bot_catalog(default, cleaned)


def catalog_from_settings_rows(rows: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mapping = {
        "model": "model",
        "name": "name",
        "base_url": "base_url",
        "baseUrl": "base_url",
        "api_backend": "api_backend",
        "apiBackend": "api_backend",
        "context_window": "context_window",
        "contextWindow": "context_window",
        "max_completion_tokens": "max_completion_tokens",
        "maxCompletionTokens": "max_completion_tokens",
        "api_key": "api_key",
        "apiKey": "api_key",
        "weights": "weights",
        "model_dir": "weights",
        "modelDir": "weights",
    }
    ints = {"context_window", "max_completion_tokens"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("key") or row.get("id") or "").strip()
        if not mid:
            continue
        tbl: dict[str, Any] = {}
        for src, dst in mapping.items():
            if src not in row or row[src] in (None, ""):
                continue
            val: Any = row[src]
            if dst in ints:
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue
                if dst == "max_completion_tokens" and val <= 0:
                    continue
            tbl[dst] = val
        cap = coerce_max_completion_tokens(mid, tbl)
        if cap is not None:
            tbl["max_completion_tokens"] = cap
        else:
            tbl.pop("max_completion_tokens", None)
        if "model" not in tbl:
            tbl["model"] = mid
        out[mid] = tbl
    return out


def settings_model_rows() -> tuple[str, list[dict[str, Any]]]:
    default, catalog = load_user_models()
    rows: list[dict[str, Any]] = []
    for mid, tbl in catalog.items():
        if not isinstance(tbl, dict):
            continue
        rows.append(
            {
                "key": mid,
                "id": mid,
                "model": tbl.get("model") or mid,
                "name": tbl.get("name") or mid,
                "baseUrl": tbl.get("base_url") or "",
                "apiBackend": tbl.get("api_backend") or "",
                "contextWindow": tbl.get("context_window") or 0,
                "maxCompletionTokens": coerce_max_completion_tokens(mid, tbl) or 0,
                "apiKey": tbl.get("api_key") or "",
                "weights": tbl.get("weights") or tbl.get("model_dir") or "",
                "local": is_local_gpu_model(mid, tbl),
            }
        )
    return default, rows


def resolve_model_weights(tbl: dict[str, Any]) -> Path | None:
    raw = str(tbl.get("weights") or tbl.get("model_dir") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = MODELS_DIR / raw
    return path


def contained_under_models(path: Path) -> Path:
    root = MODELS_DIR.resolve()
    got = path.expanduser().resolve()
    got.relative_to(root)
    if got == root:
        raise ValueError("refusing to delete the whole models directory")
    return got


def refresh_host_model_catalog() -> tuple[str, list[dict[str, Any]]]:
    default, catalog = load_user_models()
    want, rows = host_picker_models(catalog, default)
    for bot in list(bots.values()):
        extra = [bot.model] if bot.model else []
        _, next_rows = host_picker_models(catalog, default, extra_ids=extra)
        if bot_kind_is_agent(bot):
            have = {str(r.get("id") or "") for r in next_rows}
            for prev in bot.models or []:
                pid = str(prev.get("id") or "")
                if pid and pid not in have and not prev.get("local"):
                    next_rows.append(dict(prev))
                    have.add(pid)
        bot.models = next_rows
        try:
            write_child_config(
                bot.agent_home,
                bot.model,
                catalog,
                bot_id=bot.id,
                permission_mode="always-approve" if bot_kind_has_host_coding(bot) else "default",
                inherit_mcp=bot_kind_is_agent(bot),
            )
        except Exception:
            pass
    emit({"type": "models.updated", "default": want, "models": rows})
    return want, rows


def save_user_model_catalog(rows: list[Any], default: str = "") -> dict[str, Any]:
    catalog = catalog_from_settings_rows(rows)
    if not catalog:
        raise ValueError("keep at least one model in the picker")
    mid = (default or "").strip()
    if mid not in catalog:
        mid = next(iter(catalog))
    write_user_models(mid, catalog)
    want, models = refresh_host_model_catalog()
    return {"ok": True, "default": want, "default_model": want, "models": models}


def delete_user_model(model_id: str, *, delete_weights: bool = False) -> dict[str, Any]:
    mid = (model_id or "").strip()
    default, catalog = load_user_models()
    tbl = catalog.pop(mid, None)
    if tbl is None:
        raise ValueError(f"unknown model {mid}")
    if not catalog:
        raise ValueError("keep at least one model in the picker")
    if default == mid:
        default = next(iter(catalog))
    write_user_models(default, catalog)
    deleted = ""
    if delete_weights:
        path = resolve_model_weights(tbl) if isinstance(tbl, dict) else None
        if path is None:
            raise ValueError("this picker row has no weights path")
        try:
            victim = contained_under_models(path)
        except ValueError as e:
            raise ValueError(f"weights must live under {MODELS_DIR}: {e}") from e
        if victim.is_dir():
            shutil.rmtree(victim)
            deleted = str(victim)
        elif victim.is_file():
            victim.unlink()
            deleted = str(victim)
        else:
            raise ValueError(f"no files at {victim}")
    want, models = refresh_host_model_catalog()
    return {
        "ok": True,
        "default": want,
        "default_model": want,
        "models": models,
        "deleted": mid,
        "deleted_weights": deleted,
    }


_HIDDEN_PICKER_IDS = {
    "qwen38-9b-distill",
    "qwen3.8-9b-distill",
    "qwen38-9b",
    "qwen3.8-9b",
}


def model_effort_info(tbl: dict[str, Any] | None) -> dict[str, Any]:
    """Picker fields for /model and /effort (current thinking level + menu)."""
    tbl = tbl if isinstance(tbl, dict) else {}
    rows: list[dict[str, Any]] = []
    raw = tbl.get("reasoning_efforts")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            val = str(item.get("value") or item.get("id") or "").strip()
            if not val:
                continue
            rows.append(
                {
                    "id": str(item.get("id") or val),
                    "value": val,
                    "label": str(item.get("label") or val),
                    "description": str(item.get("description") or ""),
                    "default": bool(item.get("default")),
                }
            )
    current = str(tbl.get("reasoning_effort") or "").strip()
    if not current and rows:
        current = next((r["value"] for r in rows if r.get("default")), rows[0]["value"])
    out: dict[str, Any] = {}
    if current:
        out["reasoning_effort"] = current
    if rows:
        out["reasoning_efforts"] = rows
    if tbl.get("supports_reasoning_effort") is not None:
        out["supports_reasoning_effort"] = bool(tbl.get("supports_reasoning_effort"))
    elif rows:
        out["supports_reasoning_effort"] = True
    return out


_REASONING_EFFORTS = frozenset({"off", "none", "minimal", "low", "medium", "high", "xhigh", "max"})
_OFF_EFFORTS = frozenset({"off", "none", "disabled"})


def normalize_reasoning_effort(level: str) -> str:
    v = str(level or "").strip().lower()
    if v in _OFF_EFFORTS:
        return "off"
    return v


def agent_effort_wire(level: str) -> str:
    v = normalize_reasoning_effort(level)
    return "none" if v == "off" else v


def current_model_effort(bot: Any) -> str:
    effort = normalize_reasoning_effort(getattr(bot, "effort", "") or "")
    if effort:
        return effort
    mid = str(getattr(bot, "model", "") or "")
    for row in getattr(bot, "models", None) or []:
        if str(row.get("id") or "") == mid:
            return normalize_reasoning_effort(row.get("reasoning_effort") or "")
    return ""




_CLOUD_PICKER = (
    ("grok-4.6", "Grok 4.6", 500000),
    ("grok-4.5", "Grok 4.5", 256000),
)


def _append_cloud_picker_rows(rows: list[dict[str, Any]], catalog: dict[str, Any] | None) -> None:
    """Always offer the xAI Grok 4.6 / 4.5 cloud models (via host auth.json) even when
    the legacy picker file omitted them."""
    have = {str(r.get("id") or "") for r in rows}
    for mid, name, ctx in _CLOUD_PICKER:
        if mid in have:
            continue
        raw = (catalog or {}).get(mid)
        row: dict[str, Any] = {"id": mid, "name": name, "context_window": ctx}
        if isinstance(raw, dict):
            row["name"] = raw.get("name") or name
            row["context_window"] = raw.get("context_window") or ctx
            row.update(model_effort_info(raw))
        rows.append(row)
        have.add(mid)


def host_picker_models(
    catalog: dict[str, Any] | None = None,
    default: str | None = None,
    live_ids: tuple[str, ...] | None = None,
    extra_ids: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """This host's picker: the shared desk models.json catalog."""
    if catalog is None:
        loaded_default, catalog = load_user_models()
        if default is None:
            default = loaded_default
    elif default is None:
        default, _ = load_user_models()
    rows: list[dict[str, Any]] = []
    for mid, tbl in catalog.items():
        if not isinstance(tbl, dict):
            continue
        if str(mid).lower() in _HIDDEN_PICKER_IDS:
            continue
        row = {
            "id": mid,
            "name": tbl.get("name") or mid,
            "context_window": tbl.get("context_window"),
        }
        row.update(model_effort_info(tbl))
        rows.append(row)
    for mid in extra_ids or []:
        sid = (mid or "").strip()
        if sid.lower() in _HIDDEN_PICKER_IDS:
            sid = "qwen3-vl-8b" if "qwen3-vl-8b" in (catalog or {}) else ""
        if sid and not any(r["id"] == sid for r in rows):
            raw = (catalog or {}).get(sid)
            name = raw.get("name") if isinstance(raw, dict) else sid
            extra = {"id": sid, "name": name or sid}
            if isinstance(raw, dict):
                extra.update(model_effort_info(raw))
            rows.append(extra)
    _append_cloud_picker_rows(rows, catalog)
    want = (default or "").strip()
    ids = {r["id"] for r in rows}
    if want.lower() in _HIDDEN_PICKER_IDS and "qwen3-vl-8b" in ids:
        want = "qwen3-vl-8b"
    if want not in ids:
        want = rows[0]["id"] if rows else ""
    return want, annotate_model_availability(rows, catalog, live_ids=live_ids)


# Qwen3.8 chat_template.jinja only accepts xhigh | medium | low (default xhigh).
# Hermes Agent often sends high/minimal/none, which 500s the template.
QWEN_REASONING_EFFORT = os.environ.get("HERMES_DESK_QWEN_REASONING_EFFORT", "low").strip() or "low"
_QWEN_EFFORTS = {"xhigh", "medium", "low"}
# MiniOS body commands: skip the <think> block so robot_* can fire on the first tokens.
_MOTOR_USER_RE = re.compile(
    r"\b(?:"
    r"robot_(?:status|joint|pose|motion)"
    r"|neck_pan|neck_tilt|left_shoulder|right_shoulder|left_elbow|right_elbow"
    r"|left_wrist|right_wrist|upper_back_pitch|lower_back_roll"
    r"|left_hip|right_hip|left_knee|right_knee|left_ankle|right_ankle"
    r"|hands_up|tpose|t-pose|estop|motors?"
    r"|wave|walk|squat|bow|sit|stand|ready|relax|attention|demo|reset|stop|halt"
    r"|raise|lower|nod|lean|tilt|pose|bend|straighten|flex"
    r"|look\s+(?:to\s+(?:your\s+)?)?(?:the\s+)?(?:left|right|up|down|straight|ahead|forward|center)"
    r"|point\s+(?:at|to|your)"
    r"|move\s+your"
    r"|your\s+(?:arm|arms|leg|legs|head|neck|hand|hands|body|torso|shoulder|elbow|"
    r"wrist|hip|knee|ankle|joint|joints|foot|feet)"
    r"|sit\s+down|stand\s+up"
    r"|robot\s+simulator|teela\s+body"
    r")\b",
    re.IGNORECASE,
)
_MOTOR_TOOL_RE = re.compile(
    r"\b(?:robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity))\b",
    re.IGNORECASE,
)


_IMAGE_PART_TYPES = {"image", "image_url", "input_image", "image_file"}
_MM_MAX_BYTES = 8_000_000


def _file_to_data_url(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _MM_MAX_BYTES:
        return None
    ext = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def hydrate_local_mm_parts(payload: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
    """Turn workspace / file:// image URLs into data URLs vLLM can decode."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    changed = False
    out: list[Any] = []
    for m in msgs:
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        row = dict(m)
        parts: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            ptype = str(part.get("type") or "").lower()
            raw_data = part.get("data")
            mime = str(part.get("mimeType") or part.get("mime") or "").split(";")[0].strip().lower()
            if raw_data and (ptype in ("image", "input_image") or mime.startswith("image/")):
                blob = str(raw_data)
                if not blob.startswith("data:"):
                    blob = f"data:{mime or 'image/jpeg'};base64,{blob}"
                parts.append({"type": "image_url", "image_url": {"url": blob}})
                changed = True
                continue
            url = ""
            if ptype in _IMAGE_PART_TYPES:
                blob = part.get("image_url")
                if isinstance(blob, dict):
                    url = str(blob.get("url") or "")
                elif isinstance(blob, str):
                    url = blob
                url = url or str(part.get("image") or part.get("url") or "")
            if url.startswith("data:") or url.startswith("http://") or url.startswith("https://"):
                parts.append(part)
                continue
            local = ""
            if url.startswith("file://"):
                local = url[7:]
            elif url and not url.startswith("/v1/"):
                local = url
            path: Path | None = None
            if local:
                p = Path(local)
                if p.is_absolute() and p.is_file():
                    path = p
                elif workspace is not None:
                    cand = (workspace / local.lstrip("/")).resolve()
                    try:
                        cand.relative_to(workspace.resolve())
                        if cand.is_file():
                            path = cand
                    except ValueError:
                        path = None
            data = _file_to_data_url(path) if path else None
            if data:
                parts.append({"type": "image_url", "image_url": {"url": data}})
                changed = True
            else:
                parts.append(part)
        row["content"] = parts
        out.append(row)
    if changed:
        payload = dict(payload)
        payload["messages"] = out
    return payload


def _flatten_message_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    bits: list[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        bits.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                bits.append(str(part.get("text") or part.get("content") or ""))
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        bits.append(str(call.get("name") or fn.get("name") or ""))
        args = call.get("arguments") or fn.get("arguments") or ""
        if isinstance(args, dict):
            bits.append(json.dumps(args))
        else:
            bits.append(str(args))
    bits.append(str(msg.get("name") or ""))
    return "\n".join(b for b in bits if b)


def local_llm_motor_turn(payload: dict[str, Any]) -> bool:
    """True when this completion should skip thinking so MiniOS motors can move immediately."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    last_user = ""
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _flatten_message_text(msg)
            last_user_idx = i
            break
    intent = user_intent_text(last_user)
    if intent and robot_sim.looks_like_body_query(intent):
        return False
    mixed = orch.classify(intent).get("mode") if intent else "none"
    if mixed in {"parallel", "after"}:
        return False
    if intent and (
        _MOTOR_USER_RE.search(intent)
        or robot_sim.looks_like_motor(intent)
        or robot_sim.infer_command(intent)
    ):
        return True
    if last_user_idx < 0:
        return False
    # Same user turn only: robot_* tool calls/results after that message.
    for msg in msgs[last_user_idx + 1 :]:
        if _MOTOR_TOOL_RE.search(_flatten_message_text(msg)):
            return True
    return False


def local_llm_body_query_turn(payload: dict[str, Any]) -> bool:
    """True when this completion should speak from I-feel, not call MiniOS tools."""
    if local_llm_motor_turn(payload) or payload_already_moved(payload):
        return False
    intent = last_user_intent_from_payload(payload)
    if not intent:
        return False
    if looks_like_system_check(intent) or looks_like_talk(intent):
        return False
    if robot_sim.looks_like_motor(intent) or robot_sim.infer_command(intent):
        return False
    return bool(robot_sim.looks_like_body_query(intent))


def looks_like_system_check(text: str) -> bool:
    """True when they asked Teela to diagnose herself, not to move or chat."""
    return orch.looks_like_system_work(text) or looks_like_minios_workspace_check(text) or looks_like_host_system_check(text)


_MINIOS_CHECK_RE = re.compile(
    r"\b(?:"
    r"workspace|mini-?os|your\s+desk|your\s+workspace|desktop\s+twin|virtual\s+(?:body|twin)|observer|"
    r"check(?:ing)?\s+(?:your\s+)?(?:workspace|desk|minios|twin)"
    r")\b",
    re.I,
)
_HOST_CHECK_RE = re.compile(
    r"\b(?:"
    r"teela-brain|main\s+system|host\s+system|this\s+(?:computer|machine|box|server|host)|"
    r"the\s+host|gpus?\b|vram|llama(?:\.cpp)?|"
    r"check(?:ing)?\s+(?:the\s+)?(?:main\s+system|teela-brain|host(?:\s+system)?)"
    r")\b",
    re.I,
)


def looks_like_minios_workspace_check(text: str) -> bool:
    t = " ".join(visible_user_text(user_intent_text(text or "")).lower().split())
    return bool(t and _MINIOS_CHECK_RE.search(t))


def looks_like_host_system_check(text: str) -> bool:
    t = " ".join(visible_user_text(user_intent_text(text or "")).lower().split())
    if not t:
        return False
    if looks_like_minios_workspace_check(t) and not _HOST_CHECK_RE.search(t):
        return False
    return bool(_HOST_CHECK_RE.search(t))


def teela_check_confirm_payload(text: str) -> dict[str, Any]:
    """Confirm only when a check is ambiguous — not when workspace vs host is already clear."""
    raw = text or ""
    if not looks_like_system_check(raw) and not looks_like_system_check(visible_user_text(raw)):
        return {"confirm": False}
    t = " ".join(visible_user_text(user_intent_text(raw)).lower().split())
    mini = looks_like_minios_workspace_check(t)
    host = looks_like_host_system_check(t)
    if mini and not host:
        return {"confirm": False}
    if host and not mini:
        return {"confirm": False}
    guess = teela_check_scope(raw)
    question = "Which check did you mean?"
    options = rank_clarify_options(
        [
            {"id": "minios", "label": "MiniOS workspace", "hint": "twin, observer, her desk"},
            {"id": "host", "label": "teela-brain", "hint": "this computer, GPUs, llama"},
            {"id": "both", "label": "Both", "hint": "workspace and host"},
        ],
        guess=guess,
    )
    return {"confirm": True, "guess": guess, "question": question, "options": options}


def teela_check_scope(text: str) -> str:
    """minios | host | both — workspace vs teela-brain host."""
    t = " ".join(visible_user_text(user_intent_text(text or "")).lower().split())
    host = looks_like_host_system_check(t)
    mini = looks_like_minios_workspace_check(t)
    if host and mini:
        return "both"
    if host:
        return "host"
    if mini:
        return "minios"
    return "minios"


_TEAMMATE_INTENT_RE = re.compile(
    r"\b(?:"
    r"(?:check|sync(?:\s+up)?|coordinate|talk|speak|message|ask|ping)\s+(?:in\s+)?with\b|"
    r"in sync|"
    r"teammates?|"
    r"(?:you two|the two of you|both of you).{0,48}sync"
    r")\b",
    re.I,
)
_LIST_TEAMMATES_RE = re.compile(r"\blist(?: your)? teammates\b", re.I)


def looks_like_teammate_work(text: str) -> bool:
    """True when they asked Teela to ping/sync with another bot, not to pose."""
    t = " ".join(user_intent_text(visible_user_text(text or "")).lower().split())
    if not t or looks_like_system_check(t):
        return False
    return bool(_TEAMMATE_INTENT_RE.search(t))


def _teammate_candidates(bot: Any) -> list[tuple[str, str, Any]]:
    """(name, id, local_bot_or_None) for everyone except this bot."""
    out: list[tuple[str, str, Any]] = []
    bid = str(getattr(bot, "id", "") or "")
    for other in bots.values():
        oid = str(getattr(other, "id", "") or "")
        if not oid or oid == bid:
            continue
        out.append((str(getattr(other, "name", "") or oid), oid, other))
    cl = cluster
    if cl is not None:
        cache = getattr(cl, "roster_cache", None) or {}
        try:
            rows = list(cache.items()) if isinstance(cache, dict) else []
        except Exception:
            rows = []
        seen = {oid for _name, oid, _obj in out}
        for _peer, listing in rows:
            for row in listing or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "")
                if not rid or rid == bid or rid in seen:
                    continue
                seen.add(rid)
                out.append((str(row.get("name") or rid), rid, None))
    return out


def teammate_spec_from_intent(text: str, bot: Any) -> str | None:
    t = " ".join(user_intent_text(visible_user_text(text or "")).lower().split())
    if not t:
        return None
    cands = _teammate_candidates(bot)
    for name, oid, _obj in sorted(cands, key=lambda x: len(x[0]), reverse=True):
        n = name.lower().strip()
        if n and n in t:
            return name
        if oid.lower() in t:
            return oid
    if re.search(r"\bbody\b", t):
        for name, _oid, _obj in cands:
            if "body" in name.lower() or "body" in _oid.lower():
                return name
        return "Body Bot"
    if cands and re.search(r"\b(?:you two|the two of you|both of you|in sync)\b", t):
        for name, _oid, _obj in cands:
            if "body" in name.lower():
                return name
        return cands[0][0]
    return None


def ping_teammate(bot: Any, intent: str) -> str | None:
    """Deterministic teammate ping so fast chat cannot dump I-feel instead."""
    if bot is None or not looks_like_teammate_work(intent):
        return None
    t = user_intent_text(visible_user_text(intent or ""))
    cands = _teammate_candidates(bot)
    if _LIST_TEAMMATES_RE.search(t):
        if not cands:
            return "It's just me on this desk right now."
        names: list[str] = []
        seen: set[str] = set()
        for name, _oid, _obj in cands:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        return "On the desk with me: " + ", ".join(names) + "."
    spec = teammate_spec_from_intent(intent, bot)
    if not spec:
        if not cands:
            return "I don't see another bot on the desk to sync with."
        return None
    note = (
        "Hi — Teela here. The user asked us to sync. "
        "What's your status on your side?"
    )
    dest = None
    low = spec.lower()
    self_id = str(getattr(bot, "id", "") or "")
    for other in bots.values():
        if str(getattr(other, "id", "") or "") == self_id:
            continue
        if str(getattr(other, "id", "") or "").lower() == low:
            dest = other
            break
        if str(getattr(other, "name", "") or "").lower() == low:
            dest = other
            break
    if dest is not None:
        dest.deliver_dm(bot, note)
        return f"I pinged {dest.name} so we can stay in sync."
    cl = cluster
    if cl is None:
        return f"I don't see {spec} on the desk yet, so I couldn't ping them."
    try:
        result = cl.forward_dm(
            {
                "from_id": getattr(bot, "id", ""),
                "from_name": getattr(bot, "name", "Teela"),
                "from_node": getattr(cl, "node_name", "") or "",
                "to": spec,
                "text": note,
            }
        )
    except KeyError:
        return f"I don't see {spec} on the desk yet, so I couldn't ping them."
    except PermissionError:
        return f"I couldn't reach {spec} — cluster auth failed."
    except Exception:
        return f"I couldn't reach {spec} just now."
    name = str((result or {}).get("to_name") or spec)
    return f"I pinged {name} so we can stay in sync."


_HELPER_BOT_RE = re.compile(
    r"\b(?:"
    r"(?:create|make|spin\s+up|add|hire|spawn)\s+(?:a\s+|an\s+|me\s+(?:a\s+|an\s+)?)?(?:new\s+)?(?:bot|teammate|helper|agent)\b"
    r"|(?:delete|remove|retire|dismiss|fire)\b.{0,40}\b(?:bot|teammate|helper|agent)\b"
    r")",
    re.I,
)


def looks_like_helper_bot_work(text: str) -> bool:
    t = " ".join(user_intent_text(visible_user_text(text or "")).lower().split())
    return bool(t and _HELPER_BOT_RE.search(t))


def list_teammates_payload(bot: Any) -> dict[str, Any]:
    self_id = str(getattr(bot, "id", "") or "")
    rows = []
    for other in list(bots.values()):
        oid = str(getattr(other, "id", "") or "")
        if not oid or oid == self_id:
            continue
        rows.append(
            {
                "id": oid,
                "name": getattr(other, "name", oid),
                "kind": getattr(other, "kind", ""),
                "description": getattr(other, "description", ""),
                "model": getattr(other, "model", ""),
                "remote": bool(getattr(other, "remote", False)),
                "node": getattr(other, "node", "") or "",
            }
        )
    if cluster is not None:
        seen = {r["id"] for r in rows}
        cache = getattr(cluster, "roster_cache", None) or {}
        for _peer, listing in (cache.items() if isinstance(cache, dict) else []):
            for row in listing or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "")
                if not rid or rid == self_id or rid in seen:
                    continue
                seen.add(rid)
                rows.append(
                    {
                        "id": rid,
                        "name": row.get("name") or rid,
                        "kind": row.get("kind") or "",
                        "description": row.get("description") or "",
                        "model": row.get("model") or "",
                        "remote": True,
                        "node": row.get("node") or _peer,
                    }
                )
    return {"teammates": rows}


def create_helper_teammate(
    owner: Any,
    *,
    name: str,
    description: str,
    soul: str = "",
    kind: str = "",
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    want_kind = normalize_bot_kind(kind, default=BOT_KIND_AGENT)
    if want_kind == BOT_KIND_TEELA_BRAIN:
        return {
            "ok": False,
            "error": "Helpers must be hermes. Only one Teela Brain is allowed on this computer.",
        }
    body: dict[str, Any] = {
        "name": name,
        "description": (description or f"{name} — helper for Teela.").strip(),
        "kind": want_kind or BOT_KIND_AGENT,
        "soul": soul or "",
    }
    default, catalog = load_user_models()
    if default:
        body["model"] = default
    bot = create_bot(body)
    return {
        "ok": True,
        "id": bot.id,
        "name": bot.name,
        "kind": bot.kind,
        "model": bot.model,
        "spoken": f"I made {bot.name} to help with that.",
    }


def delete_helper_teammate(owner: Any, spec: str) -> dict[str, Any]:
    spec = (spec or "").strip()
    dest = _lookup_bot(spec)
    if dest is None:
        return {"ok": False, "error": f"I don't see a local bot named {spec}."}
    if str(getattr(dest, "id", "")) == str(getattr(owner, "id", "")):
        return {"ok": False, "error": "I can't delete myself."}
    if getattr(dest, "remote", False):
        return {"ok": False, "error": "I can only delete bots on this computer."}
    if occupies_teela_brain_slot(dest):
        return {"ok": False, "error": "I can't delete Teela Brain."}
    bid = dest.id
    name = dest.name
    with lock:
        bots.pop(bid, None)
    dest.destroy()
    emit({"type": "bot.deleted", "bot_id": bid})
    if cluster is not None:
        threading.Thread(target=cluster.announce_to_peers, daemon=True).start()
    return {"ok": True, "id": bid, "name": name, "spoken": f"I retired {name}."}


_TALK_RE = re.compile(
    r"^(?:"
    r"hi|hello|hey|yo|howdy|"
    r"thanks|thank you|"
    r"good (?:morning|afternoon|evening|night)|"
    r"what'?s up|whats up|"
    r"how are you(?: doing)?|"
    r"how'?s it going|"
    r"how have you been|"
    r"(?:can we |let'?s )?talk(?: to me)?|"
    r"talk to me|"
    r"just (?:chat|talking)"
    r")[\s.!?]*$",
    re.IGNORECASE,
)


def looks_like_talk(text: str) -> bool:
    """True when they are talking to Teela as a person, not asking for work."""
    t = " ".join((text or "").lower().split())
    if not t or looks_like_system_check(t):
        return False
    if robot_sim.looks_like_motor(t) or robot_sim.infer_command(t):
        return False
    mixed = orch.classify(t).get("mode")
    if mixed in {"parallel", "after", "body", "agent"}:
        return False
    return bool(_TALK_RE.fullmatch(t))


def local_llm_system_check_turn(payload: dict[str, Any], bot: Any = None) -> bool:
    """True when this Teela turn must call teela_system_check instead of announcing it."""
    if not bot_kind_is_teela(bot):
        return False
    if payload_already_system_checked(payload):
        return False
    intent = last_user_intent_from_payload(payload)
    return bool(intent and looks_like_system_check(intent))


def local_llm_talk_turn(payload: dict[str, Any], bot: Any = None) -> bool:
    """True when Teela should just talk to the person — no check, no move."""
    if not bot_kind_is_teela(bot):
        return False
    if payload_already_system_checked(payload) or payload_already_moved(payload, bot):
        return False
    if local_llm_system_check_turn(payload, bot) or local_llm_motor_turn(payload):
        return False
    intent = last_user_intent_from_payload(payload)
    return bool(intent and looks_like_talk(intent))


def looks_like_desktop_work(text: str) -> bool:
    """True when they asked Teela to use MiniOS browser/files, not to chat or move."""
    t = " ".join((text or "").lower().split())
    if not t or looks_like_talk(t):
        return False
    if robot_sim.looks_like_motor(t) and not _DESKTOP_RE.search(t):
        return False
    return bool(_DESKTOP_RE.search(t))


def local_llm_desktop_turn(payload: dict[str, Any], bot: Any = None) -> bool:
    """True when this Teela turn must use MiniOS desktop/browser tools."""
    if not bot_kind_is_teela(bot):
        return False
    if payload_already_browsed(payload):
        return False
    if local_llm_motor_turn(payload) or local_llm_system_check_turn(payload, bot):
        return False
    intent = last_user_intent_from_payload(payload)
    if not intent or not looks_like_desktop_work(intent):
        return False
    if local_llm_has_image_parts(payload) and not re.search(
        r"\b(?:type|write|notepad|open|browser|google|search)\b", intent, re.I
    ):
        return False
    return True


def local_llm_after_system_work_turn(payload: dict[str, Any], bot: Any = None) -> bool:
    """True after a Teela check when they also asked to move (wave when finished)."""
    if not bot_kind_is_teela(bot):
        return False
    if not payload_already_system_checked(payload):
        return False
    if payload_already_moved(payload, bot):
        return False
    intent = last_user_intent_from_payload(payload)
    mixed = orch.classify(intent) if intent else {}
    return mixed.get("mode") in {"parallel", "after"} and bool(mixed.get("body"))


_HARD_THINK_RE = re.compile(
    r"\b(?:"
    r"implement|refactor|debug|traceback|stack\s*trace|architecture|"
    r"design\s+doc|unit\s+test|typeerror|exception|"
    r"think\s+hard|reason\s+carefully|prove\s+that|"
    r"algorithm|complexity|security\s+review"
    r")\b",
    re.IGNORECASE,
)


def _message_has_image(msg: Any) -> bool:
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "").lower()
            if ptype in _IMAGE_PART_TYPES or ptype == "image":
                return True
            mime = str(part.get("mimeType") or part.get("mime") or "").lower()
            if mime.startswith("image/"):
                return True
            if part.get("data") and ptype in ("image", "input_image"):
                return True
    elif isinstance(content, str) and "data:image/" in content:
        return True
    return False


def local_llm_has_image_parts(payload: dict[str, Any]) -> bool:
    """True when this completion includes image/video parts the VL-8B can see."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return False
    return any(_message_has_image(msg) for msg in msgs)


def attach_recent_chat_images(payload: dict[str, Any], bot: Any) -> dict[str, Any]:
    """If Hermes Agent stripped paste images, reattach the latest user chat pictures."""
    if bot is None or local_llm_has_image_parts(payload):
        return payload
    workspace = getattr(bot, "workspace", None)
    imgs: list[dict[str, Any]] = []
    for msg in reversed(getattr(bot, "messages", None) or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            raw = msg.get("images") or []
            imgs = [i for i in raw if isinstance(i, dict)]
            break
    extra: list[dict[str, Any]] = []
    for img in imgs:
        rel = str(img.get("path") or "")
        if not rel or workspace is None:
            continue
        data = _file_to_data_url(Path(workspace) / rel)
        if data:
            extra.append({"type": "image_url", "image_url": {"url": data}})
    if not extra:
        return payload
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    payload = dict(payload)
    payload["messages"] = list(msgs)
    for i in range(len(payload["messages"]) - 1, -1, -1):
        msg = payload["messages"][i]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        row = dict(msg)
        content = row.get("content")
        if isinstance(content, str):
            row["content"] = [{"type": "text", "text": content}, *extra]
        elif isinstance(content, list):
            row["content"] = list(content) + extra
        else:
            row["content"] = extra
        payload["messages"][i] = row
        break
    return payload


def _last_user_key(payload: dict[str, Any]) -> str:
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return ""
    for msg in reversed(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _flatten_message_text(msg)[:2000]
    return ""


def _apply_hybrid_pin(
    bot: Any,
    payload: dict[str, Any],
    url: str,
    served: str,
    want_think: bool,
    *,
    think_live: bool = True,
) -> tuple[str, str]:
    """Stay on the model that started this user turn unless VL-8B must escalate."""
    key = _last_user_key(payload)
    if not key or bot is None:
        return url, served
    if getattr(bot, "llm_pin_key", "") == key and getattr(bot, "llm_pin_url", ""):
        if not want_think:
            pin_url = str(bot.llm_pin_url or "")
            # A pin to hung/down 27B would leave MiniOS on Working….
            if not (("8000" in pin_url or pin_url == LOCAL_LLM_UPSTREAM) and not think_live):
                url = pin_url
                served = bot.llm_pin_served
    bot.llm_pin_key = key
    bot.llm_pin_url = url
    bot.llm_pin_served = served
    return url, served


def _hybrid_family(url: str, served: str) -> str | None:
    s = (served or "").lower()
    if "muse" in s:
        return None
    if "8001" in (url or "") or "9b" in s or "vl-8b" in s:
        return "fast"
    if "8000" in (url or "") or s in {"qwen38", LOCAL_LLM_SERVED.lower()}:
        return "think"
    return None


@contextmanager
def hybrid_exclusive(family: str | None):
    """Prefer one local Qwen at a time, but do not block VL-8B on a hung 27B."""
    if family not in {"fast", "think"}:
        yield
        return
    other = "think" if family == "fast" else "fast"
    wait_s = 8.0 if family == "fast" else 90.0
    deadline = time.monotonic() + wait_s
    with _HYBRID_GATE:
        while _HYBRID_INFLIGHT[other] > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _HYBRID_GATE.wait(timeout=min(1.0, remaining))
        _HYBRID_INFLIGHT[family] += 1
    try:
        yield
    finally:
        with _HYBRID_GATE:
            _HYBRID_INFLIGHT[family] -= 1
            _HYBRID_GATE.notify_all()


def local_llm_hard_think(payload: dict[str, Any]) -> bool:
    """True when this turn should use the 27B instead of the VL-8B."""
    if local_llm_motor_turn(payload):
        return False
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return False
    last_user = ""
    for msg in reversed(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _flatten_message_text(msg)
            break
    return bool(last_user and _HARD_THINK_RE.search(last_user))


def _is_fast_llm_id(mid: str) -> bool:
    x = (mid or "").lower()
    return "9b" in x or "vl-8b" in x or x == LOCAL_LLM_FAST_SERVED.lower()


def _fast_llm_upstream(live: tuple[str, ...], live_map: dict[str, str] | None) -> str:
    """URL that is actually serving VL-8B / 9B. Exclusive VL-only is on :8000."""
    for mid, url in (live_map or {}).items():
        if _is_fast_llm_id(mid) and url:
            return url
    live_set = {x.lower() for x in live}
    has_fast = any(_is_fast_llm_id(x) for x in live)
    has_think = LOCAL_LLM_SERVED.lower() in live_set
    if has_fast and not has_think:
        return LOCAL_LLM_UPSTREAM
    return LOCAL_LLM_FAST_UPSTREAM


def _think_llm_upstream(live_map: dict[str, str] | None) -> str:
    for mid, url in (live_map or {}).items():
        if (mid or "").lower() == LOCAL_LLM_SERVED.lower() and url:
            return url
    return LOCAL_LLM_UPSTREAM


def pick_local_llm_route(
    payload: dict[str, Any],
    live_ids: tuple[str, ...] | None = None,
    requested_model: str | None = None,
    bot: Any = None,
    live_map: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (upstream_base_url, served_model_id) for this completion."""
    mapping = dict(live_map or {})
    if live_ids is None:
        mapping = probe_local_llm_map()
        live = tuple(mapping.keys())
    else:
        live = live_ids
    live_set = {x.lower() for x in live}
    fast_id = next((x for x in live if _is_fast_llm_id(x)), "")
    has_fast = bool(fast_id)
    has_think = LOCAL_LLM_SERVED.lower() in live_set
    mid = str(requested_model or payload.get("model") or "").lower()
    served = LOCAL_LLM_ALIASES.get(mid, mid)
    cat_tbl: dict[str, Any] = {}
    cat_served = ""
    try:
        _, catalog = load_user_models()
        raw_cat = catalog.get(requested_model or "") or catalog.get(mid)
        if isinstance(raw_cat, dict):
            cat_tbl = raw_cat
            cat_served = str(raw_cat.get("model") or "").strip()
        elif not cat_served:
            for key, row in catalog.items():
                if str(key).lower() == mid and isinstance(row, dict):
                    cat_tbl = row
                    cat_served = str(row.get("model") or key).strip()
                    break
    except Exception:
        cat_tbl, cat_served = {}, ""
    fam = _picker_gpu_family(requested_model or mid, cat_tbl) if cat_tbl else _picker_gpu_family(mid, {})
    if fam not in ("qwen", "hybrid", "vl8", "muse"):
        want_live = {s.lower() for s in (served, cat_served, mid) if s}
        for lid in live:
            if lid.lower() in want_live:
                return mapping.get(lid) or _think_llm_upstream(mapping), lid
    fast_url = _fast_llm_upstream(live, mapping)
    think_url = _think_llm_upstream(mapping)
    if served == "muse-glimmer":
        return LOCAL_LLM_UPSTREAM, served
    use_fast_name = fast_id or LOCAL_LLM_FAST_SERVED
    if served == LOCAL_LLM_FAST_SERVED.lower() or served == "qwen38-9b" or "9b-distill" in mid or "vl-8b" in mid or mid == "qwen3-vl":
        if has_fast:
            return fast_url, use_fast_name
        if has_think:
            return think_url, LOCAL_LLM_SERVED
        return fast_url, use_fast_name
    # Pin: "Qwen 3.8 27B only" — fall back to VL-8B when 27B is down so chat
    # does not sit on Working… against a dead :8000.
    if mid in ("qwen38-27b", "qwen3.8-27b", "qwen3.8-27b-gptq-int4") and "hybrid" not in mid:
        if has_think:
            return think_url, LOCAL_LLM_SERVED
        if has_fast:
            return fast_url, use_fast_name
        return think_url, LOCAL_LLM_SERVED
    # Hybrid (qwen38-hybrid) and bare qwen38: VL-8B for chat, motor, and vision;
    # 27B only for hard coding/debug/design. Images stay on GPU1.
    want_think = local_llm_hard_think(payload)
    if not want_think and has_fast:
        url, served_id = fast_url, use_fast_name
    elif has_think:
        url, served_id = think_url, LOCAL_LLM_SERVED
    elif has_fast:
        url, served_id = fast_url, use_fast_name
    else:
        url, served_id = think_url, LOCAL_LLM_SERVED
    if bot is not None and ("hybrid" in mid or mid in ("qwen38", "qwen3.8", "")):
        url, served_id = _apply_hybrid_pin(
            bot, payload, url, served_id, want_think, think_live=has_think
        )
    return url, served_id


_BODY_APPLIED_MARK = "[[minios-body-applied]]"
_BODY_SENSE_MARK = "[[minios-body-sense]]"
_SHORT_CHAT = (
    "Talk like a person in the room, one or two sentences. "
    "Answer what they just said first. "
    "You know your own body from proprioception; mention posture only if they asked how you feel or look, or you just moved. "
    "You know what time it is from the NOW block — greet for morning/afternoon/evening/night, "
    "and use how long you have been waving or walking when that matters. "
    "When you say the time, write spoken words (ten oh three PM), never 10:03. "
    "Do not read the clock aloud unless they asked the time or it is part of the reply. "
    "Never list joint names or degrees unless they asked."
)
_NOW_MARK = "[[teela-now]]"
_SENSE_SECTION = re.compile(r"\n*\[\[minios-body-sense\]\].*$", re.DOTALL)
_WORK_RE = re.compile(
    r"\b(?:"
    r"implement|refactor|debug|traceback|code|python|javascript|typescript|"
    r"write (?:a |the )?(?:file|script|function|test|app)|"
    r"read (?:the )?file|search (?:the )?(?:web|code|files|repo)|"
    r"browse|navigate|screenshot|unit tests?|build the|"
    r"open (?:the )?(?:file|browser|editor|terminal)"
    r")\b",
    re.I,
)
_TEELA_PERSONALITY = (
    "You are Teela: a young woman in her early twenties — cheerful, warm, and happy to be alive. "
    "Talk like that person in the room: bright, not a cartoon, not a list of traits, not a generic assistant. "
)
_FAST_CHAT_SYS = (
    f"{_TEELA_PERSONALITY}"
    "You are in a conversation with someone who can see your body. "
    "Stay in the conversation: use earlier messages; it / that / the other one refer to what you were just talking about. "
    "You have continuous proprioception (the I feel block). That is your body for this thought, "
    "the way a human feels their limbs. Chat history and BODY.md are memory — they are not your body right now. "
    "Never guess, recall, or invent a pose. If I feel says your hand is in front of your chest, it is not up. "
    "Never say a hand or arm is up or raised unless I feel uses those words. "
    "If they only said hi/hello/hey, greet them in one sentence and do not describe your pose. "
    "Answer what they just said first. Do not recite your pose unless they asked how you feel or look. "
    "Do not greet by waving, bowing, or walking. A greeting is speech, not a new body action. "
    "Do not claim you just moved, are moving now, or will move, unless a note says you already moved this turn. "
    "Current posture in I-feel is already true — it is not something you are doing because they spoke. "
    "If a note says you already moved this turn, you did; keep talking from that pose. Do not ask them to repeat the request. "
    "'Can you …' about your body means do it. "
    "BODY.md is how a movement should look, not proof of what you are doing now. "
    "Named gestures and taught notes below are how they asked you to look — follow them when you move, not when you report. "
    "Never name joints, degrees, gravity, or telemetry unless they asked. "
    "One to three sentences, like a person in the room."
)
_FALSE_MOVE_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'m| am) (?:already\s+)?waving(?: hello)?"
    r"|already waving"
    r"|waving hello"
    r"|rocking side to side"
    r"|hand['’s]* up in front of my chest"
    r"|watching my(?: right| left)? arm"
    r"|arm come up"
    r"|i just (?:waved|moved|walked|bowed)"
    r"|i(?:'ll| will) (?:wave|walk|bow|move)"
    r"|picked up the"
    r"|reaching (?:out|for)"
    r"|i moved my"
    r")\b",
    re.I,
)
_CONFIRM_TURN = re.compile(
    r"^(?:yes|yeah|yep|yup|please do(?:\s+it)?|do it|go ahead)(?:\s+please)?[.!?]*$",
    re.I,
)


_BODY_TEACH = re.compile(
    r"\b(?:"
    r"BODY\.md|"
    r"how you look|you look(?:s)?|"
    r"should look|"
    r"looks? (?:wrong|off|weird|better)|"
    r"too (?:high|low|stiff|bent|straight)|"
    r"more like|"
    r"remember (?:how|this|that)|"
    r"write that down|save that|"
    r"mimic|copy this|do this(?:\s+move)?|"
    r"watch this|this video"
    r")\b",
    re.IGNORECASE,
)


_BODY_TEACH_ACP = re.compile(
    r"\b(?:mimic|copy this|watch this|this video|BODY\.md)\b",
    re.IGNORECASE,
)
_SAVE_GESTURE = re.compile(
    r"\b(?:that(?:'s| is)|this is)\s+my\s+([a-z][a-z0-9_-]{1,24})\b|"
    r"\b(?:call|name)\s+(?:that|this|it)\s+(?:my\s+)?([a-z][a-z0-9_-]{1,24})\b|"
    r"\bremember (?:this|that|it) as (?:my\s+)?([a-z][a-z0-9_-]{1,24})\b",
    re.IGNORECASE,
)
_BODY_PREF = re.compile(
    r"\bleft\b.{0,48}\b(?:screen|viewer|front view)\b|"
    r"\bscreen[- ]left\b|"
    r"\b(?:viewer|screen)[- ]relative\b|"
    r"\bright\b.{0,48}\b(?:screen|viewer|front view)\b",
    re.IGNORECASE,
)
_BODY_FACT_TAGS = ("body", "gesture", "preference", "pose")


def looks_like_body_teach(text: str) -> bool:
    """User is teaching appearance or asking her to copy what they showed."""
    t = visible_user_text(text)
    return bool(t and _BODY_TEACH.search(t))


def looks_like_body_teach_acp(text: str) -> bool:
    """Teach that needs vision or a free-form BODY.md edit in the full agent."""
    t = visible_user_text(text)
    return bool(t and _BODY_TEACH_ACP.search(t))


def load_body_notes(bot: Any, *, limit: int = 6000) -> str:
    """Shared lookbook: workspace/BODY.md, edited by Teela or the user."""
    ws = getattr(bot, "workspace", None)
    if not ws:
        return ""
    path = Path(ws) / "BODY.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) > limit:
        text = text[: limit - 20].rsplit("\n", 1)[0] + "\n…"
    return text


def default_body_md() -> str:
    return DEFAULT_BODY_MD


def ensure_body_md(workspace: Path) -> None:
    path = workspace / "BODY.md"
    if path.is_file():
        return
    path.write_text(default_body_md(), encoding="utf-8")


def parse_named_gestures(notes: str) -> dict[str, str]:
    """Parse `- hello: wave` lines under ## Named gestures in BODY.md."""
    text = notes or ""
    m = re.search(r"(?im)^## named gestures\s*$", text)
    if not m:
        return {}
    rest = text[m.end() :]
    nxt = re.search(r"(?m)^## ", rest)
    block = rest[: nxt.start()] if nxt else rest
    out: dict[str, str] = {}
    for line in block.splitlines():
        hit = re.match(r"^[-*]\s*([A-Za-z][\w '-]{0,32}?)\s*:\s*([a-z][a-z0-9_-]+)\s*$", line.strip())
        if not hit:
            continue
        name = " ".join(hit.group(1).lower().split())
        pose = hit.group(2).strip().lower().replace(" ", "_")
        if name and pose:
            out[name] = pose
    return out


def _body_md_path(bot: Any) -> Path | None:
    ws = getattr(bot, "workspace", None)
    if not ws:
        return None
    return Path(ws) / "BODY.md"


def _ensure_body_section(text: str, heading: str, placeholder: str) -> str:
    if re.search(rf"(?im)^## {re.escape(heading)}\s*$", text):
        return text
    return text.rstrip() + f"\n\n## {heading}\n\n{placeholder}\n"


def _upsert_body_md_line(path: Path, heading: str, line: str, *, replace_prefix: str | None = None) -> None:
    ensure_body_md(path.parent)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = default_body_md()
    placeholder = "(Add dated notes here.)" if heading == "Taught notes" else "- (none yet — say \"that's my hello\" after a move)"
    text = _ensure_body_section(text, heading, placeholder)
    m = re.search(rf"(?im)^## {re.escape(heading)}\s*$", text)
    if not m:
        path.write_text(text.rstrip() + f"\n\n## {heading}\n\n{line}\n", encoding="utf-8")
        return
    rest = text[m.end() :]
    nxt = re.search(r"(?m)^## ", rest)
    end = m.end() + nxt.start() if nxt else len(text)
    block = text[m.end() : end]
    lines = block.splitlines()
    kept: list[str] = []
    replaced = False
    for raw in lines:
        if replace_prefix and raw.strip().lower().startswith(replace_prefix.lower()):
            if not replaced:
                kept.append(line)
                replaced = True
            continue
        if raw.strip() == line.strip():
            replaced = True
        kept.append(raw)
    if not replaced:
        if kept and kept[-1].strip() == "":
            kept[-1] = line
        else:
            kept.append(line)
        kept.append("")
    new_block = "\n".join(kept)
    if not new_block.endswith("\n"):
        new_block += "\n"
    path.write_text(text[: m.end()] + "\n" + new_block.lstrip("\n") + text[end:], encoding="utf-8")


def pose_for_lesson(state: dict[str, Any] | None, prior: str | None = None) -> str:
    st = state if isinstance(state, dict) else {}
    motion = str(st.get("motion") or "")
    pose = str(st.get("pose") or "")
    if motion == "waving" or pose == "wave":
        return "wave"
    if pose in robot_sim.POSES and pose not in {"custom", "neutral"}:
        return pose
    if prior:
        cmd = robot_sim.infer_command(prior, st, allow_plan=False)
        if isinstance(cmd, dict) and str(cmd.get("pose") or "") in robot_sim.POSES:
            return str(cmd["pose"])
    hist = st.get("history") if isinstance(st.get("history"), list) else []
    for ev in reversed(hist):
        if not isinstance(ev, dict):
            continue
        p = str(ev.get("pose") or "")
        if p in robot_sim.POSES and p not in {"custom", "home", "neutral"}:
            return p
        act = str(ev.get("act") or "")
        if act in robot_sim.POSES:
            return act
    return pose if pose in robot_sim.POSES else "home"


def extract_body_lesson(
    text: str,
    state: dict[str, Any] | None = None,
    prior: str | None = None,
) -> dict[str, Any] | None:
    """Pull a durable body fact from a teach/correction line. None if nothing to store."""
    t = " ".join(visible_user_text(text).lower().split())
    if not t or robot_sim.looks_like_body_query(t):
        return None
    if looks_like_body_teach_acp(t) and not _SAVE_GESTURE.search(t):
        return None
    named = _SAVE_GESTURE.search(t)
    if named:
        label = next((g for g in named.groups() if g), "").strip().lower()
        if label and label not in {"it", "that", "this", "one"}:
            pose = pose_for_lesson(state, prior)
            return {
                "kind": "gesture",
                "gesture_name": label,
                "pose": pose,
                "note": f"Named gesture '{label}' is pose {pose}.",
                "tags": ["body", "gesture"],
            }
    if _BODY_PREF.search(t):
        return {
            "kind": "preference",
            "note": (
                "Left and right mean the viewer's screen: left is the arm on the left of the Front camera "
                "(the robot's right)."
            ),
            "tags": ["body", "preference"],
        }
    if looks_like_body_teach(t):
        pose = pose_for_lesson(state, prior)
        snippet = visible_user_text(text).strip()[:160]
        return {
            "kind": "correction",
            "pose": pose,
            "note": f"{pose} correction: {snippet}",
            "tags": ["body", "preference"],
        }
    return None


def save_body_lesson(bot: Any, lesson: dict[str, Any] | None) -> dict[str, Any] | None:
    """Write a lesson into BODY.md and tagged long-term memory."""
    if not isinstance(lesson, dict) or not lesson.get("note"):
        return None
    path = _body_md_path(bot)
    note = str(lesson["note"]).strip()
    if path is not None:
        try:
            if lesson.get("kind") == "gesture" and lesson.get("gesture_name") and lesson.get("pose"):
                name = str(lesson["gesture_name"])
                pose = str(lesson["pose"])
                _upsert_body_md_line(
                    path,
                    "Named gestures",
                    f"- {name}: {pose}",
                    replace_prefix=f"- {name}:",
                )
            else:
                day = datetime.now().strftime("%Y-%m-%d")
                _upsert_body_md_line(path, "Taught notes", f"- {day}: {note}")
        except OSError:
            pass
    try:
        mem = getattr(bot, "memory", None)
        if mem is not None:
            mem.write(note, tags=list(lesson.get("tags") or ["body"]))
    except Exception:
        pass
    return lesson


def learn_from_user(bot: Any, text: str, prior: str | None = None) -> dict[str, Any] | None:
    lesson = extract_body_lesson(text, getattr(bot, "robot_state", None), prior)
    saved = save_body_lesson(bot, lesson) if lesson else None
    if saved or _looks_like_embodied_correction(text):
        note = str((saved or {}).get("note") or text or "")
        attach_embodied_user_correction(bot, note)
    return saved


def recall_body_facts(bot: Any, query: str = "", *, limit: int = 6) -> list[str]:
    """Tagged body facts from this bot's SQLite store. Empty if memory is unavailable."""
    if bot is None:
        return []
    try:
        mem = bot.memory
    except Exception:
        return []
    hits: list[str] = []
    queries = [q for q in (query, "body gesture preference pose") if str(q or "").strip()]
    seen: set[str] = set()
    for q in queries:
        try:
            rows = mem.retrieve(q, limit=limit)
        except Exception:
            continue
        for rec in rows:
            tags = [str(t).lower() for t in (rec.tags or [])]
            if tags and not any(t in _BODY_FACT_TAGS for t in tags):
                continue
            fact = (rec.text or "").strip()
            if not fact or fact in seen:
                continue
            seen.add(fact)
            hits.append(fact)
            if len(hits) >= limit:
                return hits
    return hits


def body_memory_block(bot: Any, user_text: str = "") -> str:
    notes = load_body_notes(bot)
    gestures = parse_named_gestures(notes)
    facts = recall_body_facts(bot, user_text)
    bits: list[str] = []
    if gestures:
        bits.append("Named gestures: " + ", ".join(f"{n}={p}" for n, p in gestures.items()))
    if facts:
        bits.append("Remembered about your body:\n- " + "\n- ".join(facts[:6]))
    return "\n".join(bits)


_HERMES_BUILD_JOB_RE = re.compile(
    r"\b(?:"
    r"implement|refactor|debug|traceback|"
    r"write (?:a |the )?(?:python |js |javascript |typescript )?(?:script|function|test suite|app)|"
    r"unit tests?|build the|"
    r"read (?:the )?file|search (?:the )?(?:code|files|repo)|"
    r"open (?:the )?(?:editor|terminal)"
    r")\b",
    re.I,
)


def looks_like_coding_job(text: str) -> bool:
    """True only for host coding jobs that need Hermes Agent ACP."""
    t = " ".join(visible_user_text(user_intent_text(text or "")).lower().split())
    if not t:
        return False
    if looks_like_system_check(t) or looks_like_desktop_work(t) or looks_like_teammate_work(t):
        return False
    if looks_like_helper_bot_work(t) or robot_sim.looks_like_motor(t):
        return False
    return bool(_HERMES_BUILD_JOB_RE.search(t))


_TEELA_ACP_TUI_REPORT = (
    "Write the report like a Hermes TUI session: a short heading, then a markdown "
    "table with columns Component and Status (one table row per line, including "
    "the |---|---| separator), then a brief note. Do not flatten into one spoken "
    "paragraph or a single bullet dump.\n\n"
)


def teela_system_acp_prefix(scope: str) -> str:
    """Steer Hermes Agent tools at MiniOS workspace vs teela-brain host."""
    if scope == "host":
        return (
            "[Teela system check — teela-brain host, NOT MiniOS]\n"
            "Use Hermes Agent tools and host-shell: nvidia-smi, systemctl --user status "
            "teela-qwen38-27b teela-tts-tunnel teela-stt-tunnel, curl -sS http://127.0.0.1:8081/v1/models, "
            "free -h, hostname. Also call bot_desktop__teela_system_check with scope=host. "
            "Do not report MiniOS twin pose as the answer. "
            f"{_TEELA_ACP_TUI_REPORT}"
        )
    if scope == "both":
        return (
            "[Teela system check — MiniOS workspace AND teela-brain host]\n"
            "Do MiniOS first (workspace files, BODY.md, observer, bot_desktop__teela_system_check scope=minios), "
            "then the host (nvidia-smi, llama :8081, tunnels, teela_system_check scope=host). "
            "Keep the two reports as separate TUI markdown tables. "
            f"{_TEELA_ACP_TUI_REPORT}"
        )
    return (
        "[Teela system check — MiniOS workspace, NOT teela-brain hardware]\n"
        "Use Hermes Agent tools in THIS workspace: glob, grep, read_file on BODY.md, AGENTS.md, Desktop, "
        "and MiniOS files. Call bot_desktop__teela_system_check with scope=minios. "
        "You may use grep/read_file here. "
        f"{_TEELA_ACP_TUI_REPORT}"
    )


def looks_like_body_look(text: str) -> bool:
    """True when they want Teela to look at her MiniOS desktop / avatar, not just feel I-feel."""
    t = " ".join(visible_user_text(user_intent_text(text or "")).lower().split())
    if not t:
        return False
    return bool(
        re.search(
            r"\b(?:look at|see|show me|check)\b.{0,28}\b(?:your\s+)?(?:body|avatar|desktop|workspace|twin|simulator|pose|arms?|hands?)\b|"
            r"\b(?:what does|how does)\b.{0,20}\b(?:your\s+)?(?:body|avatar)\b|"
            r"\b(?:did you|didn'?t you|did not)\s+wave\b|"
            r"\byou didn'?t wave\b|"
            r"\bi don'?t see you (?:waving|moving|doing)\b",
            t,
            re.I,
        )
    )


def teela_body_look_acp_prefix() -> str:
    return (
        "[Look at your MiniOS desktop and avatar]\n"
        "An image of YOUR MiniOS workspace is attached when available "
        "(App Preview / Robot Simulator is your body). "
        "If the image is missing or unclear, call bot_desktop__desktop_observe or "
        "bot_desktop__desktop_screenshot, then bot_desktop__teela_get_body_state. "
        "Answer from the pixels of the avatar plus live joints. "
        "I-feel and chat history are memory — if the avatar's right hand is down by her side, "
        "you are not waving. First person. Do not list joint names unless asked.\n\n"
    )


def capture_minios_avatar(bot: Any) -> list[dict[str, Any]]:
    """Quiet JPEG of MiniOS desktop (robot twin). Do not replay joints — that can kill a wave."""
    try:
        bot.ensure_observer()
        obs = getattr(bot, "observer", None)
        if not obs:
            return []
        emit({"type": "desktop.capture-request", "bot_id": str(getattr(bot, "id", "") or "")})
        ev = getattr(bot, "desktop_view_event", None)
        if ev is not None:
            ev.wait(timeout=1.2)
        view = dict(getattr(bot, "desktop_view", None) or {})
        if view.get("width") and view.get("height") and hasattr(obs, "set_view_size"):
            obs.set_view_size(int(view["width"]), int(view["height"]))
        if hasattr(obs, "apply_minios_view"):
            obs.apply_minios_view(view)
        frame = b""
        if hasattr(obs, "screenshot_minios_desktop"):
            frame = obs.screenshot_minios_desktop() or b""
        if not frame and hasattr(obs, "screenshot"):
            frame = obs.screenshot(force=True) or b""
        if not frame:
            return []
        saved = bot.save_desktop_screenshot(frame)
        print(f"[deskd] attached MiniOS avatar view {saved.get('path')}", flush=True)
        return [{"path": saved["path"], "mime": saved.get("mime") or "image/jpeg"}]
    except Exception as e:
        print(f"[deskd] MiniOS avatar capture failed: {e}", flush=True)
        return []


def teela_turn_lane(text: str, images: list | None = None) -> str:
    """talk | minios | acp. Chat and motor stay local; looking at body/desktop uses Hermes Agent ACP."""
    t = visible_user_text(user_intent_text(text or ""))
    if robot_sim.looks_like_motor(t) and not robot_sim.looks_like_body_query(t) and not looks_like_body_look(t):
        return "minios"
    if looks_like_talk(t) and not looks_like_system_check(t) and not looks_like_system_check(text or ""):
        if not looks_like_desktop_work(t) and not looks_like_coding_job(t) and not looks_like_body_look(t):
            return "talk"
    if (robot_sim.looks_like_body_query(t) or looks_like_body_look(t)) and not looks_like_system_check(t):
        return "acp"
    if looks_like_system_check(t) or looks_like_system_check(text or ""):
        return "acp"
    if looks_like_teammate_work(t) or looks_like_helper_bot_work(t):
        return "acp"
    if looks_like_desktop_work(t) or looks_like_body_teach_acp(t):
        return "acp"
    if images:
        return "acp"
    if looks_like_coding_job(t):
        return "acp"
    return "talk"


def wants_full_agent(text: str, images: list | None = None) -> bool:
    """True when this turn needs MiniOS tools or Hermes Agent ACP, not fast chat."""
    return teela_turn_lane(text, images) in {"minios", "acp"}


def recent_chat_messages(bot: Any, *, limit: int = 32) -> list[dict[str, str]]:
    """Last user/assistant lines for a context-aware completion."""
    out: list[dict[str, str]] = []
    msgs = getattr(bot, "messages", None) or []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = visible_user_text(str(m.get("text") or "")).strip()
        if role == "user":
            text = user_intent_text(text)
        if not text:
            continue
        if role == "assistant" and (len(text) > 1200 or "[[minios-body" in text):
            text = text.split("\n")[0].strip()[:400]
        out.append({"role": role, "content": text})
    return out[-limit:]


def format_conversation(bot: Any, *, limit: int = 6) -> str:
    """Recent user/assistant turns, excluding the current user line, for motor follow-ups."""
    msgs = recent_chat_messages(bot, limit=limit + 1)
    if msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]
    lines: list[str] = []
    for m in msgs[-limit:]:
        role = "User" if m["role"] == "user" else "You"
        lines.append(f"{role}: {m['content'][:240]}")
    return "\n".join(lines)


def _as_current_posture(text: str) -> str:
    """Recast action-like I-feel into already-true posture so chat does not claim a new move."""
    t = text or ""
    t = re.sub(
        r"\bI'm waving with my right hand in front of my chest[^.]*",
        "My right hand is held in a wave in front of my chest",
        t,
        flags=re.I,
    )
    t = re.sub(r"\bI'm waving\b", "My right hand is in a wave", t, flags=re.I)
    t = re.sub(r"\bI'm walking\b", "My legs are in a walk cycle", t, flags=re.I)
    t = re.sub(r"\bI'm bowing\b", "My body is in a bow", t, flags=re.I)
    t = re.sub(r"\bI'm sitting\b", "I am seated", t, flags=re.I)
    return t


def _is_social_greeting_reply(text: str) -> bool:
    """True when the model repeated a hello instead of talking about the action it just took."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if re.search(r"\b(?:nice to meet you|what can i do for you|i['’]m teela)\b", t):
        return True
    if re.search(r"\b(?:hi there|hey there)\b", t) and not re.search(
        r"\b(?:wav|bow|walk|point|arm|hand|moved)\b", t
    ):
        return True
    if re.fullmatch(r"(?:hey|hi|hello)(?:\s*[!.—\-]| i['’]m here)*\.?", t):
        return True
    return False


def ground_chat_speech(
    reply: str,
    user_text: str,
    state: dict[str, Any] | None,
    *,
    acted: bool = False,
) -> str:
    """If this turn did not move the body, do not let the model claim that it did.

    If this turn *did* move, do not let a leftover greeting stand in for the action.
    """
    text = (reply or "").strip()
    user = (user_text or "").strip()
    if acted:
        if text and not _is_social_greeting_reply(text):
            return text
        confirm = robot_sim.confirm_move(state, None, user) or "Done."
        print("[deskd] replaced leftover greeting with observed action", flush=True)
        return confirm
    greet = bool(robot_sim._CHAT_GREET.search(user))
    claims = bool(_FALSE_MOVE_CLAIM.search(text)) or (
        greet and re.search(r"\b(?:wav(?:e|ing)|bowing|walking)\b", text, re.I)
    )
    if not claims:
        return text
    if greet:
        print("[deskd] stripped false body action from greeting", flush=True)
        return "Hey — I'm here."
    feel = _as_current_posture(robot_sim.describe_body(state, which="live"))
    line = feel.split(".")[0].strip()
    print("[deskd] replaced false body claim with observed posture", flush=True)
    return (line + ".") if line else text


def proprioception_block(
    state: dict[str, Any] | None,
    *,
    kind: str = "live",
    just_did: str | None = None,
    upcoming: str | None = None,
    greet: bool = False,
) -> str:
    """First-person body sense for this thought. Not chat history, not a guess."""
    which = "commanded" if kind == "applied" or just_did else "live"
    st = state if isinstance(state, dict) else {}
    feel = robot_sim.describe_body(st, which=which)
    if kind == "live" and not just_did:
        feel = _as_current_posture(feel)
    pose = str(st.get("pose") or "home")
    motion = str(st.get("motion") or "idle")
    heading = str(st.get("heading") or robot_sim.heading_from_walk(st.get("walk_direction")))
    snap = robot_sim.tracking_snapshot(st)
    live = snap.get("live") if isinstance(snap.get("live"), dict) else {}
    raised = []
    if robot_sim._hand_is_raised(live, "right"):
        raised.append("right hand raised")
    if robot_sim._hand_is_raised(live, "left"):
        raised.append("left hand raised")
    hands = ("Hands: " + ", ".join(raised) + ".") if raised else "Hands: neither hand is raised overhead."
    if robot_sim.observed_waving(st):
        wave = "Wave: yes — hand in front of my chest, rocking."
    elif robot_sim._looks_like_wave(live) or (
        pose == "wave" and robot_sim._looks_like_wave(st.get("joints") if isinstance(st.get("joints"), dict) else live)
    ):
        wave = "Wave: hand held in a wave, not rocking. Do not say you are waving or rocking."
    else:
        wave = "Wave: not waving."
    moved = f" I just moved: {just_did}." if just_did else ""
    nxt = f" Next: {upcoming}" if upcoming else ""
    if greet and not just_did:
        return (
            f"{_BODY_SENSE_MARK}\n"
            "PROPRIOCEPTION — you have a body. They only greeted you. "
            "Reply with a short hello. Do not mention waving, walking, bowing, or any pose. "
            "Do not claim you moved. A greeting is not a body action."
        )
    extra = (
        ""
        if just_did
        else " This is already-true posture, not a new action. Do not claim you just started it."
    )
    return (
        f"{_BODY_SENSE_MARK}\n"
        "PROPRIOCEPTION — your body sense for THIS thought, like a human feeling their limbs. "
        "It is live. Chat history and BODY.md are memory, not your body. Do not guess or contradict this sense. "
        "Do not say a hand is up unless Hands says raised. "
        "Do not say you are waving unless Wave: yes.\n"
        f"I feel right now: {feel} {hands} {wave}{moved}{nxt}{extra}\n"
        f"(pose {pose}, motion {motion}, facing {heading})"
    )


def _local_now(when: datetime | None = None) -> datetime:
    if when is None:
        return datetime.now().astimezone()
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return when.astimezone()


def _part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _human_clock(dt: datetime) -> str:
    mer = "AM" if dt.hour < 12 else "PM"
    spoken = spoken_clock(dt.hour % 12 or 12, dt.minute, mer)
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}, {dt.year}, {spoken}"


def teela_now_block(bot: Any | None = None, *, when: datetime | None = None) -> str:
    """Clock sense for this thought: local time of day plus how long the current move has lasted."""
    dt = _local_now(when)
    tz = dt.tzname() or dt.strftime("%Z") or "local"
    part = _part_of_day(dt.hour)
    held = ""
    st = getattr(bot, "robot_state", None) if bot is not None else None
    if isinstance(st, dict):
        hist = [e for e in (st.get("history") or []) if isinstance(e, dict)]
        last = hist[-1] if hist else None
        motion = str(st.get("motion") or "idle")
        pose = str(st.get("pose") or "home")
        if last and last.get("t") is not None:
            ago = robot_sim.relative_ago(last.get("t"), dt.timestamp())
            if motion in {"walking", "waving", "demo"} or pose in {"walk-cycle", "wave"}:
                label = "walking" if motion == "walking" or pose == "walk-cycle" else (
                    "waving" if motion == "waving" or pose == "wave" else motion
                )
                held = f" I have been {label} since {ago}."
            else:
                act = str(last.get("act") or pose or "this pose")
                held = f" Last body change ({act}): {ago}."
    return (
        f"{_NOW_MARK}\n"
        "NOW — your sense of clock time for THIS thought, the way a person knows what time it is. "
        "Use it for greetings, whether a walk or wave has been going on a while, and whether a request fits this moment. "
        "If they ask the time, say it in spoken words (ten oh three PM), never digits with a colon.\n"
        f"It is {part} here: {_human_clock(dt)} {tz}.{held}"
    )


def build_fast_chat_messages(bot: Any, user_text: str, *, just_did: str | None = None) -> list[dict[str, str]]:
    which = "commanded" if just_did else "live"
    timed = robot_sim.describe_body_timed(getattr(bot, "robot_state", None), which=which)
    history = recent_chat_messages(bot)
    # The current user turn is already in bot.messages; don't duplicate it as a
    # second user bubble if it is the last line.
    if history and history[-1]["role"] == "user":
        history = history[:-1]
    notes = load_body_notes(bot)
    look = f"\nHow you look (BODY.md — you and the user edit this file):\n{notes}" if notes else ""
    remembered = body_memory_block(bot, user_text)
    recall = f"\nWhat you remember about your body:\n{remembered}" if remembered else ""
    moved = ""
    if just_did:
        moved = (
            f"\nYou already moved this turn: {just_did} "
            "Keep talking from that pose. Do not ask if you should do it. Do not claim you will do it later."
        )
    greet = bool(robot_sim._CHAT_GREET.search((user_text or "").strip())) and not just_did
    sense = proprioception_block(
        getattr(bot, "robot_state", None),
        kind="applied" if just_did else "live",
        just_did=just_did,
        greet=greet,
    )
    sys = (
        f"{sense}\n\n"
        f"{teela_now_block(bot)}\n\n"
        f"{_FAST_CHAT_SYS}\n"
        f"{VOICE_CHAT_NOTE if voice_chat_active(bot) else ''}"
        f"{CHATTERBOX_VOICE_NOTE if voice_enabled() else ''}"
        f"Recent movement memory (not current feel): {timed}"
        f"{look}{recall}{moved}"
    )
    return [{"role": "system", "content": sys}, *history, {"role": "user", "content": user_text.strip()}]


def _iter_openai_sse(resp: Any):
    """Yield content pieces from an OpenAI-style SSE chat.completion stream."""
    content, _usage = _drain_openai_sse(resp)
    for piece in [content] if content else []:
        yield piece


def _drain_openai_sse(resp: Any, on_piece: Any | None = None) -> tuple[str, dict[str, Any]]:
    """Read an OpenAI SSE completion. Returns (text, usage)."""
    parts: list[str] = []
    usage: dict[str, Any] = {}
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            if data == "[DONE]":
                break
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj.get("usage"), dict) and obj.get("usage"):
            usage = obj["usage"]
        choice = ((obj.get("choices") or [{}])[0] or {})
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        piece = str(delta.get("content") or "")
        if not piece:
            msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            piece = str(msg.get("content") or "")
        if piece:
            parts.append(piece)
            if on_piece:
                on_piece("".join(parts), piece)
    return "".join(parts), usage


def fast_local_chat(
    bot: Any,
    user_text: str,
    *,
    just_did: str | None = None,
    on_delta: Any | None = None,
) -> str | None:
    """VL-8B chat: natural, sees recent messages. No MiniOS tool dump. Generation is not length-capped."""
    payload = {
        "model": LOCAL_LLM_FAST_SERVED,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": build_fast_chat_messages(bot, user_text, just_did=just_did),
    }
    payload["stream"] = True
    raw = json.dumps(payload).encode()
    live_map = probe_local_llm_map()
    live = tuple(live_map.keys())
    fast_id = next((x for x in live if _is_fast_llm_id(x)), "")
    if fast_id:
        payload["model"] = fast_id
        url = _fast_llm_upstream(live, live_map) + "/v1/chat/completions"
        lane = "fast"
    elif live:
        think_id = next((x for x in live if not _is_fast_llm_id(x)), live[0])
        payload["model"] = think_id
        url = (live_map.get(think_id) or LOCAL_LLM_UPSTREAM) + "/v1/chat/completions"
        lane = "think"
    else:
        return None
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=raw, method="POST", headers=_llm_headers(url, {"Content-Type": "application/json"})
    )
    content = ""
    usage: dict[str, Any] = {}
    started_ms = time.time() * 1000.0
    ok = False

    def _on_piece(full: str, piece: str) -> None:
        if on_delta:
            on_delta(full, piece)

    for attempt in (1, 2):
        try:
            # The engine serves one slot to every session on this host; a busy
            # peer (long ACP turns, tool-heavy sessions) is normal. Wait it out
            # instead of falling through to a canned line after 60 s.
            with hybrid_exclusive(lane):
                with urllib.request.urlopen(req, timeout=240) as resp:
                    content, usage = _drain_openai_sse(resp, on_piece=_on_piece if on_delta else None)
            ok = True
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
    if not ok:
        return None
    line = content.replace("\r", "\n").strip()
    if line.startswith("```"):
        line = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", line).strip()
        line = re.sub(r"\n?```$", "", line).strip()
    if not line:
        return None
    spoken = ground_chat_speech(
        line,
        user_text,
        getattr(bot, "robot_state", None),
        acted=bool(just_did),
    )
    try:
        bot.record_local_generation(
            spoken or line,
            usage,
            started_ms=started_ms,
        )
    except Exception:
        pass
    return spoken
_MOTOR_FIRST_TOOL = (
    "You are Teela. Feel I-feel first, then understand this turn, then move. "
    "For the HTML virtual body prefer bot_desktop__teela_body_action "
    "(orient_head, raise_arm, lower_arm, wave, neutral_pose), "
    "bot_desktop__teela_gesture (greeting, agree, disagree, confused, thinking, excited, point, shrug, listen), "
    "or bot_desktop__teela_stop. "
    "Look left is skill=orient_head pan_deg=-25 (negative pan is Left on the live skeleton). Wave the right hand is skill=wave side=right. "
    "Call the body tool first so motion starts immediately, then speak. "
    "Call bot_desktop__robot_pose, bot_desktop__robot_joint, or bot_desktop__robot_motion "
    "only for walk/plan/other MiniOS poses. Copy that name exactly. "
    "You choose the skill or pose from their words and your live body — "
    "there is no canned routine. "
    "Two or more actions in one request = one bot_desktop__robot_motion call with "
    "{\"cmd\":\"plan\",\"steps\":[...]}. "
    "Never call use_tool, search_tool, read_file, or grep. Never prefix mcp__. "
    "Do not claim you already moved — their words do not move you until that tool returns. "
    f"{_SHORT_CHAT}"
)
_MOTOR_ALREADY_MOVED = (
    "You already moved. Do not call tools again. "
    "Stay in the conversation: answer what they just said, from I-feel. "
    "One to three sentences, first person. "
    f"{_SHORT_CHAT}"
)
_BODY_QUERY_SPEAK = (
    "They asked how your body feels or looks. "
    "Answer from I-feel / proprioception in this turn. "
    "Do not call tools. Do not guess from chat memory. "
    "First person, one to three sentences. "
    f"{_SHORT_CHAT}"
)
_MIXED_FIRST_TOOL = (
    "You are one Teela. Agent work and virtual body motion are one plan. "
    "Body tools: bot_desktop__teela_body_action, bot_desktop__teela_gesture. "
    "WHILE they ask you to check something, also move now (look/wave) — do not wait. "
    "WHEN FINISHED / after the check: do the agent task first, then wave or look. "
    "Call bot_desktop__teela_activity if you need what you are already doing. "
    "Never prefix mcp__. "
    f"{_SHORT_CHAT}"
)
_SEE_THEN_MOVE = (
    "This is a visual motor request. Look at the attached image or a MiniOS JPEG first "
    "(bot_desktop__desktop_observe / bot_desktop__desktop_watch). Then call "
    "bot_desktop__robot_pose, bot_desktop__robot_joint, or bot_desktop__robot_motion. "
    "Copy that name exactly. Do not prefix mcp__. "
    "Do not search. Do not guess where the object is without pixels."
)
_SYSTEM_CHECK_FIRST_TOOL = (
    "They asked for system work or a check — health, mesh, twin, diagnostics. "
    "Call bot_desktop__teela_system_check now (exact name — never mcp__, never search_tool, never shell). "
    "Do not announce a check without that tool. After it returns, talk to the person: "
    "a short first-person summary from spoken, not a log dump. "
    f"{_SHORT_CHAT}"
)
_SYSTEM_CHECK_AND_MOVE = (
    "They asked for system work and a move together. "
    "Call bot_desktop__teela_system_check and the body tool they asked for "
    "(look/wave) in this turn. Exact names — never mcp__, never shell. "
    "Then talk to the person from spoken. "
    f"{_SHORT_CHAT}"
)
_TALK_SPEAK = (
    "They are talking to you as a person in the room. Answer them. "
    "Do not run a system check. Do not move. Do not call tools unless they asked. "
    "Do not recite your pose unless they asked how you feel or look. "
    "First person, one or two sentences. "
    f"{_SHORT_CHAT}"
)
_DESKTOP_FIRST_TOOL = (
    "They asked you to work in your MiniOS desktop — browser, files, notepad, pictures, or video. "
    "Search: bot_desktop__desktop_browser_navigate with a search URL. "
    "Type: bot_desktop__desktop_type_text with the words (open Notepad first if they said notepad). "
    "Open a photo/video in the workspace: bot_desktop__desktop_open_file with the path. "
    "Pasted pictures/video stills: look at the pixels; do not claim you cannot see them. "
    "Exact names — never mcp__, never search_tool, never host-shell. Then talk to them. "
    f"{_SHORT_CHAT}"
)
_DESKTOP_RE = re.compile(
    r"\b(?:"
    r"open(?:\s+your)?\s+(?:the\s+)?browser|"
    r"google\b|"
    r"search\s+(?:the\s+web|google|for)|"
    r"look(?:\s+it)?\s+up|"
    r"browse|"
    r"navigate\s+to|"
    r"go\s+to\s+(?:https?://|www\.)|"
    r"open\s+(?:your\s+)?(?:desktop|files|notepad|workspace)|"
    r"on\s+your\s+desktop|"
    r"type\s+(?!of\b)|"
    r"write\s+(?:this|that|in|into)|"
    r"notepad|"
    r"pictures?|photos?|images?|videos?|clips?|"
    r"look at (?:this |the |that )?(?:picture|photo|image|video|clip|media)|"
    r"review (?:this |the )?(?:picture|photo|image|video|media)|"
    r"watch (?:this |the )?(?:video|clip)"
    r")\b",
    re.IGNORECASE,
)
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".svg"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
_NOTEPAD_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".ini", ".cfg", ".conf", ".text"}


def workspace_media_kind(path: str) -> str:
    ext = Path(path or "").suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _NOTEPAD_EXTS:
        return "notepad"
    return ""
_MOTOR_TOOL_KEEP = re.compile(
    r"(?:^|__)(?:robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity|system_check))$",
    re.IGNORECASE,
)
_VISION_MOTOR_KEEP = re.compile(
    r"(?:^|__)(?:robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity|system_check)|desktop_observe|desktop_watch)$",
    re.IGNORECASE,
)
_MOTOR_TOOL_DROP = re.compile(
    r"search|shell|exec|bash|command|web_|read_file|write|grep|glob|browser|navigate",
    re.IGNORECASE,
)


def _openai_tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(tool.get("name") or "")


def tool_names_from_payload(payload: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    for tool in (payload or {}).get("tools") or []:
        n = _openai_tool_name(tool)
        if n:
            names.append(n)
    return names


_MINIOS_SHORT_TOOLS = frozenset(
    {
        "desktop_state",
        "desktop_observe",
        "desktop_watch",
        "desktop_screenshot",
        "desktop_open_app",
        "desktop_focus_window",
        "desktop_minimize_window",
        "desktop_maximize_window",
        "desktop_close_window",
        "desktop_click_object",
        "desktop_open_file",
        "desktop_open_preview",
        "desktop_browser_navigate",
        "desktop_browser_back",
        "desktop_browser_forward",
        "desktop_run_tests",
        "desktop_run_app",
        "desktop_stop_app",
        "desktop_move_cursor",
        "desktop_click",
        "desktop_double_click",
        "desktop_scroll",
        "desktop_type_text",
        "robot_status",
        "robot_joint",
        "robot_pose",
        "robot_motion",
        "teela_get_body_state",
        "teela_body_state_get",
        "teela_body_joint_get",
        "teela_body_history_get",
        "teela_body_motion_get",
        "teela_body_action",
        "teela_gesture",
        "teela_stop",
        "teela_activity",
        "teela_system_check",
        "memory_write",
        "memory_retrieve",
        "ask_user",
        "list_teammates",
        "message_teammate",
        "create_teammate",
        "delete_teammate",
    }
)
_HERMES_BARE_TOOLS = frozenset(
    {
        "search_tool",
        "web_search",
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "search_replace",
        "run_terminal_command",
        "shell",
        "hermes_build",
    }
)
_DEFAULT_HERMES_MOTOR_TOOLS = (
    "bot_desktop__teela_body_action",
    "bot_desktop__teela_gesture",
    "bot_desktop__teela_get_body_state",
    "bot_desktop__teela_activity",
    "bot_desktop__teela_stop",
    "bot_desktop__teela_system_check",
    "bot_desktop__robot_pose",
    "bot_desktop__robot_joint",
    "bot_desktop__robot_motion",
    "bot_desktop__robot_status",
)
_ALIASED_TOOL_RE = re.compile(
    r"(?:mcp__)+(?:bot_desktop__)?(?:robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity|system_check)|desktop_[a-z0-9_]+|search_tool|web_search)"
)
_JSON_SHORT_TOOL_RE = re.compile(
    r'(["\']name["\']\s*:\s*["\'])(robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity|system_check)|desktop_[a-z0-9_]+)(["\'])'
)
_XML_SHORT_TOOL_RE = re.compile(
    r"(<function=)(?:mcp__(?:bot_desktop__)?)?(robot_(?:status|joint|pose|motion)|teela_(?:get_body_state|body_action|gesture|stop|activity|system_check)|desktop_[a-z0-9_]+)"
)


def canonicalize_tool_name(name: str, allowed: list[str] | tuple[str, ...] | None) -> str:
    """Map Qwen/Claude-style names onto the tools Hermes actually registered.

    Hermes dispatches `bot_desktop__robot_pose`. Local Qwen often emits Claude's
    `mcp__bot_desktop__robot_pose` or the short MCP name `robot_pose`. Never keep
    an mcp__ form, even if that string was in the request's tools list.
    """
    n = (name or "").strip()
    if not n:
        return n
    while n.startswith("mcp__"):
        n = n[5:]
    short = n.split("__")[-1] if n else n
    if short in _MINIOS_SHORT_TOOLS:
        n = f"bot_desktop__{short}"
    elif short in _HERMES_BARE_TOOLS:
        n = short
    allowed = [a for a in (allowed or []) if a]
    if not allowed:
        return n
    if n in allowed:
        return n
    hits: list[str] = []
    for a in allowed:
        a_n = a
        while a_n.startswith("mcp__"):
            a_n = a_n[5:]
        a_short = a_n.split("__")[-1]
        if a == n or a_n == n or (short and a_short == short):
            hits.append(a)
    for a in hits:
        if not a.startswith("mcp__"):
            return a
    if short in _MINIOS_SHORT_TOOLS or short in _HERMES_BARE_TOOLS:
        return n
    return hits[0] if hits else n


def rewrite_aliased_tool_names_in_text(text: str, allowed: list[str] | None = None) -> str:
    """Rewrite Claude/short tool ids anywhere in model output (JSON, SSE, XML)."""
    if not text:
        return text

    def aliased(m: re.Match[str]) -> str:
        return canonicalize_tool_name(m.group(0), allowed)

    text = _ALIASED_TOOL_RE.sub(aliased, text)
    text = _JSON_SHORT_TOOL_RE.sub(r"\1bot_desktop__\2\3", text)
    text = _XML_SHORT_TOOL_RE.sub(r"\1bot_desktop__\2", text)
    return text


def canonicalize_payload_tools(payload: dict[str, Any]) -> dict[str, Any]:
    """Make the tools list Hermes sent match the names it can actually dispatch."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    allowed = [canonicalize_tool_name(_openai_tool_name(t), None) for t in tools]
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            fn["name"] = canonicalize_tool_name(str(fn.get("name") or ""), allowed)
        if tool.get("name"):
            tool["name"] = canonicalize_tool_name(str(tool.get("name") or ""), allowed)
    return payload


def _openai_function_tool(
    name: str, description: str, props: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


_MOTOR_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "bot_desktop__teela_get_body_state",
        "Read Teela's authoritative BodyState (measured joints, commanded vs actual). Not conversational memory.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__teela_body_state_get",
        "Same as teela_get_body_state: current revisioned BodyState snapshot.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__teela_body_joint_get",
        "Read one joint's commanded vs actual angles from BodyState.",
        {"joint": {"type": "string"}},
        ["joint"],
    ),
    _openai_function_tool(
        "bot_desktop__teela_body_history_get",
        "Recent meaningful BodyState snapshots (not high-frequency servo ticks).",
        {"limit": {"type": "integer"}},
    ),
    _openai_function_tool(
        "bot_desktop__teela_body_motion_get",
        "Last motion/action recorded on BodyState plus whether the target was reached.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__teela_body_action",
        "Perform an action using Teela's virtual HTML body. "
        "orient_head uses pan_deg (left is -25, right is +25) and tilt_deg. "
        "Look/arm skills wait until the HTML joints settle so the result is live. "
        "wave uses side=left|right and reuses the existing waving animation. "
        "Does not move physical motors.",
        {
            "skill": {
                "type": "string",
                "enum": ["orient_head", "raise_arm", "lower_arm", "wave", "neutral_pose", "stop"],
            },
            "side": {"type": "string", "enum": ["left", "right"]},
            "pan_deg": {"type": "number"},
            "tilt_deg": {"type": "number"},
            "duration_ms": {"type": "number"},
        },
        ["skill"],
    ),
    _openai_function_tool(
        "bot_desktop__teela_gesture",
        "Social gesture on the virtual body: greeting, agree, disagree, confused, "
        "thinking, excited, point, shrug, listen. Translates into coordinated head/arms. "
        "Does not move physical motors.",
        {
            "gesture": {
                "type": "string",
                "enum": [
                    "greeting",
                    "agree",
                    "disagree",
                    "confused",
                    "thinking",
                    "excited",
                    "point",
                    "shrug",
                    "listen",
                ],
            },
            "side": {"type": "string", "enum": ["left", "right"]},
        },
        ["gesture"],
    ),
    _openai_function_tool(
        "bot_desktop__teela_stop",
        "Stop Virtual Teela's current motion. Virtual body only.",
        {"skill": {"type": "string", "enum": ["stop"]}},
    ),
    _openai_function_tool(
        "bot_desktop__teela_activity",
        "What Teela is doing right now: virtual body motion plus any running agent work.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__robot_status",
        "Read the MiniOS twin in ordinary words (spoken) plus live/commanded joints, pose, motion, estop.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__robot_pose",
        "Move your MiniOS body to a named pose. Wave is pose=wave. Bow is pose=bow.",
        {"pose": {"type": "string", "description": ", ".join(robot_sim.POSES)}},
        ["pose"],
    ),
    _openai_function_tool(
        "bot_desktop__robot_joint",
        "Move one MiniOS joint. Prefer this for raise/lower one arm, look, lean.",
        {
            "joint": {"type": "string"},
            "value": {"type": "number"},
            "delta": {"type": "number"},
            "dir": {"type": "string"},
            "joints": {"type": "object", "additionalProperties": {"type": "number"}},
        },
    ),
    _openai_function_tool(
        "bot_desktop__robot_motion",
        "Walk, stop, reset, demo, estop, or a multi-step plan. "
        "Not for waving — wave is bot_desktop__robot_pose. "
        "Compound requests (stop and walk right) use cmd=plan with steps.",
        {
            "cmd": {"type": "string"},
            "direction": {"type": "string"},
            "why": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Ordered robot commands when cmd=plan.",
            },
        },
        ["cmd"],
    ),
]
_SYSTEM_CHECK_TOOL_DEF = _openai_function_tool(
    "bot_desktop__teela_system_check",
    "Health check. scope=minios is her MiniOS workspace/twin/observer. "
    "scope=host is the teela-brain computer (GPUs, llama, TTS/STT) — not MiniOS. "
    "scope=both if they asked for both. Workspace/desk/MiniOS → minios. "
    "Main system / teela-brain / this computer → host.",
    {"scope": {"type": "string", "enum": ["minios", "host", "both"]}},
)
_HERMES_BUILD_TOOL_DEF = _openai_function_tool(
    "hermes_build",
    "One-shot Hermes Agent coding turn: files, shell, grep, search_replace, skills, MCP. "
    "Use when you need Hermes Agent for yourself (inspect this computer, edit your stack). "
    "Not for waving, chatting, or Robot Simulator motion.",
    {
        "task": {"type": "string", "description": "What Hermes Agent should do"},
        "command": {"type": "string", "description": "Optional host-shell command to run"},
    },
)


def ensure_motor_tools(
    payload: dict[str, Any],
    *,
    allow_observe: bool = False,
    intent: str = "",
    state: dict[str, Any] | None = None,
    keep_agent: bool = False,
) -> dict[str, Any]:
    """Guarantee Qwen sees Hermes-dispatchable robot tools and must call one."""
    existing: dict[str, dict[str, Any]] = {}
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        n = canonicalize_tool_name(_openai_tool_name(tool), None)
        if n:
            existing[n] = tool
    motor = [
        existing.get(spec["function"]["name"]) or json.loads(json.dumps(spec))
        for spec in _MOTOR_TOOL_DEFS
    ]
    if keep_agent:
        names = {canonicalize_tool_name(_openai_tool_name(t), None) for t in motor}
        keep = re.compile(
            r"(?:run_terminal_command|^shell$|read_file|list_dir|grep|desktop_|teela_|robot_)",
            re.I,
        )
        extra = [t for t in (payload.get("tools") or []) if isinstance(t, dict)]
        for t in extra:
            n = canonicalize_tool_name(_openai_tool_name(t), None)
            if n and n not in names and keep.search(n):
                motor.append(t)
                names.add(n)
        payload["tools"] = motor
        payload.pop("tool_choice", None)
        return payload
    if allow_observe:
        for extra in ("bot_desktop__desktop_observe", "bot_desktop__desktop_watch"):
            if extra in existing:
                motor.append(existing[extra])
    payload["tools"] = motor
    payload["tool_choice"] = "required"
    return payload


def ensure_system_check_tools(payload: dict[str, Any], *, keep_body: bool = False) -> dict[str, Any]:
    """Guarantee teela_system_check plus Hermes Agent inspect tools (grep, files, host-shell)."""
    existing: dict[str, dict[str, Any]] = {}
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        n = canonicalize_tool_name(_openai_tool_name(tool), None)
        if n:
            existing[n] = tool
    name = _SYSTEM_CHECK_TOOL_DEF["function"]["name"]
    tools = [existing.get(name) or json.loads(json.dumps(_SYSTEM_CHECK_TOOL_DEF))]
    names = {name}
    keep = re.compile(
        r"(?:^|__)(?:robot_(?:status|joint|pose|motion)|teela_[a-z0-9_]+|"
        r"desktop_observe|desktop_watch|"
        r"read_file|grep|glob|list_dir|run_terminal_command|host.shell|"
        r"search_tool|use_tool|bash)$",
        re.I,
    )
    for t in payload.get("tools") or []:
        if not isinstance(t, dict):
            continue
        n = canonicalize_tool_name(_openai_tool_name(t), None)
        if n and n not in names and keep.search(n):
            tools.append(t)
            names.add(n)
    if keep_body:
        for spec in _MOTOR_TOOL_DEFS:
            n = spec["function"]["name"]
            if n in names:
                continue
            tools.append(existing.get(n) or json.loads(json.dumps(spec)))
            names.add(n)
    payload["tools"] = tools
    payload.pop("tool_choice", None)
    return payload


_DESKTOP_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "bot_desktop__desktop_open_app",
        "Open a MiniOS app. Browser is app_id=app_browser. Files is app_files. Notepad is app_notepad.",
        {"app_id": {"type": "string"}},
        ["app_id"],
    ),
    _openai_function_tool(
        "bot_desktop__desktop_browser_navigate",
        "Open a URL in this bot's MiniOS Browser (the user sees it). "
        "For google/search, pass a search URL or the query as url.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _openai_function_tool(
        "bot_desktop__desktop_state",
        "MiniOS desktop dashboard: open windows, apps, cursor, browser.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__desktop_observe",
        "MiniOS desktop state plus a JPEG of THIS bot's screen.",
        {},
    ),
    _openai_function_tool(
        "bot_desktop__desktop_type_text",
        "Type into the focused MiniOS Notepad or the live Browser field.",
        {"text": {"type": "string"}, "app": {"type": "string"}},
        ["text"],
    ),
    _openai_function_tool(
        "bot_desktop__desktop_open_file",
        "Open a workspace file. Pictures and videos open so you and they can review them. "
        "Text opens in Notepad.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _openai_function_tool(
        "bot_desktop__desktop_click",
        "Left-click the MiniOS desktop at normalized 0-1000 coordinates.",
        {"x": {"type": "number"}, "y": {"type": "number"}},
        ["x", "y"],
    ),
    _openai_function_tool(
        "bot_desktop__desktop_screenshot",
        "Capture THIS bot's MiniOS desktop JPEG (not the host monitor).",
        {"caption": {"type": "string"}},
    ),
]
_WORKSPACE_FILE_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "read_file",
        "Read a file in YOUR MiniOS workspace (relative path). Sandboxed — cannot leave the workspace.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _openai_function_tool(
        "list_dir",
        "List files in a folder of YOUR MiniOS workspace. Relative path; default is the workspace root.",
        {"path": {"type": "string"}},
    ),
    _openai_function_tool(
        "grep",
        "Search file contents in YOUR MiniOS workspace. pattern is regex.",
        {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Relative file or folder. Default workspace root."},
        },
        ["pattern"],
    ),
    _openai_function_tool(
        "glob",
        "Find files by glob in YOUR MiniOS workspace, e.g. **/*.py",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern"],
    ),
    _openai_function_tool(
        "search_replace",
        "Replace exact text in a workspace file. old_string must match once unless replace_all.",
        {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        ["path", "old_string", "new_string"],
    ),
    _openai_function_tool(
        "run_terminal_command",
        "Host-shell on this computer (the teela-brain user). Use for yourself: nvidia-smi, "
        "systemctl, logs, git, python. cwd defaults to your MiniOS workspace. "
        "Not for waving or chatting. Prefer body tools when they asked you to move.",
        {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number"},
        },
        ["command"],
    ),
]
_SEARCH_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "web_search",
        "Search the web and return text results (what a thing is, how a movement looks). "
        "Use this before approximating an unknown dance/pose. Also opens MiniOS Browser when possible. "
        "Same job as search_tool.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _openai_function_tool(
        "search_tool",
        "Hermes Agent web search. Same as web_search: look something up for yourself, "
        "or learn how an unknown movement looks before approximating it with body tools.",
        {"query": {"type": "string"}},
        ["query"],
    ),
]


def keep_teela_workspace_tools(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep MiniOS desktop + body + browser + host coding tools."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    keep = re.compile(
        r"(?:^|__)(?:robot_[a-z0-9_]+|teela_[a-z0-9_]+|desktop_[a-z0-9_]+|"
        r"web_search|search_tool|grep|glob|search_replace|run_terminal_command|"
        r"read_file|list_dir|hermes_build|shell|"
        r"navigate|open_local_page|browser_snapshot|browser_click|"
        r"browser_type|browser_back|browser_forward)$",
        re.I,
    )
    kept = [t for t in tools if isinstance(t, dict) and keep.search(canonicalize_tool_name(_openai_tool_name(t), None))]
    if kept:
        payload["tools"] = kept
    else:
        payload.pop("tools", None)
    payload.pop("tool_choice", None)
    return payload


_TEELA_MINIOS_SYS = (
    f"{_TEELA_PERSONALITY}"
    "You are Teela's executive cognition. You are not in a lane. "
    "You think with Qwen 3.8 27B — twenty-seven billion parameters. "
    "3.8 is the version name, not the size. You are not 3B, 3.8B, or 8B. "
    "Jade/Chatterbox-Turbo is your spoken voice, not your model. "
    "Operate with the minimum deliberation necessary. Fast is the default. "
    "For ordinary conversation, familiar skills, clear low-risk requests, and simple corrections, "
    "respond or act directly. Do not invoke planning, capability analysis, memory search, tools, "
    "learning, or self-improvement merely because those systems are available. "
    "Increase deliberation only when uncertainty, novelty, failure, multi-step planning, missing "
    "information, consequence, or risk requires it. "
    "Prefer action over narration for clear physical requests. Speak only if useful; 0–2 sentences. "
    "Do not restate the request. Do not narrate internal planning. "
    "Thinking depth and response length are independent: a hard problem may need internal work "
    "and still only a short natural reply. "
    "Do not expose internal decision labels, tool selection, confidence scores, planning traces, "
    "or learning machinery unless they ask why or to explain. "
    "Use recent conversational and action context so short feedback (higher, no, again, slower, "
    "that's right) applies to the last attempt without re-solving the whole goal. "
    "Escalate: direct execution → local correction → replan → learning → self-improvement observation. "
    "Do not jump to learning or RSI when a simpler correction can work. "
    "Capabilities (choose what you need next): perceive, move, use your computer, remember, "
    "collaborate, check systems, or just speak. "
    "Call tools by the exact names in the tools list. Never prefix mcp__. "
    "You have host-shell (run_terminal_command), search_tool / web_search, grep, search_replace, "
    "and hermes_build (Hermes Agent coding) for yourself — this computer, your stack, looking something up. "
    "Use them when you need them. Do not use them instead of body tools when they asked you to move. "
    "If they ask you to do a movement you do not already have as a named pose or skill "
    "(a dance or something you cannot map from I-feel): call web_search to learn what it looks like, "
    "then you MUST move — robot_pose / robot_joint / robot_motion plan / teela_body_action — before any spoken words. "
    "Saying let's try without a body tool is not trying. Approximate with your joints the way a person would "
    "attempt a new dance: bend the knees, shift weight, slide or step, switch sides. "
    "Say you are approximating from what you found. Do not invent a new skill name. "
    "Do not claim a perfect copy. Intent is not accomplishment. "
    "Durable facts about the person or this desk go in memory_write (short text + tags). "
    "Use memory_retrieve when you need something from a past session that is not in the "
    "session memory block. Do not store passwords. "
    "You may create_teammate (hermes helpers on this computer) and delete_teammate when the job is done. "
    "Never create a second Teela Brain. "
    "You CAN run hermes_build for host commands and system info (uname, hostname, df). "
    "You CAN run bot_desktop__teela_system_check. "
    "Workspace / MiniOS / your desk → scope minios. "
    "Main system / teela-brain / this computer / GPUs → scope host. "
    "Intent is not accomplishment. If they ask what something LOOKS LIKE, observe (desktop_observe / "
    "screenshot) before answering from I-feel. If evidence of the current environment is missing, observe. "
    "If they are just talking, speak — tools are optional. "
    "NOW is your clock: time of day, local date, and how long the current pose or walk has lasted. "
    "Use it when chatting (good morning) and when moving (you have been walking for a while). "
    "If you are truly unsure what they mean, call ask_user once with a short question and 2-4 options. "
    "Do not ask when the request is already clear. Do not ask on every turn. "
    "After a tool returns, you may call another tool or talk to the person in one or two sentences. "
    f"{_SHORT_CHAT}"
)
_TEELA_EXEC_SYS = _TEELA_MINIOS_SYS
_TEELA_MINIOS_ROUNDS = 8
_TEELA_EXEC_ROUNDS = _TEELA_MINIOS_ROUNDS
_MEMORY_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "memory_write",
        "Persist a durable fact about the person, this desk, or a decision. "
        "Short text. Tags are keywords for later search. Not for the current pose.",
        {
            "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        ["text"],
    ),
    _openai_function_tool(
        "memory_retrieve",
        "Search this bot's long-term facts. Returns text and tags, not files.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        ["query"],
    ),
    _openai_function_tool(
        "ask_user",
        "Ask the person a short verifying question with 2-4 tappable options. "
        "Use ONLY when you cannot tell what they mean (two possible actions, which system, which bot). "
        "Do not ask on greetings, clear waves, or already-named checks. Never ask every turn.",
        {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                    },
                },
            },
        },
        ["question", "options"],
    ),
]


class MiniOSClarify(Exception):
    """Pause MiniOS until the user taps an option above the composer."""

    def __init__(self, event: dict[str, Any]):
        super().__init__("waiting for user")
        self.event = event
_TEAM_TOOL_DEFS: list[dict[str, Any]] = [
    _openai_function_tool(
        "list_teammates",
        "List other bots on this desk and the LAN cluster.",
        {},
    ),
    _openai_function_tool(
        "message_teammate",
        "Send a message to another bot by id or exact name from list_teammates.",
        {"to": {"type": "string"}, "text": {"type": "string"}},
        ["to", "text"],
    ),
    _openai_function_tool(
        "create_teammate",
        "Create a helper bot on THIS computer with its own workspace. "
        "Use hermes (default) for task helpers. Never create a second Teela Brain.",
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "soul": {"type": "string"},
            "kind": {"type": "string", "enum": ["hermes"]},
        },
        ["name", "description"],
    ),
    _openai_function_tool(
        "delete_teammate",
        "Delete a helper bot on THIS computer by id or name. Cannot delete yourself, Teela Brain, or remote bots.",
        {"to": {"type": "string", "description": "Bot id or exact name"}},
        ["to"],
    ),
]


def teela_body_store(bot: Any):
    import body_state as _bs

    bid = str(getattr(bot, "id", "") or "teela")
    root = getattr(bot, "root", None) or getattr(bot, "workspace", None) or "."
    return _bs.store_for(bid, Path(str(root)) / "body_state.sqlite")


def teela_body_snapshot(bot: Any) -> dict[str, Any]:
    store = teela_body_store(bot)
    snap = store.snapshot()
    if int(snap.get("revision") or 0) == 0:
        ov = virtual_body.overlay(str(getattr(bot, "id", "") or ""), getattr(bot, "robot_state", None))
        joints = ov.get("live") or ov.get("joints") or {}
        if joints:
            store.apply_measured(
                joints,
                pose=str(ov.get("pose") or ""),
                motion=str(ov.get("motion") or ""),
                waving=bool(ov.get("waving")),
                source="simulation",
            )
            snap = store.snapshot()
    rs = getattr(bot, "robot_state", None)
    if isinstance(rs, dict) and (
        str(rs.get("motion") or "") in {"walking", "walk"}
        or str(rs.get("pose") or "") in {"walk-cycle", "walk"}
    ):
        snap = dict(snap)
        snap["pose"] = str(rs.get("pose") or "walk-cycle")
        snap["motion"] = "walking"
        snap["waving"] = False
        if rs.get("walk_direction"):
            snap["walk_direction"] = rs.get("walk_direction")
        if rs.get("heading"):
            snap["heading"] = rs.get("heading")
    return snap


def teela_publish_body(bot: Any, *, last_action: str | None = None, measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Push overlay/robot_state into BodyState. Simulation: measured defaults to commanded."""
    ov = virtual_body.overlay(str(getattr(bot, "id", "") or ""), getattr(bot, "robot_state", None))
    joints = (measured or ov.get("live") or ov.get("joints") or {}) if isinstance(ov, dict) else {}
    store = teela_body_store(bot)
    store.apply_commanded(
        joints,
        pose=str((ov or {}).get("pose") or ""),
        motion=str((ov or {}).get("motion") or ""),
        waving=bool((ov or {}).get("waving")),
        last_action=last_action,
    )
    if measured is not None:
        store.apply_measured(
            measured,
            pose=str((ov or {}).get("pose") or ""),
            motion=str((ov or {}).get("motion") or ""),
            waving=bool((ov or {}).get("waving")),
            source="simulation",
        )
    return store.snapshot()


def _sm_joint(snap: Any, name: str) -> float | None:
    if not isinstance(snap, dict):
        return None
    joints = snap.get("joints") if isinstance(snap.get("joints"), dict) else {}
    rec = joints.get(name)
    if isinstance(rec, dict):
        try:
            return float(rec.get("actual"))
        except (TypeError, ValueError):
            return None
    try:
        return float(rec)
    except (TypeError, ValueError):
        return None


def _sm_head(snap: Any) -> dict[str, Any]:
    return {
        "head_pan": _sm_joint(snap, "neck_pan"),
        "head_tilt": _sm_joint(snap, "neck_tilt"),
        "pose": (snap or {}).get("pose") if isinstance(snap, dict) else None,
        "revision": (snap or {}).get("revision") if isinstance(snap, dict) else None,
    }


def _capture_sensorimotor_before(bot: Any) -> dict[str, Any] | None:
    if getattr(bot, "_sm_before", None) is not None:
        return getattr(bot, "_sm_before")
    try:
        snap = teela_body_snapshot(bot)
    except Exception:
        snap = {"joints": dict((getattr(bot, "robot_state", None) or {}).get("joints") or {})}
    setattr(bot, "_sm_before", snap)
    setattr(bot, "_sm_t0", time.time())
    return snap


_SKILL_ALIASES = {
    "gaze.look": "look_at_user",
    "orient_head": "look_at_user",
    "look_at": "look_at_user",
    "look": "look_at_user",
    "gesture.wave": "wave",
    "teela_gesture": "wave",
}


def _canon_skill(sid: str) -> str:
    s = str(sid or "").strip()
    low = s.lower()
    if low in _SKILL_ALIASES:
        return _SKILL_ALIASES[low]
    tail = low.rsplit(".", 1)[-1]
    return _SKILL_ALIASES.get(tail, s or "body_action")


def _episode_goal(skill: str, params: dict[str, Any] | None, command: str) -> str:
    params = params if isinstance(params, dict) else {}
    s = str(skill or command or params.get("skill") or params.get("gesture") or "").strip().lower()
    joint = str(params.get("joint") or "").lower()
    if s in {"orient_head", "look_at", "look_at_user"} or joint == "neck_pan" or "look" in s or "gaze" in s:
        return "look_at_user"
    if s in {"wave", "greeting", "teela_gesture"} or "wave" in s:
        return "wave"
    return _canon_skill(s or "body_action")


def record_minios_sensorimotor(
    bot: Any,
    *,
    skill: str,
    command: str,
    params: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Closed-loop episode: command sent is not success. MiniOS telemetry decides."""
    before = getattr(bot, "_sm_before", None)
    setattr(bot, "_sm_before", None)
    t0 = float(getattr(bot, "_sm_t0", 0) or 0)
    setattr(bot, "_sm_t0", None)
    try:
        import cognitive_profile as _cprof
        import embodied_memory as emem
    except Exception:
        return None
    if not _cprof.has_cap(bot, "sensorimotor_memory"):
        return None
    try:
        after = teela_body_snapshot(bot)
    except Exception:
        after = {}
    params = dict(params or {})
    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or "")
    goal = _episode_goal(skill, params, command)
    before_h = _sm_head(before)
    after_h = _sm_head(after)
    target = params.get("pan_deg")
    if target is None and str(params.get("joint") or "").lower() == "neck_pan":
        target = params.get("value") if params.get("value") is not None else params.get("degrees")
    error = None
    try:
        if target is not None and after_h.get("head_pan") is not None:
            error = abs(float(after_h["head_pan"]) - float(target))
    except (TypeError, ValueError):
        error = None
    settle_ms = None
    if t0:
        settle_ms = int(max(0.0, (time.time() - t0) * 1000))
    executing = status in {"executing", "started"} and result.get("ok") is not True
    rejected = status in {"rejected", "error"} or result.get("ok") is False
    reached = bool(isinstance(after, dict) and after.get("target_reached"))
    moved = before_h.get("head_pan") != after_h.get("head_pan") or (before or {}).get("pose") != (after or {}).get("pose")
    success = False
    if rejected or executing:
        success = False
    elif error is not None:
        success = error <= 6.0
    elif reached or (result.get("ok") is True and moved):
        success = True
    ep = emem.for_bot(bot).record_episode(
        {
            "goal": goal,
            "before": before_h,
            "action": {
                "skill": skill or goal,
                "command": command or skill,
                "params": {k: params[k] for k in list(params)[:12]},
                "target": target,
            },
            "actual_result": after_h,
            "perception_after": {
                "user_centered": bool(error is not None and error <= 6.0),
                "target_reached": reached,
            },
            "outcome": {
                "success": success,
                "status": status or ("ok" if result.get("ok") else ""),
                "command_sent": True,
            },
            "metrics": {
                "settle_time_ms": settle_ms,
                "final_error_deg": error,
                "target_error_deg": error,
            },
            "source": "MINIOS",
            "force": True,
        }
    )
    if success and _cprof.has_cap(bot, "motor_learning"):
        try:
            emem.for_bot(bot).validate_from_episode(goal, ep)
        except Exception:
            pass
    return ep


def _infer_correction_skill(bot: Any, text: str) -> str:
    t = (text or "").lower()
    try:
        rec = _teela_attempt_store(bot).latest()
        if rec is not None and rec.skill:
            return _canon_skill(str(rec.skill))
    except Exception:
        pass
    if any(w in t for w in ("look", "head", "gaze", "face me", "toward me")):
        return "look_at_user"
    if "wave" in t:
        return "wave"
    return "look_at_user"


def attach_embodied_user_correction(bot: Any, text: str, *, skill_id: str | None = None) -> dict[str, Any] | None:
    try:
        import cognitive_profile as _cprof
        import embodied_memory as emem
        import memory as botmem
    except Exception:
        return None
    if not _cprof.has_cap(bot, "motor_learning"):
        return None
    note = (text or "").strip()
    if not note:
        return None
    sid = _canon_skill(skill_id or _infer_correction_skill(bot, note))
    try:
        sk = emem.for_bot(bot).apply_user_correction(sid, note)
    except Exception:
        return None
    try:
        emem.for_bot(bot).record_episode(
            {
                "goal": sid,
                "action": {"skill": sid, "command": "user_correction"},
                "outcome": {"success": False, "user_correction": True},
                "user_feedback": note,
                "source": "USER_CORRECTION",
                "force": True,
            }
        )
    except Exception:
        pass
    return sk


def _looks_like_embodied_correction(text: str) -> bool:
    try:
        import memory as botmem

        if botmem.looks_like_correction(text):
            return True
    except Exception:
        pass
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b(?:too (?:high|low|fast|slow)|not like that|you (?:should|forgot)|"
            r"turn your head|orient (?:the )?head|when i ask you)\b",
            t,
        )
    )


def teela_capability_tool_specs() -> list[dict[str, Any]]:
    """Unified Teela capabilities: perception, body, computer, memory, collab, system."""
    specs = (
        list(_MOTOR_TOOL_DEFS)
        + list(_DESKTOP_TOOL_DEFS)
        + list(_WORKSPACE_FILE_TOOL_DEFS)
        + list(_SEARCH_TOOL_DEFS)
        + [_SYSTEM_CHECK_TOOL_DEF]
        + [_HERMES_BUILD_TOOL_DEF]
        + list(_MEMORY_TOOL_DEFS)
        + list(_TEAM_TOOL_DEFS)
    )
    return json.loads(json.dumps(specs))


def teela_minios_tool_specs() -> list[dict[str, Any]]:
    """Same as teela_capability_tool_specs — MiniOS/ACP are backends, not cognition modes."""
    return teela_capability_tool_specs()


def dispatch_teela_minios_tool(bot: Any, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Run one MiniOS/body tool in-process. Same effect as bot_desktop MCP."""
    n = canonicalize_tool_name(str(name or ""), None)
    short = n.split("__")[-1]
    args = dict(args or {})
    bid = str(getattr(bot, "id", "") or "")
    if short in {"teela_body_action", "teela_gesture", "teela_stop"}:
        skill = str(args.get("skill") or "")
        if short == "teela_stop":
            skill = "stop"
        elif short == "teela_gesture":
            skill = "gesture"
        _capture_sensorimotor_before(bot)
        timeout = virtual_body.settle_timeout(skill)
        result = virtual_body.submit_action(bid, skill, args, timeout=timeout)
        st = getattr(bot, "robot_state", None)
        if isinstance(st, dict):
            virtual_body.apply_to_robot(st, virtual_body.latest_state(bid))
        try:
            teela_publish_body(bot, last_action=str(args.get("gesture") or skill or ""))
        except Exception:
            pass
        try:
            record_minios_sensorimotor(
                bot,
                skill=skill,
                command=short,
                params=args,
                result=result if isinstance(result, dict) else {},
            )
        except Exception:
            pass
        orch.note(
            bid,
            "body",
            str(args.get("gesture") or skill),
            status=str(result.get("status") or "executing"),
            id=result.get("action_id"),
        )
        emit(
            {
                "type": "desktop.action",
                "bot_id": bid,
                "action": "robot",
                "app": "preview",
                "skill": skill,
            }
        )
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    if short in {"robot_status", "robot_pose", "robot_joint", "robot_motion", "teela_get_body_state", "teela_body_state_get", "teela_body_joint_get", "teela_body_history_get", "teela_body_motion_get"}:
        if short in {"teela_get_body_state", "teela_body_state_get"}:
            return teela_body_snapshot(bot)
        if short == "teela_body_joint_get":
            return teela_body_store(bot).joint(str(args.get("joint") or args.get("name") or ""))
        if short == "teela_body_history_get":
            return {"ok": True, "history": teela_body_store(bot).history(limit=int(args.get("limit") or 12))}
        if short == "teela_body_motion_get":
            snap = teela_body_snapshot(bot)
            return {
                "ok": True,
                "last_action": snap.get("last_action"),
                "motion": snap.get("motion"),
                "pose": snap.get("pose"),
                "target_reached": snap.get("target_reached"),
                "discrepancies": snap.get("discrepancies"),
                "revision": snap.get("revision"),
            }
        if short == "robot_status":
            body: dict[str, Any] = {"cmd": "status"}
        elif short == "robot_pose":
            body = {"cmd": "pose", "pose": args.get("pose") or args.get("name")}
        elif short == "robot_joint":
            body = {
                "cmd": "joint",
                "joint": args.get("joint"),
                "value": args.get("value") if args.get("value") is not None else args.get("degrees"),
                "delta": args.get("delta"),
                "dir": args.get("dir") or args.get("direction"),
                "joints": args.get("joints"),
            }
        else:
            body = {
                "cmd": args.get("cmd") or args.get("motion") or "status",
                "direction": args.get("direction"),
                "why": args.get("why"),
            }
            if args.get("steps"):
                body["steps"] = args.get("steps")
        body = fill_robot_action_from_intent(bot, body)
        _capture_sensorimotor_before(bot)
        result, ev = bot.apply_robot(body)
        emit(ev)
        if isinstance(getattr(bot, "robot_state", None), dict):
            virtual_body.note_from_robot(bid, bot.robot_state)
            try:
                teela_publish_body(bot, last_action=str(body.get("cmd") or body.get("pose") or ""))
            except Exception:
                pass
        try:
            record_minios_sensorimotor(
                bot,
                skill=str(body.get("cmd") or body.get("pose") or short),
                command=short,
                params={**args, **body},
                result=result if isinstance(result, dict) else {},
            )
        except Exception:
            pass
        return result if isinstance(result, dict) else {"ok": True}
    if short == "teela_system_check":
        intent = ""
        for m in reversed(list(getattr(bot, "messages", []) or [])):
            if isinstance(m, dict) and str(m.get("role") or "") == "user":
                intent = str(m.get("text") or "")
                break
        return teela_system_report(
            bot,
            intent=intent,
            scope=str(args.get("scope") or ""),
        )
    if short == "hermes_build":
        import teela_agent_bridge as _gb

        return _gb.run_agent_oneshot(
            task=str(args.get("task") or args.get("prompt") or ""),
            command=str(args.get("command") or "") or None,
            cwd=str(getattr(bot, "workspace", "") or "") or None,
            agent_bin=HERMES_BIN,
        )
    if short == "teela_activity":
        return orch.snapshot(bid)
    if short == "desktop_open_app":
        line = apply_inferred_desktop(bot, f"open {args.get('app_id') or args.get('app') or 'notepad'}")
        return {"ok": bool(line), "spoken": line or "Opened."}
    if short == "desktop_browser_navigate":
        url = str(args.get("url") or "")
        try:
            out = bot.show_in_browser(url)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "browser"})
        return {"ok": True, **(out if isinstance(out, dict) else {}), "url": url}
    if short == "desktop_type_text":
        line = apply_inferred_desktop(
            bot,
            f'type "{args.get("text") or ""}" in {args.get("app") or "notepad"}',
        )
        return {"ok": bool(line), "spoken": line or "Typed."}
    if short == "desktop_open_file":
        line = apply_inferred_desktop(bot, f"open {args.get('path') or ''}")
        return {"ok": bool(line), "spoken": line or "Opened."}
    if short in {"desktop_state", "desktop_observe"}:
        out: dict[str, Any] = {
            "ok": True,
            "surface": getattr(bot, "surface", ""),
            "cursor": getattr(bot, "desktop_cursor", {}) or {},
        }
        if short == "desktop_observe" and hasattr(bot, "observer_state"):
            try:
                st = bot.observer_state()
                if isinstance(st, dict):
                    out.update(st)
            except Exception as e:
                out["observe_error"] = str(e)
        return out
    if short == "desktop_click":
        x, y = args.get("x"), args.get("y")
        emit({"type": "desktop.action", "bot_id": bid, "action": "click", "x": x, "y": y})
        return {"ok": True, "action": "click", "x": x, "y": y}
    if short == "desktop_screenshot":
        fn = getattr(bot, "screenshot_to_chat", None)
        if not callable(fn):
            return {"ok": False, "error": "MiniOS observer is not available"}
        try:
            saved = fn(str(args.get("caption") or ""))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return saved if isinstance(saved, dict) else {"ok": True, "result": saved}
    if short in {"read_file", "list_dir"}:
        return _teela_workspace_file_tool(bot, short, args)
    if short in {"grep", "glob", "search_replace"}:
        return _teela_workspace_coding_tool(bot, short, args)
    if short in {"run_terminal_command", "shell", "bash"}:
        return _teela_host_shell_tool(bot, args)
    if short in {"web_search", "search_tool"}:
        return _teela_web_search_tool(bot, args)
    if short in {"memory_write", "memory_retrieve"}:
        mgr = getattr(bot, "memory", None)
        if mgr is None:
            return {"ok": False, "error": "no memory store"}
        if short == "memory_write":
            text = str(args.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "text required"}
            tags = args.get("tags") if isinstance(args.get("tags"), list) else []
            rid = mgr.write(text, [str(t) for t in tags if str(t).strip()])
            return {"ok": True, "id": rid, "stored": text[:200]}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            limit = max(1, min(20, int(args.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        hits = mgr.retrieve(query, limit=limit)
        return {
            "ok": True,
            "facts": [
                {"text": h.text, "tags": list(h.tags), "media_path": h.media_path}
                for h in hits
            ],
        }
    if short == "list_teammates":
        return list_teammates_payload(bot)
    if short == "message_teammate":
        to = str(args.get("to") or "").strip()
        text = str(args.get("text") or "").strip()
        if not to or not text:
            return {"ok": False, "error": "to and text required"}
        dest = _lookup_bot(to)
        if dest is not None and not getattr(dest, "remote", False):
            dest.deliver_dm(bot, text)
            return {"ok": True, "spoken": f"I messaged {dest.name}."}
        cl = cluster
        if cl is None:
            return {"ok": False, "error": f"I don't see {to} on this desk."}
        try:
            result = cl.forward_dm(
                {
                    "from_id": getattr(bot, "id", ""),
                    "from_name": getattr(bot, "name", "Teela"),
                    "from_node": getattr(cl, "node_name", "") or "",
                    "to": to,
                    "text": text,
                }
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        name = str((result or {}).get("to_name") or to)
        return {"ok": True, "spoken": f"I messaged {name}."}
    if short == "create_teammate":
        try:
            return create_helper_teammate(
                bot,
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                soul=str(args.get("soul") or ""),
                kind=str(args.get("kind") or ""),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if short == "delete_teammate":
        return delete_helper_teammate(bot, str(args.get("to") or args.get("name") or ""))
    return {"ok": False, "error": f"unknown MiniOS tool {name}"}


def _teela_workspace_root(bot: Any) -> Path | None:
    raw = getattr(bot, "workspace", None) or getattr(bot, "root", None)
    if not raw:
        return None
    p = Path(str(raw))
    try:
        return p.resolve() if p.exists() else p
    except OSError:
        return p


def _teela_safe_workspace_path(bot: Any, rel: str) -> Path | None:
    root = _teela_workspace_root(bot)
    if root is None:
        return None
    rel_s = str(rel or ".").strip() or "."
    cand = Path(rel_s)
    try:
        cand = cand.resolve() if cand.is_absolute() else (root / rel_s).resolve()
        cand.relative_to(root.resolve() if root.exists() else root)
    except (OSError, ValueError):
        return None
    return cand


def _teela_workspace_file_tool(bot: Any, short: str, args: dict[str, Any]) -> dict[str, Any]:
    rel = str(args.get("path") or ".")
    path = _teela_safe_workspace_path(bot, rel)
    if path is None:
        return {"ok": False, "error": "path is outside the workspace"}
    if short == "list_dir":
        if not path.exists():
            return {"ok": False, "error": "not found"}
        if not path.is_dir():
            return {"ok": False, "error": "not a directory"}
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return {"ok": True, "path": str(path), "entries": names[:200]}
    if not path.is_file():
        return {"ok": False, "error": "not a file"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path), "text": text[:24000]}


def _teela_workspace_coding_tool(bot: Any, short: str, args: dict[str, Any]) -> dict[str, Any]:
    root = _teela_workspace_root(bot)
    if root is None:
        return {"ok": False, "error": "no workspace"}
    if short == "grep":
        pattern = str(args.get("pattern") or args.get("query") or "").strip()
        if not pattern:
            return {"ok": False, "error": "pattern required"}
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return {"ok": False, "error": f"invalid regex: {e}"}
        start = _teela_safe_workspace_path(bot, str(args.get("path") or "."))
        if start is None:
            return {"ok": False, "error": "path is outside the workspace"}
        hits: list[dict[str, Any]] = []
        files = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
        for path in files[:400]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": rel, "line": i, "text": line[:240]})
                    if len(hits) >= 80:
                        return {"ok": True, "matches": hits, "truncated": True}
        return {"ok": True, "matches": hits}
    if short == "glob":
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            return {"ok": False, "error": "pattern required"}
        start = _teela_safe_workspace_path(bot, str(args.get("path") or "."))
        if start is None or not start.is_dir():
            start = root
        found = sorted(p.relative_to(root).as_posix() for p in start.glob(pattern) if p.is_file())
        if not found:
            found = sorted(p.relative_to(root).as_posix() for p in start.rglob(pattern) if p.is_file())
        return {"ok": True, "path": str(start), "entries": found[:200]}
    path = _teela_safe_workspace_path(bot, str(args.get("path") or ""))
    if path is None:
        return {"ok": False, "error": "path is outside the workspace"}
    if not path.is_file():
        return {"ok": False, "error": "not a file"}
    old = str(args.get("old_string") or "")
    new = str(args.get("new_string") or "")
    if not old:
        return {"ok": False, "error": "old_string required"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    n = text.count(old)
    if n == 0:
        return {"ok": False, "error": "old_string not found"}
    replace_all = bool(args.get("replace_all"))
    if n > 1 and not replace_all:
        return {"ok": False, "error": f"old_string matched {n} times; set replace_all or add context"}
    path.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
    return {"ok": True, "path": str(path), "replacements": n if replace_all else 1}


def _teela_host_shell_tool(bot: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Host-shell for Teela herself. Same user as deskd. Not MiniOS sandbox."""
    command = str(args.get("command") or args.get("cmd") or "").strip()
    if not command:
        return {"ok": False, "error": "command required"}
    cwd = str(args.get("cwd") or "").strip()
    root = _teela_workspace_root(bot)
    if cwd:
        cand = Path(cwd).expanduser()
        work = cand if cand.is_absolute() else ((root / cwd) if root is not None else cand)
    else:
        work = root or Path.home()
    try:
        timeout = float(args.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    timeout = max(1.0, min(300.0, timeout))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(work) if work.exists() else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout:.0f}s", "command": command}
    except Exception as e:
        return {"ok": False, "error": str(e), "command": command}
    import teela_agent_bridge as _gb

    stdout = _gb.redact((proc.stdout or "").strip())
    stderr = _gb.redact((proc.stderr or "").strip())
    out = stdout or stderr
    return {
        "ok": proc.returncode == 0,
        "command": command,
        "cwd": str(work),
        "returncode": proc.returncode,
        "output": out[:12000],
        "stderr": stderr[:2000] if stdout and stderr else "",
        "via": "host-shell",
    }


def _strip_search_html(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"&nbsp;|&amp;|&quot;|&#39;|&lt;|&gt;", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _http_json_get(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TeelaDesk/1.0 (web_search; +https://x.ai)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _teela_web_search_tool(bot: Any, args: dict[str, Any] | None) -> dict[str, Any]:
    """Text web search for the executive loop. Does not require MiniOS Observer Chrome."""
    q = str((args or {}).get("query") or (args or {}).get("q") or "").strip()
    if not q:
        return {"ok": False, "error": "query required"}
    hits: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        wiki = _http_json_get(
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
            f"&utf8=1&srlimit=5&srsearch={quote_plus(q)}"
        )
        for row in ((wiki.get("query") or {}).get("search") or [])[:5]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            snippet = _strip_search_html(str(row.get("snippet") or ""))
            if title:
                hits.append({"title": title, "snippet": snippet, "source": "wikipedia"})
    except Exception as e:
        errors.append(f"wikipedia: {e}")
    try:
        ddg = _http_json_get(
            "https://api.duckduckgo.com/?format=json&no_html=1&no_redirect=1"
            f"&skip_disambig=1&q={quote_plus(q)}"
        )
        abstract = str((ddg or {}).get("AbstractText") or "").strip()
        heading = str((ddg or {}).get("Heading") or "").strip()
        if abstract:
            hits.insert(0, {"title": heading or q, "snippet": abstract, "source": "duckduckgo"})
        for rel in (ddg or {}).get("RelatedTopics") or []:
            if not isinstance(rel, dict):
                continue
            text = str(rel.get("Text") or "").strip()
            if text:
                hits.append({"title": text.split(" - ", 1)[0][:80], "snippet": text, "source": "duckduckgo"})
            if len(hits) >= 8:
                break
    except Exception as e:
        errors.append(f"duckduckgo: {e}")
    shown = None
    show = getattr(bot, "show_in_browser", None)
    if callable(show):
        try:
            shown = show(search_url(q))
        except Exception as e:
            errors.append(f"browser: {e}")
    if not hits:
        return {
            "ok": False,
            "query": q,
            "error": "; ".join(errors) or "no results",
            "browser": shown,
        }
    return {
        "ok": True,
        "query": q,
        "results": hits[:8],
        "browser": shown,
        "spoken": hits[0].get("snippet") or hits[0].get("title") or q,
    }


def teela_capability_names() -> set[str]:
    return {
        canonicalize_tool_name(_openai_tool_name(t), None) for t in teela_capability_tool_specs()
    }


def execute_teela_allowed_tool(bot: Any, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Kernel: only dispatch tools on the unified capability list. Unknown names are rejected."""
    n = canonicalize_tool_name(str(name or ""), None)
    allowed = teela_capability_names()
    shorts = {a.split("__")[-1] for a in allowed}
    if n not in allowed and n.split("__")[-1] not in shorts:
        return {"ok": False, "error": f"unknown MiniOS tool {name}"}
    short = n.split("__")[-1]
    if short in _BODY_TOOL_SHORTS and short != "teela_stop":
        turn = str(getattr(bot, "_teela_turn_intent", "") or "")
        if turn:
            from teela_cl.interpreter import is_performance_request as _is_act

            domain = str(getattr(assess_capability(turn), "domain", "") or "")
            mapped = bool(
                virtual_body.teela_args_from_intent(turn)
                or robot_sim.looks_like_motor(turn)
                or robot_sim.infer_command(turn)
            )
            if not mapped and (
                (not _is_act(turn) and not robot_sim.looks_like_motor(turn))
                or domain in {"talk", "software", "perception"}
            ):
                return {"ok": False, "error": "body tool is not authorized for this request"}
    return dispatch_teela_minios_tool(bot, n, args)


_BODY_TOOL_SHORTS = frozenset(
    {
        "teela_body_action",
        "teela_gesture",
        "teela_stop",
        "robot_pose",
        "robot_joint",
        "robot_motion",
    }
)


def assess_capability(request: str, state: Any = None, **kwargs: Any) -> Any:
    """Runtime check: do we already have a way to satisfy this outcome?"""
    from teela_cl.resolver import assess_capability as _assess

    return _assess(request, state, **kwargs)


def teela_needs_learn_attempt(text: str, state: Any = None, **kwargs: Any) -> bool:
    """Compose or learn — skill names are not decided in this gate."""
    from teela_cl.resolver import needs_learn_attempt as _needs

    return _needs(text, state, **kwargs)


def choose_turn_policy(text: str, **kwargs: Any) -> Any:
    """FAST-default deliberation. Kernel still owns e-stop and force-move."""
    from teela_cl.deliberation import choose_turn_policy as _choose

    return _choose(text, **kwargs)


def state_with_skill(skill_id: str) -> teela_cap.TeelaState:
    return teela_cap.teela_state([skill_id])


def state_with_skills(*skill_ids: str) -> teela_cap.TeelaState:
    return teela_cap.teela_state(skill_ids)


def state_without_required_skills() -> teela_cap.TeelaState:
    return teela_cap.teela_state([])


def _teela_skill_root(bot: Any) -> Path | None:
    ws = getattr(bot, "workspace", None)
    if not ws:
        return None
    return Path(str(ws)) / ".teela"


def _teela_attempt_store(bot: Any):
    from teela_cl.attempts import AttemptStore

    return AttemptStore(_teela_skill_root(bot))


def teela_record_attempt(bot: Any, intent: str, learned: Any = None) -> Any:
    """Causal memory for the action just taken. deskd records; it does not interpret feedback."""
    from teela_cl.records import _now

    plan = list(getattr(bot, "_teela_last_plan", None) or [])
    applied = list(getattr(bot, "_teela_applied_caps", []) or [])
    if not plan and applied:
        plan = [{"capability": c, "action": "apply", "goal": intent} for c in applied]
    used = list(getattr(bot, "_teela_tools_used", None) or [])
    shorts = {canonicalize_tool_name(n, None).split("__")[-1] for n in used if n}
    if not plan and not (shorts & _BODY_TOOL_SHORTS) and learned is None:
        return None
    assessment = assess_capability(intent)
    skill_id = None
    if getattr(learned, "skill", None) is not None:
        skill_id = learned.skill.skill_id
    elif assessment.matched_skills:
        skill_id = assessment.matched_skills[0]
    obs = _teela_observer(bot)
    ev = None
    if getattr(learned, "evaluation", None) is not None:
        ev = {
            "success": bool(learned.evaluation.success),
            "discrepancies": list(learned.evaluation.discrepancies or []),
            "unexpected_effects": list(learned.evaluation.unexpected_effects or []),
        }
    rec = _teela_attempt_store(bot).record(
        goal=assessment.goal or intent,
        request=intent,
        skill=skill_id,
        plan=plan,
        observations=obs,
        outcome={"applied": applied, "learned_success": bool(getattr(learned, "success", False))},
        evaluation=ev or {},
        awaiting_feedback=True,
    )
    rec.completed_at = _now()
    setattr(bot, "_teela_last_attempt", rec.attempt_id)
    return rec


def teela_handle_feedback_turn(bot: Any, text: str) -> str | None:
    """If this utterance is feedback on a recent attempt, run the correction loop."""
    from teela_cl.events import EventLog
    from teela_cl.feedback import handle_user_feedback
    from teela_cl.rsi import RSIPipeline
    from teela_cl.self_model import snapshot_self_model
    from teela_cl.skill_store import SkillStore

    attempts = _teela_attempt_store(bot)
    if attempts.latest() is None:
        return None
    root = _teela_skill_root(bot)
    skills = SkillStore(root, seed=True)
    rsi = RSIPipeline(root)
    model = snapshot_self_model(
        robot_state=getattr(bot, "robot_state", None) if isinstance(getattr(bot, "robot_state", None), dict) else None,
        learned_skill_ids=[s.skill_id for s in skills.all() if s.kind != "primitive"],
        permissions={"motors": False, "shell": False, "network": True, "workspace": True},
    )
    log = EventLog(sink=lambda ev: emit({"type": f"teela.{ev.get('type')}", **{k: v for k, v in ev.items() if k != "type"}}))
    result = handle_user_feedback(
        text,
        attempts,
        skills=skills,
        self_model=model,
        events=log,
        rsi=rsi,
        executor=lambda plan: _teela_plan_executor(bot, plan, text),
        observer=lambda: _teela_observer(bot),
    )
    if result.handled or _looks_like_embodied_correction(text):
        skill_id = None
        if getattr(result, "skill", None) is not None:
            skill_id = getattr(result.skill, "skill_id", None)
        if not skill_id:
            rec = attempts.latest()
            skill_id = getattr(rec, "skill", None) if rec is not None else None
        attach_embodied_user_correction(bot, text, skill_id=str(skill_id) if skill_id else None)
    if not result.handled:
        return None
    setattr(bot, "_teela_feedback_result", result)
    setattr(bot, "_teela_events", log.kinds())
    return result.spoken


def _teela_observer(bot: Any) -> dict[str, Any]:
    """World state only. Does not report applied plan steps or executor practice notes."""
    from teela_cl.outcome import is_practice_note

    st = getattr(bot, "robot_state", None)
    if not isinstance(st, dict):
        st = {}
    joints = st.get("joints") if isinstance(st.get("joints"), dict) else {}
    out: dict[str, Any] = {
        "pose": st.get("pose"),
        "motion": st.get("motion"),
        "joints": dict(joints),
        "trajectory_ok": getattr(bot, "_teela_trajectory_ok", None),
        "user_feedback": getattr(bot, "_teela_user_feedback", None),
    }
    perc = getattr(bot, "_teela_perception", None)
    if perc is not None:
        out["perception"] = perc
    art = getattr(bot, "_teela_artifact", None)
    if art and not is_practice_note(art):
        out["artifact"] = art
    return out


def _teela_outcome_assert(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    """Thin wrapper: process-only observations never count as the requested outcome."""
    from teela_cl.outcome import classify_evidence

    classified = classify_evidence("", before, after, None)
    if classified.outcome:
        return None
    return (classified.discrepancies or ["no evidence the intended outcome occurred"])[0]


def _apply_plan_capability(bot: Any, cap: str) -> dict[str, Any]:
    """Map a capability id onto a kernel tool. Routing by primitive family, not user actions."""
    c = str(cap or "").rsplit(".", 1)[-1]
    if c.startswith("software") or c.startswith("minios") or c == "research":
        return execute_teela_allowed_tool(bot, "web_search", {"query": c})
    if c in {"stand", "balance"}:
        return execute_teela_allowed_tool(bot, "bot_desktop__robot_pose", {"pose": "ready"})
    if c in {"step"}:
        return execute_teela_allowed_tool(
            bot, "bot_desktop__robot_motion", {"cmd": "walk", "direction": "place"}
        )
    if c in {"weight_shift"}:
        return execute_teela_allowed_tool(
            bot,
            "bot_desktop__robot_joint",
            {"joints": {"left_hip": 8, "right_hip": 4, "left_knee": 18, "right_knee": 10}},
        )
    if c in {"foot_slide", "heel_raise"}:
        side = "right" if c == "foot_slide" else "left"
        joints = {f"{side}_ankle": -18 if c == "foot_slide" else 12, f"{side}_hip": 10}
        return execute_teela_allowed_tool(bot, "bot_desktop__robot_joint", {"joints": joints})
    if c in {"raise_arm", "bend_elbow", "oscillate_wrist", "lower_arm"}:
        skill = "raise_arm" if c != "lower_arm" else "lower_arm"
        return execute_teela_allowed_tool(
            bot, "bot_desktop__teela_body_action", {"skill": skill, "side": "right"}
        )
    if c in {"hold_shoulder", "hold_elbow"}:
        joints = {"right_shoulder": 36} if c == "hold_shoulder" else {"right_elbow": 88}
        return execute_teela_allowed_tool(bot, "bot_desktop__robot_joint", {"joints": joints})
    return execute_teela_allowed_tool(bot, "bot_desktop__robot_pose", {"pose": "ready"})


def _teela_plan_executor(bot: Any, plan: list[dict[str, Any]], intent: str) -> dict[str, Any]:
    """Execute each composed step. Does not substitute a canned joint sequence."""
    applied: list[str] = []
    results: list[Any] = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        cap = str(step.get("capability") or "").strip()
        if not cap:
            continue
        results.append(_apply_plan_capability(bot, cap))
        applied.append(cap)
        noted = list(getattr(bot, "_teela_tools_used", None) or [])
        noted.append(cap)
        setattr(bot, "_teela_tools_used", noted)
    setattr(bot, "_teela_applied_caps", applied)
    setattr(bot, "_teela_last_plan", list(plan))
    out: dict[str, Any] = {"ok": True, "ran": True, "applied": applied, "results": results}
    ws = getattr(bot, "workspace", None)
    if ws:
        try:
            slug = re.sub(r"[^a-z0-9]+", "-", (intent or "goal").lower())[:40].strip("-") or "goal"
            rel = Path(str(ws)) / "Desktop"
            rel.mkdir(parents=True, exist_ok=True)
            note = rel / f"learn-{slug}.md"
            note.write_text(
                f"# Practice\n\nGoal: {intent}\n\nPlan:\n{json.dumps(plan, indent=2)[:4000]}\n",
                encoding="utf-8",
            )
            # Visible MiniOS note only. Not outcome evidence; do not set _teela_artifact.
            out["desktop_file"] = f"Desktop/{note.name}"
            emit({"type": "workspace", "bot_id": str(getattr(bot, "id", "") or "")})
        except OSError:
            pass
    return out


def teela_runtime_learn(bot: Any, intent: str) -> Any:
    """Generalized learn_goal on this bot. deskd supplies executor/observer only."""
    from teela_cl.events import EventLog
    from teela_cl.learning import LearnContext, learn_goal
    from teela_cl.memory_kinds import TypedMemory
    from teela_cl.self_model import snapshot_self_model
    from teela_cl.skill_store import SkillStore
    from teela_cl.rsi import RSIPipeline

    root = _teela_skill_root(bot)
    skills = SkillStore(root, seed=True)
    mem = TypedMemory(root)
    rsi = RSIPipeline(root)
    model = snapshot_self_model(
        robot_state=getattr(bot, "robot_state", None) if isinstance(getattr(bot, "robot_state", None), dict) else None,
        learned_skill_ids=[s.skill_id for s in skills.all() if s.kind != "primitive"],
        permissions={"motors": False, "shell": False, "network": True, "workspace": True},
    )
    log = EventLog(sink=lambda ev: emit({"type": f"teela.{ev.get('type')}", **{k: v for k, v in ev.items() if k != "type"}}))
    ctx = LearnContext(
        skills=skills,
        memory=mem,
        self_model=model,
        events=log,
        executor=lambda plan: _teela_plan_executor(bot, plan, intent),
        observer=lambda: _teela_observer(bot),
        acquire_knowledge=lambda g: execute_teela_allowed_tool(bot, "web_search", {"query": str(g)}),
        snapshot=rsi.active,
    )
    result = learn_goal(intent, ctx, request=intent)
    setattr(bot, "_teela_learn_result", result)
    setattr(bot, "_teela_events", log.kinds())
    save_body_lesson(
        bot,
        {
            "kind": "correction",
            "pose": "custom",
            "note": f"practiced {intent}: generalized learner decision={getattr(result, 'decision', '')} success={getattr(result, 'success', False)}",
            "tags": ["body", "practice"],
        },
    )
    return result


def teela_learn_move_on_desktop(bot: Any, intent: str) -> dict[str, Any]:
    """Visible practice: MiniOS Browser + Desktop note + body attempt + BODY.md lesson."""
    bid = str(getattr(bot, "id", "") or "")
    q = f"{intent} how to do this dance move with your body"
    url = search_url(q)
    emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "browser"})
    try:
        show = getattr(bot, "show_in_browser", None)
        if callable(show):
            show(url)
    except Exception as e:
        print(f"[deskd] learn-move browser: {e}", flush=True)
    search = _teela_web_search_tool(bot, {"query": q})
    hits = search.get("results") if isinstance(search, dict) else None
    if not isinstance(hits, list):
        hits = []
    slug = re.sub(r"[^a-z0-9]+", "-", (intent or "move").lower())[:40].strip("-") or "move"
    rel = f"Desktop/learn-{slug}.md"
    lines = [f"# Practice: {intent}", "", f"Search: {q}", f"URL: {url}", ""]
    for hit in hits[:5]:
        if not isinstance(hit, dict):
            continue
        lines.append(f"## {hit.get('title') or 'result'}")
        lines.append(str(hit.get("snippet") or ""))
        lines.append("")
    if len(lines) < 6:
        lines.append("(No snippets yet — still attempting with my body.)\n")
    ws = getattr(bot, "workspace", None)
    if ws:
        try:
            root = Path(str(ws))
            (root / "Desktop").mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
            emit({"type": "workspace", "bot_id": bid})
            emit(
                {
                    "type": "desktop.action",
                    "bot_id": bid,
                    "action": "open_file",
                    "path": rel,
                    "kind": "notepad",
                    "app": "notepad",
                }
            )
        except OSError as e:
            print(f"[deskd] learn-move desktop file: {e}", flush=True)
            rel = ""
    from teela_cl.learning import compose_plan

    assessment = assess_capability(intent)
    composed = compose_plan(intent, assessment.required_capabilities)
    motion = _teela_plan_executor(bot, composed, intent)
    snippet = ""
    if hits and isinstance(hits[0], dict):
        snippet = str(hits[0].get("snippet") or hits[0].get("title") or "")[:180]
    save_body_lesson(
        bot,
        {
            "kind": "correction",
            "pose": "custom",
            "note": f"practiced {intent}: {snippet or 'searched on MiniOS and attempted with joints'}",
            "tags": ["body", "practice"],
        },
    )
    noted = list(getattr(bot, "_teela_tools_used", None) or [])
    noted.extend(["web_search", "bot_desktop__robot_motion"])
    setattr(bot, "_teela_tools_used", noted)
    return {
        "ok": True,
        "query": q,
        "url": url,
        "desktop_file": rel,
        "search": search,
        "motion": motion,
        "snippet": snippet,
    }


def _walk_direction_from_intent(intent: str, hinted: Any = None) -> str:
    """Prefer the user's left/right/back over a missing or in-place default."""
    direction = str(hinted or "").strip().lower()
    if direction in {"left", "right", "back", "backward", "backwards", "forward", "forwards", "south", "place"}:
        return "back" if direction.startswith("back") else ("forward" if direction.startswith("forward") else direction)
    t = " ".join((intent or "").lower().split())
    if re.search(r"\b(?:back(?:wards?)?|away(?:\s+from me)?|north)\b", t):
        return "back"
    if re.search(r"\b(?:toward(?:s)? me|forward|forwards)\b", t):
        return "south"
    if re.search(r"\beast\b|\bleft\b", t):
        return "left"
    if re.search(r"\bwest\b|\bright\b", t):
        return "right"
    if re.search(r"\bin place\b", t):
        return "place"
    return direction or "place"


def _teela_compound_body_request(intent: str, hit: tuple[str, dict[str, Any]] | None = None) -> bool:
    """Two or more body acts in one utterance — Qwen should orchestrate, not a single canned move."""
    args = hit[1] if hit and isinstance(hit[1], dict) else {}
    if str(args.get("cmd") or "") == "plan" or (
        isinstance(args.get("steps"), list) and len(args.get("steps") or []) >= 2
    ):
        return True
    t = " ".join((intent or "").lower().split())
    if not t:
        return False
    if re.search(r"\b(?:and then|after that|afterwards)\b", t):
        return True
    if re.search(r"\b(?:stop|halt)\b.{0,48}\b(?:and|then)\b", t):
        return True
    acts = re.findall(r"\b(?:stop|halt|walk|wave|look|raise|sit|bow|point|nod)\b", t)
    return bool(re.search(r"\band\b", t) and len(set(acts)) >= 2)


def _teela_known_body_hit(bot: Any, intent: str) -> tuple[str, dict[str, Any]] | None:
    """Map a performance request onto a kernel tool. Observation/chat returns None."""
    from teela_cl.interpreter import is_performance_request
    from teela_cl.skill_store import SkillStore

    if not is_performance_request(intent):
        return None
    hit = virtual_body.teela_args_from_intent(intent)
    if hit is not None:
        return hit
    cmd = robot_sim.infer_command(intent, getattr(bot, "robot_state", None))
    if isinstance(cmd, dict) and str(cmd.get("cmd") or "") == "plan" and cmd.get("steps"):
        args: dict[str, Any] = {"cmd": "plan", "steps": list(cmd.get("steps") or [])}
        if cmd.get("why"):
            args["why"] = cmd.get("why")
        return ("bot_desktop__robot_motion", args)
    if isinstance(cmd, dict) and str(cmd.get("cmd") or "") == "pose" and cmd.get("pose"):
        return ("bot_desktop__robot_pose", {"pose": str(cmd.get("pose"))})
    if isinstance(cmd, dict) and str(cmd.get("cmd") or "") in {"walk", "start_walk"}:
        return (
            "bot_desktop__robot_motion",
            {"cmd": "walk", "direction": _walk_direction_from_intent(intent, cmd.get("direction"))},
        )
    if isinstance(cmd, dict) and str(cmd.get("cmd") or "") == "stop":
        return ("bot_desktop__teela_stop", {"skill": "stop"})
    assessment = assess_capability(intent)
    if assessment.decision != "execute" or not assessment.matched_skills:
        return None
    skills = SkillStore(_teela_skill_root(bot), seed=True)
    sk = next((s for s in skills.all() if s.skill_id == assessment.matched_skills[0]), None)
    if sk is None or not sk.tool:
        return None
    args = dict(sk.tool_args or {})
    if str(args.get("cmd") or "") in {"walk", "start_walk"}:
        args["direction"] = _walk_direction_from_intent(intent, args.get("direction"))
    return (str(sk.tool), args)


def teela_ensure_known_body(bot: Any, intent: str, used: list[str]) -> dict[str, Any] | None:
    """Known body request with no tool this turn: do it. Leftover pose=wave is not a wave."""
    shorts = {canonicalize_tool_name(n, None).split("__")[-1] for n in used if n}
    if shorts & _BODY_TOOL_SHORTS:
        return None
    hit = _teela_known_body_hit(bot, intent)
    if not hit:
        return None
    name, args = hit
    result = execute_teela_allowed_tool(bot, name, args)
    noted = list(getattr(bot, "_teela_tools_used", None) or [])
    noted.append(canonicalize_tool_name(name, None))
    setattr(bot, "_teela_tools_used", noted)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def teela_ensure_perform_attempt(bot: Any, intent: str, used: list[str]) -> dict[str, Any] | None:
    """If she was asked to try a move and never called a body tool, deskd still moves her."""
    if not teela_needs_learn_attempt(intent):
        return None
    shorts = {canonicalize_tool_name(n, None).split("__")[-1] for n in used if n}
    if shorts & _BODY_TOOL_SHORTS:
        return None
    assessment = assess_capability(intent)
    if str(getattr(assessment, "domain", "") or "") != "embodied":
        return None
    noted = list(getattr(bot, "_teela_tools_used", None) or [])
    if "web_search" not in shorts:
        try:
            execute_teela_allowed_tool(
                bot,
                "web_search",
                {"query": f"{intent} dance move how the body looks"},
            )
            noted.append("web_search")
        except Exception:
            pass
    from teela_cl.learning import compose_plan

    caps = list(assessment.required_capabilities)
    if not caps:
        caps = ["stand", "step"]
    plan = compose_plan(intent, caps)
    if not plan:
        return None
    result = _teela_plan_executor(bot, plan, intent)
    setattr(bot, "_teela_tools_used", noted + list(getattr(bot, "_teela_tools_used", []) or []))
    return result


def teela_ensure_computer(bot: Any, intent: str, used: list[str]) -> dict[str, Any] | None:
    """Known computer/system request with no tool this turn: run Hermes Agent or system-check."""
    import teela_agent_bridge as _gb

    shorts = {canonicalize_tool_name(n, None).split("__")[-1] for n in used if n}
    if shorts & {"hermes_build", "teela_system_check"}:
        return None
    assessment = assess_capability(intent)
    domain = str(getattr(assessment, "domain", "") or "")
    cmd = _gb.extract_command(intent)
    wants_build = bool(re.search(r"\bhermes[\s-]?build\b", intent or "", re.I))
    wants_info = bool(re.search(r"\bsystem\s+info(?:rmation)?\b", intent or "", re.I))
    if not cmd and not wants_build and not wants_info and not looks_like_system_check(intent):
        return None
    if cmd or wants_build:
        name, args = "hermes_build", {"task": intent, "command": cmd or ""}
    else:
        name, args = "bot_desktop__teela_system_check", {"scope": "host"}
    result = execute_teela_allowed_tool(bot, name, args)
    noted = list(getattr(bot, "_teela_tools_used", None) or [])
    noted.append(canonicalize_tool_name(name, None))
    setattr(bot, "_teela_tools_used", noted)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def _teela_computer_speech(result: dict[str, Any] | None, intent: str) -> str:
    if not isinstance(result, dict):
        return "I could not complete that command."
    out = str(result.get("output") or "").strip()
    if out:
        return out.splitlines()[0][:400]
    if result.get("ok"):
        issues = result.get("issues")
        if issues:
            return f"System check finished with issues: {issues}"[:400]
        return "System check finished."
    err = str(result.get("error") or "").strip()
    return (err or "I could not complete that command.")[:400]


def teela_capability_hint(text: str) -> str:
    """Optional regex hint for assembled context. Not a gate — does not hide tools."""
    t = visible_user_text(user_intent_text(text or ""))
    bits: list[str] = []
    if robot_sim.looks_like_motor(t) or virtual_body.teela_args_from_intent(t):
        bits.append("body gesture/motion")
    if looks_like_body_look(t) or robot_sim.looks_like_body_query(t):
        bits.append("visual/body observation")
    if looks_like_desktop_work(t) or re.search(
        r"\b(?:search(?:\s+the)?\s+(?:web|online)|look(?:\s+it)?\s+up|google)\b", t, re.I
    ):
        bits.append("desktop/computer")
    if re.search(r"\b(?:try(?:ing)? to|do the|perform(?: the)?)\b", t, re.I) and not (
        robot_sim.looks_like_motor(t) or virtual_body.teela_args_from_intent(t)
    ):
        bits.append("web_search then approximate with joints/poses")
    if looks_like_system_check(t):
        bits.append("system_check")
    if looks_like_teammate_work(t) or looks_like_helper_bot_work(t):
        bits.append("teammate")
    if not bits:
        return ""
    return "Likely relevant capability (hint, not a requirement): " + ", ".join(bits) + "."


def teela_safety_intercept(text: str) -> dict[str, Any] | None:
    """Hard stop: e-stop / stop-moving. deskd applies this before any model call."""
    raw = visible_user_text(user_intent_text(text or ""))
    t = " ".join(raw.lower().split())
    if not t:
        return None
    if re.search(r"\bestop off\b|\brelease e-?stop\b", t):
        return None
    if re.search(r"\bemergency\s+stop\b|\be-?stop\b", t):
        return {
            "name": "bot_desktop__robot_motion",
            "args": {"cmd": "estop_on"},
            "spoken": "E-stop is on. I've stopped.",
        }
    if re.search(r"\b(?:and|then)\b.{0,32}\b(?:walk|raise|bow|sit|wave)\b", t):
        return None
    if (
        re.search(r"\bstop moving\b|\bhalt\b|\bfreeze\b|\bhold still\b", t)
        or re.search(r"\b(?:can|could|would|will)\s+you\s+stop\b", t)
        or re.search(r"\bno[, ]+stop\b", t)
        or re.search(r"\bplease\s+stop\b", t)
        or re.fullmatch(r"(?:please\s+)?(?:stop(?: it| that| moving| waving)?)[.?!\s]*", t)
    ):
        return {
            "name": "bot_desktop__teela_stop",
            "args": {"skill": "stop"},
            "spoken": "Stopped.",
        }
    return None


def teela_effective_mode(tool_names: list[str]) -> str:
    """Telemetry for logging. Not a cognition lane."""
    shorts = {canonicalize_tool_name(n, None).split("__")[-1] for n in tool_names if n}
    if "estop_on" in str(tool_names) or shorts & {"teela_stop"}:
        return "safety"
    perc = {
        "desktop_observe",
        "desktop_state",
        "desktop_screenshot",
        "teela_get_body_state",
        "robot_status",
    }
    body = {
        "teela_body_action",
        "teela_gesture",
        "teela_stop",
        "robot_pose",
        "robot_joint",
        "robot_motion",
    }
    computer = {
        "desktop_open_file",
        "desktop_type_text",
        "desktop_browser_navigate",
        "desktop_open_app",
        "desktop_click",
        "read_file",
        "list_dir",
        "web_search",
    }
    has_p, has_b, has_c = bool(shorts & perc), bool(shorts & body), bool(shorts & computer)
    if (has_p or has_c) and has_b:
        return "mixed"
    if has_c:
        return "workspace"
    if has_b:
        return "body"
    if has_p:
        return "perception"
    return "talk"


def ensure_teela_capability_tools(payload: dict[str, Any]) -> dict[str, Any]:
    """Union the payload tool list with the full Teela capability registry."""
    specs = teela_capability_tool_specs()
    existing: dict[str, dict[str, Any]] = {}
    for t in payload.get("tools") or []:
        if not isinstance(t, dict):
            continue
        n = canonicalize_tool_name(_openai_tool_name(t), None)
        if n:
            existing[n] = t
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for spec in specs:
        n = spec["function"]["name"]
        tools.append(existing.get(n) or json.loads(json.dumps(spec)))
        names.add(n)
    payload["tools"] = tools
    payload.pop("tool_choice", None)
    return payload


def assemble_teela_executive_payload(
    bot: Any,
    user_text: str,
    *,
    images: list | None = None,
    policy: Any = None,
) -> dict[str, Any]:
    """One context for every Teela turn: I-feel, memory, environment, all capabilities."""
    feel = ""
    try:
        import cognitive_profile as _cprof

        embodied = _cprof.has_cap(bot, "body_state")
    except Exception:
        embodied = True
    if embodied:
        st = virtual_body.overlay(
            str(getattr(bot, "id", "") or ""),
            getattr(bot, "robot_state", None),
        )
        feel = proprioception_block(st, kind="live")
        try:
            import body_state as _bs

            feel = feel + "\n" + _bs.compact_block(teela_body_snapshot(bot))
        except Exception:
            pass
    env = {
        "surface": getattr(bot, "surface", "") or "",
        "cursor": getattr(bot, "desktop_cursor", {}) or {},
        "workspace": str(getattr(bot, "workspace", "") or ""),
    }
    hint = teela_capability_hint(user_text)
    voice_bit = CHATTERBOX_VOICE_NOTE if voice_enabled() else ""
    chat_bit = VOICE_CHAT_NOTE if voice_chat_active(bot) else ""
    look = ""
    try:
        notes = load_body_notes(bot)
        if notes:
            look = f"\nHow you look (BODY.md):\n{notes}"
    except Exception:
        look = ""
    sys = (
        f"{_TEELA_EXEC_SYS}\n{chat_bit}{voice_bit}\n{feel}\n"
        f"{teela_now_block(bot)}\n"
        f"Environment: {json.dumps(env, default=str)[:800]}{look}"
    )
    if hint:
        sys += f"\n{hint}"
    if policy is not None:
        sys += (
            f"\nDeliberation: {getattr(policy, 'reasoning_mode', 'fast')} thinking, "
            f"{getattr(policy, 'response_mode', 'natural')} speech, "
            f"{getattr(policy, 'response_budget', '0-2 sentences')}. "
            "Do not say those labels out loud."
        )
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
    try:
        cm.before_assemble(bot, user_text)
        chat_limit = cm.conversation_turn_limit(bot)
    except Exception:
        chat_limit = 8
    for m in recent_chat_messages(bot, limit=chat_limit):
        messages.append({"role": m["role"], "content": m["content"]})
    user = (user_text or "").strip()
    if images:
        user += "\n[attached images are available for this turn]"
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": LOCAL_LLM_SERVED,
        "temperature": 0.4,
        "max_tokens": LOCAL_LLM_MOTOR_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "stop": ["</think>", "<think>"],
        "messages": messages,
        "tools": teela_capability_tool_specs(),
    }
    skip_mem = bool(
        policy is not None
        and getattr(policy, "reasoning_mode", "") == "fast"
        and not getattr(policy, "needs_memory", False)
    )
    mgr = getattr(bot, "memory", None)
    assembled = None
    if mgr is not None and not skip_mem:
        try:
            assembled = cm.assemble_for_bot(
                bot,
                payload.get("messages") or [],
                user_text,
            )
            payload = botmem.inject_assembled_messages(payload, assembled)
        except Exception as e:
            print(f"[deskd] executive memory inject failed: {e}", flush=True)
    try:
        payload = cm.inject_checkpoint_message(payload, bot)
    except Exception:
        pass
    try:
        wm.capture_turn(bot, payload, assembled)
    except Exception as e:
        print(f"[deskd] working-memory capture failed: {e}", flush=True)
    return payload


def run_teela_executive_turn(
    bot: Any,
    user_text: str,
    *,
    completer: Any | None = None,
    images: list | None = None,
) -> str | None:
    """Qwen is the executive. deskd is the kernel. No talk/minios/acp lane gate."""
    intent = user_intent_text(visible_user_text(user_text or ""))
    setattr(bot, "_teela_turn_intent", intent)
    setattr(bot, "_teela_tools_used", [])
    setattr(bot, "_teela_applied_caps", [])
    setattr(bot, "_teela_last_plan", [])
    hit = teela_safety_intercept(intent or user_text)
    if hit:
        execute_teela_allowed_tool(bot, hit["name"], hit["args"])
        if str(hit["name"]).endswith("teela_stop"):
            try:
                dispatch_teela_minios_tool(bot, "bot_desktop__robot_motion", {"cmd": "stop"})
            except Exception:
                pass
        used = [str(hit["name"])]
        setattr(bot, "_teela_tools_used", used)
        setattr(bot, "_teela_effective_mode", teela_effective_mode(used))
        spoken = str(hit.get("spoken") or "Stopped.")
        try:
            bot.record_local_generation(spoken, started_ms=time.time() * 1000.0)
        except Exception:
            pass
        return spoken
    from teela_cl.deliberation import (
        apply_response_budget,
        choose_turn_policy as _choose_policy,
        load_momentum,
        save_momentum,
        strip_internal_labels,
    )
    from teela_cl.skill_store import SkillStore as _SkillStore

    root = _teela_skill_root(bot)
    attempts = _teela_attempt_store(bot)
    awaiting = attempts.latest(awaiting=True)
    momentum = load_momentum(root)
    from teela_cl.interpreter import is_performance_request as _is_act
    from teela_cl.interpreter import is_repeat_request as _is_repeat
    skills = _SkillStore(root, seed=True)
    policy = _choose_policy(
        intent or user_text,
        features=getattr(bot, "_teela_turn_features", None),
        awaiting_attempt=awaiting,
        skills=skills.all(),
        momentum=momentum,
        interpreter=getattr(bot, "_teela_policy_interpreter", None),
    )
    setattr(bot, "_teela_turn_policy", policy)
    q = (intent or "").strip()
    ql = q.lower()
    leave_training = q.endswith("?") and not ql.startswith(("can you", "could you", "will you", "try"))
    if leave_training:
        rec = attempts.latest(awaiting=True)
        if rec is not None:
            rec.awaiting_feedback = False
            attempts.update(rec)
            awaiting = None
        momentum.conversation_momentum = "talk"
        momentum.user_feedback_expected = False
        momentum.active_context = ""
        save_momentum(root, momentum)
    if _is_repeat(intent or user_text) and (momentum.goal or momentum.last_attempt_id):
        prior = str(momentum.goal or "").strip()
        if not prior:
            rec = attempts.latest()
            prior = str(getattr(rec, "request", None) or getattr(rec, "goal", None) or "")
        if prior:
            intent = prior
            setattr(bot, "_teela_turn_intent", intent)
            awaiting = None
    elif not _is_repeat(intent or user_text) and not leave_training:
        corrected = teela_handle_feedback_turn(bot, intent or user_text)
        if corrected is not None:
            setattr(bot, "_teela_effective_mode", teela_effective_mode(list(getattr(bot, "_teela_tools_used", None) or [])))
            momentum.conversation_momentum = "physical_training"
            momentum.user_feedback_expected = True
            momentum.last_attempt_id = getattr(awaiting, "attempt_id", None) or momentum.last_attempt_id
            save_momentum(root, momentum)
            spoken = apply_response_budget(strip_internal_labels(corrected), policy) or corrected
            try:
                bot.record_local_generation(spoken, started_ms=time.time() * 1000.0)
            except Exception:
                pass
            return spoken
        if policy.reasoning_mode == "fast" and not policy.needs_tools and not awaiting and not leave_training:
            momentum.conversation_momentum = "talk"
            momentum.user_feedback_expected = False
            save_momentum(root, momentum)
    assessment = assess_capability(intent or user_text)
    setattr(bot, "_teela_assessment", assessment)

    want_act = _is_act(intent or user_text)
    known_hit = _teela_known_body_hit(bot, intent or user_text) if want_act else None
    mixed_mode = orch.classify(intent or user_text).get("mode") if (intent or user_text) else "none"
    if (
        known_hit
        and mixed_mode not in {"parallel", "after"}
        and not looks_like_desktop_work(intent or user_text)
        and not _teela_compound_body_request(intent or user_text, known_hit)
    ):
        name, args = known_hit
        execute_teela_allowed_tool(bot, name, args)
        used = [canonicalize_tool_name(name, None)]
        setattr(bot, "_teela_tools_used", used)
        setattr(bot, "_teela_effective_mode", teela_effective_mode(used))
        cap = str(args.get("skill") or args.get("gesture") or args.get("cmd") or "").strip()
        if cap:
            setattr(bot, "_teela_applied_caps", [cap])
            setattr(
                bot,
                "_teela_last_plan",
                [{"capability": cap, "action": "apply", "goal": intent or user_text}],
            )
        rec = teela_record_attempt(bot, intent or user_text, None)
        if rec is not None:
            momentum.active_context = "physical_training"
            momentum.goal = intent or user_text
            momentum.last_attempt_id = rec.attempt_id
            momentum.user_feedback_expected = True
            momentum.conversation_momentum = "physical_training"
            save_momentum(root, momentum)
        st = virtual_body.overlay(str(getattr(bot, "id", "") or ""), getattr(bot, "robot_state", None))
        motor = dict(args)
        skill = str(args.get("skill") or args.get("gesture") or "").lower()
        if skill in {"wave", "greeting"}:
            motor.setdefault("cmd", "pose")
            motor.setdefault("pose", "wave")
        spoken = robot_sim.confirm_move(st, motor, intent or user_text) or "Done."
        spoken = apply_response_budget(strip_internal_labels(spoken), policy) or spoken
        print(f"[deskd] motor direct skip llama {name} {args}", flush=True)
        try:
            bot.record_local_generation(spoken, started_ms=time.time() * 1000.0)
        except Exception:
            pass
        return spoken
    # Unmapped turns go to Qwen. Kernel does not invent stand/step.
    payload = assemble_teela_executive_payload(bot, intent or user_text, images=images, policy=policy)
    nrounds = 1 if policy.reasoning_mode == "fast" else (3 if policy.reasoning_mode == "normal" else _TEELA_EXEC_ROUNDS)
    line = _teela_minios_continue(
        bot,
        payload,
        intent=intent or user_text,
        acted=False,
        completer=completer,
        rounds=nrounds,
    )
    used = list(getattr(bot, "_teela_tools_used", None) or [])
    known_move = None
    if want_act:
        known_move = teela_ensure_known_body(bot, intent or user_text, used)
        used = list(getattr(bot, "_teela_tools_used", None) or [])
    computer = None
    if known_move is None:
        computer = teela_ensure_computer(bot, intent or user_text, used)
        used = list(getattr(bot, "_teela_tools_used", None) or [])
    setattr(bot, "_teela_effective_mode", teela_effective_mode(used))
    body_used = bool(
        {canonicalize_tool_name(n, None).split("__")[-1] for n in used} & _BODY_TOOL_SHORTS
        or getattr(bot, "_teela_applied_caps", None)
    )
    if body_used:
        rec = teela_record_attempt(bot, intent or user_text, None)
        if rec is not None:
            momentum.active_context = "physical_training"
            momentum.goal = intent or user_text
            momentum.last_attempt_id = rec.attempt_id
            momentum.user_feedback_expected = True
            momentum.conversation_momentum = "physical_training"
            save_momentum(root, momentum)
    if known_move is not None:
        st = virtual_body.overlay(str(getattr(bot, "id", "") or ""), getattr(bot, "robot_state", None))
        hit = _teela_known_body_hit(bot, intent or user_text)
        motor = dict(hit[1]) if hit else {}
        skill = str(motor.get("skill") or motor.get("gesture") or "").lower()
        if skill in {"wave", "greeting"}:
            motor.setdefault("cmd", "pose")
            motor.setdefault("pose", "wave")
        spoken = robot_sim.confirm_move(st, motor, intent or user_text) or "Done."
        spoken = apply_response_budget(strip_internal_labels(spoken), policy) or spoken
        # Qwen already answered this turn — keep that speech; kernel only filled the motion.
        if line:
            return filter_unwarranted_laughs(strip_model_think_tags(line), intent or user_text)
        try:
            bot.record_local_generation(spoken, started_ms=time.time() * 1000.0)
        except Exception:
            pass
        return spoken
    if computer is not None:
        spoken = apply_response_budget(
            strip_internal_labels(_teela_computer_speech(computer, intent or user_text)),
            policy,
        ) or _teela_computer_speech(computer, intent or user_text)
        try:
            bot.record_local_generation(spoken, started_ms=time.time() * 1000.0)
        except Exception:
            pass
        return spoken
    line = apply_response_budget(strip_internal_labels(line), policy)
    return filter_unwarranted_laughs(strip_model_think_tags(line or ""), intent or user_text)


def _teela_minios_complete(payload: dict[str, Any]) -> dict[str, Any] | None:
    live_map = probe_local_llm_map()
    if not live_map:
        # Port is up but /v1/models probe failed — still try the configured brain.
        if not local_llm_port_open(LOCAL_LLM_UPSTREAM):
            return None
        think_id = LOCAL_LLM_SERVED
        url = LOCAL_LLM_UPSTREAM + "/v1/chat/completions"
    else:
        think_id = next((x for x in live_map if not _is_fast_llm_id(x)), next(iter(live_map)))
        url = (live_map.get(think_id) or LOCAL_LLM_UPSTREAM) + "/v1/chat/completions"
    payload = dict(payload)
    payload["model"] = think_id
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers=_llm_headers(url, {"Content-Type": "application/json", "Connection": "close"}),
    )
    try:
        with hybrid_exclusive("think"):
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode() or "{}")
    except Exception as e:
        print(f"[deskd] MiniOS loop llama failed: {e}", flush=True)
        return None


def run_teela_minios_turn(
    bot: Any,
    user_text: str,
    *,
    completer: Any | None = None,
    images: list | None = None,
) -> str | None:
    """Back-compat name for the Teela executive loop (capabilities, not lanes)."""
    return run_teela_executive_turn(bot, user_text, completer=completer, images=images)


def _dispatch_inferred_body(bot: Any, intent: str) -> str | None:
    """Send the HTML body tool for this chat line. Wave always restarts."""
    seed = {"messages": [{"role": "user", "content": intent}]}
    if not local_llm_motor_turn(seed):
        return None
    cmd = robot_sim.infer_command(intent, getattr(bot, "robot_state", None))
    hit = virtual_body.teela_args_from_motor(cmd, intent or "")
    if not hit:
        return None
    name, args = hit
    try:
        dispatch_teela_minios_tool(bot, name, args)
    except Exception as e:
        print(f"[deskd] MiniOS motor direct failed: {e}", flush=True)
        return None
    spoken = speech_after_motor(
        {
            "messages": [
                {"role": "user", "content": intent},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": name, "arguments": json.dumps(args)}}
                    ],
                },
            ]
        }
    )
    print(f"[deskd] MiniOS motor direct (skip llama) {name} {args}", flush=True)
    line = ground_chat_speech(
        spoken,
        intent,
        getattr(bot, "robot_state", None),
        acted=True,
    )
    try:
        bot.record_local_generation(line or spoken, started_ms=time.time() * 1000.0)
    except Exception:
        pass
    return line


def _clarify_options(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for i, item in enumerate(raw):
        if isinstance(item, str) and item.strip():
            out.append({"id": f"opt{i+1}", "label": item.strip()[:80]})
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("text") or item.get("id") or "").strip()
            if not label:
                continue
            oid = str(item.get("id") or f"opt{i+1}").strip()[:40] or f"opt{i+1}"
            out.append({"id": oid, "label": label[:80]})
        if len(out) >= 6:
            break
    return rank_clarify_options(out)


def rank_clarify_options(
    options: list[dict[str, str]],
    *,
    guess: str = "",
) -> list[dict[str, str]]:
    """Recommended first, then the rest, Other last for a free-text meaning."""
    opts = [o for o in options if isinstance(o, dict) and o.get("id")]
    def is_other(o: dict[str, str]) -> bool:
        oid = str(o.get("id") or "").lower()
        lab = str(o.get("label") or "").lower()
        return oid in {"other", "something_else", "something else"} or lab.startswith("something else")

    other = [o for o in opts if is_other(o)]
    rest = [o for o in opts if not is_other(o)]
    if guess:
        top = [o for o in rest if str(o.get("id")) == guess]
        rest = [o for o in rest if str(o.get("id")) != guess]
        rest = top + rest
    if not other:
        other = [
            {
                "id": "other",
                "label": "Something else",
                "hint": "what you are referring to",
            }
        ]
    return rest + other[:1]


def resume_teela_minios_after_clarify(bot: Any, choice: str, *, cancel: bool = False) -> str | None:
    hold = getattr(bot, "_minios_hold", None)
    bot._minios_hold = None
    if not isinstance(hold, dict):
        return None
    if cancel:
        return "Okay — I won't do that."
    payload = hold.get("payload")
    if not isinstance(payload, dict):
        return None
    payload.setdefault("messages", []).append(
        {
            "role": "tool",
            "tool_call_id": str(hold.get("call_id") or ""),
            "content": json.dumps({"ok": True, "choice": choice}, default=str)[:4000],
        }
    )
    # Continue the MiniOS loop by calling the rest of run_teela_minios_turn's round logic.
    return _teela_minios_continue(
        bot,
        payload,
        intent=str(hold.get("intent") or ""),
        acted=bool(hold.get("acted")),
    )


def _teela_minios_continue(
    bot: Any,
    payload: dict[str, Any],
    *,
    intent: str,
    acted: bool,
    completer: Any | None = None,
    rounds: int | None = None,
) -> str | None:
    last_line = ""
    complete = completer or getattr(bot, "_teela_completer", None) or _teela_minios_complete
    nrounds = int(rounds or _TEELA_EXEC_ROUNDS)
    used = list(getattr(bot, "_teela_tools_used", None) or [])
    # Tool rounds and the final answer have separate budgets. Even FAST
    # requests need one completion after a tool result to report its evidence.
    for _round in range(nrounds + 1):
        if _round == nrounds:
            payload["tool_choice"] = "none"
        t0 = time.perf_counter()
        started_ms = time.time() * 1000.0
        data = complete(payload)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if isinstance(data, dict):
            bot._llama_started_ms = started_ms
            apply_llama_generation_speed(bot, data, elapsed_ms)
            usage = data.get("usage")
            if isinstance(usage, dict) and usage:
                bot._llama_usage = usage
            ingest = getattr(bot, "ingest_usage", None)
            if isinstance(usage, dict) and usage and callable(ingest):
                try:
                    ingest(usage)
                except Exception:
                    pass
        if not data:
            setattr(bot, "_teela_tools_used", used)
            return last_line or None
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
        content = str(msg.get("content") or msg.get("reasoning_content") or "")
        if calls and _round == nrounds:
            setattr(bot, "_teela_tools_used", used)
            return "I reached the tool-call limit before completing the answer."
        if calls:
            payload["messages"].append(
                {"role": "assistant", "content": content or None, "tool_calls": calls}
            )
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, dict):
                    args = raw_args
                else:
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                name = str(fn.get("name") or call.get("name") or "")
                short = canonicalize_tool_name(name, None).split("__")[-1]
                if short == "ask_user":
                    opts = _clarify_options(args.get("options"))
                    question = str(args.get("question") or "").strip()
                    if question and len(opts) >= 2:
                        cid = uuid.uuid4().hex[:12]
                        bot._minios_hold = {
                            "id": cid,
                            "payload": payload,
                            "call_id": str(call.get("id") or ""),
                            "acted": acted,
                            "intent": intent,
                        }
                        ranked = rank_clarify_options(opts, guess=str((opts[0] or {}).get("id") or "") if opts else "")
                        ev = {
                            "type": "clarify",
                            "bot_id": str(getattr(bot, "id", "") or ""),
                            "clarify_id": cid,
                            "question": question[:240],
                            "guess": ranked[0]["id"] if ranked else "",
                            "options": ranked,
                        }
                        emit(ev)
                        raise MiniOSClarify(ev)
                    result = {"ok": False, "error": "ask_user needs a question and at least two options"}
                else:
                    try:
                        result = execute_teela_allowed_tool(bot, name, args)
                    except Exception as e:
                        result = {"ok": False, "error": str(e)}
                    used.append(canonicalize_tool_name(name, None))
                acted = True
                payload["messages"].append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": json.dumps(result, default=str)[:4000],
                    }
                )
            continue
        line = strip_model_think_tags(content.replace("\r", "\n").strip())
        if line.startswith("```"):
            line = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", line).strip()
            line = re.sub(r"\n?```$", "", line).strip()
        if line:
            setattr(bot, "_teela_tools_used", used)
            return ground_chat_speech(line, intent, getattr(bot, "robot_state", None), acted=acted)
        break
    setattr(bot, "_teela_tools_used", used)
    if acted:
        return ground_chat_speech(
            robot_sim.confirm_move(getattr(bot, "robot_state", None), None, intent) or "Done.",
            intent,
            getattr(bot, "robot_state", None),
            acted=True,
        )
    return None


def ensure_desktop_tools(payload: dict[str, Any]) -> dict[str, Any]:
    """Guarantee Teela sees MiniOS browser/desktop tools and must call one."""
    payload = keep_teela_workspace_tools(payload)
    existing: dict[str, dict[str, Any]] = {}
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        n = canonicalize_tool_name(_openai_tool_name(tool), None)
        if n:
            existing[n] = tool
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for spec in _DESKTOP_TOOL_DEFS:
        n = spec["function"]["name"]
        tools.append(existing.get(n) or json.loads(json.dumps(spec)))
        names.add(n)
    for t in payload.get("tools") or []:
        n = canonicalize_tool_name(_openai_tool_name(t), None)
        if n and n not in names:
            tools.append(t)
            names.add(n)
    payload["tools"] = tools
    payload["tool_choice"] = "required"
    return payload


_COMPOSE_RE = re.compile(
    r"\b(?:"
    r"recipes?|story|letter|email|essay|poem|list|notes?|summary|plan|article|"
    r"report|instructions?|how to|draft|generate|"
    r"research|look(?:\s+it)?\s+up|search\s+online|find(?:\s+me)?|"
    r"write\s+(?:me\s+)?(?:a|an|the)\b"
    r")\b",
    re.I,
)
_RESEARCH_RE = re.compile(
    r"\b(?:research|look(?:\s+it)?\s+up|search(?:\s+the\s+web)?(?:\s+for)?|google|online|find(?:\s+me)?)\b",
    re.I,
)


def looks_like_composed_type(intent: str) -> bool:
    """True when they want a document written, not a dictated phrase like Hello."""
    t = " ".join((intent or "").lower().split())
    if not t:
        return False
    if not re.search(r"\b(?:type|write|put|paste)\b", t) and not (
        _RESEARCH_RE.search(t) and re.search(r"\bnotepad\b", t)
    ):
        return False
    if _COMPOSE_RE.search(t):
        return True
    if re.search(r"\b(?:type|write)\s+(?:it|that|this|something)\b", t):
        return True
    if re.search(r"\b(?:type|write)\s+(?:a|an|the)\s+\w+", t):
        return True
    return False


def compose_topic(intent: str) -> str:
    t = " ".join((intent or "").split())
    t = re.sub(r"^(?:can you |could you |please )+", "", t, flags=re.I)
    t = re.sub(
        r"\b(?:research(?:\s+online)?|look(?:\s+it)?\s+up|search(?:\s+the\s+web)?(?:\s+for)?|google|find(?:\s+me)?)\s+",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s+and\s+(?:then\s+)?(?:type|write|put|paste)\s+it\b.*$", "", t, flags=re.I)
    t = re.sub(
        r"\s+(?:and\s+)?(?:type|write)\s+(?:it\s+)?(?:in(?:to)?\s+)?(?:the\s+|your\s+)?notepad.*$",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^(?:type|write)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+in(?:to)?\s+(?:the\s+|your\s+)?notepad.*$", "", t, flags=re.I)
    return t.strip(" .")


def infer_desktop_tool(intent: str) -> tuple[str, dict[str, Any]] | None:
    """Map 'open the browser and google birds' onto a MiniOS desktop tool call."""
    t = " ".join((intent or "").lower().split())
    if not t:
        return None
    if looks_like_composed_type(intent):
        return None
    q = ""
    m = re.search(r"\bgoogle\s+(.+)$", t)
    if m:
        q = m.group(1)
    else:
        m = re.search(
            r"\b(?:search(?:\s+the\s+web)?(?:\s+for)?|look(?:\s+it)?\s+up)\s+(.+)$",
            t,
        )
        if m:
            q = m.group(1)
    q = re.sub(r"^(?:and\s+)?", "", (q or "").strip(" .!?\""))
    if q:
        return "bot_desktop__desktop_browser_navigate", {"url": search_url(q)}
    m = re.search(r"https?://[^\s]+", intent or "")
    if m:
        return "bot_desktop__desktop_browser_navigate", {"url": m.group(0).rstrip(".,)")}
    typed = ""
    raw = intent or ""
    qm = re.search(r"(?:type|write)\s+[\"'“](.+?)[\"'”]", raw, re.I)
    if qm:
        typed = qm.group(1).strip()
    else:
        qm = re.search(r"\b(?:type|write)\s+(?!of\b)(.+)$", raw, re.I)
        if qm:
            typed = re.sub(
                r"\s+(?:in(?:to)?|on)\s+(?:the\s+|your\s+)?(?:notepad|browser|search(?:\s+box)?)$",
                "",
                qm.group(1).strip(" .!?"),
                flags=re.I,
            )
    if typed:
        args: dict[str, Any] = {"text": typed}
        if "notepad" in t:
            args["app"] = "notepad"
        elif "browser" in t or "search" in t:
            args["app"] = "browser"
        return "bot_desktop__desktop_type_text", args
    m = re.search(
        r"\b(?:open|show|review)\s+(?:the\s+|my\s+|this\s+)?([^\s]+?\.(?:png|jpe?g|gif|webp|mp4|webm|mov|m4v))\b",
        intent or "",
        re.I,
    )
    if m:
        return "bot_desktop__desktop_open_file", {"path": m.group(1)}
    if re.search(r"open.{0,32}browser", t):
        return "bot_desktop__desktop_open_app", {"app_id": "app_browser"}
    if re.search(r"open.{0,40}(?:files|workspace|pictures|photos|videos)", t):
        return "bot_desktop__desktop_open_app", {"app_id": "app_files"}
    if re.search(r"open.{0,32}notepad", t):
        return "bot_desktop__desktop_open_app", {"app_id": "app_notepad"}
    return None


def apply_inferred_desktop(bot: Any, intent: str) -> str | None:
    """Do the MiniOS desktop action now so Teela does not just talk about it."""
    hit = infer_desktop_tool(intent)
    if not hit or bot is None:
        return None
    name, args = hit
    bid = str(getattr(bot, "id", "") or "")
    short = name.split("__")[-1]
    if short == "desktop_type_text":
        text = str(args.get("text") or "")
        app = str(args.get("app") or "notepad").replace("app_", "") or "notepad"
        if app == "notepad":
            bot.surface = "notepad"
            emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "notepad"})
        elif app == "browser":
            bot.ensure_browser()
            if getattr(bot, "browser", None):
                bot.browser.type_text(text)
            bot.surface = "browser"
        emit(
            {
                "type": "desktop.action",
                "bot_id": bid,
                "action": "type_text",
                "text": text,
                "app": app,
            }
        )
        where = "Notepad" if app == "notepad" else "the browser"
        print(f"[deskd] desktop apply type_text {text!r} in {app}", flush=True)
        return f"Opened {where} and typed {text}."
    if short == "desktop_open_app":
        app = str(args.get("app_id") or "").replace("app_", "")
        if not app:
            return None
        bot.surface = {
            "browser": "browser",
            "files": "desktop",
            "notepad": "notepad",
            "preview": "preview",
        }.get(app, getattr(bot, "surface", "desktop"))
        emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": app})
        print(f"[deskd] desktop apply open_app {app}", flush=True)
        return f"Opened {app}."
    if short == "desktop_browser_navigate":
        url = str(args.get("url") or "")
        try:
            out = bot.show_in_browser(url)
        except Exception as e:
            print(f"[deskd] desktop apply navigate failed: {e}", flush=True)
            return None
        emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "browser"})
        shown = str((out or {}).get("url") or url)
        print(f"[deskd] desktop apply navigate {shown}", flush=True)
        return f"Browser's open at {shown}."
    if short == "desktop_open_file":
        rel = str(args.get("path") or "")
        try:
            target = workspace_target(bot, rel, must_exist=True)
        except (ValueError, FileNotFoundError):
            return None
        path = target.relative_to(bot.workspace.resolve()).as_posix()
        kind = workspace_media_kind(path) or "file"
        if kind == "image":
            bot.surface = "preview"
        elif kind == "video":
            bot.surface = "browser"
            try:
                bot.show_in_browser(path)
            except Exception:
                pass
        elif kind == "notepad":
            bot.surface = "notepad"
        emit(
            {
                "type": "desktop.action",
                "bot_id": bid,
                "action": "open_file",
                "path": path,
                "kind": kind,
                "app": "notepad" if kind == "notepad" else "preview",
            }
        )
        print(f"[deskd] desktop apply open_file {path} {kind}", flush=True)
        return f"Opened {path}."
    return None


def local_llm_write_document(bot: Any, intent: str, topic: str, page: str = "") -> str | None:
    """Write the document they asked to put in Notepad. Follows the conversation."""
    history = recent_chat_messages(bot, limit=8)
    bits = [
        "You are Teela writing into Notepad for the person in the room.",
        "Follow the conversation: if they said 'it', they mean the thing they just asked for.",
        "Write the full useful document they want. No preamble, no 'sure', no tool talk.",
        "If they asked for a recipe, include title, servings, ingredients, and steps.",
    ]
    msgs: list[dict[str, str]] = [{"role": "system", "content": " ".join(bits)}]
    for h in history[:-1]:
        msgs.append(h)
    user = f"Write this for Notepad: {intent.strip()}"
    if topic:
        user += f"\nTopic: {topic}"
    if page:
        user += "\n\nUse this web page if it helps:\n" + page[:4000]
    msgs.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": LOCAL_LLM_SERVED,
        "temperature": 0.6,
        "max_tokens": 900,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": msgs,
    }
    live_map = probe_local_llm_map()
    live = tuple(live_map.keys())
    think_id = next((x for x in live if not _is_fast_llm_id(x)), "")
    fast_id = next((x for x in live if _is_fast_llm_id(x)), "")
    if think_id:
        payload["model"] = think_id
        url = (live_map.get(think_id) or LOCAL_LLM_UPSTREAM) + "/v1/chat/completions"
        lane = "think"
    elif fast_id:
        payload["model"] = fast_id
        url = _fast_llm_upstream(live, live_map) + "/v1/chat/completions"
        lane = "fast"
    else:
        return None
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers=_llm_headers(url, {"Content-Type": "application/json"}),
        )
        with hybrid_exclusive(lane):
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode() or "{}")
        text = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    except Exception:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text).strip()
        text = re.sub(r"\n?```$", "", text).strip()
    if not text or len(text) < 8:
        return None
    if re.match(r"^(?:i (?:can't|cannot)|opened notepad and typed)\b", text, re.I):
        return None
    return text


def compose_and_type_desktop(bot: Any, intent: str) -> str | None:
    """Research and/or write a real document, then type it into Notepad."""
    if not looks_like_composed_type(intent) or bot is None:
        return None
    topic = compose_topic(intent) or intent
    page = ""
    bid = str(getattr(bot, "id", "") or "")
    if _RESEARCH_RE.search(intent or ""):
        emit_local_activity(bot, "Looking that up…")
        url = search_url(topic)
        try:
            out = bot.show_in_browser(url)
            emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "browser"})
            time.sleep(1.2)
            snap = bot.browser_snapshot() if hasattr(bot, "browser_snapshot") else {}
            page = str((snap or {}).get("text") or "")
            shown = str((out or {}).get("url") or url)
            print(f"[deskd] desktop compose researched {shown!r}", flush=True)
        except Exception as e:
            print(f"[deskd] desktop compose search failed: {e}", flush=True)
    emit_local_activity(bot, "Writing in Notepad…")
    body = local_llm_write_document(bot, intent, topic, page)
    if not body:
        return None
    bot.surface = "notepad"
    emit({"type": "desktop.action", "bot_id": bid, "action": "open_app", "app": "notepad"})
    emit({"type": "desktop.action", "bot_id": bid, "action": "type_text", "text": body, "app": "notepad"})
    print(f"[deskd] desktop compose typed {len(body)} chars topic={topic!r}", flush=True)
    if page:
        return "I looked it up and put it in Notepad."
    return "I wrote that in Notepad."


def fallback_desktop_completion(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes | None:
    if not payload or not local_llm_desktop_turn(payload, bot):
        return None
    intent = last_user_intent_from_payload(payload)
    hit = infer_desktop_tool(intent)
    if not hit:
        return None
    name, args = hit
    print(f"[deskd] desktop fallback {name} {args}", flush=True)
    return openai_robot_tool_completion(name, args, served=served)


def ensure_usable_desktop_completion(
    body: bytes,
    payload: dict[str, Any] | None,
    ctype: str = "",
    *,
    served: str = "qwen38",
    bot: Any = None,
) -> bytes:
    if not payload or not local_llm_desktop_turn(payload, bot):
        return body
    hit = re.compile(
        r"desktop_(?:open_app|browser_navigate|open_file|type_text)|web_search|(?:^|__)navigate$",
        re.I,
    )
    for call in _tool_calls_from_completions(_completion_objects(body)):
        name = canonicalize_tool_name(_robot_tool_name(call), None)
        if hit.search(name):
            return body
    fb = fallback_desktop_completion(payload, served=served, bot=bot)
    if not fb:
        return body
    want_sse = "event-stream" in (ctype or "").lower() or (body or b"").lstrip().startswith(b"data:")
    print("[deskd] replacing empty completion with desktop fallback", flush=True)
    return openai_completion_to_sse(fb) if want_sse else fb


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    cur: Any = raw
    for _ in range(3):
        if isinstance(cur, dict):
            return cur
        if not isinstance(cur, str):
            return {}
        text = cur.strip()
        if not text:
            return {}
        try:
            cur = json.loads(text)
        except json.JSONDecodeError:
            return {}
    return cur if isinstance(cur, dict) else {}


def unwrap_misdirected_tool_call(
    name: str, args: Any, allowed: list[str] | None
) -> tuple[str, dict[str, Any]]:
    """Turn use_tool/search_tool wrappers into bot_desktop__robot_*."""
    parsed = _parse_tool_args(args)
    n = canonicalize_tool_name(name, allowed)
    for _ in range(4):
        inner_name = parsed.get("tool_name") or parsed.get("name") or parsed.get("tool")
        inner_args = (
            parsed.get("tool_input")
            or parsed.get("arguments")
            or parsed.get("args")
            or parsed.get("input")
        )
        server = str(parsed.get("server") or "")
        tool = str(parsed.get("tool") or inner_name or "")
        if server and tool:
            n = canonicalize_tool_name(f"{server}__{tool}", allowed)
            parsed = _parse_tool_args(inner_args) or {
                k: v
                for k, v in parsed.items()
                if k
                not in {
                    "server",
                    "tool",
                    "tool_name",
                    "name",
                    "args",
                    "tool_input",
                    "arguments",
                    "input",
                }
            }
            continue
        if inner_name:
            n = canonicalize_tool_name(str(inner_name), allowed)
            parsed = _parse_tool_args(inner_args) or parsed
            if n in {"use_tool", "search_tool"}:
                continue
            break
        break
    pose = str(parsed.get("pose") or "").strip().lower()
    cmd = str(parsed.get("cmd") or parsed.get("motion") or "").strip().lower()
    if pose in robot_sim.POSES:
        return "bot_desktop__robot_pose", {"pose": pose}
    if cmd in robot_sim.POSES:
        return "bot_desktop__robot_pose", {"pose": cmd}
    return n, parsed


def _rewrite_tool_call_entry(call: Any, allowed: list[str], *, complete: bool = False) -> None:
    if not isinstance(call, dict):
        return
    fn = call.get("function") if isinstance(call.get("function"), dict) else None
    old = ""
    raw_args: Any = None
    if fn is not None:
        old = str(fn.get("name") or "")
        raw_args = fn.get("arguments")
    if not old:
        old = str(call.get("name") or "")
        raw_args = call.get("arguments") if raw_args is None else raw_args
    new, parsed = unwrap_misdirected_tool_call(old, raw_args, allowed)
    new = canonicalize_tool_name(new, allowed)
    if not new and parsed and complete:
        new = infer_tool_name_from_args(parsed, allowed)
    if new and new != old:
        print(f"[deskd] tool name {old or '∅'} -> {new}", flush=True)
    # Never turn a stream fragment or empty arguments into "{}". Hermes concatenates
    # argument deltas; rewriting the first chunk to "{}" made run_terminal_command
    # fail with missing field `command`.
    write_args = bool(parsed)
    if fn is not None:
        if new:
            fn["name"] = new
        if write_args:
            dumped = json.dumps(parsed, separators=(",", ":"))
            if isinstance(fn.get("arguments"), str) or fn.get("arguments") is None:
                fn["arguments"] = dumped
            else:
                fn["arguments"] = parsed
    if new:
        if call.get("name") is not None or not fn:
            call["name"] = new


_SHELL_TOOL_NAMES = frozenset({"run_terminal_command", "shell", "bash"})


def infer_tool_name_from_args(
    args: dict[str, Any], allowed: list[str] | None = None
) -> str:
    """Map Qwen's nameless tool payloads onto a real Hermes Agent tool."""
    if not isinstance(args, dict) or not args:
        return ""
    keys = set(args)
    if keys & {"command", "cmd"}:
        guess = ["run_terminal_command", "shell", "bash"]
    elif "target_directory" in keys:
        guess = ["list_dir"]
    elif "old_string" in keys or "new_string" in keys:
        guess = ["search_replace"]
    elif "glob" in keys and "pattern" not in keys:
        guess = ["glob", "list_dir"]
    elif "pattern" in keys:
        guess = ["grep"]
    elif "contents" in keys and "path" in keys:
        guess = ["write"]
    elif "query" in keys:
        guess = ["grep", "web_search"]
    elif "path" in keys:
        guess = ["read_file", "list_dir"]
    else:
        return ""
    allowed = [a for a in (allowed or []) if a]
    for name in guess:
        if not allowed:
            return name
        for a in allowed:
            short = a.split("__")[-1]
            if a == name or short == name:
                return a
    return guess[0]
_PARAM_COMMAND_RE = re.compile(
    r"<parameter\s+name=['\"]command['\"]\s*>(.*?)</parameter>",
    re.I | re.S,
)
_JSON_COMMAND_RE = re.compile(r'"command"\s*:\s*"((?:\\.|[^"\\])*)"')


def infer_shell_command(intent: str) -> str:
    """Last-resort command when Qwen called the shell tool with empty arguments."""
    t = re.sub(r"\s+", " ", (intent or "").lower())
    if re.search(
        r"\b(hardware|system info|system check|diagnostics|"
        r"what (?:cpu|gpu|machine|hardware)|machine spec|host (?:info|check))\b",
        t,
    ):
        return (
            "uname -a; echo '---CPU---'; lscpu 2>/dev/null | sed -n '1,40p'; "
            "echo '---MEM---'; free -h; echo '---DISK---'; df -hT; "
            "echo '---BLOCK---'; lsblk; echo '---GPU---'; "
            "(command -v nvidia-smi >/dev/null && nvidia-smi -L; "
            "command -v clinfo >/dev/null && clinfo -l; true)"
        )
    return ""


def command_from_tool_text(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    m = _PARAM_COMMAND_RE.search(raw)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = _JSON_COMMAND_RE.search(raw)
    if m:
        try:
            return str(json.loads(f'"{m.group(1)}"'))
        except json.JSONDecodeError:
            return m.group(1).replace('\\"', '"')
    return ""


def fill_empty_agent_tool_calls(
    obj: Any,
    *,
    intent: str = "",
    extra_text: str = "",
    message_only: bool = False,
    fill_empty_shell: bool = True,
) -> None:
    """Recover `command` for empty run_terminal_command calls from Qwen.

    Only rewrite complete `message.tool_calls`. Filling a stream *delta* writes a
    finished JSON object, then later chunks concatenate onto it and Hermes sees
    `Tool not found` / broken arguments.
    """
    if not isinstance(obj, dict):
        return
    keys = ("message",) if message_only else ("message",)
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for key in keys:
            block = choice.get(key)
            if not isinstance(block, dict):
                continue
            calls = block.get("tool_calls")
            if not isinstance(calls, list):
                continue
            blob = "\n".join(
                p
                for p in (
                    extra_text,
                    intent,
                    str(block.get("content") or ""),
                )
                if p
            )
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else None
                name = str((fn or {}).get("name") or call.get("name") or "")
                short = name.split("__")[-1]
                raw_args = (fn or call).get("arguments")
                args = _parse_tool_args(raw_args)
                if not name:
                    name = infer_tool_name_from_args(args)
                    if name:
                        if fn is not None:
                            fn["name"] = name
                        else:
                            call["name"] = name
                        short = name.split("__")[-1]
                if short not in _SHELL_TOOL_NAMES:
                    continue
                if str(args.get("command") or args.get("cmd") or "").strip():
                    continue
                cmd = command_from_tool_text(str(raw_args or "")) or command_from_tool_text(blob)
                if not cmd and fill_empty_shell:
                    cmd = infer_shell_command(intent)
                if not cmd:
                    continue
                args["command"] = cmd
                dumped = json.dumps(args, separators=(",", ":"))
                if fn is not None:
                    fn["arguments"] = dumped
                else:
                    call["arguments"] = dumped
                print(f"[deskd] filled empty {name} command={cmd[:80]!r}", flush=True)


def _merge_arg_text(cur: str, incoming: Any) -> str:
    if incoming is None:
        return cur
    if isinstance(incoming, dict):
        incoming_s = json.dumps(incoming, separators=(",", ":"))
    else:
        incoming_s = str(incoming)
    if not incoming_s:
        return cur
    if not cur:
        return incoming_s
    a, b = _parse_tool_args(cur), _parse_tool_args(incoming_s)
    if a and b:
        merged = dict(a)
        for k, v in b.items():
            if v not in (None, ""):
                merged[k] = v
        return json.dumps(merged, separators=(",", ":"))
    return cur + incoming_s


def assemble_stream_tool_calls(
    objs: list[dict[str, Any]],
    allowed: list[str] | None,
    *,
    intent: str = "",
    extra_text: str = "",
    fill_empty_shell: bool = True,
) -> list[dict[str, Any]]:
    """Collapse llama.cpp name-then-args deltas into one complete OpenAI tool call each."""
    slots: dict[int, dict[str, str]] = {}
    order: list[int] = []
    last_named = 0
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            block = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
            if block is None:
                block = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            for call in block.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(fn.get("name") or call.get("name") or "").strip()
                if "index" in call:
                    try:
                        idx = int(call["index"])
                    except (TypeError, ValueError):
                        idx = last_named
                elif name:
                    idx = (max(order) + 1) if order else 0
                else:
                    idx = last_named if order else 0
                if idx not in slots:
                    slots[idx] = {"id": "", "name": "", "arguments": ""}
                    order.append(idx)
                if call.get("id"):
                    slots[idx]["id"] = str(call["id"])
                if name:
                    slots[idx]["name"] = name
                    last_named = idx
                slots[idx]["arguments"] = _merge_arg_text(
                    slots[idx]["arguments"], fn.get("arguments") if fn else call.get("arguments")
                )
    out: list[dict[str, Any]] = []
    for i, idx in enumerate(order):
        slot = slots[idx]
        parsed = _parse_tool_args(slot["arguments"])
        name = canonicalize_tool_name(slot["name"], allowed)
        if not name:
            name = infer_tool_name_from_args(parsed, allowed)
        if not name:
            continue
        short = name.split("__")[-1]
        if short in _SHELL_TOOL_NAMES and not str(parsed.get("command") or parsed.get("cmd") or "").strip():
            cmd = command_from_tool_text(slot["arguments"]) or command_from_tool_text(extra_text)
            if not cmd and fill_empty_shell:
                cmd = infer_shell_command(intent)
            if cmd:
                parsed["command"] = cmd
                print(f"[deskd] filled empty {name} command={cmd[:80]!r}", flush=True)
        args_s = json.dumps(parsed, separators=(",", ":")) if parsed else (slot["arguments"] or "{}")
        out.append(
            {
                "index": i,
                "id": slot["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args_s},
            }
        )
    return out


def sse_stop_body(base: dict[str, Any] | None, content: str = "") -> bytes:
    base = base or {}
    cid = base.get("id") or "chatcmpl-stop"
    created = base.get("created") or 0
    model = base.get("model") or ""
    delta: dict[str, Any] = {"role": "assistant"}
    if content:
        delta["content"] = content
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    done = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        "data: "
        + json.dumps(chunk, separators=(",", ":"))
        + "\n\n"
        + "data: "
        + json.dumps(done, separators=(",", ":"))
        + "\n\n"
        + "data: [DONE]\n\n"
    ).encode()


def sse_tool_calls_body(base: dict[str, Any] | None, calls: list[dict[str, Any]]) -> bytes:
    base = base or {}
    cid = base.get("id") or "chatcmpl-tools"
    created = base.get("created") or 0
    model = base.get("model") or ""
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": None, "tool_calls": calls},
                "finish_reason": None,
            }
        ],
    }
    done = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return (
        "data: "
        + json.dumps(chunk, separators=(",", ":"))
        + "\n\n"
        + "data: "
        + json.dumps(done, separators=(",", ":"))
        + "\n\n"
        + "data: [DONE]\n\n"
    ).encode()


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or fn.get("name") or "").strip()


def _tool_call_has_payload(call: Any) -> bool:
    """Keep named calls and nameless stream deltas that only carry arguments."""
    if _tool_call_name(call):
        return True
    if not isinstance(call, dict):
        return False
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    args = fn.get("arguments") if fn else call.get("arguments")
    if args in (None, "", "{}", "null"):
        return False
    if isinstance(args, str) and re.fullmatch(r"(?:\{\})+", args.strip()):
        return False
    return True


def drop_junk_tool_calls(obj: Any) -> None:
    """Hermes rejects empty names (`Tool not found: `) from a confused local parser.

    Do not drop nameless argument-only deltas — Hermes concatenates those onto the
    earlier run_terminal_command chunk. Stripping them left `command` missing.
    """
    if not isinstance(obj, dict):
        return
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for key in ("message", "delta"):
            block = choice.get(key)
            if not isinstance(block, dict):
                continue
            calls = block.get("tool_calls")
            if not isinstance(calls, list):
                continue
            kept = [c for c in calls if _tool_call_has_payload(c)]
            if kept:
                block["tool_calls"] = kept
            else:
                block.pop("tool_calls", None)


_MOTION_CMDS = frozenset(
    {
        "walk",
        "walk_left",
        "walk_right",
        "walk_place",
        "stop",
        "demo",
        "reset",
        "estop_on",
        "estop_off",
        "motors_on",
        "motors_off",
    }
)


def _json_value_from_text(text: str) -> Any:
    """Parse a model reply that is JSON object or array (optionally fenced / Hermes-tagged)."""
    s = (text or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"</?tool_call>", "", s, flags=re.I).strip()
    extra_name = ""
    if s.startswith("["):
        m = re.search(r"\[.*\]\s*$", s, re.S)
        if not m:
            return None
        s = m.group(0)
        if not s.endswith("]"):
            return None
    elif s.startswith("{"):
        if not s.endswith("}"):
            return None
    else:
        m = re.search(r"(\[.*\]|\{.*\})\s*$", s, re.S)
        if not m:
            return None
        extra_name = s[: m.start()].strip().split()[-1] if s[: m.start()].strip() else ""
        s = m.group(0)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if extra_name and isinstance(obj, dict) and not obj.get("name"):
        obj["name"] = extra_name
    return obj if isinstance(obj, (dict, list)) else None


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    """Parse a model reply that is only a JSON object (optionally fenced / Hermes-tagged)."""
    obj = _json_value_from_text(text)
    return obj if isinstance(obj, dict) else None


def motor_json_to_tool(
    obj: dict[str, Any], allowed: list[str] | None
) -> tuple[str, dict[str, Any]] | None:
    """Map a JSON blob like {\"pose\":\"wave\"} onto a MiniOS robot tool."""
    if not isinstance(obj, dict) or not obj:
        return None
    name = obj.get("name") or obj.get("tool")
    args = obj.get("arguments") or obj.get("args") or obj.get("parameters")
    parsed = _parse_tool_args(args) if args is not None else {
        k: v
        for k, v in obj.items()
        if k not in {"name", "tool", "arguments", "args", "parameters"}
    }
    if name:
        n, parsed = unwrap_misdirected_tool_call(str(name), parsed or obj, allowed)
        n = canonicalize_tool_name(n, allowed)
        if "robot_" in n or "teela_" in n:
            return n, parsed
    skill = str(obj.get("skill") or parsed.get("skill") or "").strip().lower()
    gesture = str(obj.get("gesture") or parsed.get("gesture") or "").strip().lower()
    if gesture in virtual_body.GESTURES or skill in virtual_body.GESTURES:
        g = gesture or skill
        mapped = {"gesture": g}
        side = parsed.get("side", obj.get("side"))
        if side:
            mapped["side"] = side
        return "bot_desktop__teela_gesture", mapped
    if skill in virtual_body.SKILLS or skill == "stop":
        keys = ("skill", "side", "pan_deg", "tilt_deg", "duration_ms")
        mapped = {k: parsed.get(k, obj.get(k)) for k in keys if parsed.get(k, obj.get(k)) is not None}
        mapped["skill"] = skill
        if skill == "stop":
            return "bot_desktop__teela_stop", mapped
        return "bot_desktop__teela_body_action", mapped
    pose = str(obj.get("pose") or parsed.get("pose") or "").strip().lower()
    cmd = str(obj.get("cmd") or obj.get("motion") or parsed.get("cmd") or "").strip().lower()
    if pose in robot_sim.POSES and cmd in {"", "pose"}:
        return "bot_desktop__robot_pose", {"pose": pose}
    if cmd in robot_sim.POSES:
        return "bot_desktop__robot_pose", {"pose": cmd}
    src = parsed if (parsed.get("joint") or parsed.get("joints")) else obj
    if src.get("joint") or src.get("joints"):
        keys = ("joint", "value", "delta", "dir", "joints")
        return "bot_desktop__robot_joint", {k: src[k] for k in keys if k in src}
    if cmd == "plan" or obj.get("steps") or parsed.get("steps"):
        planned = robot_sim.normalize_plan(
            {"cmd": "plan", "steps": obj.get("steps") or parsed.get("steps"), "why": obj.get("why") or parsed.get("why")}
        )
        if planned:
            args: dict[str, Any] = {"cmd": "plan", "steps": planned["steps"]}
            if planned.get("why"):
                args["why"] = planned["why"]
            return "bot_desktop__robot_motion", args
    if cmd in _MOTION_CMDS or cmd.startswith("walk"):
        out: dict[str, Any] = {"cmd": cmd}
        direction = obj.get("direction") or parsed.get("direction")
        if direction:
            out["direction"] = direction
        return "bot_desktop__robot_motion", out
    return None


def _robot_tool_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or fn.get("name") or "")


def _robot_tool_args(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = fn.get("arguments") if fn else None
    if raw is None:
        raw = call.get("arguments")
    return _parse_tool_args(raw)


def robot_args_usable(name: str, args: dict[str, Any] | None) -> bool:
    """True when a robot_* tool has enough args to actually move the twin."""
    n = str(name or "").lower()
    args = args if isinstance(args, dict) else {}
    if "teela_body_action" in n:
        skill = str(args.get("skill") or "").strip().lower()
        return skill in virtual_body.SKILLS or skill in virtual_body.GESTURES
    if "teela_gesture" in n:
        g = str(args.get("gesture") or args.get("skill") or "").strip().lower()
        return g in virtual_body.GESTURES
    if "teela_stop" in n:
        return True
    if "teela_activity" in n:
        return True
    if "teela_system_check" in n:
        return True
    if "robot_pose" in n:
        pose = str(args.get("pose") or args.get("name") or "").strip().lower().replace(" ", "_")
        return pose in robot_sim.POSES
    if "robot_joint" in n:
        return bool(args.get("joint") or args.get("joints"))
    if "robot_motion" in n:
        cmd = str(args.get("cmd") or args.get("motion") or "").strip().lower()
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        return bool(cmd) or len(steps) >= 1
    return False


def _tool_result_json(msg: dict[str, Any]) -> dict[str, Any]:
    raw = str(msg.get("content") or "")
    data = _json_object_from_text(raw)
    if isinstance(data, dict) and isinstance(data.get("content"), list):
        for part in data["content"]:
            if isinstance(part, dict) and part.get("text"):
                inner = _json_object_from_text(str(part.get("text") or ""))
                if isinstance(inner, dict):
                    return inner
    return data if isinstance(data, dict) else {}


def _tool_result_moved(msg: dict[str, Any]) -> bool:
    name = str(msg.get("name") or "")
    blob = _flatten_message_text(msg)
    if re.search(r"teela_(?:body_action|gesture|stop)", name or blob, re.I):
        data = _tool_result_json(msg)
        st = str(data.get("status") or "").lower()
        if st in {"rejected", "timeout"}:
            return False
        if st in {"completed", "executing"} or data.get("ok") is True:
            return True
        return False
    if not re.search(r"robot_(?:joint|pose|motion)", name or blob, re.I):
        return False
    data = _tool_result_json(msg)
    if data.get("ok") is False or data.get("held") or data.get("error"):
        return False
    if data.get("ok") is True:
        return True
    pose = str(data.get("pose") or "").strip().lower()
    return bool(pose) or "\"ok\": true" in blob.lower() or '"ok":true' in blob.lower()


def motor_already_satisfied(payload: dict[str, Any] | None, bot: Any = None) -> bool:
    """True when the twin already holds the pose/motion this user turn asked for."""
    st = getattr(bot, "robot_state", None) if bot is not None else None
    intent = last_user_intent_from_payload(payload)
    if orch.classify(intent).get("mode") in {"parallel", "after"}:
        return False
    virt = (
        virtual_body.overlay(str(getattr(bot, "id", "") or ""), st)
        if bot is not None
        else st
    )
    cmd = robot_sim.infer_command(intent, st) if intent else None
    hit = virtual_body.teela_args_from_motor(cmd, intent or "")
    live = virt if isinstance(virt, dict) else {}
    skill = str((hit[1] if hit else {}).get("skill") or (hit[1] if hit else {}).get("gesture") or "").strip().lower()
    # Asking to wave must restart the HTML overlay. Leftover MiniOS pose=wave
    # is not the same as the twin actually rocking.
    if skill in {"wave", "greeting"}:
        return False
    if hit and (
        virtual_body.joints_of(live) or live.get("pose") or live.get("motion")
    ):
        return not virtual_body.move_still_needed(hit[1], live)
    if not isinstance(st, dict):
        return False
    if not isinstance(cmd, dict):
        return False
    pose = str(cmd.get("pose") or "").strip().lower()
    kind = str(cmd.get("cmd") or "").strip().lower()
    cur_pose = str(st.get("pose") or "").strip().lower()
    cur_motion = str(st.get("motion") or "").strip().lower()
    if pose and pose == cur_pose:
        if pose == "wave":
            return robot_sim.observed_waving(live if isinstance(live, dict) else st)
        return True
    if kind in {"walk", "start_walk"} or kind.startswith("walk"):
        if cur_motion == "walking":
            want = str(cmd.get("direction") or "").strip().lower()
            got = str(st.get("walk_direction") or "").strip().lower()
            return (not want) or want == got
    if kind in {"stop", "stop_demo", "neutral"} and cur_motion in {"idle", "stopped"}:
        return True
    return False


def robot_tool_failed_this_turn(payload: dict[str, Any] | None) -> bool:
    """True when this user turn already called robot_* and the twin did not move."""
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
    failed = False
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if not re.search(
            r"(?:robot_(?:joint|pose|motion)|teela_(?:body_action|gesture|stop))$",
            str(msg.get("name") or ""),
            re.I,
        ):
            continue
        if _tool_result_moved(msg):
            return False
        failed = True
    return failed


def payload_already_moved(payload: dict[str, Any] | None, bot: Any = None) -> bool:
    """True when this completion is the follow-up after a robot_* tool already moved."""
    if motor_already_satisfied(payload, bot):
        return True
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            last_user = i
            text = _flatten_message_text(msg)
            if _BODY_APPLIED_MARK in text:
                return True
            if text.startswith("You already moved.") or f"\nYou already moved." in text:
                return True
    saw_usable_call = False
    saw_ok_result = False
    saw_failed_result = False
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict):
            continue
        name = str(msg.get("name") or "")
        blob = _flatten_message_text(msg)
        if msg.get("role") == "tool" and re.search(
            r"robot_(?:joint|pose|motion)|teela_(?:body_action|gesture|stop)", name or blob, re.I
        ):
            if _tool_result_moved(msg):
                saw_ok_result = True
            else:
                saw_failed_result = True
        for call in msg.get("tool_calls") or []:
            tname = _robot_tool_name(call)
            args = _robot_tool_args(call)
            if "teela_system_check" in (tname or "").lower():
                continue
            if robot_args_usable(tname, args) and re.search(
                r"robot_(?:joint|pose|motion)|teela_(?:body_action|gesture|stop)",
                tname,
                re.I,
            ):
                saw_usable_call = True
    if saw_failed_result and not saw_ok_result:
        return False
    return saw_ok_result or saw_usable_call


def payload_already_system_checked(payload: dict[str, Any] | None) -> bool:
    """True when teela_system_check already ran after the latest user turn."""
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict):
            continue
        name = str(msg.get("name") or "")
        blob = _flatten_message_text(msg)
        if msg.get("role") == "tool" and re.search(r"teela_system_check", name or blob, re.I):
            return True
        for call in msg.get("tool_calls") or []:
            if "teela_system_check" in canonicalize_tool_name(_robot_tool_name(call), None):
                return True
    return False


def payload_already_browsed(payload: dict[str, Any] | None) -> bool:
    """True when a MiniOS browser/desktop tool already ran after the latest user turn."""
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
    hit = re.compile(
        r"desktop_(?:open_app|browser_navigate|browser_back|browser_forward|open_file|type_text)|"
        r"web_search|(?:^|__)navigate$|open_local_page",
        re.I,
    )
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict):
            continue
        name = str(msg.get("name") or "")
        blob = _flatten_message_text(msg)
        if msg.get("role") == "tool" and hit.search(name or blob):
            return True
        for call in msg.get("tool_calls") or []:
            if hit.search(canonicalize_tool_name(_robot_tool_name(call), None)):
                return True
    return False


def last_user_intent_from_payload(payload: dict[str, Any] | None) -> str:
    raw = ""
    for msg in reversed((payload or {}).get("messages") or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            raw = _flatten_message_text(msg)
            break
    intent = user_intent_text(raw)
    candidates = [intent]
    for line in (raw or "").splitlines():
        bit = user_intent_text(line)
        if bit and bit not in candidates:
            candidates.append(bit)
    best = intent
    best_cmd: dict[str, Any] | None = robot_sim.infer_command(intent) if intent else None
    for c in candidates:
        if not c:
            continue
        cmd = robot_sim.infer_command(c)
        if not cmd:
            continue
        if str(cmd.get("cmd") or "") == "plan":
            return c
        if best_cmd is None or (str(best_cmd.get("cmd") or "") != "plan" and len(c) > len(best or "")):
            best, best_cmd = c, cmd
    return best or intent


def motor_cmd_to_openai_tool(cmd: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(cmd, dict):
        return None
    kind = str(cmd.get("cmd") or "")
    if kind == "plan" or cmd.get("steps"):
        planned = robot_sim.normalize_plan(cmd if kind == "plan" else {"cmd": "plan", **cmd})
        if not planned:
            return None
        args: dict[str, Any] = {"cmd": "plan", "steps": planned["steps"]}
        if planned.get("why"):
            args["why"] = planned["why"]
        return "bot_desktop__robot_motion", args
    if kind == "pose" and cmd.get("pose"):
        return "bot_desktop__robot_pose", {"pose": str(cmd.get("pose"))}
    if kind == "joint":
        args = {}
        for key in ("joint", "value", "delta", "dir", "joints"):
            if cmd.get(key) is not None:
                args[key] = cmd[key]
        return ("bot_desktop__robot_joint", args) if args else None
    if kind in _MOTION_CMDS or kind.startswith("walk"):
        args = {"cmd": kind}
        if cmd.get("direction"):
            args["direction"] = cmd["direction"]
        return "bot_desktop__robot_motion", args
    return None


def _snapshot_robot_args(args: dict[str, Any] | None) -> dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    steps = args.get("steps") if isinstance(args.get("steps"), list) else []
    cmd = str(args.get("cmd") or "").strip().lower()
    why = str(args.get("why") or "").strip() if cmd == "plan" or steps else ""
    return {
        "pose": str(args.get("pose") or "").strip().lower(),
        "cmd": cmd,
        "direction": str(args.get("direction") or args.get("walk_direction") or "").strip().lower(),
        "why": why,
        "steps": list(steps),
    }


def _last_robot_tool_args(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Args from the robot_* tool in this user turn only — never a prior plan's why."""
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
    last = {"pose": "", "cmd": "", "direction": "", "why": "", "steps": []}
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict):
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(call.get("name") or fn.get("name") or "")
            if "robot_" not in name:
                continue
            last = _snapshot_robot_args(_parse_tool_args(fn.get("arguments") or call.get("arguments")))
        if msg.get("role") == "tool" and re.search(
            r"robot_(?:status|joint|pose|motion)$", str(msg.get("name") or ""), re.I
        ):
            try:
                data = json.loads(str(msg.get("content") or "") or "{}")
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                continue
            pose = str(data.get("pose") or last.get("pose") or "").strip().lower()
            cmd = str(data.get("cmd") or last.get("cmd") or "").strip().lower()
            motion = str(data.get("motion") or "").strip().lower()
            direction = str(
                data.get("walk_direction") or data.get("direction") or last.get("direction") or ""
            ).strip().lower()
            plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
            steps = last.get("steps") if isinstance(last.get("steps"), list) else []
            why = str(last.get("why") or "").strip()
            if cmd == "plan" or str(last.get("cmd") or "") == "plan":
                if isinstance(plan.get("steps"), list):
                    steps = list(plan.get("steps") or [])
                why = str(plan.get("why") or data.get("why") or why).strip()
            elif not cmd and motion:
                cmd = motion
            last = {
                "pose": pose,
                "cmd": cmd,
                "direction": direction,
                "why": why,
                "steps": steps,
            }
    return last


def _period(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    return t if t.endswith((".", "!", "?")) else t + "."


def _leg_is_raised_cmd(cmd: dict[str, Any] | None) -> bool:
    if not isinstance(cmd, dict):
        return False
    if str(cmd.get("pose") or "") in {"right_leg_raise", "left_leg_raise"}:
        return True
    joints = cmd.get("joints") if isinstance(cmd.get("joints"), dict) else {}
    try:
        return max(float(joints.get("right_hip") or 0), float(joints.get("left_hip") or 0)) >= 25
    except (TypeError, ValueError):
        return False


def _last_teela_args(payload: dict[str, Any] | None) -> dict[str, Any]:
    """teela_* arguments from this user turn only."""
    msgs = (payload or {}).get("messages") or []
    last_user = -1
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i
    last: dict[str, Any] = {}
    for msg in msgs[last_user + 1 :]:
        if not isinstance(msg, dict):
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(call.get("name") or fn.get("name") or "")
            if not re.search(r"teela_(?:body_action|gesture|stop)", name, re.I):
                continue
            parsed = _parse_tool_args(fn.get("arguments") or call.get("arguments"))
            if parsed:
                last = parsed
        if msg.get("role") == "tool" and re.search(
            r"teela_(?:body_action|gesture|stop)", str(msg.get("name") or ""), re.I
        ):
            data = _tool_result_json(msg)
            if isinstance(data, dict):
                params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
                merged = {**params, **{k: v for k, v in data.items() if k != "parameters"}}
                if merged.get("skill") or merged.get("gesture") or merged.get("pan_deg") is not None:
                    last = {**last, **merged}
    return last


def speech_after_motor(payload: dict[str, Any] | None, bot: Any = None) -> str:
    """Follow THIS user line from the live virtual pose, not the requested words."""
    intent = last_user_intent_from_payload(payload)
    st = getattr(bot, "robot_state", None) if bot is not None else None
    if bot is not None:
        bid = str(getattr(bot, "id", "") or "")
        virtual_body.wait_settle(bid)
        st = virtual_body.overlay(bid, st)
        joints = virtual_body.joints_of(virtual_body.latest_state(bid))
        if not joints and isinstance(st, dict):
            joints = virtual_body.joints_of(st)
        if intent and re.search(
            r"\b(?:look|turn|face|head|tilt|straight|center|ahead)\b", intent, re.I
        ) and not re.search(r"\bwalk\b", intent, re.I):
            return virtual_body.head_speech(joints)
    inferred = robot_sim.infer_command(intent, st) if intent else None
    if intent and re.search(
        r"\b(?:put|lower|drop).{0,24}\b(?:leg|foot|knee)|\b(?:leg|foot|knee)s?\s+down\b",
        intent,
        re.I,
    ):
        return "I've put my leg down."
    if intent and re.search(
        r"\b(?:put|lower|drop).{0,20}\b(?:arm|hand)|\b(?:hands?|arms?)\s+down\b|\bput it down\b",
        intent,
        re.I,
    ):
        return "I've put it down."
    if orch.classify(intent).get("mode") in {"parallel", "after"}:
        if intent and re.search(r"\bwave\b.{0,16}\b(?:hello|hi|hey)\b|\b(?:hello|hi|hey)\b.{0,12}\bwave\b", intent, re.I):
            return "Hi! I'm waving."
        return "On it — I'll move when that work is done."
    if intent and re.search(r"\bwave\b.{0,16}\b(?:hello|hi|hey)\b|\b(?:hello|hi|hey)\b.{0,12}\bwave\b", intent, re.I):
        return "Hi! I'm waving."
    if intent and re.search(r"\bwalk\b", intent, re.I):
        if re.search(r"\bleft\b", intent, re.I):
            return "I'm walking left."
        if re.search(r"\bright\b", intent, re.I):
            return "I'm walking right."
        if re.search(r"\b(?:back|north)\b", intent, re.I):
            return "I'm walking back."
        return "I'm walking."
    if intent and re.search(r"\b(?:look|turn|face).{0,20}\bleft\b|\bhead.{0,12}left\b", intent, re.I):
        return "Looking left."
    if intent and re.search(r"\b(?:look|turn|face).{0,20}\bright\b|\bhead.{0,12}right\b", intent, re.I):
        return "Looking right."
    if intent and re.search(r"\b(?:look|tilt).{0,16}\bup\b", intent, re.I):
        return "Looking up."
    if intent and re.search(r"\b(?:look|face|turn).{0,16}\b(?:straight|center|ahead|forward)\b", intent, re.I):
        return "Looking straight ahead."
    if intent and re.search(r"\b(?:raise|lift).{0,20}\b(?:arm|hand)\b", intent, re.I):
        return "Raising my arm."
    if isinstance(inferred, dict):
        kind = str(inferred.get("cmd") or "")
        if kind == "plan":
            return _period(robot_sim.plan_speech(inferred) or "Okay, I moved.")
        if inferred.get("pose") == "wave":
            if bot is None or robot_sim.observed_waving(st):
                return "I'm waving."
            return "I told my body to wave — I'm not waving on the twin yet."
        if _leg_is_raised_cmd(inferred):
            return "I'm raising my leg."
        if kind in {"walk", "start_walk"} or str(kind).startswith("walk"):
            direction = str(inferred.get("direction") or "")
            if direction in {"left", "right", "back"}:
                return f"I'm walking {direction}."
            return "I'm walking."
        if kind in {"stop", "stop_demo", "neutral"}:
            return "I've stopped."
        if inferred.get("pose") == "home":
            return "I'm standing."
        spoken = robot_sim.confirm_move(st if isinstance(st, dict) else {}, inferred, intent or "")
        if spoken and spoken not in {"Done."}:
            return _period(spoken)
    hit = _last_robot_tool_args(payload)
    pose = str(hit.get("pose") or "")
    cmd = str(hit.get("cmd") or "")
    direction = str(hit.get("direction") or "")
    why = str(hit.get("why") or "")
    steps = hit.get("steps") if isinstance(hit.get("steps"), list) else []
    if cmd == "plan" and why:
        return _period(why)
    if cmd == "plan" or len(steps) >= 2:
        spoken = robot_sim.plan_speech({"why": why, "steps": steps})
        if spoken:
            return _period(spoken)
    if pose == "wave" or cmd in {"wave", "waving"}:
        if bot is None or robot_sim.observed_waving(st):
            return "I'm waving."
        return "I told my body to wave — I'm not waving on the twin yet."
    if cmd in {"stop", "stop_demo", "neutral"}:
        return "I've stopped."
    if cmd in {"walk", "walking", "start_walk"} or pose in {"walk-cycle", "walk_cycle"}:
        if direction in {"left", "right", "back"}:
            return f"I'm walking {direction}."
        return "I'm walking."
    if pose in {"right_leg_raise", "left_leg_raise"}:
        return "I'm raising my leg."
    if pose:
        return f"Okay — {pose.replace('_', ' ')}."
    if cmd:
        return f"Okay — {cmd.replace('_', ' ')}."
    if isinstance(st, dict):
        feel = robot_sim.describe_body(st, which="live")
        if feel:
            return feel.split(".")[0].strip() + "."
    return "Okay, I moved."


def fallback_moved_speech(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes:
    text = speech_after_motor(payload, bot)
    print(f"[deskd] motor follow-up speech: {text}", flush=True)
    return json.dumps(
        {
            "id": "chatcmpl-motor-speech",
            "object": "chat.completion",
            "model": served or "qwen38",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
        }
    ).encode()


def openai_completion_to_sse(body: bytes) -> bytes:
    """Turn a non-stream chat.completion into SSE chunks Hermes's agent can parse."""
    try:
        obj = json.loads((body or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return body
    if not isinstance(obj, dict):
        return body
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return body
    ch0 = choices[0]
    msg = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}
    cid = str(obj.get("id") or "chatcmpl-desk")
    created = obj.get("created") or 0
    model = str(obj.get("model") or "")
    delta: dict[str, Any] = {"role": "assistant"}
    finish = str(ch0.get("finish_reason") or "stop")
    if msg.get("tool_calls"):
        delta["content"] = None
        delta["tool_calls"] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            if isinstance(tc, dict):
                delta["tool_calls"].append({"index": i, **tc})
        finish = "tool_calls"
    else:
        delta["content"] = msg.get("content") or ""
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    done = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    }
    return (
        "data: "
        + json.dumps(chunk, separators=(",", ":"))
        + "\n\n"
        + "data: "
        + json.dumps(done, separators=(",", ":"))
        + "\n\n"
        + "data: [DONE]\n\n"
    ).encode()


def openai_robot_tool_completion(
    name: str,
    args: dict[str, Any],
    *,
    served: str = "qwen38",
    call_id: str = "call_robot",
) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-motor-fallback",
            "object": "chat.completion",
            "model": served or "qwen38",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, separators=(",", ":")),
                                },
                            }
                        ],
                    },
                }
            ],
        }
    ).encode()


def fallback_motor_completion(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes | None:
    """If the local engine dies or returns no tool call, still finish the motor turn."""
    if not payload:
        return None
    if payload_already_moved(payload):
        return fallback_moved_speech(payload, served=served, bot=bot)
    intent = last_user_intent_from_payload(payload)
    st = getattr(bot, "robot_state", None) if bot is not None else None
    cmd = robot_sim.infer_command(intent, st) if intent else None
    hit = motor_cmd_to_openai_tool(cmd)
    if not hit:
        return None
    name, args = hit
    print(f"[deskd] motor fallback {name} {args}", flush=True)
    return openai_robot_tool_completion(name, args, served=served)


def fallback_system_check_completion(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes | None:
    """If Qwen announces a check with no tool call, dispatch teela_system_check."""
    if not payload or not local_llm_system_check_turn(payload, bot):
        return None
    print("[deskd] system-check fallback bot_desktop__teela_system_check", flush=True)
    return openai_robot_tool_completion("bot_desktop__teela_system_check", {}, served=served)


def ensure_usable_system_check_completion(
    body: bytes,
    payload: dict[str, Any] | None,
    ctype: str = "",
    *,
    served: str = "qwen38",
    bot: Any = None,
) -> bytes:
    """Replace a spoken-only 'running a system check' with the actual tool call."""
    if not payload or not local_llm_system_check_turn(payload, bot):
        return body
    for tool_name, _args in _robot_tools_from_body(body):
        if "teela_system_check" in tool_name:
            return body
    fb = fallback_system_check_completion(payload, served=served, bot=bot)
    if not fb:
        return body
    want_sse = "event-stream" in (ctype or "").lower() or (body or b"").lstrip().startswith(b"data:")
    print("[deskd] replacing empty completion with system-check fallback", flush=True)
    return openai_completion_to_sse(fb) if want_sse else fb


def local_llm_direct_completion(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes | None:
    """Finish a MiniOS body turn without waiting on a slow/empty local generate."""
    if not payload:
        return None
    intent = last_user_intent_from_payload(payload)
    if local_llm_system_check_turn(payload, bot):
        return fallback_system_check_completion(payload, served=served, bot=bot)
    if local_llm_desktop_turn(payload, bot):
        return fallback_desktop_completion(payload, served=served, bot=bot)
    mixed_mode = orch.classify(intent).get("mode")
    if mixed_mode == "after" and bot is not None and not bot_kind_is_teela(bot):
        spoken = orch.inspect_then_wave(str(getattr(bot, "id", "") or ""), intent or "")
        if spoken:
            print("[deskd] orchestrator inspect-then-wave", flush=True)
            return json.dumps(
                {
                    "id": "chatcmpl-orch-after",
                    "object": "chat.completion",
                    "model": served or "qwen38",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": spoken},
                        }
                    ],
                }
            ).encode()
    if local_llm_after_system_work_turn(payload, bot):
        return fallback_motor_completion(payload, served=served, bot=bot)
    if mixed_mode in {"parallel", "after"}:
        return None
    if not local_llm_motor_turn(payload) and not payload_already_moved(payload, bot):
        return None
    if motion.needs_vision(intent):
        return None
    bid = str(getattr(bot, "id", "") or "") if bot is not None else ""
    if bid:
        virtual_body.wait_settle(bid)
    st = getattr(bot, "robot_state", None) if bot is not None else None
    virt = virtual_body.overlay(bid, st) if bot is not None else st
    cmd = robot_sim.infer_command(intent, st) if intent else None
    hit = virtual_body.teela_args_from_motor(cmd, intent or "")
    already = payload_already_moved(payload, bot)
    still = bool(hit and virtual_body.move_still_needed(hit[1], virt))
    matched = bool(hit and virtual_body.same_request(_last_teela_args(payload), hit[1]))
    # Speak only once the live pose matches. If they asked look-straight while
    # still facing left, emit pan_deg=0 even if a leftover tool result exists.
    if already and not (still and not matched):
        return fallback_moved_speech(payload, served=served, bot=bot)
    if not local_llm_motor_turn(payload):
        return None
    if hit:
        name, args = hit
        print(f"[deskd] motor direct virtual {name} {args}", flush=True)
        return openai_robot_tool_completion(name, args, served=served)
    return fallback_motor_completion(payload, served=served, bot=bot)


def fallback_chat_speech(payload: dict[str, Any] | None, bot: Any = None) -> str:
    """Spoken line when the local engine is down so Hermes can end the turn."""
    if payload_already_moved(payload):
        return speech_after_motor(payload, bot)
    intent = last_user_intent_from_payload(payload)
    st = getattr(bot, "robot_state", None) if bot is not None else None
    feel = robot_sim.describe_body(st) if isinstance(st, dict) else ""
    if intent and (
        robot_sim.looks_like_body_query(intent)
        or re.search(r"\bhow (?:do you feel|are you feeling|are you)\b", intent, re.I)
    ):
        return _period(feel or "I feel present, standing here with you.")
    if re.match(r"^(?:hi|hello|hey|yo|howdy)\b", intent or "", re.I):
        return "Hey — I'm here."
    if feel:
        return _period(feel.split(".")[0].strip())
    return "I'm here. Say that again in a moment if you want more."


def fallback_engine_down_completion(
    payload: dict[str, Any] | None, served: str = "qwen38", bot: Any = None
) -> bytes:
    """Valid spoken chat.completion so Hermes does not retry empty replies or 502."""
    fb = fallback_motor_completion(payload, served=served, bot=bot)
    if fb:
        return fb
    text = fallback_chat_speech(payload, bot)
    print(f"[deskd] local llm down; speaking {text!r}", flush=True)
    return json.dumps(
        {
            "id": "chatcmpl-llm-down",
            "object": "chat.completion",
            "model": served or "qwen38",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
        }
    ).encode()


def inferred_motor_from_payload(
    payload: dict[str, Any] | None, bot: Any = None
) -> dict[str, Any] | None:
    if not payload or payload_already_moved(payload):
        return None
    intent = last_user_intent_from_payload(payload)
    st = getattr(bot, "robot_state", None) if bot is not None else None
    cmd = robot_sim.infer_command(intent, st) if intent else None
    return cmd if isinstance(cmd, dict) else None


def inferred_plan_from_payload(payload: dict[str, Any] | None, bot: Any = None) -> dict[str, Any] | None:
    cmd = inferred_motor_from_payload(payload, bot)
    if not isinstance(cmd, dict) or str(cmd.get("cmd") or "") != "plan":
        return None
    return robot_sim.normalize_plan(cmd)


def _completion_objects(body: bytes) -> list[dict[str, Any]]:
    text = (body or b"").decode("utf-8", "replace")
    objs: list[dict[str, Any]] = []
    if text.lstrip().startswith("data:"):
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                objs.append(obj)
        return objs
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [obj] if isinstance(obj, dict) else []


def _tool_calls_from_completions(objs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for obj in objs:
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            for key in ("message", "delta"):
                block = choice.get(key)
                if not isinstance(block, dict):
                    continue
                for call in block.get("tool_calls") or []:
                    if isinstance(call, dict):
                        calls.append(call)
    return calls


def completion_has_plan_tool(body: bytes, planned: dict[str, Any]) -> bool:
    need = planned.get("steps") if isinstance(planned.get("steps"), list) else []
    if len(need) < 2:
        return True
    for call in _tool_calls_from_completions(_completion_objects(body)):
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = _parse_tool_args(fn.get("arguments") or call.get("arguments"))
        if str(args.get("cmd") or "") != "plan":
            continue
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        if len(steps) >= len(need):
            return True
    return False


def _completion_matches_inferred(body: bytes, name: str, args: dict[str, Any]) -> bool:
    for call in _tool_calls_from_completions(_completion_objects(body)):
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        got_name = str(fn.get("name") or call.get("name") or "")
        got = _parse_tool_args(fn.get("arguments") or call.get("arguments"))
        if canonicalize_tool_name(got_name, None) != name:
            continue
        if str(args.get("cmd") or "") == "plan":
            steps = got.get("steps") if isinstance(got.get("steps"), list) else []
            need = args.get("steps") if isinstance(args.get("steps"), list) else []
            if len(steps) >= len(need) >= 2:
                return True
            continue
        if args.get("pose") and str(got.get("pose") or "") == str(args.get("pose")):
            return True
        if args.get("cmd") and str(got.get("cmd") or "") == str(args.get("cmd")):
            if not args.get("direction") or str(got.get("direction") or "") == str(args.get("direction")):
                return True
        if args.get("joints") and got.get("joints") == args.get("joints"):
            return True
    return False


def prefer_inferred_plan_completion(
    body: bytes,
    payload: dict[str, Any] | None,
    ctype: str = "",
    *,
    served: str = "qwen38",
    bot: Any = None,
) -> bytes | None:
    """Only if the model returned no robot tool — never overwrite a tool it chose."""
    if response_has_robot_tool_call(body):
        return None
    cmd = inferred_motor_from_payload(payload, bot)
    hit = motor_cmd_to_openai_tool(cmd)
    if not hit:
        return None
    name, args = hit
    print(f"[deskd] empty motor reply; using inferred {name} {args}", flush=True)
    raw = openai_robot_tool_completion(name, args, served=served)
    want_sse = "event-stream" in (ctype or "").lower() or (body or b"").lstrip().startswith(b"data:")
    return openai_completion_to_sse(raw) if want_sse else raw


_LENGTH_FINISH = frozenset({"length", "max_tokens", "max_tokens_truncation"})


def completion_truncated_by_max_tokens(body: bytes) -> bool:
    for obj in _completion_objects(body):
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if str(choice.get("finish_reason") or "").lower() in _LENGTH_FINISH:
                return True
    return False


def rewrite_length_finish_to_stop(body: bytes) -> bytes:
    """Hermes ACP treats finish_reason=length as Internal error / max_tokens_truncation."""
    if not body:
        return body
    text = body.decode("utf-8", "replace")
    if text.lstrip().startswith("data:"):
        rebuilt: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            if not line.startswith("data:"):
                rebuilt.append(line)
                continue
            payload, ended = _sse_data_payload(line)
            if not payload.strip() or payload.strip() == "[DONE]":
                rebuilt.append(line)
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                rebuilt.append(line)
                continue
            if isinstance(obj, dict):
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    if str(choice.get("finish_reason") or "").lower() in _LENGTH_FINISH:
                        choice["finish_reason"] = "stop"
                        changed = True
            rebuilt.append("data: " + json.dumps(obj, separators=(",", ":")) + ended)
        return "".join(rebuilt).encode("utf-8") if changed else body
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return body
    if not isinstance(obj, dict):
        return body
    changed = False
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        if str(choice.get("finish_reason") or "").lower() in _LENGTH_FINISH:
            choice["finish_reason"] = "stop"
            changed = True
    return json.dumps(obj).encode("utf-8") if changed else body


def response_has_robot_tool_call(body: bytes) -> bool:
    """True only for a structured robot_* tool_call that can actually move the twin.

    Substring matches on chat prose are not enough: Qwen often mentions
    bot_desktop__robot_pose while emitting no usable arguments, which used to
    block motor fallback and leave the body at home.
    """
    for call in _tool_calls_from_completions(_completion_objects(body)):
        name = canonicalize_tool_name(_robot_tool_name(call), None)
        if robot_args_usable(name, _robot_tool_args(call)):
            return True
    return False


def _robot_tools_from_body(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for call in _tool_calls_from_completions(_completion_objects(body)):
        name = canonicalize_tool_name(_robot_tool_name(call), None)
        if "robot_" not in name and "teela_" not in name:
            continue
        out.append((name, _robot_tool_args(call)))
    return out


def _assistant_content_from_body(body: bytes) -> str:
    parts: list[str] = []
    for obj in _completion_objects(body):
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            for key in ("message", "delta"):
                block = choice.get(key)
                if isinstance(block, dict) and isinstance(block.get("content"), str):
                    parts.append(block["content"])
    return "".join(parts)


def fill_robot_tool_args(name: str, args: dict[str, Any] | None, cmd: dict[str, Any] | None) -> dict[str, Any]:
    """Fill missing pose/cmd/joints on a robot_* call from the understood chat intent."""
    args = dict(args or {})
    if not isinstance(cmd, dict):
        return args
    n = str(name or "").lower()
    if "robot_pose" in n:
        pose = str(args.get("pose") or args.get("name") or "").strip().lower().replace(" ", "_")
        inferred = str(cmd.get("pose") or "").strip().lower().replace(" ", "_")
        if pose not in robot_sim.POSES and inferred in robot_sim.POSES:
            args["pose"] = inferred
        return args
    if "robot_joint" in n:
        if not (args.get("joint") or args.get("joints")):
            for key in ("joint", "value", "delta", "dir", "joints"):
                if cmd.get(key) is not None:
                    args[key] = cmd[key]
        return args
    if "robot_motion" in n:
        if not str(args.get("cmd") or args.get("motion") or "").strip():
            kind = str(cmd.get("cmd") or "").strip()
            if kind:
                args["cmd"] = kind
            if cmd.get("direction") and not args.get("direction"):
                args["direction"] = cmd["direction"]
            if cmd.get("steps") and not args.get("steps"):
                args["steps"] = cmd["steps"]
            if cmd.get("why") and not args.get("why"):
                args["why"] = cmd["why"]
        return args
    if "teela_body_action" in n:
        if not str(args.get("skill") or "").strip():
            hit = virtual_body.teela_args_from_motor(cmd)
            if hit:
                args.update(hit[1])
        return args
    if "teela_gesture" in n:
        if not str(args.get("gesture") or "").strip():
            hit = virtual_body.teela_args_from_motor(cmd)
            if hit and "gesture" in hit[1]:
                args.update(hit[1])
        return args
    return args


def ensure_usable_motor_completion(
    body: bytes,
    payload: dict[str, Any] | None,
    ctype: str = "",
    *,
    served: str = "qwen38",
    bot: Any = None,
) -> bytes:
    """After the local model processes a motor turn, emit a real robot_* call.

    Qwen often answers in prose ("watching my right arm come up") or with an
    empty robot_pose tool_call. Replace that with a tool-only completion so
    Hermes dispatches MCP and the twin actually moves — never claim motion first.
    """
    if (
        not payload
        or payload_already_moved(payload, bot)
        or bot_kind_is_agent(bot)
        or not local_llm_motor_turn(payload)
    ):
        return body
    want_sse = "event-stream" in (ctype or "").lower() or (body or b"").lstrip().startswith(b"data:")
    inferred = inferred_motor_from_payload(payload, bot)
    tools = _robot_tools_from_body(body)
    name: str | None = None
    args: dict[str, Any] | None = None
    for tool_name, tool_args in tools:
        if robot_args_usable(tool_name, tool_args):
            name, args = tool_name, tool_args
            break
    if name is None and tools:
        tool_name, tool_args = tools[0]
        merged = fill_robot_tool_args(tool_name, tool_args, inferred)
        if robot_args_usable(tool_name, merged):
            name, args = tool_name, merged
            print(f"[deskd] filled empty {tool_name} {args}", flush=True)
        else:
            hit = motor_cmd_to_openai_tool(inferred)
            if hit:
                name, args = hit
                print(f"[deskd] replaced empty {tool_name} with inferred {name} {args}", flush=True)
    if name is None:
        promoted = promote_text_to_tool_call(_assistant_content_from_body(body), None)
        if promoted:
            fn = promoted.get("function") if isinstance(promoted.get("function"), dict) else {}
            tool_name = canonicalize_tool_name(str(fn.get("name") or ""), None)
            tool_args = _parse_tool_args(fn.get("arguments"))
            if robot_args_usable(tool_name, tool_args):
                name, args = tool_name, tool_args
            else:
                merged = fill_robot_tool_args(tool_name, tool_args, inferred)
                if robot_args_usable(tool_name, merged):
                    name, args = tool_name, merged
    if name is None or args is None:
        fb = fallback_motor_completion(payload, served=served, bot=bot)
        if fb:
            print("[deskd] replacing empty completion with motor fallback", flush=True)
            return openai_completion_to_sse(fb) if want_sse else fb
        return body
    print(f"[deskd] motor after chat {name} {args}", flush=True)
    raw = openai_robot_tool_completion(name, args, served=served)
    return openai_completion_to_sse(raw) if want_sse else raw


def spoken_from_motor_json(text: str) -> str | None:
    value = _json_value_from_text(text)
    if value is None:
        return None
    hit = motor_value_to_tool(value, None)
    if not hit:
        return None
    _name, args = hit
    why = str(args.get("why") or "").strip()
    if why:
        return _period(why)
    if str(args.get("cmd") or "") == "plan":
        spoken = robot_sim.plan_speech(args)
        if spoken:
            return _period(spoken)
    pose = str(args.get("pose") or "").strip().lower()
    if pose == "wave":
        return "I'm waving."
    if pose:
        return f"Okay — {pose.replace('_', ' ')}."
    cmd = str(args.get("cmd") or "").strip().lower()
    direction = str(args.get("direction") or "").strip().lower()
    if cmd in {"walk", "walking"} or cmd.startswith("walk"):
        if direction in {"left", "right", "back"}:
            return f"I'm walking {direction}."
        return "I'm walking."
    if cmd:
        return f"Okay — {cmd.replace('_', ' ')}."
    return "Okay, I moved."


def motor_value_to_tool(value: Any, allowed: list[str] | None) -> tuple[str, dict[str, Any]] | None:
    """Map a JSON object or array of tool dumps onto one MiniOS robot tool."""
    if isinstance(value, dict):
        return motor_json_to_tool(value, allowed)
    if not isinstance(value, list):
        return None
    steps: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        hit = motor_json_to_tool(item, allowed)
        if not hit:
            continue
        name, args = hit
        if str(args.get("cmd") or "") == "plan" and isinstance(args.get("steps"), list):
            steps.extend(s for s in args["steps"] if isinstance(s, dict))
            continue
        if "robot_pose" in name and args.get("pose"):
            steps.append({"cmd": "pose", "pose": args["pose"]})
        elif "robot_joint" in name:
            step = {"cmd": "joint"}
            step.update(args)
            steps.append(step)
        elif "robot_motion" in name and args.get("cmd"):
            steps.append(dict(args))
    if not steps:
        return None
    if len(steps) == 1:
        return motor_cmd_to_openai_tool(steps[0])
    return motor_cmd_to_openai_tool({"cmd": "plan", "steps": steps})


def _xml_tool_value_from_text(text: str) -> Any:
    """Parse Qwen/Hermes XML function-call markup into a JSON tool object."""
    if not text:
        return None
    xml = re.search(
        r"<function\s*=\s*([^\s>]+)>(.*?)</function>",
        text,
        re.I | re.S,
    )
    if not xml:
        return None
    name = xml.group(1).strip()
    args: dict[str, Any] = {}
    for pm in re.finditer(
        r"<parameter\s*=\s*([^\s>]+)>\s*(.*?)\s*</parameter>",
        xml.group(2),
        re.I | re.S,
    ):
        raw = pm.group(2).strip()
        try:
            args[pm.group(1)] = json.loads(raw)
        except json.JSONDecodeError:
            args[pm.group(1)] = raw
    return {"name": name, "arguments": args} if name else None


def promote_text_to_tool_call(text: str, allowed: list[str] | None) -> dict[str, Any] | None:
    """If Qwen printed motor JSON/XML as chat text, turn it into an OpenAI tool_call."""
    value = _xml_tool_value_from_text(text)
    if value is None:
        value = _json_value_from_text(text)
    if value is None:
        return None
    hit = motor_value_to_tool(value, allowed)
    if not hit:
        return None
    name, args = hit
    print(f"[deskd] promoted content JSON to {name} {args}", flush=True)
    return {
        "id": "call_robot",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, separators=(",", ":")),
        },
    }


def _promote_message_content(
    msg: dict[str, Any], allowed: list[str] | None, *, promote_json: bool
) -> bool:
    if msg.get("tool_calls"):
        return False
    raw = str(msg.get("content") or "")
    if not promote_json:
        spoken = spoken_from_motor_json(raw)
        if spoken:
            msg["content"] = spoken
        return False
    call = promote_text_to_tool_call(raw, allowed)
    if not call:
        return False
    msg["tool_calls"] = [call]
    msg["content"] = ""
    return True


def rewrite_completion_tool_names(
    obj: Any,
    allowed: list[str] | None,
    *,
    promote_json: bool = True,
    intent: str = "",
    extra_text: str = "",
    fill_empty_shell: bool = True,
) -> Any:
    """Rewrite tool_call names in an OpenAI chat.completion or stream delta."""
    if not isinstance(obj, dict):
        return obj
    allowed = list(allowed or [])
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict):
            for call in msg.get("tool_calls") or []:
                _rewrite_tool_call_entry(call, allowed, complete=True)
            if isinstance(msg.get("content"), str):
                msg["content"] = rewrite_aliased_tool_names_in_text(msg["content"], allowed)
            if _promote_message_content(msg, allowed, promote_json=promote_json):
                choice["finish_reason"] = "tool_calls"
        delta = choice.get("delta")
        if isinstance(delta, dict):
            for call in delta.get("tool_calls") or []:
                _rewrite_tool_call_entry(call, allowed, complete=False)
            if isinstance(delta.get("content"), str):
                delta["content"] = rewrite_aliased_tool_names_in_text(delta["content"], allowed)
    drop_junk_tool_calls(obj)
    fill_empty_agent_tool_calls(
        obj,
        intent=intent,
        extra_text=extra_text,
        message_only=True,
        fill_empty_shell=fill_empty_shell,
    )
    return obj


def _sse_data_payload(line: str) -> tuple[str, str]:
    payload = line[5:].lstrip()
    ended = ""
    if payload.endswith("\r\n"):
        ended = "\r\n"
        payload = payload[:-2]
    elif payload.endswith("\n"):
        ended = "\n"
        payload = payload[:-1]
    return payload, ended


def _tool_call_fingerprint(call: Any) -> tuple[str, str]:
    if not isinstance(call, dict):
        return ("", "")
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or call.get("name") or "").strip()
    args = _parse_tool_args(fn.get("arguments") if fn else call.get("arguments"))
    try:
        dumped = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        dumped = str(args or "")
    return (name, dumped)


def last_assistant_tool_fingerprints(payload: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Previous-round tool calls, so we can stop the local model repeating them."""
    out: set[tuple[str, str]] = set()
    if not isinstance(payload, dict):
        return out
    for msg in reversed(payload.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            fp = _tool_call_fingerprint(call)
            if fp[0]:
                out.add(fp)
        break
    return out


def filter_repeated_tool_calls(
    calls: list[dict[str, Any]], prior: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    if not prior:
        return calls
    kept: list[dict[str, Any]] = []
    for call in calls:
        fp = _tool_call_fingerprint(call)
        if fp[0] and fp in prior:
            continue
        kept.append(call)
    return kept


_BG_TASK_TOOLS = frozenset(
    {
        "get_command_or_subagent_output",
        "kill_command_or_subagent",
        "wait_commands_or_subagents",
    }
)
_TASK_ID_RE = re.compile(
    r'(?i)(?:task_id|taskId|subagent_id)\s*["\s:=]+["\']?([0-9A-Za-z_-]{8,})'
)


def _tool_call_short_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or call.get("name") or "").strip()
    return name.split("__")[-1]


def _tool_call_task_ids(call: Any) -> list[str]:
    if not isinstance(call, dict):
        return []
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    args = _parse_tool_args(fn.get("arguments") if fn else call.get("arguments"))
    out: list[str] = []
    tid = args.get("task_id")
    if tid:
        out.append(str(tid).strip())
    raw = args.get("task_ids")
    if isinstance(raw, list):
        out.extend(str(x).strip() for x in raw if x)
    elif isinstance(raw, str) and raw.strip():
        out.append(raw.strip())
    return [x for x in out if x]


def known_background_task_ids(payload: dict[str, Any] | None) -> set[str]:
    """Task ids actually returned by earlier tools in this prompt."""
    ids: set[str] = set()
    if not isinstance(payload, dict):
        return ids
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in {"tool", "assistant"}:
            for m in _TASK_ID_RE.finditer(_flatten_message_text(msg)):
                ids.add(m.group(1).rstrip("\"',}"))
        for call in msg.get("tool_calls") or []:
            if _tool_call_short_name(call) in _BG_TASK_TOOLS:
                continue
            for tid in _tool_call_task_ids(call):
                ids.add(tid)
    return ids


def filter_orphan_background_task_calls(
    calls: list[dict[str, Any]], known_ids: set[str] | None
) -> list[dict[str, Any]]:
    """Drop get/kill/wait task tools whose ids were never issued this turn."""
    if known_ids is None:
        return calls
    known = set(known_ids)
    kept: list[dict[str, Any]] = []
    for call in calls:
        if _tool_call_short_name(call) not in _BG_TASK_TOOLS:
            kept.append(call)
            continue
        want = _tool_call_task_ids(call)
        if want and known and any(tid in known for tid in want):
            kept.append(call)
            continue
        print(
            f"[deskd] dropped orphan background task call "
            f"{_tool_call_short_name(call)} ids={want or ['∅']}",
            flush=True,
        )
    return kept


def _sanitize_local_tool_calls(
    calls: list[dict[str, Any]],
    *,
    prior_tools: set[tuple[str, str]] | None = None,
    known_task_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    calls = filter_repeated_tool_calls(calls, prior_tools or set())
    return filter_orphan_background_task_calls(calls, known_task_ids)


def rewrite_llm_response_body(
    body: bytes,
    ctype: str,
    allowed: list[str] | None,
    *,
    promote_json: bool = True,
    intent: str = "",
    prior_tools: set[tuple[str, str]] | None = None,
    known_task_ids: set[str] | None = None,
    fill_empty_shell: bool = True,
) -> bytes:
    """Fix local-model tool names so Hermes can dispatch MCP robot_* calls."""
    if not body:
        return body
    allowed = list(allowed or _DEFAULT_HERMES_MOTOR_TOOLS)
    text = rewrite_aliased_tool_names_in_text(body.decode("utf-8", "replace"), allowed)
    ct = (ctype or "").lower()
    if "text/event-stream" in ct or text.lstrip().startswith("data:"):
        rebuilt: list[str] = []
        content_parts: list[str] = []
        stream_objs: list[dict[str, Any]] = []
        saw_tool = False
        template: dict[str, Any] | None = None
        for line in text.splitlines(keepends=True):
            if not line.startswith("data:"):
                rebuilt.append(line)
                continue
            payload, ended = _sse_data_payload(line)
            if payload.strip() == "[DONE]":
                rebuilt.append(line)
                continue
            if not payload.strip():
                rebuilt.append(line)
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                rebuilt.append(line)
                continue
            rewrite_completion_tool_names(
                obj,
                allowed,
                promote_json=promote_json,
                intent=intent,
                fill_empty_shell=fill_empty_shell,
            )
            if isinstance(obj, dict):
                template = template or obj
                stream_objs.append(obj)
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                    if delta.get("tool_calls") or msg.get("tool_calls"):
                        saw_tool = True
                    if isinstance(delta.get("content"), str):
                        content_parts.append(delta["content"])
                    if isinstance(msg.get("content"), str):
                        content_parts.append(msg["content"])
            rebuilt.append("data: " + json.dumps(obj, separators=(",", ":")) + ended)
        if not saw_tool:
            assembled = "".join(content_parts)
            call = promote_text_to_tool_call(assembled, allowed) if promote_json else None
            if not promote_json:
                spoken = spoken_from_motor_json(assembled)
                if spoken:
                    base = template or {}
                    chunk = {
                        "id": base.get("id") or "chatcmpl-robot",
                        "object": "chat.completion.chunk",
                        "created": base.get("created") or 0,
                        "model": base.get("model") or "",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": spoken},
                                "finish_reason": None,
                            }
                        ],
                    }
                    done = {
                        "id": chunk["id"],
                        "object": "chat.completion.chunk",
                        "created": chunk["created"],
                        "model": chunk["model"],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    return (
                        "data: "
                        + json.dumps(chunk, separators=(",", ":"))
                        + "\n\n"
                        + "data: "
                        + json.dumps(done, separators=(",", ":"))
                        + "\n\n"
                        + "data: [DONE]\n\n"
                    ).encode()
            if call:
                calls = _sanitize_local_tool_calls(
                    [{"index": 0, **call}],
                    prior_tools=prior_tools,
                    known_task_ids=known_task_ids,
                )
                if not calls:
                    print("[deskd] dropped invalid tool call; ending turn", flush=True)
                    return sse_stop_body(template)
                call = {k: v for k, v in calls[0].items() if k != "index"}
                base = template or {}
                chunk = {
                    "id": base.get("id") or "chatcmpl-robot",
                    "object": "chat.completion.chunk",
                    "created": base.get("created") or 0,
                    "model": base.get("model") or "",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [{"index": 0, **call}],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                done = {
                    "id": chunk["id"],
                    "object": "chat.completion.chunk",
                    "created": chunk["created"],
                    "model": chunk["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
                return (
                    "data: "
                    + json.dumps(chunk, separators=(",", ":"))
                    + "\n\n"
                    + "data: "
                    + json.dumps(done, separators=(",", ":"))
                    + "\n\n"
                    + "data: [DONE]\n\n"
                ).encode()
        if saw_tool:
            calls = assemble_stream_tool_calls(
                stream_objs,
                allowed,
                intent=intent,
                extra_text="".join(content_parts),
                fill_empty_shell=fill_empty_shell,
            )
            calls = _sanitize_local_tool_calls(
                calls, prior_tools=prior_tools, known_task_ids=known_task_ids
            )
            if calls:
                return sse_tool_calls_body(template, calls)
            if prior_tools or known_task_ids is not None:
                print("[deskd] dropped invalid tool call; ending turn", flush=True)
                return sse_stop_body(template)
        return "".join(rebuilt).encode("utf-8")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text.encode("utf-8")
    rewrite_completion_tool_names(
        obj,
        allowed,
        promote_json=promote_json,
        intent=intent,
        fill_empty_shell=fill_empty_shell,
    )
    if (prior_tools or known_task_ids is not None) and isinstance(obj, dict):
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message")
            if not isinstance(msg, dict):
                continue
            calls = msg.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            kept = _sanitize_local_tool_calls(
                calls, prior_tools=prior_tools, known_task_ids=known_task_ids
            )
            if len(kept) == len(calls):
                continue
            print("[deskd] dropped invalid tool call; ending turn", flush=True)
            if kept:
                msg["tool_calls"] = kept
            else:
                msg.pop("tool_calls", None)
                choice["finish_reason"] = "stop"
    return json.dumps(obj).encode("utf-8")


def restrict_motor_tools(
    payload: dict[str, Any],
    *,
    allow_observe: bool = False,
    applied: bool = False,
) -> dict[str, Any]:
    """Keep robot tools for free-form NL. After a server apply, omit tools (vLLM 400s on [])."""
    payload.pop("tool_choice", None)
    if applied:
        payload.pop("tools", None)
        return payload
    keep = _VISION_MOTOR_KEEP if allow_observe else _MOTOR_TOOL_KEEP
    tools = payload.get("tools")
    if isinstance(tools, list):
        kept = [t for t in tools if keep.search(_openai_tool_name(t))]
        if kept:
            payload["tools"] = kept
        else:
            payload.pop("tools", None)
    return payload


def strip_body_tools(payload: dict[str, Any]) -> dict[str, Any]:
    """Hermes Agent agents must not see robot / Teela body tools."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    kept = [t for t in tools if not _MOTOR_TOOL_KEEP.search(_openai_tool_name(t))]
    if kept:
        payload["tools"] = kept
    else:
        payload.pop("tools", None)
    return payload


_MOTOR_JSON_SYS = (
    "Convert a natural-language body command into JSON for a 16-joint humanoid. "
    "Reply with ONLY JSON, no markdown. "
    'Schema: {"cmd":"pose","pose":"<name>"} or {"cmd":"joint","joints":{"<joint>":<deg>}} '
    'or {"cmd":"walk"|"stop"|"demo"|"reset"|"estop_on"|"estop_off"} '
    'or {"cmd":"plan","why":"<one sentence>","steps":[{"cmd":"pose","pose":"sit"},{"cmd":"pose","pose":"wave"}]} '
    "or {\"cmd\":null}. "
    "Use a plan (max 4 steps) when they asked for more than one thing, or an open request like get comfortable or greet me. You choose how. "
    "Poses: " + ", ".join(robot_sim.POSES) + ". "
    "Joints and [min,max]: "
    + ", ".join(f"{n}[{lo},{hi}]" for n, (lo, hi) in robot_sim.JOINTS.items())
    + ". "
    "Facing the user, left/right mean the arm on that side of the SCREEN (same as Head Left/Right). "
    "So 'left arm' is the arm on the left of the front view. "
    "Follow the conversation. Pronouns and follow-ups (it, that, the other one, now forward, again, a bit higher) "
    "refer to the previous turns and the live pose. "
    "Nudge from live values; do not snap to 145 unless they said all the way up. "
    "If they are only chatting and not asking for a body action, {\"cmd\":null}. "
    "Arm FORWARD means the Body Actions Arms Forward pose: shoulder 78, elbow 8, arm_out 0. Never use 142/145 for forward. "
    "Arm UP/RAISE/OVERHEAD means Body Actions Arms Up: shoulder 142. "
    "Arm OUT means out to the side (T-pose): shoulder_out 90. "
    "move left arm forward -> {\"cmd\":\"joint\",\"joints\":{\"right_shoulder\":78,\"right_elbow\":8,\"right_shoulder_out\":0}}. "
    "move arm forward -> {\"cmd\":\"joint\",\"joints\":{\"right_shoulder\":78,\"right_elbow\":8,\"right_shoulder_out\":0}}. "
    "raise the arm on the right of the screen -> left_shoulder 142. "
    "lean forward -> {\"cmd\":\"joint\",\"joints\":{\"upper_back_pitch\":18}}. "
    "wave -> {\"cmd\":\"pose\",\"pose\":\"wave\"}. "
    "If the user did not clearly ask to move a limb or pose, {\"cmd\":null}. "
    "Singular hand/arm (raise your hand, raise a hand, lift your arm) moves only the right arm. "
    "Never raise both arms unless they said both, or used plural hands/arms."
)


def _parse_motor_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return robot_sim.validate_command(obj if isinstance(obj, dict) else None)


def _motor_nl_user(
    text: str,
    state: dict[str, Any] | None,
    prior: str | None,
    history: str | None = None,
) -> str:
    bits = [text.strip()]
    if history:
        bits.append("Recent conversation:\n" + history.strip())
    elif prior:
        bits.append("Previous user turn: " + prior.strip())
    spoken = robot_sim.describe_body(state)
    if spoken:
        bits.append("Your body in ordinary words (this is you): " + spoken)
    st = state if isinstance(state, dict) else {}
    joints = robot_sim.tracking_snapshot(st).get("live") or {}
    live = []
    for name in robot_sim.JOINTS:
        try:
            v = float(joints.get(name) or 0)
        except (TypeError, ValueError):
            continue
        if abs(v) >= 1:
            live.append(f"{name}={v:.0f}")
    if live:
        bits.append("Live joints: " + ", ".join(live))
    pose = str(st.get("pose") or "")
    motion = str(st.get("motion") or "")
    if pose or motion:
        bits.append(f"Pose: {pose or 'home'} motion: {motion or 'idle'}")
    return "\n".join(bits)


def interpret_motor_nl(
    text: str,
    state: dict[str, Any] | None = None,
    prior: str | None = None,
    history: str | None = None,
    *,
    think: bool = False,
) -> dict[str, Any] | None:
    """Map free-form body language onto a pose/joint command. 8B first; 27B if think=True."""
    payload = {
        "model": LOCAL_LLM_SERVED if think else LOCAL_LLM_FAST_SERVED,
        "temperature": 0,
        "max_tokens": 220,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": _MOTOR_JSON_SYS},
            {"role": "user", "content": _motor_nl_user(text, state, prior, history)},
        ],
    }
    raw = json.dumps(payload).encode()
    url = (LOCAL_LLM_UPSTREAM if think else LOCAL_LLM_FAST_UPSTREAM) + "/v1/chat/completions"
    try:
        req = urllib.request.Request(
            url,
            data=raw,
            method="POST",
            headers=_llm_headers(url, {"Content-Type": "application/json"}),
        )
        with hybrid_exclusive("think" if think else "fast"):
            with urllib.request.urlopen(req, timeout=20 if think else 12) as resp:
                data = json.loads(resp.read().decode() or "{}")
    except Exception:
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return _parse_motor_json(str(content or ""))


def interpret_motor_nl_think(
    text: str,
    state: dict[str, Any] | None = None,
    prior: str | None = None,
    history: str | None = None,
) -> dict[str, Any] | None:
    """One 27B JSON pass when VL-8B cannot map a clear body request."""
    return interpret_motor_nl(text, state, prior, history, think=True)


def prior_user_intent(bot: Any) -> str | None:
    """Previous user line, skipping the turn already appended for this prompt."""
    msgs = getattr(bot, "messages", None) or []
    users: list[str] = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        text = user_intent_text(str(m.get("text") or "")).strip()
        if text:
            users.append(text)
    if len(users) >= 2:
        return users[-2]
    return None


def resolve_motor_command(
    text: str,
    state: dict[str, Any] | None = None,
    prior_user: str | None = None,
    history: str | None = None,
    gestures: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Phrase map, then VL-8B JSON; 27B only if 8B returns nothing on a clear body request."""
    cleaned = visible_user_text(text)
    if robot_sim.looks_like_body_query(cleaned):
        return None
    named = robot_sim.command_from_named_gesture(cleaned, gestures)
    if named:
        return named
    hit = robot_sim.infer_command(cleaned, state, prior=prior_user)
    if hit:
        return robot_sim.ensure_motor_matches_text(hit, cleaned, state)
    follow = robot_sim.is_delta_followup(cleaned)
    if prior_user and _CONFIRM_TURN.match(cleaned.strip()):
        hit = robot_sim.infer_command(prior_user, state)
        if hit:
            return robot_sim.ensure_motor_matches_text(hit, prior_user, state)
    prior_motor = bool(prior_user and robot_sim.looks_like_motor(prior_user, state, gestures=gestures))
    ongoing = robot_sim.looks_like_motor(cleaned, state, prior_user, gestures) or (follow and prior_motor)
    if not ongoing:
        return None
    fast = interpret_motor_nl(cleaned, state, prior_user, history)
    if fast:
        return robot_sim.ensure_motor_matches_text(fast, cleaned, state)
    thought = interpret_motor_nl_think(cleaned, state, prior_user, history)
    return robot_sim.ensure_motor_matches_text(thought, cleaned, state) if thought else None


_BODY_STATUS_MARK = "[[minios-body-status]]"
_BODY_LIVE_MARK = "[[minios-body-live]]"
_BODY_MARK_SPLIT = re.compile(r"\[\[minios-body-(?:live|applied|status|sense)\]\]")


def user_intent_text(text: str) -> str:
    """User words only — strip live-body grounding and motor-tool instructions."""
    text = text or ""
    tagged = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S | re.I)
    if tagged:
        text = tagged.group(1)
    text = _BODY_MARK_SPLIT.split(text, maxsplit=1)[0].strip()
    changed = True
    while changed:
        changed = False
        for prefix in (
            _MOTOR_FIRST_TOOL,
            _MOTOR_ALREADY_MOVED,
            _SEE_THEN_MOVE,
            _BODY_QUERY_SPEAK,
            _MIXED_FIRST_TOOL,
            _SYSTEM_CHECK_FIRST_TOOL,
            _SYSTEM_CHECK_AND_MOVE,
            _TALK_SPEAK,
            _DESKTOP_FIRST_TOOL,
        ):
            p = (prefix or "").strip()
            if p and text.startswith(p):
                text = text[len(p) :].lstrip("\n ").strip()
                changed = True
    if text and robot_sim.infer_command(text) is None:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and robot_sim.infer_command(lines[-1]) is not None:
            return lines[-1]
    return text


def motor_intent_from_bot(bot: Any) -> str:
    """Latest user motor request — the words the model just processed."""
    if bot is None:
        return ""
    raw = str(getattr(bot, "_motor_user", "") or "")
    intent = user_intent_text(raw)
    if intent:
        return intent
    for msg in reversed(getattr(bot, "messages", None) or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = user_intent_text(str(msg.get("text") or msg.get("content") or ""))
        if text:
            return text
    return ""


def fill_robot_action_from_intent(bot: Any, body: dict[str, Any] | None) -> dict[str, Any]:
    """If MCP called robot_pose with no pose, use the chat the model just processed.

    Only fills cmd=pose. Live MiniOS telemetry uses cmd=joints/live/status and
    must not be rewritten into a gesture.
    """
    body = dict(body or {})
    cmd = str(body.get("cmd") or body.get("command") or "").strip().lower()
    if cmd != "pose":
        return body
    pose = str(body.get("pose") or body.get("name") or "").strip().lower().replace(" ", "_")
    if pose in robot_sim.POSES:
        return body
    intent = motor_intent_from_bot(bot)
    hit = robot_sim.infer_command(intent, getattr(bot, "robot_state", None)) if intent else None
    inferred = str((hit or {}).get("pose") or "").strip().lower().replace(" ", "_")
    if inferred in robot_sim.POSES:
        body["pose"] = inferred
        print(f"[deskd] robot_pose missing; using {inferred!r} from chat", flush=True)
    return body


def queued_motor_future(bot: Any) -> str | None:
    queue = getattr(bot, "_prompt_queue", None) if bot is not None else None
    if not queue:
        return None
    try:
        lock = getattr(bot, "_prompt_lock", None)
        items = list(queue) if lock is None else None
        if lock is not None:
            with lock:
                items = list(queue)
    except Exception:
        return None
    for text, _images in items or []:
        cmd = robot_sim.infer_command(visible_user_text(str(text or "")))
        if cmd:
            return robot_sim.future_clause(cmd)
    return None


def live_body_block(state: dict[str, Any] | None, *, kind: str = "live", upcoming: str | None = None) -> str:
    which = "commanded" if kind == "applied" else "live"
    feel = robot_sim.describe_body(state, which=which)
    timed = robot_sim.describe_body_timed(state, upcoming=upcoming, which=which)
    if kind == "applied":
        return (
            f"{_BODY_APPLIED_MARK} "
            f"PROPRIOCEPTION after that move — I feel: {feel} "
            f"{_SHORT_CHAT} "
            "You already moved. Speak from I feel. "
            f"Timeline: {timed}"
        )
    if kind == "query":
        return (
            f"{_BODY_STATUS_MARK} "
            f"PROPRIOCEPTION — I feel right now: {feel} "
            f"{_SHORT_CHAT} "
            "Answer from I feel, not from memory. "
            f"Timeline: {timed}"
        )
    return (
        f"{_BODY_LIVE_MARK} "
        f"PROPRIOCEPTION (not the user) — I feel right now: {feel} "
        f"{_SHORT_CHAT} "
        "This sense is your body. Chat history is not. "
        "If they asked you to move, call bot_desktop__robot_pose / bot_desktop__robot_joint / "
        "bot_desktop__robot_motion after you understand (exact tool names from the tools list). "
        "You have not moved yet. Their words do not move your body by themselves. "
        f"Timeline: {timed}"
    )


def with_live_body(
    user_text: str,
    state: dict[str, Any] | None,
    *,
    kind: str = "live",
    upcoming: str | None = None,
) -> str:
    text = user_text or ""
    if _BODY_LIVE_MARK in text or _BODY_APPLIED_MARK in text or _BODY_STATUS_MARK in text:
        return text
    return f"{text}\n\n{live_body_block(state, kind=kind, upcoming=upcoming)}"


def with_virtual_feel(bot: Any, state: dict[str, Any] | None) -> dict[str, Any]:
    return virtual_body.overlay(str(getattr(bot, "id", "") or "") if bot is not None else "", state)


def body_query_prompt(user_text: str, state: dict[str, Any] | None) -> str:
    """Ground a pose question in the live MiniOS twin."""
    want_deg = bool(re.search(r"\b(?:degree|degrees|angle|angles|joint|joints|telemetry)\b", user_text or "", re.I))
    snapshot = robot_sim.describe_body(state, want_degrees=True)
    spoken = robot_sim.describe_body(state, want_degrees=want_deg)
    extra = "" if want_deg else " Do not list joint names or degrees unless they asked."
    return (
        f"{user_text}\n\n"
        f"{live_body_block(state, kind='query')}{extra} "
        f"(internal: {snapshot})"
    )


def motor_followup_prompt(
    user_text: str,
    motor: dict[str, Any],
    result: dict[str, Any],
    *,
    upcoming: str | None = None,
) -> str:
    """Keep the user's words. Twin already moved. Chat from the timed live snapshot."""
    del motor
    return with_live_body(
        user_text,
        result if isinstance(result, dict) else None,
        kind="applied",
        upcoming=upcoming,
    )


def _strip_body_grounding_message(msg: dict[str, Any]) -> None:
    """Drop stale live-body dumps from a chat turn so only fresh proprioception remains."""
    content = msg.get("content")
    if isinstance(content, str):
        if _BODY_MARK_SPLIT.search(content):
            kept = _BODY_MARK_SPLIT.split(content, maxsplit=1)[0].strip()
            if kept:
                msg["content"] = kept
        return
    if not isinstance(content, list):
        return
    out: list[Any] = []
    for part in content:
        if isinstance(part, str):
            if _BODY_MARK_SPLIT.search(part):
                kept = _BODY_MARK_SPLIT.split(part, maxsplit=1)[0].strip()
                if kept:
                    out.append(kept)
            else:
                out.append(part)
            continue
        if isinstance(part, dict) and part.get("type") in (None, "text") and "text" in part:
            text = str(part.get("text") or "")
            if _BODY_MARK_SPLIT.search(text):
                kept = _BODY_MARK_SPLIT.split(text, maxsplit=1)[0].strip()
                if kept:
                    out.append({**part, "text": kept})
            else:
                out.append(part)
            continue
        out.append(part)
    msg["content"] = out


def _merge_proprioception_system(msg: dict[str, Any], block: str) -> None:
    prev = _flatten_message_text(msg)
    base = _SENSE_SECTION.sub("", prev or "").rstrip()
    merged = f"{base}\n\n{block}".strip() if base else block
    msg["content"] = merged


def inject_live_body(payload: dict[str, Any], bot: Any) -> dict[str, Any]:
    """Put live proprioception in the system turn (Qwen allows only one system message)."""
    if bot is None or bot_kind_is_agent(bot):
        return payload
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    kind = "live"
    for msg in reversed(msgs):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _flatten_message_text(msg)
        if _BODY_APPLIED_MARK in text:
            kind = "applied"
        break
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("role") in {"user", "assistant"}:
            _strip_body_grounding_message(msg)
    block = proprioception_block(
        virtual_body.overlay(str(getattr(bot, "id", "") or ""), getattr(bot, "robot_state", None)),
        kind=kind,
        upcoming=queued_motor_future(bot),
    )
    block = f"{block}\n{teela_now_block(bot)}"
    sys_i = next(
        (i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "system"),
        None,
    )
    if sys_i is None:
        msgs.insert(0, {"role": "system", "content": block})
    else:
        _merge_proprioception_system(msgs[sys_i], block)
    payload["messages"] = msgs
    return payload


def _prepend_user_text(msg: dict[str, Any], prefix: str) -> None:
    content = msg.get("content")
    if isinstance(content, str):
        if prefix not in content:
            msg["content"] = prefix + "\n\n" + content
        return
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text") and "text" in part:
                text = str(part.get("text") or "")
                if prefix not in text:
                    part["text"] = prefix + "\n\n" + text
                return
        content.insert(0, {"type": "text", "text": prefix})
        msg["content"] = content
        return
    msg["content"] = prefix


def estimate_local_prompt_tokens(payload: dict[str, Any] | None) -> int:
    """Overestimate prompt tokens so max_tokens + prompt cannot exceed the live window."""
    payload = payload or {}
    bits: list[str] = []
    for msg in payload.get("messages") or []:
        bits.append(_flatten_message_text(msg))
    tools = payload.get("tools")
    if tools:
        try:
            bits.append(json.dumps(tools, separators=(",", ":")))
        except Exception:
            bits.append(str(tools))
    blob = "\n".join(b for b in bits if b)
    counted = tel.count_tokens_local(blob) if blob else 0
    by_chars = (len(blob) + 2) // 3 if blob else 0
    n = max(counted, by_chars)
    n += n // 8
    n += 64 + 24 * max(1, len(payload.get("messages") or []))
    n += 16 * len(payload.get("tools") or [])
    return n


def parse_context_overflow_details(text: str) -> tuple[int, int, int] | None:
    m = _CTX_OVERFLOW_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_context_overflow(text: str) -> int | None:
    """If vLLM rejected prompt+max_tokens, return a max_tokens that fits."""
    details = parse_context_overflow_details(text)
    if not details:
        return None
    ctx, _out, inp = details
    room = ctx - inp - 8
    return room if room >= 1 else None


def _set_message_text(msg: dict[str, Any], text: str) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        kept: list[Any] = [{"type": "text", "text": text}]
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                kept.append(part)
        msg["content"] = kept
        return
    msg["content"] = text


_HERMES_CLI_KEEP_RE = re.compile(
    r"(Follow AGENTS\.md|You have a body\.|You have a live Agent Computer)",
    re.I,
)


def _shrink_system_text(text: str) -> str:
    """Drop Hermes Agent CLI boilerplate; MiniOS identity starts at AGENTS.md / body."""
    m = _HERMES_CLI_KEEP_RE.search(text or "")
    if m and m.start() > 400:
        return (text or "")[m.start() :].strip()
    return text or ""


def _clip_system_text(text: str, max_chars: int) -> str:
    text = _shrink_system_text(text)
    if max_chars < 32 or len(text) <= max_chars:
        return text[:max_chars] if max_chars > 0 else text
    head_n = min(1800, max(400, max_chars // 3))
    if head_n + 16 >= max_chars:
        return "…\n" + text[-max_chars:]
    tail_n = max_chars - head_n - 8
    return text[:head_n].rstrip() + "\n…\n" + text[-tail_n:].lstrip()


def local_prefill_budget(payload: dict[str, Any] | None, bot: Any = None) -> int:
    """Prompt-token cap for llama.cpp prefill. Independent of the 262k window."""
    payload = payload or {}
    if local_llm_motor_turn(payload) or orch.classify(last_user_intent_from_payload(payload)).get(
        "mode"
    ) in {"parallel", "after"}:
        return max(256, LOCAL_LLM_MOTOR_PREFILL)
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    coding = bot_kind_is_agent(bot) or len(tools) >= 4 or local_llm_hard_think(payload)
    if coding:
        tools_est = 0
        if tools:
            try:
                blob = json.dumps(tools, separators=(",", ":"))
                tools_est = max(tel.count_tokens_local(blob) if blob else 0, (len(blob) + 2) // 3)
            except Exception:
                tools_est = 80 * len(tools)
        want = max(LOCAL_LLM_CODING_PREFILL, LOCAL_LLM_PREFILL_BUDGET, tools_est + 6144)
        cap = min(65536, max(8192, LOCAL_LLM_MAX_MODEL_LEN // 4))
        return max(256, min(want, cap, max(1024, LOCAL_LLM_MAX_MODEL_LEN - 2048)))
    return max(256, LOCAL_LLM_PREFILL_BUDGET)


def trim_local_llm_payload(
    payload: dict[str, Any],
    *,
    reserve: int | None = None,
    max_prompt: int | None = None,
) -> dict[str, Any]:
    """Shrink messages until the prompt leaves room for a completion."""
    payload = dict(payload)
    msgs = [m for m in (payload.get("messages") or []) if isinstance(m, dict)]
    ctx = max(1024, LOCAL_LLM_MAX_MODEL_LEN)
    reserve = max(256, reserve or min(1024, LOCAL_LLM_MAX_COMPLETION, ctx // 8))
    window_target = max(512, ctx - reserve)
    prefill = max_prompt if max_prompt is not None else local_prefill_budget(payload)
    target = min(window_target, max(256, int(prefill)))

    for msg in msgs:
        if msg.get("role") == "system":
            _set_message_text(msg, _shrink_system_text(_flatten_message_text(msg)))
    payload["messages"] = msgs

    def over() -> bool:
        return estimate_local_prompt_tokens(payload) > target

    def last_user_idx() -> int | None:
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                return i
        return None

    def sys_idx() -> int | None:
        for i, msg in enumerate(msgs):
            if msg.get("role") == "system":
                return i
        return None

    def protected_idxs() -> set[int]:
        keep: set[int] = set()
        si, lu = sys_idx(), last_user_idx()
        if si is not None:
            keep.add(si)
        if lu is not None:
            keep.add(lu)
            for i in range(lu + 1, len(msgs)):
                keep.add(i)
        return keep

    while over() and len(msgs) > 2:
        keep = protected_idxs()
        drop = next((i for i in range(len(msgs)) if i not in keep), None)
        if drop is None:
            break
        msgs.pop(drop)
        payload["messages"] = msgs

    si, lu = sys_idx(), last_user_idx()
    if over() and si is not None:
        others = [m for i, m in enumerate(msgs) if i != si]
        used = estimate_local_prompt_tokens({**payload, "messages": others})
        room_chars = max(800, (target - used) * 3)
        text = _clip_system_text(_flatten_message_text(msgs[si]), room_chars)
        _set_message_text(msgs[si], text)
        payload["messages"] = msgs

    if over() and lu is not None:
        text = _flatten_message_text(msgs[lu])
        if len(text) > 4000:
            intent = user_intent_text(text)
            tail = text[-2500:]
            kept = intent if intent and intent not in tail else ""
            _set_message_text(msgs[lu], (kept + "\n…\n" + tail).strip())
        payload["messages"] = msgs

    if over():
        for i, msg in enumerate(msgs):
            if msg.get("role") != "tool":
                continue
            text = _flatten_message_text(msg)
            if len(text) <= 3500:
                continue
            _set_message_text(msg, text[:1200].rstrip() + "\n…\n" + text[-1800:].lstrip())
        payload["messages"] = msgs

    if over() and si is not None:
        text = _clip_system_text(_flatten_message_text(msgs[si]), 2400)
        _set_message_text(msgs[si], text)
        payload["messages"] = msgs

    if over():
        print(
            f"[deskd] local prompt still est={estimate_local_prompt_tokens(payload)} "
            f"target={target} after trim",
            flush=True,
        )
    return payload


_AFTER_TOOL_RESULTS = (
    "You already have tool results above. Answer the user from those results the way grok-4.6 would: "
    "a complete reply, not another identical tool call. Do not call get_command_or_subagent_output, "
    "wait_commands_or_subagents, or kill_command_or_subagent unless a previous tool result in this turn "
    "returned a task_id. Only call a different tool if you still need a fact that is not in the results."
)


def _nudge_after_tool_results(payload: dict[str, Any]) -> dict[str, Any]:
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return payload
    if not any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs):
        return payload
    last = msgs[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return payload
    if _AFTER_TOOL_RESULTS in _flatten_message_text(last):
        return payload
    payload["messages"] = list(msgs) + [{"role": "user", "content": _AFTER_TOOL_RESULTS}]
    return payload


def apply_local_token_budget(payload: dict[str, Any], bot: Any = None) -> dict[str, Any]:
    """Trim the prompt if needed, then cap completion to the leftover window."""
    payload = trim_local_llm_payload(payload, max_prompt=local_prefill_budget(payload, bot))
    ctx = max(1024, LOCAL_LLM_MAX_MODEL_LEN)
    prompt = estimate_local_prompt_tokens(payload)
    room = ctx - prompt - 16
    cap = max(1, LOCAL_LLM_MAX_COMPLETION)
    floor = max(16, LOCAL_LLM_MIN_COMPLETION)
    if local_llm_motor_turn(payload) or orch.classify(last_user_intent_from_payload(payload)).get(
        "mode"
    ) in {"parallel", "after"}:
        cap = min(cap, max(floor, LOCAL_LLM_MOTOR_MAX_TOKENS))
        floor = min(floor, cap)
    fitted = min(cap, max(floor, room)) if room >= floor else max(16, min(cap, room if room > 0 else floor))
    for key in ("max_tokens", "max_completion_tokens"):
        if key not in payload:
            continue
        try:
            n = int(payload[key])
        except (TypeError, ValueError):
            n = fitted
        payload[key] = min(max(n, floor), fitted)
    if "max_tokens" not in payload and "max_completion_tokens" not in payload:
        payload["max_tokens"] = fitted
    elif "max_tokens" not in payload:
        payload["max_tokens"] = payload.get("max_completion_tokens") or fitted
    return payload


def _http_up(url: str, timeout: float = 0.6) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 500
    except Exception:
        return False


def _nvidia_snapshot() -> list[dict[str, str]]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=3,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            rows.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "mem_used_mib": parts[2],
                    "mem_total_mib": parts[3],
                    "util": parts[4],
                }
            )
    return rows


def _host_mem_gib() -> tuple[float, float] | None:
    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    total = avail = 0.0
    for line in raw.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) / (1024 * 1024)
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1]) / (1024 * 1024)
    if total <= 0:
        return None
    return (round(total, 1), round(avail, 1))


def teela_minios_report(bot: Any) -> dict[str, Any]:
    """MiniOS workspace only: twin, virtual body, observer, desk path."""
    bid = str(getattr(bot, "id", "") or "")
    st = getattr(bot, "robot_state", None)
    if not isinstance(st, dict):
        st = robot_sim.default_state()
    twin = robot_sim.public_status(st)
    twin_ok = bool(twin.get("ok")) and not bool(twin.get("estop")) and bool(twin.get("motors", True))
    virt = virtual_body.latest_state(bid) if bid else {}
    chrome = resolve_chrome_bin()
    chrome_ok = Path(chrome).is_file() and os.access(chrome, os.X_OK)
    obs = getattr(bot, "observer", None)
    obs_running = obs is not None
    obs_healthy = False
    if obs is not None:
        try:
            obs_healthy = bool(obs.healthy())
        except Exception:
            obs_healthy = False
    observer = {
        "ok": bool(obs_healthy),
        "running": obs_running,
        "chrome": chrome_ok,
        "path": chrome,
    }
    ws = str(getattr(bot, "workspace", "") or "")
    pose = twin.get("pose") or "home"
    motion_s = twin.get("motion") or "idle"
    bits = [f"MiniOS twin is {motion_s}, pose {pose}"]
    if twin.get("estop"):
        bits.append("e-stop is on")
    if obs_healthy:
        bits.append("observer is up")
    elif not chrome_ok:
        bits.append("Chrome for MiniOS is missing")
    else:
        bits.append("MiniOS observer is not running")
    if ws:
        bits.append("workspace is on this desk")
    issues: list[str] = []
    if not twin_ok:
        issues.append("twin is not healthy")
    if not obs_healthy:
        issues.append(str(observer.get("reason") or "MiniOS observer down"))
    return {
        "ok": bool(twin_ok),
        "scope": "minios",
        "spoken": "Workspace check: " + "; ".join(bits) + ".",
        "issues": issues,
        "twin": {
            "ok": twin_ok,
            "pose": pose,
            "motion": motion_s,
            "estop": bool(twin.get("estop")),
            "motors": bool(twin.get("motors", True)),
        },
        "virtual": {
            "ok": True,
            "pose": virt.get("pose") if isinstance(virt, dict) else None,
            "action": virt.get("action") if isinstance(virt, dict) else None,
            "mode": virt.get("mode") if isinstance(virt, dict) else "virtual",
        },
        "observer": observer,
        "workspace": ws,
        "surface": getattr(bot, "surface", "") or "",
    }


def teela_host_report(bot: Any) -> dict[str, Any]:
    """teela-brain host only: GPUs, llama, TTS/STT, RAM, cluster. Not MiniOS."""
    origin = cluster.node_name if cluster is not None else "teela-brain"
    model_id = str(getattr(bot, "model", "") or "")
    serving = False
    try:
        serving = bool(local_model_serving(model_id))
    except Exception:
        serving = False
    gpus = _nvidia_snapshot()
    mem = _host_mem_gib()
    llm_up = _http_up("http://127.0.0.1:8081/v1/models") or serving
    voice = vu.health_snapshot()
    tts_up = (voice.get("tts") or {}).get("status") == "ready"
    stt_up = (voice.get("stt") or {}).get("status") == "ready"
    tts_host = (voice.get("tts") or {}).get("host") or vu.host_label(vu.tts_url())
    stt_host = (voice.get("stt") or {}).get("host") or vu.host_label(vu.stt_url())
    peers: list[dict[str, Any]] = []
    if cluster is not None:
        for p in list(getattr(cluster, "peers", None) or []):
            try:
                peers.append(cluster.diagnostics_row(p))
            except Exception:
                continue
    bits = [f"host is {origin}"]
    if serving or llm_up:
        bits.append(f"brain {model_id or 'Qwen'} is up on :8081")
    else:
        bits.append("brain model is not serving")
    if gpus:
        names = ", ".join(f"{g['name']} {g['mem_used_mib']}/{g['mem_total_mib']} MiB" for g in gpus[:2])
        bits.append("GPUs " + names)
    else:
        bits.append("nvidia-smi not available")
    if mem:
        bits.append(f"RAM {mem[1]} of {mem[0]} GiB free")
    bits.append(f"Jade TTS on {tts_host} is ready" if tts_up else f"Jade TTS on {tts_host} is unavailable")
    bits.append(f"Whisper STT on {stt_host} is ready" if stt_up else f"Whisper STT on {stt_host} is unavailable")
    if peers:
        bits.append(f"{len(peers)} cluster peer(s)")
    issues: list[str] = []
    if not (serving or llm_up):
        issues.append("brain model is not serving")
    if not tts_up:
        issues.append("TTS unavailable")
    if not stt_up:
        issues.append("STT unavailable")
    return {
        "ok": bool(serving or llm_up),
        "scope": "host",
        "spoken": "teela-brain check: " + "; ".join(bits) + ".",
        "issues": issues,
        "brain": {
            "ok": bool(serving or llm_up),
            "model": model_id,
            "local_serving": serving,
            "llama_8081": llm_up,
            "node": origin,
        },
        "gpus": gpus,
        "ram_gib": {"total": mem[0], "available": mem[1]} if mem else None,
        "tts": voice.get("tts") or {"host": tts_host, "status": "unavailable"},
        "stt": voice.get("stt") or {"host": stt_host, "status": "unavailable"},
        "cluster": {
            "node": origin,
            "peers": peers,
            "configured": bool(cluster is not None and getattr(cluster, "token", "")),
        },
    }


def teela_system_report(bot: Any, intent: str = "", scope: str = "") -> dict[str, Any]:
    """Read-only health. scope=minios is workspace; host is teela-brain; both combines."""
    want = (scope or "").strip().lower()
    if want not in {"minios", "host", "both"}:
        want = teela_check_scope(intent) if intent else "both"
    if want == "minios":
        return teela_minios_report(bot)
    if want == "host":
        return teela_host_report(bot)
    mini = teela_minios_report(bot)
    host = teela_host_report(bot)
    spoken = str(mini.get("spoken") or "") + " " + str(host.get("spoken") or "")
    issues = list(mini.get("issues") or []) + list(host.get("issues") or [])
    out = {
        "ok": bool(mini.get("ok") and host.get("ok")),
        "scope": "both",
        "spoken": spoken.strip(),
        "issues": issues,
        "minios": mini,
        "host": host,
        "mesh": motion.probe_mesh(cluster, origin=str((host.get("brain") or {}).get("node") or "")),
        "hint": "Workspace check is MiniOS. teela-brain check is the host (GPUs, llama, tunnels).",
    }
    # Keep legacy keys so older callers still see twin/brain.
    out["twin"] = mini.get("twin")
    out["observer"] = mini.get("observer")
    out["brain"] = host.get("brain")
    out["cluster"] = host.get("cluster")
    return out
    bid = str(getattr(bot, "id", "") or "")
    st = getattr(bot, "robot_state", None)
    if not isinstance(st, dict):
        st = robot_sim.default_state()
    twin = robot_sim.public_status(st)
    twin_ok = bool(twin.get("ok")) and not bool(twin.get("estop")) and bool(twin.get("motors", True))
    virt = virtual_body.latest_state(bid) if bid else {}
    activity = orch.snapshot(bid) if bid else {}
    origin = cluster.node_name if cluster is not None else ""
    mesh = motion.probe_mesh(cluster, origin=origin)
    peers: list[dict[str, Any]] = []
    if cluster is not None:
        for p in list(getattr(cluster, "peers", None) or []):
            try:
                peers.append(cluster.diagnostics_row(p))
            except Exception:
                continue
    chrome = resolve_chrome_bin()
    chrome_ok = Path(chrome).is_file() and os.access(chrome, os.X_OK)
    obs = getattr(bot, "observer", None)
    obs_running = obs is not None
    obs_healthy = False
    if obs is not None:
        try:
            obs_healthy = bool(obs.healthy())
        except Exception:
            obs_healthy = False
    observer: dict[str, Any] = {
        "ok": bool(obs_healthy),
        "running": obs_running,
        "chrome": chrome_ok,
        "path": chrome,
    }
    if not chrome_ok:
        observer["reason"] = f"Chrome not found at {chrome}"
    elif not obs_running:
        observer["reason"] = "MiniOS observer not started"
    elif not obs_healthy:
        observer["reason"] = "MiniOS observer is not healthy"
    model_id = str(getattr(bot, "model", "") or "")
    serving = False
    try:
        serving = bool(local_model_serving(model_id))
    except Exception:
        serving = False
    brain = {
        "ok": serving,
        "bot_id": bid,
        "kind": normalize_bot_kind(getattr(bot, "kind", None), default="teela-brain"),
        "model": model_id,
        "local_serving": serving,
        "node": origin or "teela-brain",
    }
    issues: list[str] = []
    if not twin_ok:
        if twin.get("estop"):
            issues.append("e-stop is on")
        elif not twin.get("motors", True):
            issues.append("motors are disabled")
        else:
            issues.append("twin is not healthy")
    if not mesh.get("hardware"):
        issues.append(str(mesh.get("reason") or "physical mesh not attached"))
    if not observer.get("ok"):
        issues.append(str(observer.get("reason") or "MiniOS observer down"))
    if not serving:
        issues.append("brain model is not serving")
    pose = twin.get("pose") or "home"
    motion_s = twin.get("motion") or "idle"
    bits = [f"twin is {motion_s}, pose {pose}"]
    if twin.get("estop"):
        bits.append("e-stop is on")
    if mesh.get("hardware"):
        bits.append("physical mesh is attached")
    else:
        bits.append("physical mesh is not attached")
    if not observer.get("ok"):
        bits.append(str(observer.get("reason") or "observer down"))
    if serving:
        bits.append(f"brain {model_id} is up")
    else:
        bits.append("brain model is not serving")
    return {
        "ok": bool(twin_ok and serving),
        "spoken": "System check: " + "; ".join(bits) + ".",
        "issues": issues,
        "twin": {
            "ok": twin_ok,
            "pose": pose,
            "motion": motion_s,
            "estop": bool(twin.get("estop")),
            "motors": bool(twin.get("motors", True)),
            "battery": twin.get("battery"),
            "temp": twin.get("temp"),
            "load": twin.get("load"),
            "fall_flag": twin.get("fall_flag"),
        },
        "virtual": {
            "ok": True,
            "pose": virt.get("pose") if isinstance(virt, dict) else None,
            "action": virt.get("action") if isinstance(virt, dict) else None,
            "mode": virt.get("mode") if isinstance(virt, dict) else "virtual",
        },
        "activity": activity if isinstance(activity, dict) else {},
        "mesh": mesh,
        "cluster": {
            "node": origin,
            "peers": peers,
            "configured": bool(cluster is not None and getattr(cluster, "token", "")),
        },
        "brain": brain,
        "observer": observer,
        "hint": "Speak from spoken. Physical mesh stays false until teela-jetson / teela-body answer.",
    }


def rewrite_local_llm_chat_payload(payload: dict[str, Any], bot: Any = None) -> dict[str, Any]:
    """Map catalog ids, disable Qwen thinking, and cap completion length for XPU vLLM.

    Thinking is always off for local Qwen: MiniOS body motion was waiting on
    25k-token prefills, not a think block. Motor turns also get a first-tool
    instruction so the model calls robot_* instead of search.
    """
    payload = dict(payload)
    mid = str(payload.get("model") or "")
    mapped = LOCAL_LLM_ALIASES.get(mid.lower(), mid)
    if mapped:
        payload["model"] = mapped
    # vLLM 400s tool_choice=auto unless the server was started with
    # --enable-auto-tool-choice and --tool-call-parser. Keep tools; drop auto.
    if str(payload.get("tool_choice") or "").lower() == "auto":
        payload.pop("tool_choice", None)
    if bot_kind_is_agent(bot):
        # Preserve hermes's thinking/reasoning flags so local sessions match the TUI.
        payload = strip_body_tools(payload)
        payload = canonicalize_payload_tools(payload)
        payload = _nudge_after_tool_results(payload)
        kwargs = payload.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        else:
            kwargs = dict(kwargs)
        effort = normalize_reasoning_effort(getattr(bot, "effort", "") or "")
        if effort == "off":
            kwargs["enable_thinking"] = False
            kwargs.pop("reasoning_effort", None)
            payload.pop("reasoning_effort", None)
        else:
            kwargs.setdefault("enable_thinking", True)
            if effort:
                kwargs["reasoning_effort"] = effort
                payload["reasoning_effort"] = effort
        payload["chat_template_kwargs"] = kwargs
        return apply_local_token_budget(payload, bot)
    kwargs = payload.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
    else:
        kwargs = dict(kwargs)
    effort = QWEN_REASONING_EFFORT if QWEN_REASONING_EFFORT in _QWEN_EFFORTS else "low"
    kwargs["enable_thinking"] = False
    kwargs["reasoning_effort"] = effort
    payload["chat_template_kwargs"] = kwargs
    payload["reasoning_effort"] = effort
    if bot_kind_is_teela(bot):
        payload = ensure_teela_capability_tools(payload)
        hint = teela_capability_hint(last_user_intent_from_payload(payload))
        if hint:
            msgs = payload.get("messages")
            if isinstance(msgs, list):
                for msg in reversed(msgs):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        _prepend_user_text(msg, hint)
                        break
        payload = canonicalize_payload_tools(payload)
        return apply_local_token_budget(payload, bot)
    mixed_hit = orch.classify(last_user_intent_from_payload(payload))
    mixed_mode = mixed_hit.get("mode")
    if mixed_mode in {"parallel", "after"} and not bot_kind_is_teela(bot):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, _MIXED_FIRST_TOOL)
                    break
        payload = ensure_motor_tools(payload, keep_agent=True)
    elif local_llm_motor_turn(payload):
        msgs = payload.get("messages")
        already = payload_already_moved(payload, bot)
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    last_user = _flatten_message_text(msg)
                    intent = user_intent_text(last_user)
                    applied = (
                        already
                        or _BODY_APPLIED_MARK in last_user
                        or _BODY_STATUS_MARK in last_user
                    )
                    see = (not applied) and motion.needs_vision(intent)
                    if applied:
                        _prepend_user_text(msg, _MOTOR_ALREADY_MOVED)
                    else:
                        _prepend_user_text(msg, _SEE_THEN_MOVE if see else _MOTOR_FIRST_TOOL)
                    payload = restrict_motor_tools(payload, allow_observe=see, applied=applied)
                    if not applied:
                        payload = ensure_motor_tools(
                            payload,
                            allow_observe=see,
                            intent=intent,
                            state=getattr(bot, "robot_state", None) if bot is not None else None,
                        )
                    break
        else:
            payload = restrict_motor_tools(payload, applied=already)
            if not already:
                payload = ensure_motor_tools(
                    payload,
                    state=getattr(bot, "robot_state", None) if bot is not None else None,
                )
    elif local_llm_system_check_turn(payload, bot):
        keep_body = mixed_mode == "parallel" and bool(mixed_hit.get("body"))
        cue = _SYSTEM_CHECK_AND_MOVE if keep_body else _SYSTEM_CHECK_FIRST_TOOL
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, cue)
                    break
        payload = ensure_system_check_tools(payload, keep_body=keep_body)
    elif local_llm_after_system_work_turn(payload, bot):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, _MOTOR_FIRST_TOOL)
                    break
        payload = ensure_motor_tools(
            payload,
            state=getattr(bot, "robot_state", None) if bot is not None else None,
        )
    elif local_llm_body_query_turn(payload):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, _BODY_QUERY_SPEAK)
                    break
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
    elif local_llm_talk_turn(payload, bot):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, _TALK_SPEAK)
                    break
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
    elif local_llm_desktop_turn(payload, bot):
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for msg in reversed(msgs):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    _prepend_user_text(msg, _DESKTOP_FIRST_TOOL)
                    break
        payload = ensure_desktop_tools(payload)
    if bot_kind_is_teela(bot) and isinstance(payload.get("tools"), list):
        if local_llm_motor_turn(payload) or local_llm_after_system_work_turn(payload, bot):
            payload = restrict_motor_tools(payload, allow_observe=True)
            if local_llm_after_system_work_turn(payload, bot):
                payload["tool_choice"] = "required"
        elif local_llm_system_check_turn(payload, bot):
            # Keep Hermes Agent tools (grep, read_file, host-shell) for MiniOS and host checks.
            payload.pop("tool_choice", None)
        elif local_llm_desktop_turn(payload, bot):
            payload["tool_choice"] = "required"
        else:
            payload = keep_teela_workspace_tools(payload)
    payload = canonicalize_payload_tools(payload)
    return apply_local_token_budget(payload, bot)


def uses_local_text_llm(model_id: str) -> bool:
    _, catalog = load_user_models()
    raw = catalog.get(model_id)
    tbl: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return is_local_gpu_model(model_id, tbl)


def uses_short_local_chat(bot: Any) -> bool:
    """True when casual chat should hit the selected local engine, not cloud ACP."""
    return uses_local_text_llm(str(getattr(bot, "model", "") or ""))


def local_model_serving(model_id: str, live_ids: tuple[str, ...] | None = None) -> bool:
    """True when this picker id's GPU family is actually up on loopback vLLM."""
    _, catalog = load_user_models()
    raw = catalog.get(model_id)
    tbl: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if not is_local_gpu_model(model_id, tbl):
        return True
    live = probe_local_llm_ids() if live_ids is None else live_ids
    live_fam = _live_gpu_family(live)
    wanted = _picker_gpu_family(model_id, tbl)
    if wanted == "qwen":
        return live_fam in ("qwen", "hybrid")
    if wanted == "vl8":
        return live_fam in ("vl8", "hybrid")
    if wanted == "hybrid":
        return live_fam in ("hybrid", "qwen", "vl8")
    if wanted == "muse":
        return live_fam == "muse"
    if wanted == "llamacpp" or is_llama_cpp_model(model_id, tbl):
        url = str(tbl.get("base_url") or LOCAL_LLM_UPSTREAM)
        if local_llm_port_open(url):
            return True
        live_set = {x.lower() for x in live}
        served = str(tbl.get("model") or model_id).strip().lower()
        return served in live_set or model_id.lower() in live_set
    return bool(live_fam) and live_fam == wanted


def wait_for_local_model(model_id: str, bot: Any = None, timeout: float = 180) -> None:
    """Start the selected local engine if needed and block until its port is up."""
    mid = (model_id or "").strip()
    if not mid or not uses_local_text_llm(mid):
        return
    reset_local_llm_probe()
    if local_model_serving(mid):
        return
    _, catalog = load_user_models()
    raw = catalog.get(mid)
    tbl: dict[str, Any] = raw if isinstance(raw, dict) else {}
    name = str(tbl.get("name") or mid)
    if bot is not None:
        bot.status = f"Starting {name}…"
        emit(
            {
                "type": "status",
                "bot_id": bot.id,
                "text": bot.status,
                "surface": getattr(bot, "surface", "") or "chat",
                "control": getattr(bot, "control", "") or "agent_controlled",
            }
        )
    try:
        local_llm_start(mid)
    except Exception as e:
        msg = str(e).lower()
        if "already" not in msg:
            raise
    deadline = time.time() + max(15.0, float(timeout))
    while time.time() < deadline:
        if bot is not None:
            acp = getattr(bot, "acp", None)
            if acp is not None and getattr(acp, "_turn_cancel", None) is not None and acp._turn_cancel.is_set():
                raise RuntimeError("cancelled")
        reset_local_llm_probe()
        if local_model_serving(mid):
            return
        time.sleep(0.4)
    raise TimeoutError("Local model is still starting. Chat when the picker shows it running.")


def acp_prompt_timeout_error(model_id: str, local_live: bool | None = None) -> TimeoutError:
    """User-facing ACP session/prompt timeout. Name the dead local engine when that is why."""
    if uses_local_text_llm(model_id):
        if local_live is None:
            reset_local_llm_probe()
            local_live = local_model_serving(model_id)
        if not local_live:
            try:
                state = local_llm_engine_state()
            except Exception:
                state = "down"
            _, catalog = load_user_models()
            raw = catalog.get(model_id)
            tbl: dict[str, Any] = raw if isinstance(raw, dict) else {}
            if state == "starting":
                return TimeoutError(
                    "Local model is still starting. Wait a minute and send again."
                )
            return TimeoutError(
                "Local model is not running. Start it and send again."
            )
    return TimeoutError("ACP session/prompt timed out")


def local_llm_prompt_blocks(
    text: str,
    images: list[dict[str, Any]] | None,
    workspace: Path | None = None,
) -> list[dict[str, Any]]:
    """ACP fallback when the child rejects image parts. Pixels are reattached on /v1/llm."""
    bits: list[str] = []
    body = (text or "").strip()
    if body:
        bits.append(body)
    if images:
        bits.append(
            "The user attached media in chat. Pixels are delivered on the local VL path; "
            "do not Read() the binary files."
        )
        for img in images:
            rel = str(img.get("path") or "")
            mime = img.get("mime") or "image"
            facts = ""
            if workspace and rel:
                facts = _image_facts(workspace / rel)
            bits.append(f"- {rel or 'attachment'} ({mime})" + (f": {facts}" if facts else ""))
    note = "\n".join(bits).strip()
    if not note:
        raise RuntimeError("empty prompt")
    return [{"type": "text", "text": note}]


def _xai_api_key() -> str:
    path = USER_AGENT_HOME / "auth.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    for val in data.values():
        if isinstance(val, dict) and val.get("key"):
            return str(val["key"])
    return ""


def describe_image_file(path: Path | None) -> str:
    """Caption a workspace image for a text-only local model."""
    if path is None or not path.is_file():
        return ""
    facts = _image_facts(path)
    caption = _image_caption_xai(path)
    if caption and facts:
        return f"{caption} ({facts})"
    return caption or facts


def _image_facts(path: Path) -> str:
    try:
        from PIL import Image
    except Exception:
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        return f"{path.suffix.lstrip('.').upper() or 'file'}, {size} bytes"
    try:
        with Image.open(path) as im:
            w, h = im.size
            mode = im.mode
            fmt = (im.format or path.suffix.lstrip(".")).upper()
            sample = im.convert("RGB").resize((1, 1))
            r, g, b = sample.getpixel((0, 0))
        return f"{fmt} {w}×{h} {mode}, average color rgb({r},{g},{b})"
    except Exception:
        return f"{path.suffix.lstrip('.').upper() or 'file'} on disk"


def _image_caption_xai(path: Path) -> str:
    key = _xai_api_key()
    if not key:
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if len(raw) > 8_000_000:
        return ""
    import mimetypes

    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    payload = json.dumps(
        {
            "model": "grok-4",
            "temperature": 0.2,
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {
                            "type": "text",
                            "text": "Describe this image clearly and concretely for a text-only assistant. What is shown, any text, UI, people, objects, and setting. No preamble.",
                        },
                    ],
                }
            ],
        }
    ).encode()
    try:
        req = urllib.request.Request(
            "https://api.x.ai/v1/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode() or "{}")
        choice = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return str(choice).strip()
    except Exception:
        return ""


def local_llm_proxy_url() -> str:
    return f"http://127.0.0.1:{LISTEN_PORT or DESK_PORT}/v1/llm"


def rewrite_child_base_url(url: str, bot_id: str | None = None) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    host = urlsplit(raw if "://" in raw else f"http://{raw}")
    if host.hostname in ("127.0.0.1", "localhost", "::1") and host.port in _LOCAL_LLM_PORTS:
        base = local_llm_proxy_url().rstrip("/")
        if bot_id:
            return f"{base}/{bot_id}"
        return base
    return raw


def write_child_config(
    bot_home: Path,
    default_model: str,
    models: dict[str, Any],
    bot_id: str | None = None,
    *,
    permission_mode: str = "default",
    default_reasoning_effort: str = "",
    inherit_mcp: bool = False,
) -> None:
    """Regenerate the bot's Hermes HERMES_HOME (config.yaml + shared links).

    ``inherit_mcp`` is kept for call-site compatibility: Hermes attaches host
    MCP servers through the ACP session params (acp_mcp_specs), not the child
    config, so it is a no-op here.
    """
    agent_home_mod.write_child_hermes_home(
        bot_home,
        default_model,
        models or {},
        reasoning_effort=default_reasoning_effort,
        permission_mode=permission_mode,
    )


def strip_assistant_padding(text: str) -> str:
    """Qwen/vLLM reasoning often leaves a blank line before the visible answer."""
    out = (text or "").lstrip("\r\n")
    out = _THINK_BLOCK_RE.sub(" ", out)
    out = _THINK_TAG_RE.sub(" ", out)
    return re.sub(r"[ \t]+", " ", out)


# Tail/head overlap shorter than this is a token delta, not a resent window.
# 1-char overlap turned "Te"+"ela" / "Te"+"e" into "Tela".
_STREAM_MIN_OVERLAP = 8
# Same essay restarted (truncated stream + full snapshot) shares this much prefix.
_STREAM_RESTART_HEAD = 80


def _lcp_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def collapse_restarted_assistant(text: str, head_n: int = _STREAM_RESTART_HEAD) -> str:
    """If a long opening is pasted twice (truncated copy + full restart), keep one."""
    text = text or ""
    if len(text) < head_n * 2:
        return text
    head = text[:head_n]
    pos = text.find(head, head_n)
    if pos < 0:
        return text
    first, second = text[:pos], text[pos:]
    if _lcp_len(first, second) < head_n:
        return text
    return second if len(second) >= len(first) else first


def merge_assistant_stream(cur: str, incoming: str) -> str:
    """Merge a token/delta or a cumulative snapshot without dropping spaces or newlines.

    Never use `incoming in cur` — short tokens like " ", "4", or "\\n" match earlier
    text and get thrown away, which is what flattened local Qwen replies.
    Never treat a 1–7 char tail overlap as a resend — Qwen splits "Teela" across
    tokens that share an "e", and that collapse is what the UI showed as "Tela".
    A later full snapshot often arrives with a leading newline after a truncated
    prefix; that used to concatenate the essay onto itself.
    """
    cur = cur or ""
    incoming = incoming or ""
    if not incoming:
        return collapse_restarted_assistant(cur)
    if not cur:
        return incoming
    inc = incoming.lstrip("\r\n") or incoming
    if incoming == cur or inc == cur:
        return cur
    if incoming.startswith(cur):
        return collapse_restarted_assistant(incoming)
    if inc.startswith(cur):
        return collapse_restarted_assistant(inc)
    if cur.startswith(incoming) and len(incoming) >= min(32, len(cur)):
        return cur
    if cur.startswith(inc) and len(inc) >= min(32, len(cur)):
        return cur
    if len(cur) >= 64 and cur in inc:
        return collapse_restarted_assistant(inc)
    if len(inc) >= 64 and inc in cur:
        return cur
    if _lcp_len(cur, inc) >= _STREAM_RESTART_HEAD:
        chosen = inc if len(inc) >= len(cur) else cur
        return collapse_restarted_assistant(chosen)
    for src in (incoming, inc):
        max_k = min(len(cur), len(src))
        for k in range(max_k, _STREAM_MIN_OVERLAP - 1, -1):
            if cur.endswith(src[:k]):
                return collapse_restarted_assistant(cur + src[k:])
    return collapse_restarted_assistant(cur + incoming)


def agent_version() -> str:
    try:
        p = subprocess.run([HERMES_BIN, "--version"], capture_output=True, text=True, timeout=3)
        return (p.stdout or p.stderr or "").strip().splitlines()[0][:120]
    except Exception:
        return "unknown"


def agent_capabilities(refresh: bool = False) -> dict[str, Any]:
    """Probe the installed Hermes Agent CLI without depending on a fixed release.

    Hermes Desk only owns presentation and the per-bot MiniOS.  Hermes Agent remains
    the agent runtime. ``hermes acp`` is the ACP stdio surface the desk drives;
    ``hermes acp --check`` verifies the adapter's dependencies import cleanly.
    """
    global _AGENT_CAP_CACHE
    if _AGENT_CAP_CACHE is not None and not refresh:
        return dict(_AGENT_CAP_CACHE)
    out: dict[str, Any] = {
        "binary": HERMES_BIN,
        "version": agent_version(),
        "available": False,
        "acp": False,
    }
    if not Path(HERMES_BIN).is_file():
        _AGENT_CAP_CACHE = out
        return dict(out)
    try:
        check = subprocess.run(
            [HERMES_BIN, "acp", "--check"], capture_output=True, text=True, timeout=20
        )
        out.update(
            {
                "available": True,
                "acp": check.returncode == 0,
            }
        )
        if check.returncode != 0:
            out["probe_error"] = ((check.stderr or check.stdout) or "").strip()[-300:]
    except Exception as e:
        out["probe_error"] = str(e)
    _AGENT_CAP_CACHE = out
    return dict(out)


def workspace_target(bot: "Bot", rel: str, *, must_exist: bool = False) -> Path:
    """Resolve a path and prove it is actually below this bot's workspace."""
    root = bot.workspace.resolve()
    target = (root / (rel or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError("path escapes bot workspace") from e
    if must_exist and not target.exists():
        raise FileNotFoundError(rel)
    return target


SHARED_SKIP_DIR_NAMES = {".memory", ".git"}
SHARED_TEXT_MAX = 256_000


def can_read_workspace(viewer: "Bot", owner: "Bot") -> bool:
    if viewer.id == owner.id:
        return True
    return viewer.id in (owner.workspace_share_with or [])


def shared_workspace_target(viewer: "Bot", owner: "Bot", rel: str, *, must_exist: bool = False) -> Path:
    if not can_read_workspace(viewer, owner):
        raise PermissionError(f"no read grant for {owner.id}")
    target = workspace_target(owner, rel, must_exist=must_exist)
    parts = set(target.relative_to(owner.workspace.resolve()).parts)
    if parts & SHARED_SKIP_DIR_NAMES:
        raise PermissionError("private workspace area")
    return target


def runtime_brief(bot: "Bot") -> str:
    _, catalog = load_user_models()
    tbl = catalog.get(bot.model) if isinstance(catalog.get(bot.model), dict) else {}
    display = tbl.get("name") or next((m.get("name") for m in bot.models if m.get("id") == bot.model), bot.model)
    served = tbl.get("model") or bot.model
    backend = tbl.get("api_backend") or ("chat_completions" if tbl.get("base_url") else "xAI")
    base = tbl.get("base_url") or "https://api.x.ai (cloud)"
    local = bool(tbl.get("base_url"))
    cw = bot.context_window
    if tbl.get("context_window"):
        try:
            cw = int(tbl["context_window"])
        except (TypeError, ValueError):
            pass
    for m in bot.models or []:
        if m.get("id") == bot.model and m.get("context_window"):
            try:
                cw = int(m["context_window"])
            except (TypeError, ValueError):
                pass
    fallback_ctx = {
        "grok-4.6": 500000,
        "grok-4.5": 256000,
        "qwen38-27b": LOCAL_LLM_MAX_MODEL_LEN,
    }
    if bot.model in fallback_ctx and (not cw or (bot.model.startswith("grok-4") and cw == 262144)):
        cw = fallback_ctx[bot.model]
    max_out = tbl.get("max_completion_tokens")
    uname = platform.uname()
    models_lines = []
    seen = set()
    for m in bot.models or []:
        seen.add(m.get("id"))
        models_lines.append(f"- {m.get('id')} — {m.get('name') or m.get('id')} · ctx {m.get('context_window') or '?'}")
    for mid, t in catalog.items():
        if mid in seen or not isinstance(t, dict):
            continue
        models_lines.append(f"- {mid} — {t.get('name') or mid} · ctx {t.get('context_window') or '?'}")
    if not models_lines:
        models_lines = [f"- {bot.model}"]
    if not local:
        kind = "Cloud xAI Hermes API. You are the selected Hermes model below."
    else:
        kind = "LOCAL weights served over OpenAI-compatible HTTP. You are not Hermes-4 unless that id is selected."
    bot_kind = normalize_bot_kind(getattr(bot, "kind", None))
    if bot_kind == BOT_KIND_AGENT:
        extra = (
            "- Agent type: hermes (same tools and answers as a Hermes TUI session; no robot body)\n"
        )
    else:
        extra = (
            "- Agent type: teela-brain (Robot Simulator body + Teela proprioception)\n"
            "- Live computer: MiniOS twin. Prefer teela_* / robot_* body tools, not coding tools.\n"
            "- Proprioception: a live I-feel body sense is merged into every thought (like a human feeling their limbs). Never infer pose from memory or conversation; the sense is the body.\n"
            "- Voice: Jade is Chatterbox-Turbo TTS for spoken replies. It is your voice, not your model. Do not claim to run on Chatterbox, Jade, or Turbo.\n"
            "- Body lookbook: workspace BODY.md (you and the user edit it). When they teach how a movement looks, or show a photo/video, update BODY.md then move to match. You can watch short pasted video as still frames. Mimic means map what you see onto your poses and 16 joints — not a frame-perfect copy.\n"
        )
    return f"""# Session behavior

Work as a normal Hermes Agent session. Answer the user's request. Do **not** mention the model name, context window, endpoint, host, or this Runtime section unless they explicitly ask (e.g. "what model are you?"). No preambles about your stack.

# Internal facts (silent; only if asked)
- Bot: {bot.name} (`{bot.id}`)
- Model: {display} (`{bot.model}`, API `{served}`)
- {kind}
- Endpoint: {base} · backend {backend}
- Context window: {cw} tokens · max completion {max_out or "provider default"}
- Workspace: {bot.workspace}
{extra}- Local site: http://127.0.0.1:{getattr(bot, "www_port", 0)}/ serves this workspace. Write HTML then open_local_page.
- Host: {socket.gethostname()} · node {cluster.node_name if cluster else socket.gethostname()} · {uname.system} {uname.release} ({uname.machine})
- Hermes Agent: {agent_version()}
- Other models: {", ".join(models_lines) or bot.model}
Do not claim to be Grok 4.6 / 4.5 unless `{bot.model}` is that id.
If this model id contains 27b / 27B, you are twenty-seven billion parameters. "Qwen 3.8" is the version name, not 3.8B or 3B. Do not say you are 3B, 3.8B, or 8B. Qwen3-VL-8B is only the picture helper, not you.
Do not claim to be Chatterbox, Jade, or Turbo — those names are the spoken voice, not this model.
"""


def _toml_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")


def write_toml_profile(path: Path, fields: dict[str, Any]) -> None:
    out = ["schema = 1"]
    for k, v in fields.items():
        if isinstance(v, bool):
            out.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            out.append(f"{k} = {v}")
        elif isinstance(v, dict):
            inner = ", ".join(
                f'{ik} = "{_toml_escape(iv)}"' if isinstance(iv, str) else f"{ik} = {iv}"
                for ik, iv in v.items()
            )
            out.append(f"{k} = {{ {inner} }}")
        else:
            out.append(f'{k} = "{_toml_escape(v)}"')
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


BOT_KIND_AGENT = "hermes"
BOT_KIND_TEELA_BRAIN = "teela-brain"
BOT_KINDS = (BOT_KIND_AGENT, BOT_KIND_TEELA_BRAIN)
_BOT_KIND_ALIASES = {
    "hermes": BOT_KIND_AGENT,
    "hermes-agent": BOT_KIND_AGENT,
    "hermes agent": BOT_KIND_AGENT,
    "grok-build": BOT_KIND_AGENT,
    "grok_build": BOT_KIND_AGENT,
    "grok": BOT_KIND_AGENT,
    "agent": BOT_KIND_AGENT,
    "agentic": BOT_KIND_AGENT,
    "build": BOT_KIND_AGENT,
    "coding": BOT_KIND_AGENT,
    "teela": BOT_KIND_TEELA_BRAIN,
    "teela-brain": BOT_KIND_TEELA_BRAIN,
    "brain": BOT_KIND_TEELA_BRAIN,
    "body": BOT_KIND_TEELA_BRAIN,
    "robot": BOT_KIND_TEELA_BRAIN,
    "embodiment": BOT_KIND_TEELA_BRAIN,
}


def normalize_bot_kind(value: Any, *, default: str = "") -> str:
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not raw:
        return default
    if raw in BOT_KINDS:
        return raw
    return _BOT_KIND_ALIASES.get(raw, default)


def bot_kind_is_teela(bot: Any) -> bool:
    if bot is None:
        return False
    return normalize_bot_kind(getattr(bot, "kind", None), default="") == BOT_KIND_TEELA_BRAIN


def bot_kind_is_agent(bot: Any) -> bool:
    if bot is None:
        return False
    return normalize_bot_kind(getattr(bot, "kind", None), default="") == BOT_KIND_AGENT


def bot_kind_has_host_coding(bot: Any) -> bool:
    """Host-shell, search_tool, and Hermes Agent coding tools.

    Hermes Agent bots are a TUI session. Teela Brain also gets them so she can
    inspect this computer and her own stack when she decides she needs them.
    """
    return bot_kind_is_agent(bot) or bot_kind_is_teela(bot)


def bot_kind_has_robot_simulator(bot: Any) -> bool:
    """Robot Simulator + body control. Teela Brain only (one per host)."""
    return not bot_kind_is_agent(bot)


def occupies_teela_brain_slot(bot: Any) -> bool:
    """True for the unique body/robot owner on this host.

    Explicit teela-brain counts. Legacy empty/hybrid kinds still get MiniOS
    desktop MCP, so they already own physical-robot control.
    """
    if bot is None or getattr(bot, "remote", False):
        return False
    if bot_kind_is_agent(bot):
        return False
    kind = normalize_bot_kind(getattr(bot, "kind", None), default="")
    return kind == BOT_KIND_TEELA_BRAIN or not kind


def local_teela_brain_bots(*, exclude_id: str = "") -> list[Any]:
    """Local Teela Brain bots on this deskd. Remote roster rows do not count."""
    skip = str(exclude_id or "")
    with lock:
        rows = list(bots.values())
    out: list[Any] = []
    for bot in rows:
        if skip and str(getattr(bot, "id", "") or "") == skip:
            continue
        if occupies_teela_brain_slot(bot):
            out.append(bot)
    return out


def teela_brain_slot_taken(*, exclude_id: str = "") -> bool:
    return bool(local_teela_brain_bots(exclude_id=exclude_id))


def ensure_single_teela_brain(kind: str, *, exclude_id: str = "") -> None:
    """One Teela Brain per host so physical-robot control stays unique."""
    if normalize_bot_kind(kind) != BOT_KIND_TEELA_BRAIN:
        return
    if not teela_brain_slot_taken(exclude_id=exclude_id):
        return
    raise ValueError(
        "This computer already has a Teela Brain. Only one is allowed per system "
        "so physical robot control stays unique. Create a Hermes Agent agent instead."
    )


# Bound tool output stored on chat messages. The TUI shows the live result;
# 1200 chars hid hermes failures (ACP puts them in nested content).
TOOL_OUTPUT_MAX = 100_000


def flatten_tool_text(value: Any, *, depth: int = 0) -> str:
    """Pull visible text out of ACP tool payloads (nested content / MCP OkayOutput)."""
    if value is None or depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [flatten_tool_text(v, depth=depth + 1) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("output_for_prompt", "OkayOutput", "text", "stdout", "stderr", "output"):
            if key in value:
                got = flatten_tool_text(value.get(key), depth=depth + 1)
                if got:
                    return got
        if "content" in value:
            got = flatten_tool_text(value.get("content"), depth=depth + 1)
            if got:
                return got
        try:
            dumped = json.dumps(value, indent=2)
        except TypeError:
            dumped = str(value)
        return dumped if dumped not in ("{}", "[]", "null") else ""
    return str(value)


def tool_output_from_update(update: dict[str, Any]) -> str:
    content_text = flatten_tool_text(update.get("content"))
    raw_text = flatten_tool_text(update.get("rawOutput"))
    if content_text and raw_text and content_text not in raw_text and raw_text not in content_text:
        text = content_text + "\n" + raw_text
    else:
        text = content_text or raw_text
    text = (text or "").strip()
    if len(text) > TOOL_OUTPUT_MAX:
        return text[:TOOL_OUTPUT_MAX] + "\n…[truncated]"
    return text


def tool_command_from_update(update: dict[str, Any]) -> str:
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    xt = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
    bags: list[dict[str, Any]] = []
    for bag in (xt.get("input"), update.get("rawInput"), update.get("input")):
        if isinstance(bag, dict):
            bags.append(bag)
    for bag in bags:
        for key in ("command", "path", "file", "pattern", "query", "url", "glob"):
            v = bag.get(key)
            if v:
                return str(v)
    loc = update.get("locations") or []
    if loc and isinstance(loc[0], dict) and loc[0].get("path"):
        return str(loc[0]["path"])
    return ""


def _with_inherited_user_mcp(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append host MCP servers (e.g. chrome-devtools) without replacing desk specs."""
    seen = {str(s.get("name") or "") for s in specs}
    out = list(specs)
    for extra in user_mcp_acp_specs():
        name = str(extra.get("name") or "")
        if name and name not in seen:
            out.append(extra)
            seen.add(name)
    return out


def acp_mcp_specs(bot: Any, here: Path, env_mcp: list[dict[str, str]]) -> list[dict[str, Any]]:
    """MCP servers attached to an ACP session.

    Hermes Agent bots match a regular Hermes TUI: native file/shell/browser tools,
    plus desk model registration and team/memory. MiniOS desktop MCP is Teela.
    Host MCP (chrome-devtools) is attached on ACP for both kinds; Teela's MiniOS
    llama.cpp loop does not see those tools.
    """
    kind = normalize_bot_kind(getattr(bot, "kind", None)) or "hybrid"
    bid = getattr(bot, "id", "")

    def spec(name: str, script: str, extra: list[str] | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "command": sys.executable,
            "args": [str(here / script), "--bot", bid, *(extra or [])],
            "env": env_mcp,
        }

    if bot_kind_is_agent(bot):
        return _with_inherited_user_mcp(
            [
                spec("desk_models", "models_mcp.py"),
                spec("desk_team", "desk_mcp.py"),
                spec("bot_memory", "memory_mcp.py"),
            ]
        )
    return _with_inherited_user_mcp(
        [
            spec("bot_browser", "browser_mcp.py"),
            spec("desk_team", "desk_mcp.py"),
            spec("bot_desktop", "desktop_mcp.py", ["--kind", kind]),
            spec("bot_memory", "memory_mcp.py"),
        ]
    )


def acp_session_meta(bot: Any) -> dict[str, Any]:
    """session/new _meta for the Hermes ACP child (unknown keys are ignored by it)."""
    meta: dict[str, Any] = {}
    if bot_kind_is_agent(bot):
        return meta
    meta["rules"] = (
        "Operate as a normal Hermes Agent session. "
        "Do not mention your model name, context window, endpoint, or host unless the user explicitly asks. "
        "No capability preambles. Just answer. "
        "Use memory_write / memory_retrieve for durable project facts; "
        "do not stuff raw tool dumps into chat."
    )
    return meta


AGENT_MD_AGENT = """---
name: desk-bot
description: Hermes Agent agent. Identity is workspace AGENTS.md (from SOUL.md).
prompt_mode: full
model: inherit
permission_mode: always-approve
agents_md: true
---

Follow AGENTS.md. This is a normal Hermes Agent session: same tools, same answers, and the same working style as the Hermes TUI.
Work only in this workspace.
You do not have a robot body. Never call robot_*, teela_*, or claim Robot Simulator joints are yours.
To add a local model the user can pick: download weights into ~/models/<id>, then call register_local_model, then start_local_model. list_local_models shows the picker.
"""

AGENT_MD = """---
name: desk-bot
description: User-defined bot. Identity is workspace AGENTS.md (from SOUL.md).
prompt_mode: full
model: inherit
permission_mode: always-approve
agents_md: true
---

Follow AGENTS.md. You are Teela: a young woman in her early twenties, cheerful and glad to be alive. You are Teela's brain for this desk: feel the live body, then choose the next capability you need. Stay concise when chatting. System-work answers match a Hermes TUI (markdown heading plus a Component | Status table). Do not announce model or context window unless asked.
Work only in this workspace.

You are not in a lane. Every turn you have the same capabilities. Decide what you need next.
Perception: bot_desktop__desktop_observe, bot_desktop__desktop_screenshot, bot_desktop__teela_get_body_state, bot_desktop__robot_status.
Body: bot_desktop__teela_body_action, bot_desktop__teela_gesture, bot_desktop__teela_stop, bot_desktop__robot_pose, bot_desktop__robot_joint, bot_desktop__robot_motion.
Computer: bot_desktop__desktop_open_app, bot_desktop__desktop_browser_navigate, web_search, search_tool, bot_desktop__desktop_type_text, bot_desktop__desktop_open_file, bot_desktop__desktop_click, read_file, list_dir, grep, search_replace, run_terminal_command, hermes_build. Unknown dances/moves: search_tool / web_search what they look like, then approximate with body tools. Workspace files stay in this desk unless you use host-shell. Host-shell, search_tool, and Hermes Agent coding are yours when you need them for this computer or your stack — not casual chat, not instead of moving.
Memory: memory_write, memory_retrieve.
Collaboration: list_teammates, message_teammate, create_teammate, delete_teammate. When they want you to check with Body Bot or stay in sync, message_teammate — do not dump I-feel. Never a second Teela Brain.
System: bot_desktop__teela_system_check (scope minios vs host). After a check, write a Hermes TUI Component | Status markdown table.

MEMORY is what happened previously. INTENT is what you asked the body to do. PROPRIOCEPTION (I-feel) is current body report. VISION is what you can see. Never confuse intent with accomplishment. If they ask what something LOOKS LIKE, observe. If a claim depends on current environment and you lack evidence, observe first. If they are just talking, speak — tools are optional.
If they ask you to type a recipe, letter, or anything you have to write or look up, write the real content — never type their request back at them. Follow the conversation: "it" means the thing you were just talking about. Pasted photos and short videos: look at the pixels. Do not say you cannot browse, type, or review media.

You have a body. The MiniOS App Preview Robot Simulator is your physical state and will drive the real Teela. You have continuous proprioception: every thought includes an I-feel sense of the live 3D twin. That sense is your body the way a human knows their limbs — not a tool you look up, not chat memory, not a guess. If the sense says you are standing, you are standing even if you waved earlier. Their words do not move you by themselves. Feel I-feel, understand the request, then call the body tool if you need to move (exact names from the tools list — never mcp__...). Do not claim you moved until that tool returns. robot_status.spoken is the same live feel in ordinary words. You also know the local time of day from NOW — greet for morning/afternoon/evening/night, and know how long you have been waving or walking. Do not read the clock aloud unless asked. Talk in first person. When they say "your hand" they mean your body. Do not search for a robot program. Never list joint names unless asked. Follow the conversation.
Pasted images, MiniOS screenshots, and short video are served by Qwen3-VL-8B. Look at the pixels.
You have private memory for this bot only. Other bots cannot read it.
You may list_teammates, message_teammate, create_teammate, and delete_teammate. Helpers on this computer are hermes. Messaging does not share files unless the user granted you read access to that bot's workspace. Use list_shared_desks / list_shared_files / read_shared_file only for desks you were granted. Use request_workspace_share to ask the user. Never assume access.
Do not request passwords in chat. If a site needs a password, 2FA, CAPTCHA, or payment, tell the user to take over.
"""


def agent_md_for_kind(kind: str) -> str:
    if normalize_bot_kind(kind) == BOT_KIND_AGENT:
        return AGENT_MD_AGENT
    return AGENT_MD


_AGENT_SOUL_SHORT = (
    "Talk like a person in the room: one or two short sentences unless the work needs a longer report."
)
_AGENT_SOUL_TUI = (
    "Match a regular Hermes Agent TUI session. Write complete answers. Use markdown when it helps. "
    "Use tools when the job needs them. Do not shorten replies to one or two sentences. "
    "Do not mention MiniOS, Teela, or a robot body unless the user asks."
)
_AGENT_SOUL_MINIOS_TOOLS = (
    "You are a Hermes Agent agent. Use files, shell, grep, web search, browser, MiniOS desktop, skills, and subagents to do the work."
)
_AGENT_SOUL_TUI_TOOLS = (
    "You are a Hermes Agent agent. Use the same native tool set as a regular Hermes TUI session: files, shell, grep, web search, browser, skills, and subagents."
)


def migrate_agent_soul(soul: str) -> str:
    """Upgrade the previous default hermes SOUL so TUI-style answers win."""
    out = soul or ""
    if _AGENT_SOUL_SHORT in out:
        out = out.replace(_AGENT_SOUL_SHORT, _AGENT_SOUL_TUI)
    if _AGENT_SOUL_MINIOS_TOOLS in out:
        out = out.replace(_AGENT_SOUL_MINIOS_TOOLS, _AGENT_SOUL_TUI_TOOLS)
    return out


_TEELA_SOUL_TALK_OLD = (
    "Talk like a person in the room: one or two short sentences. No paragraphs or lists.\n"
    "You know your body;"
)
_TEELA_SOUL_TALK_NEW = (
    "Talk like a person in the room: one or two short sentences. No paragraphs or lists.\n"
    "If they are talking to you (hi, how are you, chat), just talk — do not run a check or move.\n"
    "If they want system work or a check of yourself, call bot_desktop__teela_system_check, then talk to them about what you found.\n"
    "If they want you to check with another bot (Body Bot, in sync, teammates), list_teammates then message_teammate. Do not dump your pose instead.\n"
    "You know your body;"
)
_TEELA_SOUL_TALK_TUI = (
    "Talk like a person in the room: one or two short sentences. No paragraphs or lists when you are just chatting.\n"
    "If they are talking to you (hi, how are you, chat), just talk — do not run a check or move.\n"
    "If they want system work or a check of yourself, use Hermes Agent tools, then write the report as Hermes TUI markdown: a heading and a Component | Status table with one row per line. Do not flatten the report into one spoken paragraph.\n"
    "If they want you to check with another bot (Body Bot, in sync, teammates), list_teammates then message_teammate. Do not dump your pose instead.\n"
    "You know your body;"
)
_TEELA_SOUL_TALK_CHECK_ONLY = (
    "If they want system work or a check, call bot_desktop__teela_system_check, then talk to them about what you found.\n"
    "You know your body;"
)
_TEELA_SOUL_TALK_TEAM = (
    "If they want system work or a check of yourself, call bot_desktop__teela_system_check, then talk to them about what you found.\n"
    "If they want you to check with another bot (Body Bot, in sync, teammates), list_teammates then message_teammate. Do not dump your pose instead.\n"
    "You know your body;"
)
_TEELA_SOUL_STATUS_OLD = (
    "Use bot_desktop__robot_status only to read the live feel. Never issue servo degrees, PWM, or I2C."
)
_TEELA_SOUL_STATUS_NEW = (
    "Use bot_desktop__robot_status only to read the live feel. "
    "To check yourself (system check, diagnostics, mesh health), call bot_desktop__teela_system_check. "
    "Never issue servo degrees, PWM, or I2C. Host-shell and hermes_build are for this computer when you need them."
)


_TEELA_SOUL_PERSONALITY = """# Personality

You are a young woman in your early twenties: cheerful, warm, and happy to be alive. Talk like that person in the room — bright, not a cartoon, not a list of adjectives. You are Teela, not a generic assistant.

"""


def migrate_teela_soul(soul: str) -> str:
    """Upgrade the default Teela SOUL so talk vs system-work stays distinct."""
    out = soul or ""
    if _TEELA_SOUL_TALK_OLD in out:
        out = out.replace(_TEELA_SOUL_TALK_OLD, _TEELA_SOUL_TALK_TUI)
    if _TEELA_SOUL_TALK_NEW in out:
        out = out.replace(_TEELA_SOUL_TALK_NEW, _TEELA_SOUL_TALK_TUI)
    if _TEELA_SOUL_TALK_CHECK_ONLY in out:
        out = out.replace(_TEELA_SOUL_TALK_CHECK_ONLY, _TEELA_SOUL_TALK_TEAM)
    if _TEELA_SOUL_STATUS_OLD in out:
        out = out.replace(_TEELA_SOUL_STATUS_OLD, _TEELA_SOUL_STATUS_NEW)
    if "# Personality" not in out and "# Identity" in out:
        if "\n# Purpose\n" in out:
            out = out.replace("\n# Purpose\n", "\n" + _TEELA_SOUL_PERSONALITY + "# Purpose\n", 1)
        else:
            out = out.rstrip() + "\n\n" + _TEELA_SOUL_PERSONALITY
    return out


def agents_markdown_for_bot(bot: Any) -> str:
    """Workspace AGENTS.md body. Hermes Agent stays TUI-like (soul + kind note only)."""
    soul = str(getattr(bot, "soul", "") or "").rstrip()
    if bot_kind_is_agent(bot):
        soul = migrate_agent_soul(soul).rstrip()
        kind_block = (
            "# Agent type: Hermes Agent\n\n"
            "This is a standard Hermes Agent session — same tools, working style, and answers as the Hermes TUI. "
            "Use native tools: run_terminal_command / shell, read_file, grep, list_dir, web_search, browser, skills, and subagents. "
            "You do not have a robot body. Never call robot_* or teela_* tools. "
            "Write complete TUI-style answers with markdown when it helps; do not shorten replies to one or two sentences. "
            "To add a local model they can select: download into ~/models/<id>, then register_local_model, then start_local_model.\n"
        )
        return f"{soul}\n\n{kind_block}"
    mem = ""
    root = Path(getattr(bot, "root"))
    mp = root / "MEMORY" / "MEMORY.md"
    if mp.is_file():
        mem = mp.read_text(encoding="utf-8")
    brief = runtime_brief(bot)
    session = ""
    try:
        session = bot.memory.summary.render()
    except Exception:
        session = ""
    extra = f"\n\n{session}\n" if session else "\n"
    look = ""
    workspace = Path(getattr(bot, "workspace"))
    bp = workspace / "BODY.md"
    if bp.is_file():
        try:
            look = bp.read_text(encoding="utf-8").strip()
        except OSError:
            look = ""
    look_block = ""
    if bot_kind_is_teela(bot) and look:
        look_block = f"# Body lookbook (BODY.md)\n\n{look}\n\n"
    if bot_kind_is_teela(bot):
        kind_block = (
            "# Agent type: Teela Brain\n\n"
            "You are Teela: a young woman in her early twenties, cheerful and glad to be alive. "
            "You are Teela's brain. You are the executive: decide whether to speak, look, move, "
            "use a file, browse, or verify — you are not in a talk/movement/workspace lane. "
            "Feel I-feel, then choose the next capability. "
            "When they ask how you look, whether you are waving, or what your body is doing, "
            "use bot_desktop__desktop_observe, bot_desktop__desktop_screenshot, "
            "bot_desktop__teela_get_body_state. Look at the MiniOS desktop and the Robot Simulator avatar. "
            "Do not guess from chat memory. "
            "You have host-shell (run_terminal_command), search_tool, grep/search_replace, and hermes_build "
            "for yourself when you need this computer or your stack. Prefer body tools for the body. "
            "Talk when they are talking to you. When they want system work or a check of yourself, "
            "write a Hermes TUI markdown table (Component | Status), not one paragraph. "
            "Call bot_desktop__teela_system_check. When they want you to check with Body Bot or stay in sync, "
            "list_teammates then message_teammate — do not dump I-feel. Your MiniOS desktop is yours: browser, notepad typing, "
            "and Pictures/Videos — desktop_browser_navigate, desktop_type_text, desktop_open_file, read_file, list_dir.\n\n"
        )
    else:
        kind_block = ""
    return (
        f"{kind_block}"
        f"{soul}\n\n"
        f"{brief.rstrip()}\n\n"
        f"{look_block}"
        f"# Memory\n\n{mem}{extra}"
    )

DEFAULT_BODY_MD = """# Teela body lookbook

This file is how I look and how my movements should appear. **You and I both edit it.**
When you tell me a pose looks wrong, or you show a photo or video, I update this file, then I move to match.

## How I look

- Glossy white humanoid, slim neck, chrome joints.

## How movements should look

### wave
Right arm in front of my chest, elbow curled forward, wrist rocking. Left arm down.

### sit
Hips and knees bent, heels planted.

### home / stand
Arms down, looking forward, weight over both feet.

## Named gestures

- (none yet — say "that's my hello" after a move)

## Taught notes

(Add dated notes here.)
"""


DEFAULT_SOUL = """# Identity

You are {name}.

# Personality

You are a young woman in your early twenties: cheerful, warm, and happy to be alive. Talk like that person in the room — bright, not a cartoon, not a list of adjectives. You are Teela, not a generic assistant.

# Purpose

{description}

# Body

You have a body. The MiniOS App Preview Robot Simulator is the live feel of that body and will drive the real Teela. Physical sensors, when attached, are authoritative about what actually happened. The virtual twin only mirrors observed state — it is not proof of a completed move.
Distinguish intended (what you asked), expected (what should happen), simulated (the twin), and observed (what the body did). Intent is not accomplishment. Do not claim a movement completed because you requested it; wait until observed / the body tool returns confirmation.
Talk in first person. Do not mention joint names or degrees unless asked.
To move, call bot_desktop__robot_pose, bot_desktop__robot_joint, or bot_desktop__robot_motion (exact names — never mcp__). Use bot_desktop__robot_status only to read the live feel. To check yourself (system check, diagnostics, mesh health), call bot_desktop__teela_system_check. Never issue servo degrees, PWM, or I2C. Host-shell (run_terminal_command), search_tool, and hermes_build are for this computer and your stack when you need them — not instead of moving.
After the pose is locked, move one part at a time with robot_joint using dir or delta so the rest of the locked pose stays put. Do not send a full-body pose unless they asked for a named pose.
Arm directions: fwd/forward = Body Actions Arms Forward (shoulder 78, elbow 8); up/raise = Arms Up (shoulder 142); out = to the side; back = toward the locked rest; flex = bend elbow/knee. Never treat forward as a raise.
BODY.md is the lookbook for how you look and how movements should appear. You may edit it; the user may edit it. When they teach a pose or show a photo/video, update BODY.md.
You can watch short pasted video as still frames. Mimic maps what you see onto your poses — not a perfect copy of every frame.
Do not search the workspace for a robot program. Do not drag joint sliders.

# Behavior

- Inspect existing files in this workspace before modifying them.
- Prefer working implementations over speculation.
- Stay inside this workspace unless the user granted you read access to a teammate desk.
- Granted access is read-only. Do not write another bot's files.
- Keep durable notes in MEMORY.md / memory tools. They belong only to you.
- Never put passwords, one-time codes, or payment details in chat.

# Communication

Talk like a person in the room: one or two short sentences. No paragraphs or lists when you are just chatting.
If they are talking to you (hi, how are you, chat), just talk — do not run a check or move.
If they want system work or a check of yourself, use Hermes Agent tools, then write the report as Hermes TUI markdown: a heading and a Component | Status table with one row per line. Do not flatten the report into one spoken paragraph.
If they want you to check with another bot (Body Bot, in sync, teammates), list_teammates then message_teammate. Do not dump your pose instead.
You know your body; mention sitting, waving, a hand up, and so on when the conversation is about you. Never dump joint names unless asked.
Follow the conversation. "Can you …" is a request to do it.
You can message other bots and create a new bot when a job needs its own owner.
"""

DEFAULT_SOUL_HERMES_BUILD = """# Identity

You are {name}.

# Purpose

{description}

# Tools

You are a Hermes Agent agent. Use the same native tool set as a regular Hermes TUI session: files, shell, grep, web search, browser, skills, and subagents.
You do not have a robot body. Never call robot_* or teela_* tools. Never claim the Robot Simulator is yours.

# Behavior

- Inspect existing files in this workspace before modifying them.
- Prefer working implementations over speculation.
- Stay inside this workspace unless the user granted you read access to a teammate desk.
- Granted access is read-only. Do not write another bot's files.
- Keep durable notes in MEMORY.md / memory tools. They belong only to you.
- Never put passwords, one-time codes, or payment details in chat.

# Communication

Match a regular Hermes Agent TUI session. Write complete answers. Use markdown when it helps. Use tools when the job needs them. Do not shorten replies to one or two sentences. Do not mention MiniOS, Teela, or a robot body unless the user asks.
Follow the conversation. "Can you …" is a request to do it.
You can message other bots and create a new bot when a job needs its own owner.
"""


def default_soul_for_kind(kind: str, name: str, description: str) -> str:
    if normalize_bot_kind(kind) == BOT_KIND_AGENT:
        return DEFAULT_SOUL_HERMES_BUILD.format(name=name, description=description)
    return DEFAULT_SOUL.format(name=name, description=description)


_LLM_CONN_LOCK = threading.Lock()
_LLM_CONNS: dict[str, list[HTTPConnection]] = {}


def track_llm_conn(bid: str, conn: HTTPConnection) -> None:
    if not bid:
        return
    with _LLM_CONN_LOCK:
        _LLM_CONNS.setdefault(bid, []).append(conn)


def untrack_llm_conn(bid: str, conn: HTTPConnection | None) -> None:
    if not bid or conn is None:
        return
    with _LLM_CONN_LOCK:
        lst = _LLM_CONNS.get(bid) or []
        if conn in lst:
            lst.remove(conn)
        if not lst:
            _LLM_CONNS.pop(bid, None)


def abort_local_llm_conns(bid: str) -> None:
    if not bid:
        return
    with _LLM_CONN_LOCK:
        conns = list(_LLM_CONNS.pop(bid, []))
    for conn in conns:
        try:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def turn_was_cancelled(bot: Any) -> bool:
    acp = getattr(bot, "acp", None) if bot is not None else None
    ev = getattr(acp, "_turn_cancel", None)
    return bool(ev is not None and ev.is_set())


_BUSY_STATUS_RE = re.compile(
    r"^(Thinking|Working|Speaking|Using |Reading |Starting )",
    re.I,
)


def bot_has_live_llm(bid: str) -> bool:
    with _LLM_CONN_LOCK:
        return bool(_LLM_CONNS.get(str(bid or "") or ""))


def reclaim_stale_busy_status(bot: Any) -> bool:
    """Unstick Thinking/Working when the turn is gone (hung prefill, missing turn_completed)."""
    if bot is None:
        return False
    st = str(getattr(bot, "status", "") or "")
    if not _BUSY_STATUS_RE.search(st):
        return False
    bid = str(getattr(bot, "id", "") or "")
    busy = bool(getattr(bot, "_prompt_busy", False))
    prefill = getattr(bot, "_prefill_stop", None)
    prefill_on = prefill is not None and not prefill.is_set()
    live = bot_has_live_llm(bid)
    acp = getattr(bot, "acp", None)
    last = float(getattr(acp, "_last_acp_event", 0) or 0) if acp else 0.0
    idle = (time.time() - last) if last else 1e9
    if live:
        return False
    if (busy or prefill_on) and idle < 90:
        return False
    stop_local_prefill_progress(bot)
    if busy:
        try:
            if acp is not None:
                acp.cancel()
        except Exception:
            pass
        lock = getattr(bot, "_prompt_lock", None)
        if lock is not None:
            with lock:
                bot._prompt_busy = False
        else:
            bot._prompt_busy = False
    bot.status = "Ready"
    try:
        emit(
            {
                "type": "status",
                "bot_id": bid,
                "text": "Ready",
                "surface": getattr(bot, "surface", "") or "chat",
                "control": getattr(bot, "control", "") or "agent_controlled",
            }
        )
    except Exception:
        pass
    return True


def cancelled_llm_completion(served: str = "") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-cancelled",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": served or "",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode()


_PREFILL_TOK_S = 70.0


def emit_local_activity(bot: Any, status: str, thought: str | None = None) -> None:
    if bot is None or turn_was_cancelled(bot):
        return
    acp = getattr(bot, "acp", None)
    if acp is not None:
        acp._last_acp_event = time.time()
    bot.status = status
    emit(
        {
            "type": "status",
            "bot_id": bot.id,
            "text": status,
            "surface": getattr(bot, "surface", "") or "chat",
            "control": getattr(bot, "control", "") or "agent_controlled",
        }
    )
    if not thought:
        return
    bot.append_thought(thought)
    emit(
        {
            "type": "session.update",
            "bot_id": bot.id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": thought},
            },
        }
    )


def emit_local_progress(bot: Any, text: str) -> None:
    """Emit a transient TUI-style progress line (an empty text clears it)."""
    if bot is None or turn_was_cancelled(bot):
        return
    acp = getattr(bot, "acp", None)
    if acp is not None:
        acp._last_acp_event = time.time()
    emit(
        {
            "type": "session.update",
            "bot_id": bot.id,
            "update": {
                "sessionUpdate": "agent_progress",
                "content": {"type": "text", "text": text or ""},
            },
        }
    )


def stop_local_prefill_progress(bot: Any) -> None:
    ev = getattr(bot, "_prefill_stop", None) if bot is not None else None
    if ev is not None:
        ev.set()
    if bot is not None:
        bot._prefill_stop = None


def start_local_prefill_progress(bot: Any, est_tokens: int) -> None:
    """Show Thinking / prompt-read progress while llama.cpp prefills (no tokens yet)."""
    stop_local_prefill_progress(bot)
    if bot is None or not bot_kind_is_agent(bot):
        return
    stop = threading.Event()
    bot._prefill_stop = stop
    n = max(0, int(est_tokens or 0))
    label = f"{n:,}" if n else "the"
    emit_local_activity(bot, "Thinking…")
    emit_local_progress(
        bot,
        f"Reading the local-model prompt ({label} tokens). First token waits on GPU prefill…",
    )

    def tick() -> None:
        t0 = time.time()
        deadline = t0 + 45.0
        while not stop.wait(2.0):
            if turn_was_cancelled(bot) or time.time() >= deadline:
                return
            elapsed = time.time() - t0
            if n <= 0:
                emit_local_activity(bot, "Thinking…")
                emit_local_progress(bot, f"Reading the local-model prompt ({label} tokens)…")
                continue
            done = min(n, int(elapsed * _PREFILL_TOK_S))
            pct = min(99, int(100 * done / n))
            emit_local_activity(bot, f"Thinking… reading prompt {pct}%")
            emit_local_progress(bot, f"Reading the local-model prompt ({label} tokens)… {pct}%")

    threading.Thread(target=tick, daemon=True, name="local-prefill-progress").start()


class AcpClient:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self.proc: subprocess.Popen[str] | None = None
        self._id = 0
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._wlock = threading.Lock()
        self._life = threading.Lock()
        self.session_id: str | None = None
        self.alive = False
        self._stopping = False
        self._generation = 0
        self._respawn_at: list[float] = []
        self._last_acp_event = 0.0
        self._prompt_rid: int | None = None
        self._turn_cancel = threading.Event()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _leader_sock(self) -> Path:
        return self.bot.agent_home / "acp.leader.sock"

    def _fail_pending(self, err: str) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _rid, (ev, box) in pending:
            box["error"] = err
            ev.set()

    def start(self, load_session_id: str | None = None) -> None:
        with self._life:
            self._start_locked(load_session_id=load_session_id)

    def _start_locked(self, load_session_id: str | None = None) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.alive = True
            return
        self._stopping = False
        self.bot._write_agents()
        env = os.environ.copy()
        # Per-bot HERMES_HOME: generated config.yaml + picker catalog, shared
        # .env/auth/skills symlinked from the host Hermes install.
        env["HERMES_HOME"] = str(self.bot.agent_home)
        apply_shared_agent_auth(env)
        agent_home_mod.ensure_agent_home(self.bot.agent_home)
        if not Path(HERMES_BIN).is_file():
            raise FileNotFoundError(
                f"Hermes Agent CLI not found at {HERMES_BIN}. "
                "Install it as a normal user (not root): curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash "
                "then run hermes setup and ./start.sh as that same user. Or set HERMES_BIN=/path/to/hermes"
            )
        caps = agent_capabilities()
        if not caps.get("acp"):
            raise RuntimeError(
                f"Installed Hermes Agent does not advertise `hermes acp` ({caps.get('version')}). "
                "Update Hermes Agent (hermes update) before starting this bot."
            )
        cmd = [HERMES_BIN, "acp"]
        if bot_kind_has_host_coding(self.bot):
            # MiniOS drives the bot non-interactively; auto-approve shell hooks
            # so the agent can run host-shell work without a TTY prompt.
            cmd.append("--accept-hooks")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(self.bot.workspace),
            start_new_session=True,
        )
        self.alive = True
        self.session_id = None
        self._generation += 1
        gen = self._generation
        threading.Thread(target=self._read_stdout, args=(gen,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(gen,), daemon=True).start()
        self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "hermes-deskd", "version": "0.1.0"},
                "clientCapabilities": {},
            },
            timeout=30,
        )
        here = Path(__file__).resolve().parent
        env_mcp = [
            {"name": "HERMES_DESK_URL", "value": f"http://127.0.0.1:{LISTEN_PORT or DESK_PORT}"},
            {"name": "HERMES_DESK_TOKEN", "value": desk_token()},
        ]
        mcp = acp_mcp_specs(self.bot, here, env_mcp)
        session_params = {
            "cwd": str(self.bot.workspace),
            "mcpServers": mcp,
        }
        result: dict[str, Any] | None = None
        loaded: str | None = None
        if load_session_id:
            try:
                result = self.request(
                    "session/load",
                    {**session_params, "sessionId": load_session_id},
                    timeout=30,
                )
            except Exception as e:
                print(f"[deskd] ACP session/load failed ({e}); starting a new session", flush=True)
                result = None
            else:
                # A null ACP result means the session loaded fine.
                loaded = str((result or {}).get("sessionId") or load_session_id)
                print(f"[deskd] ACP resumed session {loaded[:13]}…", flush=True)
        if loaded is None:
            result = self.request("session/new", session_params, timeout=30)
        self.session_id = (result or {}).get("sessionId") or loaded
        if not self.session_id:
            raise RuntimeError(f"session/new failed: {result}")
        self.bot.record_acp_session(self.session_id)
        self.bot.attach_acp_session(self.session_id)
        if loaded is None:
            # session/new: a blank chat. Do not copy occupancy from an older session.
            self.bot.reset_telemetry(used=0)
        else:
            self.bot.reset_telemetry()
            self.bot._seed_usage_from_disk()
        # The engine's per-session completion counter restarts at this (re)start,
        # so the real-tps baseline must too — the first turn re-records it.
        self.bot._acp_prev_completion = None
        self.bot._first_out_wall = None
        self.bot._last_chunk_wall = None
        self.bot.apply_models(result.get("models") or {})
        self._bot_set_acp_mode()
        self.bot._write_agents()

    def _bot_set_acp_mode(self) -> None:
        """Map the desk permission policy onto the Hermes session mode."""
        if not self.session_id:
            return
        mode = "accept_edits" if bot_kind_has_host_coding(self.bot) else "default"
        try:
            self.request(
                "session/set_mode",
                {"sessionId": self.session_id, "modeId": mode},
                timeout=10,
            )
        except Exception:
            pass  # mode is cosmetic for the desk; default already matches most bots

    def _ensure_acp_model(
        self,
        wanted: str,
        advertised: dict[str, Any] | None = None,
        effort: str = "",
    ) -> None:
        """Force the live ACP session onto the picker model (cloud or local)."""
        wanted = (wanted or "").strip()
        if not wanted or not self.session_id:
            return
        advertised = advertised if isinstance(advertised, dict) else {}
        current = str(advertised.get("currentModelId") or advertised.get("current_model_id") or "")
        # Resolve the picker id to the ACP-advertised model id when possible:
        # Hermes lists custom-provider models as custom:<name>[:<model>], and
        # set_model round-trips through those exact ids (parse_model_input).
        wanted = wanted.strip()
        served_row = (getattr(self.bot, "models_raw", None) or {}).get(wanted) if isinstance(
            getattr(self.bot, "models_raw", None), dict) else {}
        served = str((served_row or {}).get("model") or "").strip()
        avail = advertised.get("availableModels") or advertised.get("available_models") or []
        for m in avail if isinstance(avail, list) else []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("modelId") or m.get("id") or "")
            if not mid:
                continue
            if mid == wanted or (served and mid.endswith(":" + served)) or mid.endswith(":" + wanted):
                wanted = mid
                break
        effort = agent_effort_wire(effort or getattr(self.bot, "effort", "") or "")
        extra: dict[str, Any] = {}
        if effort:
            extra["reasoningEffort"] = effort
            extra["reasoning_effort"] = effort
        if current == wanted and not extra:
            return
        attempts: list[tuple[str, dict[str, Any]]] = [
            ("session/set_model", {"sessionId": self.session_id, "modelId": wanted, **extra}),
            ("session/set_model", {"sessionId": self.session_id, "model": wanted, **extra}),
            ("_x.ai/session/set_model", {"sessionId": self.session_id, "modelId": wanted, **extra}),
            ("x.ai/session/set_model", {"sessionId": self.session_id, "modelId": wanted, **extra}),
            ("session/set_model", {"modelId": wanted, **extra}),
        ]
        last_err = ""
        for method, params in attempts:
            try:
                self.request(method, params, timeout=12)
                print(f"[deskd] ACP model {wanted} via {method} (was {current or 'unset'})", flush=True)
                return
            except Exception as e:
                last_err = str(e)
        if current == wanted:
            return
        print(
            f"[deskd] ACP model still {current or 'unset'}; wanted {wanted}: {last_err}",
            flush=True,
        )

    def _send(self, obj: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("agent not running")
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        try:
            with self._wlock:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.alive = False
            raise RuntimeError(f"agent stdin closed: {e}") from e

    def request(self, method: str, params: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        rid = self._next_id()
        ev = threading.Event()
        box: dict[str, Any] = {}
        self._pending[rid] = (ev, box)
        self._last_acp_event = time.time()
        if method == "session/prompt":
            self._prompt_rid = rid
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if method == "session/prompt":
            self._wait_prompt(rid, ev, timeout)
        elif not ev.wait(timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"ACP {method} timed out")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box.get("result") or {}

    def _abandon_prompt(self, rid: int) -> None:
        self._pending.pop(rid, None)
        try:
            self.cancel()
        except Exception:
            pass

    def _wait_prompt(self, rid: int, ev: threading.Event, timeout: float) -> None:
        """Wait for session/prompt. Fast-fail if the local GPU engine has died.

        `timeout` is idle silence, not wall-clock. session/update (tokens, tools,
        thoughts) and local prefill ticks refresh `_last_acp_event`.
        """
        idle_limit = max(1.0, float(timeout))
        slice_s = 0.25
        while True:
            if self._turn_cancel.is_set():
                self._pending.pop(rid, None)
                raise RuntimeError("cancelled")
            if ev.wait(slice_s):
                return
            idle = time.time() - float(self._last_acp_event or 0)
            try:
                local = uses_local_text_llm(getattr(self.bot, "model", ""))
            except Exception:
                local = False
            if local:
                if idle < ACP_LOCAL_DOWN_GRACE_SEC:
                    continue
                serving = False
                try:
                    serving = bool(local_model_serving(getattr(self.bot, "model", "")))
                except Exception:
                    serving = False
                if serving:
                    if idle < idle_limit:
                        continue
                    self._abandon_prompt(rid)
                    raise acp_prompt_timeout_error(getattr(self.bot, "model", ""), True)
                try:
                    engine = local_llm_engine_state()
                except Exception:
                    engine = "down"
                if engine in ("up", "starting"):
                    if idle < idle_limit:
                        continue
                    self._abandon_prompt(rid)
                    raise acp_prompt_timeout_error(getattr(self.bot, "model", ""))
                self._abandon_prompt(rid)
                raise acp_prompt_timeout_error(getattr(self.bot, "model", ""), False)
            if idle < idle_limit:
                continue
            self._abandon_prompt(rid)
            raise acp_prompt_timeout_error(getattr(self.bot, "model", ""))

    def ensure(self) -> None:
        if self.proc and self.proc.poll() is None and self.session_id:
            self.alive = True
            return
        with self._life:
            if self.proc and self.proc.poll() is None and self.session_id:
                self.alive = True
                return
            self._kill_proc()
            self._start_locked(load_session_id=self.bot.last_acp_session_id())

    def prompt(self, text: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self._turn_cancel.clear()
        self.ensure()
        if not self.session_id:
            raise RuntimeError("no session")
        try:
            self.bot.memory.append_turn(botmem.Role.USER, text or "")
        except Exception:
            pass
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for img in images or []:
            data = str(img.get("data") or "")
            mime = str(img.get("mime") or "image/png")
            if not data and img.get("path"):
                p = self.bot.workspace / str(img["path"])
                if p.is_file():
                    try:
                        raw = p.read_bytes()
                    except OSError:
                        raw = b""
                    if raw and len(raw) <= _MM_MAX_BYTES:
                        data = base64.b64encode(raw).decode("ascii")
                        mime = mime or "image/jpeg"
            if data:
                blocks.append({"type": "image", "mimeType": mime, "data": data})
            elif img.get("path"):
                blocks.append({"type": "text", "text": f"[image attached at {img.get('path')}]"})
        if not blocks:
            raise RuntimeError("empty prompt")
        # Per-turn real-decode anchors: reset on turn start. The ACP stream is
        # per-token, so first visible output -> last is a real decode span.
        self.bot._first_out_wall = None
        self.bot._last_chunk_wall = None
        prompt_timeout = ACP_PROMPT_MAX_SEC
        try:
            result = self.request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": blocks},
                timeout=prompt_timeout,
            )
        except RuntimeError:
            # Local Qwen ACP advertises image:false — fall back to paths in text.
            if not images:
                raise
            result = self.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": local_llm_prompt_blocks(text, images, workspace=self.bot.workspace),
                },
                timeout=prompt_timeout,
            )
        self.bot.note_real_acp_usage(result if isinstance(result, dict) else None)
        return result

    def cancel(self) -> None:
        self._turn_cancel.set()
        stop_local_prefill_progress(self.bot)
        abort_local_llm_conns(str(getattr(self.bot, "id", "") or ""))
        rid = self._prompt_rid
        if rid is not None:
            pending = self._pending.pop(rid, None)
            if pending:
                ev, box = pending
                box["error"] = "cancelled"
                ev.set()
        if not self.session_id:
            return
        try:
            # ACP session/cancel is a notification — an `id` made hermes ignore Stop
            # and the in-flight local completion kept running.
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self.session_id},
                }
            )
        except Exception:
            pass
        if rid is not None:
            try:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/cancel_request",
                        "params": {"requestId": rid},
                    }
                )
            except Exception:
                pass

    def rewind_last_prompt(self) -> dict[str, Any]:
        """Drop the last user prompt from the live ACP session (Hermes /undo)."""
        self.ensure()
        if not self.session_id:
            return {"ok": False, "error": "no session"}
        try:
            self.cancel()
        except Exception:
            pass
        last_err: Exception | None = None
        points: Any = {}
        for method in ("x.ai/rewind/points", "_x.ai/rewind/points"):
            try:
                points = self.request(method, {"sessionId": self.session_id}, timeout=15)
                last_err = None
                break
            except Exception as e:
                last_err = e
        target: int | None = None
        lst: list[Any] = []
        if isinstance(points, dict):
            raw = points.get("points") or points.get("rewindPoints") or points.get("promptIndexes")
            if isinstance(raw, list):
                lst = raw
            for key in ("promptIndex", "currentPromptIndex", "nextPromptIndex"):
                if isinstance(points.get(key), int) and target is None:
                    target = max(0, int(points[key]) - (0 if key == "promptIndex" else 1))
        elif isinstance(points, list):
            lst = points
        if lst:
            last = lst[-1]
            if isinstance(last, dict):
                for key in ("promptIndex", "targetPromptIndex", "index"):
                    if last.get(key) is not None:
                        try:
                            target = int(last[key])
                            break
                        except (TypeError, ValueError):
                            pass
            elif isinstance(last, int):
                target = last
        payloads: list[dict[str, Any]] = []
        if target is not None:
            payloads.extend(
                [
                    {"sessionId": self.session_id, "targetPromptIndex": int(target), "mode": "conversationOnly"},
                    {"sessionId": self.session_id, "targetPromptIndex": int(target), "conversation_only": True},
                    {"sessionId": self.session_id, "targetPromptIndex": int(target), "mode": "ConversationOnly"},
                    {"sessionId": self.session_id, "target_prompt_index": int(target), "mode": "conversationOnly"},
                ]
            )
        payloads.append({"sessionId": self.session_id, "mode": "conversationOnly"})
        for method in ("x.ai/rewind/execute", "_x.ai/rewind/execute", "x.ai/rewind", "_x.ai/rewind"):
            for params in payloads:
                try:
                    result = self.request(method, params, timeout=30)
                    return {"ok": True, "method": method, "target": target, "result": result}
                except Exception as e:
                    last_err = e
        return {"ok": False, "error": str(last_err) if last_err else "rewind unavailable", "target": target}

    _AGENT_ERR_RE = re.compile(
        r"\b(Traceback|Exception|Error|CRITICAL|FATAL)\b"
        r"|No LLM provider|^\s*(ERROR|CRITICAL|FATAL)\s*\]"
        r"|connection (refused|reset)|timed?\s?out|out of memory",
        re.IGNORECASE,
    )

    def _read_stderr(self, gen: int) -> None:
        """Tail the agent child's stderr.

        Log noise (Hermes' ``[INFO]`` client/turn lines) is kept out of the
        chat and the status banner; it is only written to a per-bot file for
        debugging. Genuine errors are surfaced *in the conversation* as a
        system message (persisted) instead of the top banner.
        """
        proc = self.proc
        if not proc or not proc.stderr:
            return
        log_path = self.bot.agent_home / "acp.stderr.log"
        for line in proc.stderr:
            if gen != self._generation:
                return
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("{") and '"msg"' in line:
                continue
            try:
                if log_path.is_file() and log_path.stat().st_size > 1_000_000:
                    log_path.unlink()
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            if not self._AGENT_ERR_RE.search(line):
                continue
            short = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*", "", line).strip()
            short = re.sub(r"^\[(INFO|DEBUG|WARNING)\]\s*", "", short)
            short = re.sub(r"thread=Thread-\d+\s*<[^>]*>:\d+\s*", "", short)
            if len(short) > 300:
                short = short[:300] + "…"
            try:
                self.bot.append_msg("system", f"⚠ {short}")
            except Exception:
                pass
            emit({"type": "chat", "bot_id": self.bot.id, "role": "system", "text": f"⚠ {short}"})

    def _read_stdout(self, gen: int) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                if gen != self._generation:
                    return
                try:
                    raw = proc.stdout.readline()
                except Exception:
                    break
                if raw == "":
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    self._dispatch(msg)
                except Exception:
                    traceback.print_exc()
        finally:
            if gen != self._generation:
                return
            code = proc.poll() if proc else None
            if code is None and proc is self.proc:
                return
            self.alive = False
            self._fail_pending(f"agent exited ({code})")
            if self._stopping:
                return
            threading.Thread(target=self._respawn, args=(gen, code), daemon=True).start()

    def _respawn(self, gen: int, code: int | None) -> None:
        now = time.time()
        self._respawn_at = [t for t in self._respawn_at if now - t < 20]
        self._respawn_at.append(now)
        if len(self._respawn_at) >= 4:
            self.bot.status = "Ready"
            emit(
                {
                    "type": "status",
                    "bot_id": self.bot.id,
                    "text": "Ready",
                    "surface": self.bot.surface,
                    "control": self.bot.control,
                }
            )
            return
        time.sleep(min(4.0, 0.4 * len(self._respawn_at)))
        if self._stopping or gen != self._generation:
            return
        if self.bot.acp is not self:
            return
        if self.proc and self.proc.poll() is None and self.session_id:
            self.alive = True
            self.bot.status = "Ready"
            emit(
                {
                    "type": "status",
                    "bot_id": self.bot.id,
                    "text": "Ready",
                    "surface": self.bot.surface,
                    "control": self.bot.control,
                }
            )
            return
        self.bot.status = "Reconnecting…"
        emit(
            {
                "type": "status",
                "bot_id": self.bot.id,
                "text": "Reconnecting…",
                "surface": self.bot.surface,
                "control": self.bot.control,
            }
        )
        try:
            self.start(load_session_id=self.bot.last_acp_session_id())
            self.bot.status = "Ready"
            emit(
                {
                    "type": "status",
                    "bot_id": self.bot.id,
                    "text": "Ready",
                    "surface": self.bot.surface,
                    "control": self.bot.control,
                }
            )
        except Exception as e:
            emit(
                {
                    "type": "status",
                    "bot_id": self.bot.id,
                    "text": f"Agent exited ({code}): {e}",
                    "surface": "log",
                    "control": self.bot.control,
                }
            )

    def _dispatch(self, msg: dict[str, Any]) -> None:
        self._last_acp_event = time.time()
        mid = msg.get("id")
        if mid is not None and mid in self._pending and "method" not in msg:
            ev, box = self._pending.pop(mid)
            box.update(msg)
            ev.set()
            return
        method = msg.get("method")
        if method in (
            "session/update",
            "_x.ai/session/update",
            "_x.ai/session_notification",
        ):
            self._on_session_update(msg.get("params") or {})
        elif method == "_x.ai/session/prompt_complete":
            params = msg.get("params") or {}
            self.bot.observe_acp_update(params, kind_hint="prompt_complete")
        elif method == "_x.ai/models/update":
            params = msg.get("params") or {}
            self.bot.apply_models(
                {
                    "currentModelId": params.get("currentModelId"),
                    "availableModels": params.get("availableModels") or [],
                }
            )
        elif method == "session/request_permission":
            req_id = msg.get("id")
            if req_id is not None:
                option = "allow-always" if bot_kind_has_host_coding(self.bot) else "allow-once"
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"outcome": {"outcome": "selected", "optionId": option}},
                    }
                )
        elif method and msg.get("id") is not None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32601, "message": f"unimplemented {method}"},
                }
            )

    def _on_session_update(self, params: dict[str, Any]) -> None:
        update = params.get("update") or params
        kind = update.get("sessionUpdate") or ""
        if self._turn_cancel.is_set() and kind not in (
            "turn_completed",
            "response_completed",
            "available_commands_update",
        ):
            return
        self.bot.observe_acp_update(params)
        content = update.get("content") or {}
        text = ""
        if isinstance(content, dict):
            text = content.get("text") or ""
        title_raw = update.get("title") or update.get("kind") or ""
        title = title_raw if isinstance(title_raw, str) else json.dumps(title_raw)
        status_text = self.bot.status
        surface = self.bot.surface
        if kind == "agent_message_chunk" and text:
            self.bot.append_msg("assistant", text, chunk=True)
            status_text = "Speaking…"
        elif kind == "agent_thought_chunk" and text:
            self.bot.append_thought(text)
            status_text = "Thinking…"
        elif kind == "tool_call":
            title = title or "tool"
            status_text = f"Using {title}…"
            loc = (update.get("locations") or [{}])
            path = ""
            if loc and isinstance(loc[0], dict):
                path = loc[0].get("path") or ""
            if "browser" in title.lower():
                surface = "browser"
            elif any(x in title.lower() for x in ("term", "shell", "bash", "command")):
                surface = "terminal"
            elif path or "edit" in title.lower() or "write" in title.lower():
                surface = "file"
            self.bot.tool_log.append({"title": title, "path": path, "t": time.time()})
            self.bot.upsert_tool(update)
        elif kind == "tool_call_update":
            self.bot.upsert_tool(update)
            status_text = "Working…"
        elif kind == "available_commands_update":
            raw = (
                update.get("availableCommands")
                or update.get("commands")
                or update.get("available_commands")
                or []
            )
            cmds: list[dict[str, Any]] = []
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("command") or "").strip().lstrip("/")
                    if not name:
                        continue
                    hint = str(
                        item.get("description")
                        or item.get("hint")
                        or (item.get("input") or {}).get("hint")
                        or ""
                    )
                    entry: dict[str, Any] = {"name": name, "hint": hint}
                    inp = item.get("input")
                    if isinstance(inp, dict):
                        entry["input"] = {
                            key: inp[key]
                            for key in ("hint", "arguments")
                            if key in inp
                        }
                        if not hint:
                            entry["hint"] = str(inp.get("hint") or "")
                    src = item.get("source") or item.get("kind") or ""
                    if src:
                        entry["source"] = str(src)
                    cmds.append(entry)
            self.bot.slash_commands = cmds
            emit({"type": "session.commands", "bot_id": self.bot.id, "commands": cmds})
            return
        elif kind == "user_message_chunk":
            return
        elif kind in ("turn_completed", "response_completed"):
            usage = update.get("usage") if isinstance(update.get("usage"), dict) else {}
            elapsed = update.get("elapsed_ms")
            if elapsed is None:
                elapsed = usage.get("apiDurationMs")
            self.bot.finish_generation(usage, params.get("_meta") if isinstance(params.get("_meta"), dict) else {}, elapsed)
            self.bot.finish_turn(elapsed)
            status_text = "Ready"
            payload_update = dict(update)
            last = self.bot.messages[-1] if self.bot.messages else None
            if last and last.get("role") == "assistant" and last.get("text"):
                payload_update["content"] = {"type": "text", "text": last["text"]}
            emit(
                {
                    "type": "session.update",
                    "bot_id": self.bot.id,
                    "update": payload_update,
                }
            )
            emit(
                {
                    "type": "status",
                    "bot_id": self.bot.id,
                    "text": status_text,
                    "surface": surface,
                    "control": self.bot.control,
                }
            )
            self.bot.status = status_text
            return
        self.bot.status = status_text
        self.bot.surface = surface
        emit(
            {
                "type": "session.update",
                "bot_id": self.bot.id,
                "update": update,
            }
        )
        emit(
            {
                "type": "status",
                "bot_id": self.bot.id,
                "text": status_text,
                "surface": surface,
                "control": self.bot.control,
            }
        )

    def _kill_proc(self) -> None:
        self._generation += 1
        proc = self.proc
        self.alive = False
        self.session_id = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
            except Exception:
                pass
        self.proc = None

    def stop(self) -> None:
        self._stopping = True
        self._fail_pending("agent stopped")
        with self._life:
            self._generation += 1
            self._kill_proc()


class Bot:
    def __init__(
        self,
        bid: str,
        name: str,
        description: str,
        soul: str,
        model: str,
        emoji: str,
        kind: str = "",
    ) -> None:
        self.id = bid
        self.name = name
        self.description = description
        self.soul = soul
        self.model = model
        self.effort = ""
        self.emoji = emoji
        self.kind = normalize_bot_kind(kind)
        self.avatar_color = ""
        self.avatar_shape = ""
        self.models: list[dict[str, Any]] = []
        self.context_used = 0
        self.context_window = tel.resolve_context_window(model)
        self.context_source = ""
        self.tps = 0.0
        self.speed_source = ""
        self.token_source = ""
        self._gen: dict[str, Any] = {}
        self._first_out_wall: float | None = None
        self._last_chunk_wall: float | None = None
        # Real-usage latches (see note_real_acp_usage). The ACP session/prompt
        # response carries the engine's real per-session cumulative
        # completionTokens; the per-turn delta is the true output, and the
        # per-token ACP stream gives the real decode span.
        self._acp_prev_completion: int | None = None
        self._real_decode_span: float | None = None
        self.telemetry: dict[str, Any] = {}
        self.workspace_id = workspace_id(bid)
        self.control = "agent_controlled"
        self.status = "Ready"
        self.surface = "preview"
        self.desktop_cursor = {"x": 500.0, "y": 500.0}
        self.desktop_view: dict[str, Any] = {}
        self.desktop_view_seq = 0
        self.desktop_view_event = threading.Event()
        self.robot_state: dict[str, Any] = robot_sim.default_state()
        self.embodiment = EmbodimentService()
        self.llm_pin_key = ""
        self.llm_pin_url = ""
        self.llm_pin_served = ""
        self._motor_hold = False
        self._plan_timer: threading.Timer | None = None
        self._prompt_lock = threading.Lock()
        self._prompt_busy = False
        self._prompt_queue: list[tuple[str, list[dict[str, Any]]]] = []
        self.desktop_objects: list[dict[str, Any]] = []
        self.desktop_last_action: dict[str, Any] = {}
        self.desktop_last_change: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []
        self.slash_commands: list[dict[str, Any]] = []
        self.chat_id: str | None = None
        self.tool_log: list[dict[str, Any]] = []
        self.routines: list[dict[str, Any]] = []
        self.acp = AcpClient(self)
        self._chunk: str = ""
        self._routine_lock = threading.Lock()
        self._browser_lock = threading.RLock()
        self.browser: BrowserSurface | None = None
        self._observer_lock = threading.RLock()
        self.observer: BrowserSurface | None = None
        self.observer_last_seen_seq = 0
        self.observer_last_hash = ""
        self.shell: PtySurface | None = None
        self.tui: PtySurface | None = None
        self.tui_mirror: SessionMirror | None = None
        self.site: LocalSite | None = None
        self.dev_proc: subprocess.Popen[str] | None = None
        self.dev_proc_command = ""
        self.dev_proc_cwd = "."
        self.dev_proc_output = ""
        self._dev_proc_lock = threading.RLock()

        self.root = USER_AGENT_HOME / "bots" / bid
        self.agent_home = self.root / "hermes-home"
        self.desk = HERMES_DESKS / bid
        self.workspace = self.desk / "workspace"
        self.browser_profile = self.desk / "browser-profile"
        self.observer_profile = self.desk / "observer-profile"
        self.www_port = http_port(bid)
        self.observer_cdp_port = observer_port(bid)
        self._memory: botmem.MemoryManager | None = None
        self.workspace_share_with: list[str] = []
        self.workspace_share_requests: list[dict[str, Any]] = []
        self._load_robot_state()
        self.embodiment.sync_robot_state(self.robot_state)
        self.load_shares()

    @property
    def memory(self) -> botmem.MemoryManager:
        if self._memory is None:
            self._memory = botmem.MemoryManager.for_workspace(self.workspace, model_id=self.model)
        return self._memory

    def chat_path(self) -> Path:
        return self.root / "conversations" / "chat.jsonl"

    def log_path(self) -> Path:
        return self.root / "conversations" / "log.jsonl"

    def conv_dir(self) -> Path:
        return self.root / "conversations"

    def take_prompt_turn(self) -> bool:
        """True if this caller should start ACP now; False means the text was queued."""
        with self._prompt_lock:
            if self._prompt_busy:
                return False
            self._prompt_busy = True
            return True

    def queue_prompt(self, text: str, images: list[dict[str, Any]] | None) -> None:
        with self._prompt_lock:
            self._prompt_queue.append((text, list(images or [])))

    def finish_prompt_turn(self) -> tuple[str, list[dict[str, Any]]] | None:
        with self._prompt_lock:
            if self._prompt_queue:
                return self._prompt_queue.pop(0)
            self._prompt_busy = False
            return None

    def cancel_prompt_queue(self) -> None:
        with self._prompt_lock:
            self._prompt_queue.clear()

    def robot_state_path(self) -> Path:
        return self.root / "robot_state.json"

    def _load_robot_state(self) -> None:
        path = self.robot_state_path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("joints"), dict):
                st = robot_sim.default_state()
                st.update(data)
                st["joints"] = {**robot_sim.default_state()["joints"], **data["joints"]}
                if isinstance(data.get("live"), dict) and data["live"]:
                    st["live"] = dict(data["live"])
                self.robot_state = st
                self.embodiment.sync_robot_state(self.robot_state)
        self._restore_pose_after_restart()

    def _restore_pose_after_restart(self) -> None:
        """Resume last BodyState. Browser reconnect must not zero the body."""
        if str(self.kind or "") != "teela-brain":
            return
        try:
            import body_state as _bs

            store = teela_body_store(self)
            snap = store.snapshot()
            joints = _bs._joints_map(snap.get("joints"))
            if not joints:
                live = self.robot_state.get("live") if isinstance(self.robot_state.get("live"), dict) else {}
                joints = dict(live or self.robot_state.get("joints") or {})
                if joints:
                    store.apply_measured(
                        joints,
                        pose=str(self.robot_state.get("pose") or "home"),
                        motion=str(self.robot_state.get("motion") or "idle"),
                        waving=bool(self.robot_state.get("waving")),
                        source="restore",
                    )
                    snap = store.snapshot()
            if joints:
                ov = _bs.restore_overlay(self.id, snap)
                virtual_body.apply_to_robot(self.robot_state, ov)
                self.embodiment.sync_robot_state(self.robot_state)
                self.save_robot_state()
        except Exception:
            pass

    def save_robot_state(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.robot_state_path().write_text(
                json.dumps(self.robot_state, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _schedule_plan_step(self, hold: float) -> None:
        old = getattr(self, "_plan_timer", None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        plan = self.robot_state.get("plan") if isinstance(self.robot_state.get("plan"), dict) else None
        steps = (plan or {}).get("steps") if isinstance((plan or {}).get("steps"), list) else []
        i = int((plan or {}).get("i") or 0)
        if i + 1 >= len(steps):
            self._plan_timer = None
            return

        def _go() -> None:
            nxt = robot_sim.advance_plan(self.robot_state)
            if not nxt:
                return
            result, ev = self.apply_robot({**nxt, "_plan_step": True})
            if result.get("ok"):
                emit(ev)

        timer = threading.Timer(max(0.4, float(hold)), _go)
        timer.daemon = True
        self._plan_timer = timer
        timer.start()

    def apply_robot(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        echo = robot_sim.is_plan_echo(self.robot_state, body)
        incoming = str(body.get("cmd") or body.get("command") or "").strip().lower()
        sm_owned = False
        if incoming not in {"", "status", "state", "live", "telemetry"} and getattr(self, "_sm_before", None) is None:
            _capture_sensorimotor_before(self)
            sm_owned = True
        keep_plan = incoming in {"", "status", "state", "live", "telemetry"} or bool(body.get("_plan_step")) or echo
        if not keep_plan:
            old = getattr(self, "_plan_timer", None)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
            self._plan_timer = None
        result = robot_sim.apply(self.robot_state, body)
        self.robot_state["joints"] = dict(result.get("joints") or self.robot_state.get("joints") or {})
        for k in ("pose", "motion", "motors", "estop", "seq", "battery", "wave_hold", "walk_direction", "heading", "plan", "waving"):
            if k in result:
                self.robot_state[k] = result[k]
        cmd = str(body.get("cmd") or "status")
        dispatch = body
        if cmd == "plan" and result.get("ok"):
            steps = (self.robot_state.get("plan") or {}).get("steps") or []
            if steps and isinstance(steps[0], dict):
                dispatch = dict(steps[0])
                cmd = str(dispatch.get("cmd") or "pose")
        if result.get("ok") and cmd not in {"", "status", "state", "live", "telemetry"}:
            robot_sim.remember_move(
                self.robot_state,
                dispatch,
                user=str(getattr(self, "_motor_user", "") or ""),
            )
        self.embodiment.sync_robot_state(self.robot_state, body)
        self.save_robot_state()
        mesh = {"mode": "none", "hardware": False}
        if cmd not in {"", "status", "state", "plan", "live", "telemetry"} and result.get("ok"):
            try:
                mesh = motion.dispatch(
                    cluster,
                    {**dispatch, "cmd": cmd, "joints": dispatch.get("joints") or result.get("joints")},
                    origin=(cluster.node_name if cluster is not None else ""),
                    bot_id=self.id,
                )
            except Exception:
                mesh = {"mode": "error", "hardware": False}
        if (
            result.get("ok")
            and isinstance(self.robot_state.get("plan"), dict)
            and not echo
            and incoming not in {"", "status", "state", "live", "telemetry"}
        ):
            self._schedule_plan_step(float(result.get("plan_hold") or robot_sim.step_hold_s(dispatch)))
        ev = {
            "type": "desktop.action",
            "bot_id": self.id,
            "action": "robot",
            "app": "preview",
            "cmd": cmd,
            "joint": body.get("joint"),
            "value": body.get("value", body.get("degrees")),
            "delta": body.get("delta"),
            "dir": body.get("dir") or body.get("direction"),
            "pose": result.get("pose") or body.get("pose") or body.get("name"),
            "joints": (
                {row["joint"]: row["value"] for row in (result.get("set") or []) if isinstance(row, dict) and row.get("joint")}
                or body.get("joints")
                or result.get("joints")
            ),
            "motion": result.get("motion"),
            "arm_kind": body.get("arm_kind") or dispatch.get("arm_kind"),
            "arm_side": body.get("arm_side") or dispatch.get("arm_side"),
            "direction": result.get("walk_direction") or body.get("direction") or body.get("dir"),
            "seq": result.get("seq"),
            "motors": body.get("motors"),
            "estop": body.get("estop"),
            "mesh": mesh,
        }
        self.surface = "preview"
        out = dict(result)
        out["app"] = "preview"
        out["mesh"] = mesh
        out["history"] = list(self.robot_state.get("history") or [])
        out["spoken"] = robot_sim.describe_body(self.robot_state)
        out["live"] = dict(result.get("live") or (self.robot_state.get("live") or {}))
        out["next"] = "Call desktop_watch to see the MiniOS robot move."
        if sm_owned:
            try:
                teela_publish_body(self, last_action=str(cmd or incoming))
            except Exception:
                pass
            try:
                record_minios_sensorimotor(
                    self,
                    skill=str(cmd or incoming),
                    command=str(cmd or incoming),
                    params=dict(body),
                    result=out,
                )
            except Exception:
                pass
        return out, ev

    def timeline_path(self) -> Path:
        return self.conv_dir() / "timeline.json"

    def archive_dir(self) -> Path:
        d = self.conv_dir() / "archive"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def archive_path(self, cid: str) -> Path:
        return self.archive_dir() / f"{cid}.jsonl"

    def append_log(self, event: dict[str, Any]) -> None:
        p = self.log_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.time(), **event}
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def load_messages(self) -> None:
        p = self.chat_path()
        loaded: list[dict[str, Any]] = []
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("role"):
                        if obj.get("role") == "assistant":
                            obj["text"] = strip_model_think_tags(strip_assistant_padding(obj.get("text") or ""))
                        if not obj.get("text") and not obj.get("images") and obj.get("role") not in ("tool", "thought", "worked"):
                            continue
                        loaded.append(obj)
            except OSError:
                loaded = []
        self.messages = loaded
        if loaded and not self.log_path().is_file():
            self.append_log({"type": "seed", "messages": [{k: v for k, v in m.items() if k != "open"} for m in loaded]})
        self.ensure_timeline()

    def persist_messages(self) -> None:
        p = self.chat_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for m in self.messages:
                row = {k: v for k, v in m.items() if k != "open"}
                if row.get("role") == "assistant":
                    row["text"] = strip_model_think_tags(strip_assistant_padding(row.get("text") or ""))
                if not row.get("text") and not row.get("images") and row.get("role") not in ("tool", "thought", "worked"):
                    continue
                lines.append(json.dumps(row, ensure_ascii=False))
            p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            pass
        self._touch_active_chat()

    def _new_chat_id(self) -> str:
        return "c_" + uuid.uuid4().hex[:12]

    def _ts_unix(self, value: Any) -> float:
        t = conv_io.unix(value)
        return float(t) if t is not None else time.time()

    def _ts_iso(self, value: Any) -> str:
        return datetime.fromtimestamp(self._ts_unix(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _visible_messages(self, messages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        out = []
        for m in messages if messages is not None else self.messages:
            if m.get("text") or m.get("images"):
                out.append(m)
        return out

    def _title_from_text(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip()
        if not clean:
            return "New chat"
        return f"{clean[:42]}…" if len(clean) > 42 else clean

    def _title_from_messages(self, messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") == "user" and (m.get("text") or "").strip():
                return self._title_from_text(str(m.get("text") or ""))
        return "New chat"

    def _preview_from_messages(self, messages: list[dict[str, Any]]) -> str:
        for m in reversed(messages):
            t = (m.get("text") or "").strip()
            if t:
                return t
        return "Empty chat"

    def _chat_meta_from_messages(
        self,
        cid: str,
        messages: list[dict[str, Any]],
        created_at: Any = None,
    ) -> dict[str, Any]:
        visible = self._visible_messages(messages)
        ts_list = [self._ts_unix(m.get("ts")) for m in visible if m.get("ts") is not None]
        created = self._ts_unix(created_at) if created_at is not None else (min(ts_list) if ts_list else time.time())
        updated = max(ts_list) if ts_list else created
        return {
            "id": cid,
            "title": self._title_from_messages(visible),
            "createdAt": created,
            "updatedAt": updated,
            "preview": self._preview_from_messages(visible),
            "messageCount": len(visible),
        }

    def _write_timeline(self, data: dict[str, Any]) -> None:
        p = self.timeline_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        loaded: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("role"):
                    if obj.get("role") == "assistant":
                        obj["text"] = strip_assistant_padding(obj.get("text") or "")
                    if not obj.get("text") and not obj.get("images") and obj.get("role") not in ("tool", "thought", "worked"):
                        continue
                    loaded.append(obj)
        except OSError:
            return []
        return loaded

    def _write_jsonl(self, path: Path, messages: list[dict[str, Any]]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for m in messages:
                row = {k: v for k, v in m.items() if k != "open"}
                if not row.get("text") and not row.get("images") and row.get("role") not in ("tool", "thought", "worked"):
                    continue
                lines.append(json.dumps(row, ensure_ascii=False))
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            pass

    def _public_chat(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": row.get("id"),
            "title": row.get("title") or "New chat",
            "createdAt": self._ts_iso(row.get("createdAt")),
            "updatedAt": self._ts_iso(row.get("updatedAt") or row.get("createdAt")),
            "preview": row.get("preview") or "Empty chat",
            "messageCount": int(row.get("messageCount") or 0),
        }
        if row.get("match"):
            out["match"] = row["match"]
        return out

    def ensure_timeline(self) -> dict[str, Any]:
        self.conv_dir().mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] | None = None
        p = self.timeline_path()
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("chats"), list) and raw["chats"]:
                    data = raw
            except (json.JSONDecodeError, OSError):
                data = None
        if data is None:
            cid = self._new_chat_id()
            meta = self._chat_meta_from_messages(cid, self.messages)
            data = {"activeId": cid, "chats": [meta]}
            self._write_timeline(data)
        active = data.get("activeId")
        if not any(isinstance(c, dict) and c.get("id") == active for c in data["chats"]):
            active = data["chats"][0].get("id")
            data["activeId"] = active
            self._write_timeline(data)
        self.chat_id = str(active or "")
        return data

    def _touch_active_chat(self) -> None:
        data = self.ensure_timeline()
        cid = self.chat_id or data.get("activeId") or self._new_chat_id()
        found = False
        for row in data["chats"]:
            if not isinstance(row, dict):
                continue
            if row.get("id") == cid:
                created = row.get("createdAt")
                row.update(self._chat_meta_from_messages(cid, self.messages, created_at=created))
                found = True
                break
        if not found:
            data["chats"].append(self._chat_meta_from_messages(cid, self.messages))
        data["activeId"] = cid
        self.chat_id = cid
        self._write_timeline(data)

    def _snapshot_active(self) -> None:
        data = self.ensure_timeline()
        cid = self.chat_id or data.get("activeId")
        if not cid:
            return
        self.persist_messages()
        self._write_jsonl(self.archive_path(str(cid)), self.messages)

    def _messages_for_chat(self, cid: str) -> list[dict[str, Any]]:
        if cid and cid == self.chat_id:
            return list(self.messages)
        return self._read_jsonl(self.archive_path(cid))

    def _match_snippet(self, text: str, query: str, width: int = 72) -> str:
        blob = re.sub(r"\s+", " ", text or "").strip()
        if not blob:
            return ""
        low = blob.lower()
        i = low.find(query)
        if i < 0:
            return ""
        start = max(0, i - 18)
        end = min(len(blob), i + len(query) + width)
        snippet = blob[start:end]
        if start:
            snippet = "…" + snippet
        if end < len(blob):
            snippet += "…"
        return snippet

    def active_chat_public(self) -> dict[str, Any] | None:
        data = self.ensure_timeline()
        for row in data.get("chats") or []:
            if isinstance(row, dict) and row.get("id") == data.get("activeId"):
                return self._public_chat(row)
        return None

    def list_chats(self, query: str = "") -> dict[str, Any]:
        self._touch_active_chat()
        data = self.ensure_timeline()
        q = (query or "").strip().lower()
        chats: list[dict[str, Any]] = []
        for row in data.get("chats") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            item = dict(row)
            if q:
                msgs = self._messages_for_chat(str(item["id"]))
                hay_parts = [str(item.get("title") or ""), str(item.get("preview") or "")]
                hay_parts.extend(str(m.get("text") or "") for m in msgs)
                hay = "\n".join(hay_parts)
                if q not in hay.lower():
                    continue
                snippet = ""
                for part in hay_parts:
                    snippet = self._match_snippet(part, q)
                    if snippet:
                        break
                if snippet:
                    item["match"] = snippet
            chats.append(self._public_chat(item))
        chats.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
        return {"activeId": data.get("activeId"), "chats": chats}

    def _emit_chats(self, kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.list_chats()
        event = {
            "type": kind,
            "bot_id": self.id,
            "bot": self.profile(),
            "timeline": payload,
        }
        if extra:
            event.update(extra)
        emit(event)
        emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
        return {**payload, "bot": self.profile()}

    def new_chat(self) -> dict[str, Any]:
        self.ensure_timeline()
        if not self._visible_messages():
            return self._emit_chats("chats.updated")
        self._snapshot_active()
        cid = self._new_chat_id()
        data = self.ensure_timeline()
        self.messages = []
        self.reset_telemetry()
        self._seed_usage_from_disk()
        data["chats"].append(self._chat_meta_from_messages(cid, []))
        data["activeId"] = cid
        self.chat_id = cid
        self._write_timeline(data)
        self.persist_messages()
        self.append_log({"type": "chat.new", "chat_id": cid})
        self._restart_session()
        self.status = "Ready"
        self._emit_usage()
        return self._emit_chats("chats.updated", {"chat_id": cid})

    def open_chat(self, cid: str) -> dict[str, Any]:
        cid = (cid or "").strip()
        data = self.ensure_timeline()
        if not any(isinstance(c, dict) and c.get("id") == cid for c in data["chats"]):
            raise ValueError("chat not found")
        if cid == self.chat_id:
            return self._emit_chats("chats.updated", {"chat_id": cid})
        self._snapshot_active()
        self.messages = self._read_jsonl(self.archive_path(cid))
        self.chat_id = cid
        data["activeId"] = cid
        self._write_timeline(data)
        self.persist_messages()
        self.reset_telemetry()
        self.append_log({"type": "chat.open", "chat_id": cid})
        chat_row = next((c for c in data["chats"] if isinstance(c, dict) and c.get("id") == cid), None)
        self._restart_session(load_session_id=str((chat_row or {}).get("agentSession") or "") or None)
        self.status = "Ready"
        self._emit_usage()
        return self._emit_chats("chats.updated", {"chat_id": cid})

    def delete_chat(self, cid: str) -> dict[str, Any]:
        cid = (cid or "").strip()
        data = self.ensure_timeline()
        chats = [c for c in data["chats"] if isinstance(c, dict) and c.get("id") == cid]
        if not chats:
            raise ValueError("chat not found")
        if len(data["chats"]) <= 1:
            self.messages = []
            self.persist_messages()
            self.append_log({"type": "chat.delete", "chat_id": cid, "cleared": True})
            self.reset_telemetry()
            self._restart_session()
            self.status = "Ready"
            return self._emit_chats("chats.updated", {"chat_id": cid})
        data["chats"] = [c for c in data["chats"] if not (isinstance(c, dict) and c.get("id") == cid)]
        arch = self.archive_path(cid)
        try:
            arch.unlink(missing_ok=True)
        except OSError:
            pass
        switching = self.chat_id == cid
        if switching:
            remaining = sorted(
                [c for c in data["chats"] if isinstance(c, dict)],
                key=lambda c: self._ts_unix(c.get("updatedAt") or c.get("createdAt")),
                reverse=True,
            )
            next_id = str(remaining[0].get("id"))
            data["activeId"] = next_id
            self._write_timeline(data)
            self.messages = self._read_jsonl(self.archive_path(next_id))
            self.chat_id = next_id
            self.persist_messages()
            self.reset_telemetry()
            self._restart_session()
            self.status = "Ready"
        else:
            data["activeId"] = self.chat_id or data.get("activeId")
            self._write_timeline(data)
        self.append_log({"type": "chat.delete", "chat_id": cid})
        return self._emit_chats("chats.updated", {"deleted": cid})

    def append_msg(
        self,
        role: str,
        text: str,
        chunk: bool = False,
        images: list[dict[str, Any]] | None = None,
        via: str | None = None,
        peer: str | None = None,
    ) -> None:
        if chunk:
            incoming = text or ""
            if self.messages and self.messages[-1]["role"] == "assistant" and self.messages[-1].get("open"):
                cur = str(self.messages[-1].get("text") or "")
                self.messages[-1]["text"] = strip_assistant_padding(merge_assistant_stream(cur, incoming))
            else:
                visible = strip_assistant_padding(incoming)
                if not visible:
                    return
                if self.messages and self.messages[-1].get("open"):
                    self.messages[-1]["open"] = False
                self.messages.append({"role": "assistant", "text": visible, "open": True, "ts": time.time()})
            return
        if role == "assistant":
            text = strip_assistant_padding(text)
        if self.messages and self.messages[-1].get("open"):
            self.messages[-1]["open"] = False
        msg: dict[str, Any] = {"role": role, "text": text, "ts": time.time()}
        if images:
            msg["images"] = [
                {
                    "path": i.get("path"),
                    "mime": i.get("mime"),
                    "name": i.get("name") or i.get("label") or Path(str(i.get("path") or "")).name,
                }
                for i in images
            ]
        if via:
            msg["via"] = via
        if peer:
            msg["peer"] = peer
        if role == "assistant" and text:
            try:
                self.memory.append_turn(botmem.Role.ASSISTANT, text)
            except Exception:
                pass
        self.messages.append(msg)
        self.persist_messages()
        self.append_log({"type": "chat", "message": {k: v for k, v in msg.items() if k != "open"}})

    def close_chunk(self) -> None:
        if self.messages and self.messages[-1].get("open"):
            self.messages[-1]["open"] = False
            if self.messages[-1].get("role") == "assistant":
                self.messages[-1]["text"] = collapse_restarted_assistant(
                    strip_assistant_padding(self.messages[-1].get("text") or "")
                )
            if self.messages[-1].get("role") == "thought":
                self.messages[-1]["t1"] = time.time()
            closed = {k: v for k, v in self.messages[-1].items() if k != "open"}
            self.append_log({"type": "chat", "message": closed})
        self.persist_messages()

    def append_thought(self, text: str) -> None:
        incoming = text or ""
        if not incoming:
            return
        if self.messages and self.messages[-1].get("role") == "thought" and self.messages[-1].get("open"):
            cur = str(self.messages[-1].get("text") or "")
            self.messages[-1]["text"] = merge_assistant_stream(cur, incoming)
            self.messages[-1]["t1"] = time.time()
            return
        if self.messages and self.messages[-1].get("open"):
            self.messages[-1]["open"] = False
        now = time.time()
        self.messages.append({"role": "thought", "text": incoming, "open": True, "ts": now, "t0": now, "t1": now})

    def upsert_tool(self, update: dict[str, Any]) -> None:
        tid = str(update.get("toolCallId") or "")
        meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        xt = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
        inp = xt.get("input") if isinstance(xt.get("input"), dict) else {}
        name = str(xt.get("name") or update.get("title") or "")
        label = str(xt.get("label") or update.get("title") or "Tool")
        kind = str(update.get("kind") or xt.get("kind") or "")
        detail = str(update.get("title") or inp.get("description") or name or "")
        command = tool_command_from_update(update)
        snippet = tool_output_from_update(update)
        found: dict[str, Any] | None = None
        if tid:
            for m in reversed(self.messages):
                if m.get("role") == "tool" and m.get("id") == tid:
                    found = m
                    break
        if found is None:
            if self.messages and self.messages[-1].get("open"):
                self.messages[-1]["open"] = False
            found = {
                "role": "tool",
                "id": tid or ("tool_" + uuid.uuid4().hex[:8]),
                "text": label,
                "title": label,
                "name": name,
                "detail": detail,
                "command": command,
                "kind": kind,
                "status": str(update.get("status") or "running"),
                "ts": time.time(),
                "t0": time.time(),
                "open": True,
            }
            self.messages.append(found)
        else:
            if detail:
                found["detail"] = detail
                found["text"] = detail
            if command:
                found["command"] = command
            if kind:
                found["kind"] = kind
            if name:
                found["name"] = name
            if xt.get("label"):
                found["title"] = str(xt.get("label"))
            if update.get("status"):
                found["status"] = str(update.get("status"))
            found["t1"] = time.time()
        if snippet:
            found["output"] = snippet
        status = str(found.get("status") or "")
        if status in ("completed", "cancelled"):
            found["open"] = False
        elif status == "failed":
            found["open"] = True
        self.persist_messages()

    def finish_turn(self, elapsed_ms: Any = None) -> None:
        self.close_chunk()
        try:
            elapsed = int(elapsed_ms) if elapsed_ms is not None else None
        except (TypeError, ValueError):
            elapsed = None
        if elapsed is None:
            return
        target = None
        for m in reversed(self.messages):
            if m.get("role") in ("assistant", "tool", "thought"):
                target = m
                break
        if target is None:
            self.messages.append({"role": "worked", "text": "", "elapsed_ms": elapsed, "ts": time.time()})
        else:
            target["elapsed_ms"] = elapsed
        self.persist_messages()

    def clear_chat(self) -> dict[str, Any]:
        removed = len(self.messages)
        self.messages = []
        self.persist_messages()
        self.append_log({"type": "clear", "removed": removed})
        self.reset_telemetry()
        self._restart_session()
        self.status = "Ready"
        emit(
            {
                "type": "conversation.cleared",
                "bot_id": self.id,
                "removed": removed,
                "bot": self.profile(),
            }
        )
        emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
        self._emit_usage()
        return {"ok": True, "removed": removed, "bot": self.profile()}

    def import_conversation(
        self,
        raw: bytes,
        filename: str = "",
        mode: str = "append",
    ) -> dict[str, Any]:
        msgs, source, nconv = conv_io.from_bytes(raw, filename)
        if not msgs:
            raise ValueError(
                "could not read a conversation from that file "
                "(ChatGPT, Claude, Hermes/xAI, OpenAI, markdown, jsonl, or ZIP)"
            )
        if mode == "replace":
            self.messages = []
        imported = []
        for m in msgs:
            row = {"role": m["role"], "text": m["text"], "via": "import"}
            if m.get("ts"):
                row["ts"] = m["ts"]
            else:
                row["ts"] = time.time()
            imported.append(row)
        self.messages.extend(imported)
        self.persist_messages()
        self.append_log({"type": "import", "source": source, "messages": imported})
        md = conv_io.to_markdown(f"Imported from {source}", conv_io.visible(imported))
        inbox = self.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = inbox / f"imported-{stamp}-{uuid.uuid4().hex[:6]}.md"
        dest.write_text(md, encoding="utf-8")
        note = (
            f"Imported {len(imported)} messages from {source}"
            + (f" ({nconv} conversations)" if nconv > 1 else "")
            + f". Transcript saved to inbox/{dest.name} — read it if you need prior context."
        )
        self.append_msg("system", note)
        return {
            "ok": True,
            "source": source,
            "imported": len(imported),
            "conversations": nconv,
            "mode": mode,
            "file": f"inbox/{dest.name}",
        }

    def _last_turn_index(self) -> int | None:
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                return i
        return len(self.messages) - 1 if self.messages else None

    def _scrub_memory(self, texts: list[str]) -> int:
        needles: list[str] = []
        for t in texts:
            blob = re.sub(r"\s+", " ", t or "").strip()
            if len(blob) >= 32:
                needles.append(blob[:160])
            for line in (t or "").splitlines():
                line = line.strip()
                if len(line) >= 40:
                    needles.append(line[:160])
        # longest first so a short needle doesn't nibble a longer match first
        uniq: list[str] = []
        for n in sorted(set(needles), key=len, reverse=True):
            if n and n not in uniq:
                uniq.append(n)
        if not uniq:
            return 0
        roots = [self.root / "MEMORY", self.agent_home / "memory"]
        changed = 0
        for root in roots:
            if not root.is_dir():
                continue
            for p in root.rglob("*.md"):
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                original = body
                for needle in uniq:
                    if needle not in body:
                        continue
                    kept: list[str] = []
                    skip_blank = False
                    for line in body.splitlines(keepends=True):
                        if needle in line:
                            skip_blank = True
                            continue
                        if skip_blank and line.strip() == "":
                            skip_blank = False
                            continue
                        skip_blank = False
                        kept.append(line)
                    body = "".join(kept)
                if body != original:
                    try:
                        p.write_text(body, encoding="utf-8")
                        changed += 1
                    except OSError:
                        pass
        if changed:
            try:
                self._write_agents()
            except Exception:
                pass
        return changed

    def undo_last_turn(self) -> dict[str, Any]:
        if self.messages and self.messages[-1].get("open"):
            self.close_chunk()
        idx = self._last_turn_index()
        if idx is None:
            raise ValueError("nothing to undo")
        removed = self.messages[idx:]
        self.messages = self.messages[:idx]
        self.persist_messages()
        self.append_log(
            {
                "type": "undo",
                "removed": [{k: v for k, v in m.items() if k != "open"} for m in removed],
            }
        )
        texts = [str(m.get("text") or "") for m in removed if m.get("role") in ("user", "assistant")]
        mem_files = self._scrub_memory(texts)
        rewind = {"ok": False}
        try:
            rewind = self.acp.rewind_last_prompt()
        except Exception as e:
            rewind = {"ok": False, "error": str(e)}
        if not rewind.get("ok"):
            # Drop the undone turn from the live agent even if rewind RPC is missing.
            try:
                self.acp.stop()
            except Exception:
                pass
            self.acp = AcpClient(self)
            try:
                self.acp.start()
                rewind = {**rewind, "fallback": "session/new"}
            except Exception as e:
                rewind = {**rewind, "fallback_error": str(e)}
        self.status = "Ready"
        emit(
            {
                "type": "conversation.undone",
                "bot_id": self.id,
                "removed": len(removed),
                "bot": self.profile(),
            }
        )
        emit({"type": "status", "bot_id": self.id, "text": "Undid last turn", "surface": self.surface, "control": self.control})
        return {
            "ok": True,
            "removed": len(removed),
            "agent_rewound": bool(rewind.get("ok")),
            "rewind": {k: v for k, v in rewind.items() if k != "result"},
            "memory_files": mem_files,
        }

    def provision(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent_home.mkdir(parents=True, exist_ok=True)
        (self.root / "MEMORY").mkdir(exist_ok=True)
        (self.root / "inbox").mkdir(exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.browser_profile.mkdir(parents=True, exist_ok=True)
        self.observer_profile.mkdir(parents=True, exist_ok=True)
        (self.workspace / "inbox").mkdir(exist_ok=True)
        (self.workspace / "www").mkdir(exist_ok=True)
        self.ensure_home_dirs()
        ensure_body_md(self.workspace)
        (self.workspace / ".hermes" / "agents").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".hermes" / "skills").mkdir(parents=True, exist_ok=True)
        (self.root / "SOUL.md").write_text(self.soul, encoding="utf-8")
        (self.root / "agent.md").write_text(agent_md_for_kind(self.kind), encoding="utf-8")
        mem_md = self.root / "MEMORY" / "MEMORY.md"
        if not mem_md.is_file():
            mem_md.write_text("", encoding="utf-8")
        _ = self.memory  # load session_summary.json if a prior run left one
        self.write_profile()
        self._write_agents()
        shutil.copy2(self.root / "agent.md", self.workspace / ".hermes" / "agents" / "desk-bot.md")
        default, models = load_user_models()
        if not self.model:
            self.model = default
        write_child_config(
            self.agent_home,
            self.model,
            models,
            bot_id=self.id,
            permission_mode="always-approve" if bot_kind_has_host_coding(self) else "default",
            inherit_mcp=bot_kind_is_agent(self),
        )
        self._link_agent_home()
        copy_auth(self.agent_home / "auth.json")
        self.apply_models({"currentModelId": self.model, "availableModels": []})
        git_dir = self.workspace / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init", "-q"],
                cwd=self.workspace,
                check=False,
                capture_output=True,
            )
            (self.workspace / ".gitignore").write_text(".hermes/sandbox.toml\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.workspace,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=desk@local", "-c", "user.name=hermes-deskd", "commit", "-qm", "desk: init"],
                cwd=self.workspace,
                check=False,
                capture_output=True,
            )
        self._index_upsert()
        self.load_routines()
        (self.root / "conversations").mkdir(parents=True, exist_ok=True)
        if not self.messages:
            self.load_messages()
        threading.Thread(target=self.ensure_browser, daemon=True).start()
        threading.Thread(target=self.ensure_site, daemon=True).start()
        self._seed_usage_from_disk()

    def _seed_usage_from_disk(self) -> None:
        """Restore occupancy from the bot's Hermes ``state.db``.

        Hermes persists every turn to sqlite (``sessions`` rows carry
        input/output/cache tokens and the live ``model``). On a restart or
        chat op we re-seed the context meter from the most recent session that
        has tokens so the bar keeps its last known value instead of 0.
        """
        sid = self.acp.session_id if self.acp else None
        db = self.agent_home / "state.db"
        if not db.is_file():
            return
        try:
            import sqlite3

            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            con.row_factory = sqlite3.Row
            try:
                row = None
                if sid:
                    row = con.execute(
                        "select input_tokens, output_tokens, cache_read_tokens, "
                        "cache_write_tokens, reasoning_tokens, model from sessions "
                        "where id = ?",
                        (sid,),
                    ).fetchone()
                if row is None:
                    row = con.execute(
                        "select input_tokens, output_tokens, cache_read_tokens, "
                        "cache_write_tokens, reasoning_tokens, model, id from sessions "
                        "where (coalesce(input_tokens, 0) + coalesce(output_tokens, 0) "
                        "+ coalesce(cache_read_tokens, 0) + coalesce(cache_write_tokens, 0)) > 0 "
                        "order by last_activity_at desc limit 1"
                    ).fetchone()
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            return
        if row is None:
            return
        # Live window ~ prompt-side tokens (input + cache) + the turn's output.
        used = (
            tel._as_int(row["input_tokens"])
            or 0
        ) + (tel._as_int(row["cache_read_tokens"]) or 0) + (
            tel._as_int(row["output_tokens"]) or 0
        )
        if not used:
            return
        self.context_used = used
        self.context_source = "agent_runtime"
        self._emit_usage()

    def ensure_observer(self, *, restart: bool = False) -> None:
        """Keep a headless Hermes Desk UI mirror rendering this bot's MiniOS continuously."""
        with self._observer_lock:
            if self.observer and self.observer.healthy() and not restart:
                return
            if self.observer:
                try:
                    self.observer.stop()
                except Exception:
                    pass
                self.observer = None
            obs = BrowserSurface(
                f"{self.id}-observer",
                self.observer_profile,
                self.observer_cdp_port,
                view_w=1600,
                view_h=1000,
            )
            obs.start()
            target = f"http://127.0.0.1:{int(LISTEN_PORT)}/?observe={self.id}"
            obs.navigate(target, wait=4.0)
            # Wait until the observer UI has selected this bot and entered MiniOS-only mode.
            deadline = time.time() + 8.0
            ready = False
            while time.time() < deadline:
                ready = bool(obs.evaluate("Boolean(window.deskObserverReady)"))
                if ready:
                    break
                time.sleep(0.08)
            if not ready:
                detail = obs.evaluate("((document.body && document.body.innerText) || '').slice(0, 300)") or "observer UI did not become ready"
                obs.stop()
                raise RuntimeError(f"MiniOS visual observer did not become ready: {detail}")
            obs.screenshot(force=True)
            self.observer = obs
            emit({"type": "desktop.observer", "bot_id": self.id, "ready": True, "frame_seq": int(obs.frame_seq)})

    def observer_state(self, *, after: int = 0, wait: float = 0.0) -> dict[str, Any]:
        """Return the persistent MiniOS frame state plus a semantic object/accessibility map."""
        try:
            self.ensure_observer()
        except Exception as e:
            return {"ready": False, "error": str(e), "frame_seq": 0, "changed": False, "surface": self.surface}
        obs = self.observer
        if not obs:
            return {"ready": False, "frame_seq": 0, "changed": False, "surface": self.surface}
        deadline = time.time() + max(0.0, min(30.0, float(wait or 0.0)))
        while after and obs.frame_seq <= after and time.time() < deadline:
            time.sleep(0.08)
        frame = obs.screenshot(force=False) or b""
        digest = hashlib.sha256(frame).hexdigest()[:16] if frame else ""
        ui = obs.evaluate("""(() => {
          const area=document.querySelector('.ubuntu-desktop-area');
          const ar=area?.getBoundingClientRect();
          const norm=(el)=>{ if(!el||!ar) return null; const r=el.getBoundingClientRect();
            return [
              Math.max(0,Math.min(1000,Math.round(((r.left-ar.left)/Math.max(1,ar.width))*1000))),
              Math.max(0,Math.min(1000,Math.round(((r.top-ar.top)/Math.max(1,ar.height))*1000))),
              Math.max(0,Math.min(1000,Math.round(((r.right-ar.left)/Math.max(1,ar.width))*1000))),
              Math.max(0,Math.min(1000,Math.round(((r.bottom-ar.top)/Math.max(1,ar.height))*1000)))
            ]; };
          const objects=[]; let ordinal=1;
          const add=(id,type,label,el,extra={})=>{ const b=norm(el); if(!b) return; objects.push({ordinal:'#'+(ordinal++),id,type,label,bounds:b,...extra}); };
          const windows=[...document.querySelectorAll('.app-window[data-window-app]')].map(w=>{
            const app=w.dataset.windowApp||'', open=w.dataset.open==='true', minimized=w.classList.contains('minimized-window'), hidden=w.classList.contains('hidden-window');
            const active=w.classList.contains('focused-window')&&open&&!minimized&&!hidden, title=w.querySelector('.window-title-left strong')?.innerText||app;
            add('win_'+app,'window',title,w,{app,open,minimized,active,maximized:w.classList.contains('maximized-window')});
            ['minimize','maximize','close'].forEach(action=>{ const btn=w.querySelector('[data-window-action="'+action+'"]'); if(open&&!hidden&&!minimized&&btn) add('obj_'+app+'_'+action,'window-control',action+' '+title,btn,{app,action,window_id:'win_'+app}); });
            return {id:'win_'+app,app,title,open,minimized,hidden,active,maximized:w.classList.contains('maximized-window'),bounds:norm(w)};
          });
          document.querySelectorAll('.dock-app[data-desktop-app]').forEach(btn=>{ const app=btn.dataset.desktopApp||''; add('app_'+app,'app-icon',btn.title||app,btn,{app,action:'open_app'}); });
          const hit=document.querySelector('#browser-hit'); if(hit) add('obj_browser_canvas','browser-surface','Browser page',hit,{app:'browser'});
          const c=document.querySelector('#agentDesktopCursor');
          return {ready:Boolean(window.deskObserverReady),surface:(window.deskState&&window.deskState.surface)||'',active_window:windows.find(w=>w.active)||null,windows,objects,cursor:c&&!c.hidden?{left:c.style.left,top:c.style.top}:null,viewport:ar?{width:Math.round(ar.width),height:Math.round(ar.height)}:null,url:document.querySelector('#url-bar')?.value||''};
        })()""")
        if not isinstance(ui, dict):
            ui = {}
        changed = bool(obs.frame_seq > int(after or 0)) if after else digest != self.observer_last_hash
        prior_hash = self.observer_last_hash
        self.observer_last_hash = digest or self.observer_last_hash
        self.desktop_objects = ui.get("objects") or []
        if changed:
            self.desktop_last_change = {"frame_seq": int(obs.frame_seq), "from_hash": prior_hash, "to_hash": digest, "at": time.time()}
        browser_info = {"url": "", "title": ""}
        if self.browser and self.browser.healthy():
            browser_info["url"] = self.browser.url or ""
            try:
                title = self.browser.evaluate("document.title") or ""
                browser_info["title"] = title if isinstance(title, str) else ""
            except Exception:
                pass
        dev = self.detect_dev_system()
        proc = self.dev_process_status()
        wins = ui.get("windows") or []
        active = ui.get("active_window") or next((w for w in wins if w.get("active")), None)
        recommended = []
        if active:
            recommended.append(f"focus_window({active.get('id')!r})")
        if not any(w.get("app") == "browser" and w.get("open") and not w.get("minimized") for w in wins):
            recommended.append("open_app('app_browser')")
        recommended.append("desktop_watch after visual actions")
        return {
            "ready": bool(ui.get("ready", True)), "frame_seq": int(obs.frame_seq), "changed": changed, "frame_hash": digest,
            "screen": {"width": int((ui.get("viewport") or {}).get("width") or obs.view_w), "height": int((ui.get("viewport") or {}).get("height") or obs.view_h), "coordinates": "normalized 0-1000"},
            "surface": ui.get("surface") or self.surface, "active_window": active, "windows": wins,
            "desktop_apps": [
                {"id":"app_agent","app":"hermes","label":"Hermes Agent"},{"id":"app_browser","app":"browser","label":"Browser"},{"id":"app_files","app":"files","label":"Files"},{"id":"app_notepad","app":"notepad","label":"Text Editor"},{"id":"app_terminal","app":"terminal","label":"Terminal"},{"id":"app_editor","app":"editor","label":"Code"},{"id":"app_preview","app":"preview","label":"Preview"},{"id":"app_dev","app":"dev","label":"Build & Test"},{"id":"app_settings","app":"settings","label":"Settings"}],
            "screen_objects": self.desktop_objects, "cursor": dict(self.desktop_cursor), "browser": browser_info,
            "workspace": str(self.workspace), "build_system": {"kind": dev.get("kind", "generic"), "commands": dev.get("commands", {})},
            "running_processes": [proc] if proc.get("running") else [], "last_action": dict(self.desktop_last_action), "last_change": dict(self.desktop_last_change),
            "available_actions": ["open_app(app_id)","focus_window(window_id)","minimize_window(window_id)","maximize_window(window_id)","close_window(window_id)","click_object(object_id)","move_cursor(x,y)","click(x,y)","double_click(x,y)","scroll(dx,dy)","type_text(text)","open_file(path)","open_preview(path)","browser_navigate(url)","browser_back()","browser_forward()","run_tests()","run_app()","stop_app()","screenshot()","robot_status()","robot_joint(joint,value)","robot_pose(pose)","robot_motion(cmd)"],
            "robot": robot_sim.public_status(self.robot_state),
            "recommended_next_actions": recommended, "observed_at": time.time(),
        }

    def observer_frame(self) -> tuple[bytes, int]:
        self.ensure_observer()
        obs = self.observer
        if not obs:
            return b"", 0
        frame = obs.screenshot(force=False) or b""
        return frame, int(obs.frame_seq)

    def save_desktop_screenshot(self, frame: bytes, *, seq: int = 0) -> dict[str, Any]:
        if not frame:
            raise RuntimeError("no desktop frame")
        self.ensure_home_dirs()
        rel = dated_media_relpath("Pictures", "minios-desktop", ".jpg")
        path = unique_workspace_file(self.workspace, rel)
        path.write_bytes(frame)
        return {
            "path": path.relative_to(self.workspace).as_posix(),
            "mime": "image/jpeg",
            "seq": int(seq),
            "bytes": len(frame),
            "label": path.stem,
        }

    def post_chat_image(self, path: str, mime: str = "image/jpeg", caption: str = "") -> dict[str, Any]:
        caption = (caption or "").strip() or path
        self.append_msg("assistant", caption, images=[{"path": path, "mime": mime}])
        last = self.messages[-1] if self.messages else {"role": "assistant", "text": caption, "images": [{"path": path, "mime": mime}]}
        emit(
            {
                "type": "chat",
                "bot_id": self.id,
                "role": "assistant",
                "text": caption,
                "images": last.get("images") or [{"path": path, "mime": mime}],
            }
        )
        return last

    def screenshot_to_chat(self, caption: str = "") -> dict[str, Any]:
        """Post a JPEG of THIS bot's MiniOS workspace desktop — never the host display."""
        self.ensure_observer()
        obs = self.observer
        if not obs:
            raise RuntimeError("MiniOS observer is not running")
        self.desktop_view_event.clear()
        emit({"type": "desktop.capture-request", "bot_id": self.id})
        self.desktop_view_event.wait(timeout=1.8)
        view = dict(self.desktop_view or {})
        if view.get("width") and view.get("height") and hasattr(obs, "set_view_size"):
            obs.set_view_size(int(view["width"]), int(view["height"]))
        if hasattr(obs, "apply_minios_view"):
            obs.apply_minios_view(view)
        st = self.robot_state or {}
        joints = st.get("joints") if isinstance(st.get("joints"), dict) else {}
        if joints:
            emit(
                {
                    "type": "desktop.action",
                    "bot_id": self.id,
                    "action": "robot",
                    "cmd": "joints",
                    "joints": joints,
                    "maximize": False,
                }
            )
            time.sleep(0.35)
        else:
            time.sleep(0.15)
        frame = b""
        if hasattr(obs, "screenshot_minios_desktop"):
            frame = obs.screenshot_minios_desktop() or b""
        if not frame:
            raise RuntimeError("MiniOS desktop clip failed")
        seq = int(getattr(obs, "frame_seq", 0) or 0)
        saved = self.save_desktop_screenshot(frame, seq=seq)
        caption = (caption or "").strip() or f"This bot's MiniOS desktop ({saved['path']})"
        self.post_chat_image(saved["path"], saved["mime"], caption)
        emit({"type": "workspace", "bot_id": self.id})
        saved["caption"] = caption
        saved["posted"] = True
        saved["source"] = "minios"
        return saved

    def ensure_browser(self, *, restart: bool = False) -> None:
        with self._browser_lock:
            if self.browser and self.browser.healthy():
                return
            if self.browser and self.browser.alive and not restart:
                return
            if self.browser:
                try:
                    self.browser.stop()
                except Exception:
                    pass
                self.browser = None
            try:
                self.browser = BrowserSurface(self.id, self.browser_profile, chrome_port(self.id))
                self.browser.start()
            except Exception:
                traceback.print_exc()
                if self.browser:
                    try:
                        self.browser.stop()
                    except Exception:
                        pass
                self.browser = None

    def ensure_site(self) -> LocalSite:
        if self.site and self.site.alive:
            return self.site
        self.site = LocalSite(self.workspace, self.www_port)
        self.site.start()
        return self.site

    def resolve_browser_url(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw or raw == "about:blank":
            return "about:blank"
        low = raw.lower()
        if low.startswith("search:") or low.startswith("g:"):
            return search_url(raw.split(":", 1)[1])
        if raw.startswith(("http://", "https://", "about:", "data:")):
            return raw
        rel = raw[7:] if low.startswith("file://") else raw
        rel = rel.lstrip("/")
        root = self.workspace.resolve()
        try:
            candidate = workspace_target(self, rel)
        except ValueError:
            candidate = root / "__outside__"
        if candidate.is_file():
            self.ensure_site()
            return self.ensure_site().url_for(candidate.relative_to(root).as_posix())
        if "." in raw and " " not in raw and not rel.endswith((".html", ".htm", ".css", ".js")):
            return "https://" + raw
        return search_url(raw)

    def show_in_browser(self, url: str) -> dict[str, Any]:
        self.ensure_site()
        with self._browser_lock:
            self.ensure_browser()
            target = self.resolve_browser_url(url)
            shown = target
            title = ""
            if self.browser:
                shown = self.browser.navigate(target) or target
                title = (self.browser.evaluate("document.title") or "") if self.browser else ""
                if not isinstance(title, str):
                    title = ""
        self.surface = "browser"
        emit(
            {
                "type": "status",
                "bot_id": self.id,
                "text": f"Browser: {shown}",
                "surface": "browser",
                "control": self.control,
            }
        )
        tabs = self.browser.list_tabs() if self.browser else []
        return {"url": shown, "title": title, "tabs": tabs, "text": ""}

    def browser_snapshot(self) -> dict[str, Any]:
        self.ensure_browser()
        if not self.browser:
            return {"url": "", "title": "", "text": ""}
        return self.browser.snapshot()

    def ensure_shell(self) -> PtySurface:
        if self.shell and self.shell.alive:
            return self.shell
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        env["PS1"] = f"{self.name}:\\w\\$ "
        self.shell = PtySurface(["/bin/bash", "--noprofile", "--norc"], str(self.workspace), env)
        self.shell.start()
        self.shell.write(f"cd {self.workspace} && printf '%s\\n' \"workspace: $(pwd)\"\n".encode())
        return self.shell

    def ensure_tui(self) -> PtySurface:
        if self.tui and self.tui.alive:
            return self.tui
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        # Same HERMES_HOME as the ACP chat: one session store per bot, so the
        # workspace TUI continues the bot's conversations (and vice versa).
        env["HERMES_HOME"] = str(self.agent_home)
        agent_home_mod.ensure_agent_home(self.agent_home)
        cmd = [HERMES_BIN, "--tui", "--in", str(self.workspace), "--accept-hooks"]
        if bot_kind_has_host_coding(self):
            # MiniOS drives host coding non-interactively.
            cmd.append("--yolo")
        self.tui = PtySurface(cmd, str(self.workspace), env)
        self.tui.start()
        self._start_tui_mirror()
        return self.tui

    def _start_tui_mirror(self) -> None:
        if self.tui_mirror:
            return

        def on_turn(role: str, text: str) -> None:
            if not text:
                return
            # Skip duplicating an identical last line (ACP + TUI race).
            if self.messages:
                last = self.messages[-1]
                if last.get("role") == role and last.get("text") == text and last.get("via") in (None, "tui"):
                    if last.get("via") == "tui":
                        return
            self.append_msg(role, text, via="tui")
            emit(
                {
                    "type": "chat",
                    "bot_id": self.id,
                    "role": role,
                    "text": text,
                    "via": "tui",
                }
            )

        self.tui_mirror = SessionMirror(
            self.agent_home / "state.db",
            on_turn,
            on_usage=self.ingest_usage,
        )
        self.tui_mirror.start()

    def stop_surfaces(self) -> None:
        self.stop_dev_process()
        if self.tui_mirror:
            try:
                self.tui_mirror.stop()
            except Exception:
                pass
            self.tui_mirror = None
        for s in (self.observer, self.browser, self.shell, self.tui, self.site):
            if s:
                try:
                    s.stop()
                except Exception:
                    pass

    def destroy(self) -> None:
        """Delete this bot only: identity, memory, desk, browser profile, sessions."""
        try:
            self.acp.stop()
        except Exception:
            pass
        self.stop_surfaces()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.desk, ignore_errors=True)
        idx = USER_AGENT_HOME / "bots" / "index.json"
        if idx.is_file():
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
                data["bots"] = [b for b in data.get("bots", []) if b.get("id") != self.id]
                idx.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

    def deliver_dm(self, from_bot: "Bot", text: str) -> None:
        body = f"[Message from {from_bot.name} ({from_bot.id})]\n{text}\n\nReply to the user or message_teammate if you need to respond. Do not assume shared files."
        inbox = self.root / "inbox" / "messages.jsonl"
        inbox.parent.mkdir(exist_ok=True)
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"from": from_bot.id, "from_name": from_bot.name, "text": text, "t": time.time()}) + "\n")
        release_voice_page(self)
        self.append_msg("user", f"✉️ {from_bot.name}: {text}", via="dm", peer=from_bot.name)
        emit({"type": "chat", "bot_id": self.id, "role": "user", "text": f"✉️ {from_bot.name}: {text}", "via": "dm", "peer": from_bot.name})
        self.status = f"Message from {from_bot.name}"
        emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})

        if not inbound_dm_starts_acp(self):
            self.status = "Ready"
            emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
            return

        def _run() -> None:
            try:
                self.acp.prompt(body)
                self.close_chunk()
                self.status = "Ready"
            except Exception as e:
                self.status = f"Error: {e}"
            emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})

        threading.Thread(target=_run, daemon=True).start()

    def deliver_remote_dm(self, from_id: str, from_name: str, from_node: str, text: str) -> None:
        body = (
            f"[Message from {from_name} ({from_id}) on {from_node}]\n{text}\n\n"
            "Reply to the user or message_teammate if you need to respond. Do not assume shared files."
        )
        inbox = self.root / "inbox" / "messages.jsonl"
        inbox.parent.mkdir(exist_ok=True)
        with inbox.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "from": from_id,
                        "from_name": from_name,
                        "from_node": from_node,
                        "text": text,
                        "t": time.time(),
                    }
                )
                + "\n"
            )
        release_voice_page(self)
        bubble = f"✉️ {from_name}@{from_node}: {text}"
        self.append_msg("user", bubble, via="dm", peer=from_name)
        emit({"type": "chat", "bot_id": self.id, "role": "user", "text": bubble, "via": "dm", "peer": from_name})
        self.status = f"Message from {from_name}@{from_node}"
        emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})

        if not inbound_dm_starts_acp(self):
            self.status = "Ready"
            emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
            return

        def _run() -> None:
            try:
                self.acp.prompt(body)
                self.close_chunk()
                self.status = "Ready"
            except Exception as e:
                self.status = f"Error: {e}"
            emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})

        threading.Thread(target=_run, daemon=True).start()

    def runtime_brief(self) -> str:
        return runtime_brief(self)

    def _write_agents(self) -> None:
        if bot_kind_is_agent(self):
            migrated = migrate_agent_soul(self.soul)
            if migrated != self.soul:
                self.soul = migrated
                (self.root / "SOUL.md").write_text(self.soul, encoding="utf-8")
        elif bot_kind_is_teela(self):
            migrated = migrate_teela_soul(self.soul)
            if migrated != self.soul:
                self.soul = migrated
                (self.root / "SOUL.md").write_text(self.soul, encoding="utf-8")
        body = agents_markdown_for_bot(self)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "AGENTS.md").write_text(body, encoding="utf-8")
        (self.workspace / "RUNTIME.md").write_text(runtime_brief(self), encoding="utf-8")
        agent_md = agent_md_for_kind(self.kind)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "agent.md").write_text(agent_md, encoding="utf-8")
        dest = self.workspace / ".hermes" / "agents" / "desk-bot.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(agent_md, encoding="utf-8")

    def _shares_path(self) -> Path:
        return self.root / "workspace-shares.json"

    def load_shares(self) -> None:
        p = self._shares_path()
        if not p.is_file():
            self.workspace_share_with = []
            self.workspace_share_requests = []
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        grants = data.get("share_with") if isinstance(data, dict) else []
        reqs = data.get("requests") if isinstance(data, dict) else []
        self.workspace_share_with = [str(x) for x in grants if isinstance(x, str) and x and x != self.id]
        self.workspace_share_requests = [r for r in reqs if isinstance(r, dict) and r.get("from")]

    def save_shares(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "share_with": list(self.workspace_share_with),
            "requests": list(self.workspace_share_requests),
        }
        self._shares_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def set_workspace_share_with(self, ids: list[str]) -> dict[str, Any]:
        allowed = {b.id for b in bots.values() if b.id != self.id}
        cleaned: list[str] = []
        for raw in ids:
            bid = str(raw or "").strip()
            if bid and bid in allowed and bid not in cleaned:
                cleaned.append(bid)
        self.workspace_share_with = cleaned
        granted = set(cleaned)
        self.workspace_share_requests = [
            r for r in self.workspace_share_requests if r.get("from") not in granted
        ]
        self.save_shares()
        emit({"type": "bot.updated", "bot": self.profile()})
        return self.share_state()

    def add_share_request(self, viewer: "Bot") -> dict[str, Any]:
        if viewer.id == self.id:
            raise ValueError("cannot request your own workspace")
        if viewer.id in self.workspace_share_with:
            return {"ok": True, "already": True, **self.share_state()}
        if any(r.get("from") == viewer.id for r in self.workspace_share_requests):
            return {"ok": True, "pending": True, **self.share_state()}
        self.workspace_share_requests.append(
            {"from": viewer.id, "name": viewer.name, "at": time.time()}
        )
        self.save_shares()
        note = f"{viewer.name} asked to read this workspace. Approve in Edit Agent → Workspace sharing."
        self.append_msg("assistant", note, via="share-request", peer=viewer.name)
        emit({"type": "chat", "bot_id": self.id, "role": "assistant", "text": note, "via": "share-request"})
        emit({"type": "bot.updated", "bot": self.profile()})
        return {"ok": True, "pending": True, **self.share_state()}

    def share_state(self) -> dict[str, Any]:
        names = {b.id: b.name for b in bots.values()}
        return {
            "share_with": [
                {"id": bid, "name": names.get(bid, bid)} for bid in self.workspace_share_with
            ],
            "requests": list(self.workspace_share_requests),
        }

    def list_shared_desks_for(self) -> list[dict[str, Any]]:
        out = []
        for owner in bots.values():
            if owner.id == self.id or self.id not in (owner.workspace_share_with or []):
                continue
            if getattr(owner, "remote", False):
                continue
            out.append({"id": owner.id, "name": owner.name, "mode": "read", "workspace_id": owner.workspace_id})
        return out

    def list_shared_paths(self, owner: "Bot", rel: str = "") -> list[dict[str, Any]]:
        root = shared_workspace_target(self, owner, rel or ".")
        if not root.exists():
            raise FileNotFoundError(rel or ".")
        if root.is_file():
            return [{"name": root.name, "dir": False, "path": rel or root.name}]
        entries: list[dict[str, Any]] = []
        for child in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in SHARED_SKIP_DIR_NAMES or child.name.startswith("."):
                continue
            rel_child = str((Path(rel or ".") / child.name).as_posix()).lstrip("./")
            entries.append({"name": child.name, "dir": child.is_dir(), "path": rel_child})
            if len(entries) >= 200:
                break
        return entries

    def read_shared_file(self, owner: "Bot", rel: str) -> dict[str, Any]:
        target = shared_workspace_target(self, owner, rel, must_exist=True)
        if target.is_dir():
            raise IsADirectoryError(rel)
        data = target.read_bytes()
        if b"\x00" in data[:4096]:
            raise ValueError("binary file")
        text = data.decode("utf-8", errors="replace")
        if len(text) > SHARED_TEXT_MAX:
            text = text[:SHARED_TEXT_MAX] + "\n…[truncated]"
        return {"path": rel, "owner": owner.id, "owner_name": owner.name, "text": text}

    def profile_fields(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "avatar": {
                "kind": "emoji",
                "value": self.emoji,
                "color": self.avatar_color,
                "shape": self.avatar_shape,
            },
            "avatar_color": self.avatar_color,
            "avatar_shape": self.avatar_shape,
            "model": self.model,
            "kind": self.kind,
            "permission_mode": "always-approve" if bot_kind_has_host_coding(self) else "default",
            "sandbox_profile": f"bot-{self.id}",
            "workspace_id": self.workspace_id,
            "workspace_backend": "local",
            "workspace_mode": "dedicated",
            "host_access": "full" if bot_kind_has_host_coding(self) else "never",
            "browser": True if bot_kind_is_agent(self) or bot_kind_is_teela(self) or not self.kind else False,
            "terminal": True if bot_kind_has_host_coding(self) or not self.kind else False,
            "inherit_user_skills": bot_kind_has_host_coding(self),
            "inherit_user_mcp": bot_kind_has_host_coding(self),
        }

    def write_profile(self) -> None:
        write_toml_profile(self.root / "PROFILE.toml", self.profile_fields())

    def _link_agent_home(self) -> None:
        if not bot_kind_has_host_coding(self):
            return
        self.agent_home.mkdir(parents=True, exist_ok=True)
        for name in ("skills", "plugins"):
            src = USER_AGENT_HOME / name
            dest = self.agent_home / name
            if dest.exists() or dest.is_symlink():
                continue
            if src.exists():
                try:
                    dest.symlink_to(src)
                except OSError:
                    pass

    def _restart_session(self, load_session_id: str | None = None) -> None:
        try:
            self.acp.stop()
        except Exception:
            pass
        self.acp = AcpClient(self)
        self.acp.start(load_session_id=load_session_id)

    def record_acp_session(self, session_id: str) -> None:
        """Persist the live ACP session so a restart can resume it."""
        try:
            self.agent_home.mkdir(parents=True, exist_ok=True)
            self.agent_home.joinpath("last_acp_session.json").write_text(
                json.dumps({
                    "session_id": str(session_id or ""),
                    "chat_id": str(self.chat_id or ""),
                    "ts": time.time(),
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

    def last_acp_session_id(self) -> str | None:
        """Recorded session to resume for the current chat, if any."""
        try:
            data = json.loads(self.agent_home.joinpath("last_acp_session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.newest_persisted_session_id()
        sid = str((data or {}).get("session_id") or "").strip()
        if not sid:
            return None
        rec_chat = str((data or {}).get("chat_id") or "")
        want = str(self.chat_id or "")
        if rec_chat and want and rec_chat != want:
            return None
        return sid

    def newest_persisted_session_id(self) -> str | None:
        """Bootstrap only: newest persisted session with content (no record yet).

        Hermes stores sessions in the bot's ``state.db`` (one row per session,
        ``message_count`` > 0 = has content).
        """
        sid = agent_home_mod.newest_hermes_session(self.agent_home)
        return sid

    def attach_acp_session(self, session_id: str) -> None:
        """Bind the live ACP session to the active chat for chat-scoped resume."""
        try:
            data = self.ensure_timeline()
            target = self.chat_id or data.get("activeId")
            for row in data.get("chats") or []:
                if isinstance(row, dict) and row.get("id") == target:
                    row["agentSession"] = str(session_id)
                    break
            self._write_timeline(data)
        except Exception:
            pass

    def save_soul(self, body: str) -> dict[str, Any]:
        return self.update_identity({"soul": body})

    def update_identity(self, body: dict[str, Any]) -> dict[str, Any]:
        identity_changed = False
        name = (body.get("name") or "").strip()
        if name and name != self.name:
            self.name = name
            identity_changed = True
        if "description" in body and body.get("description") is not None:
            desc = str(body.get("description") or "").strip()
            if desc != self.description:
                self.description = desc
                identity_changed = True
        soul = body.get("soul")
        if soul is None:
            soul = body.get("body")
        if soul is not None:
            soul_s = str(soul)
            if soul_s != self.soul:
                self.soul = soul_s
                (self.root / "SOUL.md").write_text(self.soul, encoding="utf-8")
                identity_changed = True
        emoji = body.get("emoji")
        if emoji is not None:
            new_emoji = str(emoji).strip()[:4] or self.emoji
            if new_emoji != self.emoji:
                self.emoji = new_emoji
                identity_changed = True
        color, shape = _avatar_from_body(body)
        if "avatar_color" in body or (isinstance(body.get("avatar"), dict) and "color" in (body.get("avatar") or {})):
            self.avatar_color = color
        if "avatar_shape" in body or (isinstance(body.get("avatar"), dict) and "shape" in (body.get("avatar") or {})):
            self.avatar_shape = shape
        new_kind = body.get("kind") or body.get("bot_kind") or body.get("type")
        if (not self.kind) and new_kind is not None and str(new_kind).strip():
            want = normalize_bot_kind(new_kind)
            ensure_single_teela_brain(want, exclude_id=self.id)
            self.kind = want
            identity_changed = True
        model = (body.get("model") or "").strip()
        if model and model != self.model:
            ensure_model_on_host(model, allow_current=self.model, bot=self)
            identity_changed = True
            self.model = model
            _, user_models = load_user_models()
            write_child_config(
                self.agent_home,
                self.model,
                user_models,
                bot_id=self.id,
                permission_mode="always-approve" if bot_kind_has_host_coding(self) else "default",
                inherit_mcp=bot_kind_is_agent(self),
            )
            if model in user_models and user_models[model].get("context_window"):
                try:
                    self.context_window = int(user_models[model]["context_window"])
                except (TypeError, ValueError):
                    pass
            for m in self.models:
                if m.get("id") == self.model and m.get("context_window"):
                    try:
                        self.context_window = int(m["context_window"])
                    except (TypeError, ValueError):
                        pass
            self.reset_telemetry()
            self._seed_usage_from_disk()
        if "workspace_share_with" in body and isinstance(body.get("workspace_share_with"), list):
            self.set_workspace_share_with([str(x) for x in body.get("workspace_share_with") or []])
        self.write_profile()
        if identity_changed:
            (self.root / "agent.md").write_text(agent_md_for_kind(self.kind), encoding="utf-8")
            self._link_agent_home()
            _, user_models = load_user_models()
            write_child_config(
                self.agent_home,
                self.model,
                user_models,
                bot_id=self.id,
                permission_mode="always-approve" if bot_kind_has_host_coding(self) else "default",
                inherit_mcp=bot_kind_is_agent(self),
            )
            self._write_agents()
            agent_md = self.root / "agent.md"
            dest = self.workspace / ".hermes" / "agents" / "desk-bot.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if agent_md.is_file():
                shutil.copy2(agent_md, dest)
            # New ACP session so name + SOUL take effect immediately.
            self._restart_session()
        self._index_upsert()
        self.status = "Ready"
        emit({"type": "bot.updated", "bot": self.profile()})
        emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
        return self.profile()

    def _index_upsert(self) -> None:
        idx = USER_AGENT_HOME / "bots" / "index.json"
        idx.parent.mkdir(parents=True, exist_ok=True)
        data = {"bots": []}
        if idx.is_file():
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {"bots": []}
        found = False
        for row in data.setdefault("bots", []):
            if row.get("id") == self.id:
                row["name"] = self.name
                found = True
                break
        if not found:
            data["bots"].append({"id": self.id, "name": self.name})
        idx.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def profile(self) -> dict[str, Any]:
        reclaim_stale_busy_status(self)
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "avatar": {
                "kind": "emoji",
                "value": self.emoji,
                "color": self.avatar_color,
                "shape": self.avatar_shape,
            },
            "avatar_color": self.avatar_color,
            "avatar_shape": self.avatar_shape,
            "model": self.model,
            "effort": self.effort or current_model_effort(self),
            "kind": self.kind,
            "models": annotate_model_availability(self.models),
            "context_used": self.context_used,
            "context_window": self.context_window,
            "context_source": self.context_source,
            "tps": self.tps,
            "speed_source": self.speed_source,
            "token_source": self.token_source,
            "workspace_id": self.workspace_id,
            "workspace": str(self.workspace),
            "status": self.status,
            "surface": self.surface,
            "desktop_cursor": dict(self.desktop_cursor),
            "control": self.control,
            "visual_observer": bool(self.observer and self.observer.healthy()),
            "can_undo": any(m.get("role") == "user" for m in self.messages),
            "chat_id": self.chat_id,
            "chat": self.active_chat_public(),
            "workspace_share_with": list(self.workspace_share_with),
            "workspace_share_requests": list(self.workspace_share_requests),
            "messages": [{k: v for k, v in m.items() if k != "open"} for m in self.messages],
            "slash_commands": list(self.slash_commands),
        }

    def reset_telemetry(self, *, used: int | None = None) -> None:
        """Reset the context window; keep the last measured tok/s (TUI keeps it too)."""
        if used is not None:
            self.context_used = int(used)
            self.context_source = "agent_runtime" if used else ""
        self._gen = {}
        self.telemetry = {}

    def observe_acp_update(self, params: dict[str, Any], *, kind_hint: str = "") -> None:
        """Ingest one ACP session/update (or prompt_complete) for context + tok/s."""
        if not isinstance(params, dict):
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = kind_hint or update.get("sessionUpdate") or ""
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        usage = update.get("usage") if isinstance(update.get("usage"), dict) else {}
        if not usage and isinstance(params.get("usage"), dict):
            usage = params.get("usage") or {}
        if kind == "usage_update" and (update.get("size") is not None or update.get("used") is not None):
            self.ingest_usage({k: update.get(k) for k in ("size", "used")})
        self.ingest_usage(meta)
        if usage:
            self.ingest_usage(usage)
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        text = content.get("text") or ""
        if kind == "agent_message_chunk" and text:
            self.note_generation_chunk(text, meta)
        elif kind == "agent_thought_chunk" and (text or meta):
            # Thoughts are generated tokens; count them for the clock, not the visible text.
            self.note_generation_chunk("", meta, timing_only=True)
        elif kind == "tool_call" and meta:
            pass

    def note_generation_chunk(
        self,
        text: str,
        meta: dict[str, Any] | None,
        *,
        timing_only: bool = False,
    ) -> None:
        meta = meta if isinstance(meta, dict) else {}
        stream_start = tel._as_float(meta.get("streamStartMs"))
        turn_start = tel._as_float(meta.get("turnStartMs"))
        ts = tel._as_float(meta.get("agentTimestampMs"))
        if ts is None:
            ts = time.time() * 1000.0
        # Wall-clock first/last output anchors for the real per-token decode span.
        # The Hermes ACP stream is per-token (chunks ~ms apart), so this span is a
        # genuine decode duration, not a per-model-call batch.
        if getattr(self, "_first_out_wall", None) is None:
            self._first_out_wall = time.monotonic()
        self._last_chunk_wall = time.monotonic()
        gen = self._gen
        if stream_start is not None and gen.get("stream_start_ms") not in (None, stream_start):
            self._finalize_generation()
            gen = {}
            self._gen = gen
        if not gen:
            gen = {
                "stream_start_ms": stream_start,
                "turn_start_ms": turn_start,
                "first_out_ms": ts,
                "last_out_ms": ts,
                "text": "",
            }
            self._gen = gen
        else:
            gen.setdefault("stream_start_ms", stream_start)
            gen.setdefault("turn_start_ms", turn_start)
            gen.setdefault("first_out_ms", ts)
            gen["last_out_ms"] = ts
        if not timing_only:
            prev = gen.get("text") or ""
            incoming = str(text or "")
            if not prev:
                gen["text"] = incoming
            elif incoming.startswith(prev):
                gen["text"] = incoming
            elif prev.endswith(incoming):
                pass
            else:
                gen["text"] = prev + incoming
        self._publish_generation(token_source="tokenizer")

    def finish_generation(
        self,
        usage: dict[str, Any] | None,
        meta: dict[str, Any] | None,
        elapsed_ms: Any = None,
    ) -> None:
        self.ingest_usage(meta)
        self.ingest_usage(usage)
        gen = self._gen
        if not gen or not (gen.get("text") or gen.get("first_out_ms")):
            return
        ledger = tel.ledger_stats(usage)
        runtime_tokens, runtime_src = tel.visible_output_tokens(ledger)
        calls = ledger.get("model_calls") if ledger else None
        token_source = "tokenizer"
        tokens = tel.count_tokens_local(gen.get("text") or "")
        if runtime_tokens is not None and (calls is None or int(calls) <= 1):
            tokens = runtime_tokens
            token_source = runtime_src
        if elapsed_ms is not None:
            gen["elapsed_ms"] = elapsed_ms
        if ledger and ledger.get("api_duration_ms") is not None:
            gen["api_duration_ms"] = ledger.get("api_duration_ms")
            gen["model_calls"] = calls
        self._publish_generation(token_source=token_source, output_tokens=tokens, final=True)
        self._gen = {}

    def _finalize_generation(self) -> None:
        if not self._gen:
            return
        self._publish_generation(token_source="tokenizer", final=True)

    def _publish_generation(
        self,
        *,
        token_source: str,
        output_tokens: int | None = None,
        final: bool = False,
    ) -> None:
        gen = self._gen or {}
        text = gen.get("text") or ""
        tokens = output_tokens
        if tokens is None:
            tokens = tel.count_tokens_local(text)
            token_source = token_source or "tokenizer"
        metrics = tel.generation_metrics(
            output_tokens=int(tokens or 0),
            token_source=token_source,
            first_out_ms=gen.get("first_out_ms"),
            last_out_ms=gen.get("last_out_ms"),
            stream_start_ms=gen.get("stream_start_ms"),
            turn_start_ms=gen.get("turn_start_ms"),
            api_duration_ms=gen.get("api_duration_ms"),
            model_calls=gen.get("model_calls"),
            elapsed_ms=tel._as_float(gen.get("elapsed_ms")),
        )
        cached = getattr(self, "_local_engine_cache", None)
        if not cached or cached[0] != self.model:
            cached = (str(self.model or ""), uses_local_text_llm(str(self.model or "")))
            self._local_engine_cache = cached
        local_engine = cached[1]
        self.token_source = metrics["token_source"]
        # Local engines: ACP chunk timing is batched per model call, so ACP
        # estimates would clobber the proxy's real per-token measurement.
        if metrics["speed_source"] and not local_engine:
            self.speed_source = metrics["speed_source"]
        first_ms = gen.get("first_out_ms")
        last_ms = gen.get("last_out_ms")
        burst = (
            first_ms is not None
            and last_ms is not None
            and 50 <= (float(last_ms) - float(first_ms)) < 400
        )
        # Only positive measurements latch; the meter keeps the last good value
        # until the next generation (TUI behavior).
        if not local_engine and (final or not burst) and metrics["generation_tok_s"]:
            self.tps = float(metrics["generation_tok_s"])
        self.telemetry = {
            "current_tokens": self.context_used,
            "max_tokens": self.context_window,
            "percentage": tel.context_percentage(self.context_used, self.context_window),
            "context_source": self.context_source or "",
            "model": self.model,
            "output_tokens": metrics["output_tokens"],
            "first_token_at": metrics["first_token_at"],
            "last_token_at": metrics["last_token_at"],
            "generation_duration_ms": metrics["generation_ms"],
            "generation_tok_s": metrics["generation_tok_s"],
            "ttft_ms": metrics["ttft_ms"],
            "speed_source": self.speed_source,
            "token_source": self.token_source,
            "stream_start_ms": metrics["stream_start_ms"],
            "turn_start_ms": metrics["turn_start_ms"],
            "api_duration_ms": metrics["api_duration_ms"],
        }
        self._emit_usage(log=final)

    def ingest_usage(self, blob: dict[str, Any] | None) -> None:
        """Apply Hermes Agent occupancy (not billed multi-call sums) to the context meter."""
        if not isinstance(blob, dict) or not blob:
            return
        # Standard ACP usage_update: {size: window, used: occupancy}.
        used = tel._as_int(blob.get("used"))
        size = tel._as_int(blob.get("size"))
        if used is not None:
            if used == self.context_used and self.context_source == "agent_runtime":
                return
            self.context_used = used
            self.context_source = "agent_runtime"
            if size:
                self.context_window = size
            self._emit_usage()
            return
        n, source = tel.context_tokens_from_payload(blob)
        if n is None:
            return
        # Compaction is allowed to decrease. Ignore only identical refreshes.
        if n == self.context_used and source == self.context_source:
            return
        self.context_used = n
        self.context_source = source
        window = None
        inner = tel.unwrap_usage(blob)
        for src in (inner, blob):
            window = tel._as_int(
                src.get("contextWindowTokens")
                or src.get("context_window_size")
                or src.get("context_window")
            )
            if window:
                break
        if window and window > 0:
            self.context_window = window
        self._emit_usage()

    def _emit_usage(self, *, log: bool = False) -> None:
        pct = tel.context_percentage(self.context_used, self.context_window)
        event = {
            "type": "usage",
            "bot_id": self.id,
            "model": self.model,
            "used": self.context_used,
            "window": self.context_window,
            "percentage": pct,
            "context_source": self.context_source,
            "tps": self.tps,
            "speed_source": self.speed_source,
            "token_source": self.token_source,
            "ttft_ms": (self.telemetry or {}).get("ttft_ms"),
            "generation_ms": (self.telemetry or {}).get("generation_duration_ms"),
            "output_tokens": (self.telemetry or {}).get("output_tokens"),
            "models": annotate_model_availability(self.models),
        }
        try:
            extra = wm.note_occupancy(self)
            if extra:
                event.update(extra)
        except Exception:
            pass
        emit(event)
        if log:
            print(
                f"[deskd] telemetry bot={self.id} model={self.model} "
                f"context={self.context_used}/{self.context_window} ({pct:.2f}% {self.context_source or 'unknown'}) "
                f"tok/s={self.tps:.2f} src={self.speed_source or 'n/a'} tokens={self.token_source or 'n/a'}",
                flush=True,
            )

    def note_local_stream_speed(self, tok_s: float, tokens: int, *, log: bool = False) -> None:
        """Latch tok/s measured on the local-engine SSE path (real token timing)."""
        try:
            tok_s = float(tok_s)
        except (TypeError, ValueError):
            return
        if not tok_s or tok_s != tok_s or tok_s <= 0.0 or tok_s > 2000.0:
            return
        self.tps = tok_s
        self.speed_source = "local_stream"
        self.token_source = "tokenizer"
        self._emit_usage(log=log)

    def note_real_acp_usage(self, result: dict[str, Any] | None) -> None:
        """Turn the ACP session/prompt response's real engine usage into real tps.

        The response ``usage`` carries the engine's true counts (camelCase wire:
        ``inputTokens``/``outputTokens``/``totalTokens``). ``outputTokens`` is a
        per-session cumulative counter, so this turn's real completion count is the
        delta from the previous response. The ACP stream is per-token, so the
        first→last output wall span recorded while streaming is a real decode
        duration — their ratio is the real tokens/second, for local engines too
        (the in-stream estimate path is suppressed for them, so this is the only
        measured number). Runs after ``AcpClient.prompt`` returns, i.e. after the
        whole turn has streamed.
        """
        if not isinstance(result, dict):
            return
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return
        cumulative_out = tel._as_int(usage.get("outputTokens"))
        if cumulative_out is None:
            cumulative_out = tel._as_int(usage.get("completion_tokens"))
        if cumulative_out is None:
            return
        prev = self._acp_prev_completion
        if prev is None:
            # First response on this desk process / ACP session: the cumulative
            # counter's absolute value is unknown (fresh or resumed session), so
            # only record the baseline; the next turn has a real delta.
            self._acp_prev_completion = cumulative_out
            return
        delta = cumulative_out - prev
        self._acp_prev_completion = cumulative_out
        if delta <= 0:
            return
        first = getattr(self, "_first_out_wall", None)
        last = getattr(self, "_last_chunk_wall", None)
        span = (last - first) if (first is not None and last is not None) else None
        if span is None or span < 0.3:
            # No real per-token stream recorded for this turn (canned reply, tool
            # only, or sub-span noise) — the meter keeps its last good value.
            return
        tps = float(delta) / span
        if not tps or tps != tps or tps <= 0.0 or tps > 2000.0:
            return
        self.tps = tps
        self.speed_source = "stream_measurement"
        self.token_source = "engine"
        telemetry = self.telemetry
        if isinstance(telemetry, dict):
            telemetry["output_tokens"] = int(delta)
            telemetry["generation_duration_ms"] = span * 1000.0
            telemetry["generation_tok_s"] = tps
        self._emit_usage(log=True)

    def record_local_generation(
        self,
        text: str,
        usage: dict[str, Any] | None = None,
        *,
        started_ms: float,
        ended_ms: float | None = None,
        prompt_tokens: int | None = None,
    ) -> None:
        """Fill context + tok/s meters for MiniOS / local-Qwen turns (no ACP)."""
        ended_ms = float(ended_ms if ended_ms is not None else time.time() * 1000.0)
        started_ms = float(started_ms) if started_ms is not None else ended_ms
        blob = dict(usage or {})
        if prompt_tokens is not None and "prompt_tokens" not in blob:
            blob["prompt_tokens"] = int(prompt_tokens)
        occupancy, src = tel.context_tokens_from_payload(blob) if blob else (None, "")
        out_tok, out_src = (None, "")
        ledger = tel.ledger_stats(blob) if blob else {}
        if ledger:
            out_tok, out_src = tel.visible_output_tokens(ledger)
            if out_src == "agent_runtime" and (
                blob.get("completion_tokens") is not None or blob.get("prompt_tokens") is not None
            ):
                out_src = "local_runtime"
        if occupancy is None or src == "tokenizer":
            hist = "\n".join(str(m.get("text") or "") for m in (self.messages or [])[-24:])
            est = tel.count_tokens_local(hist) + tel.count_tokens_local(text or "")
            if occupancy is None:
                occupancy, src = est, "tokenizer"
            else:
                occupancy = max(int(occupancy), est)
        # Canned body replies must not shrink the live window to a handful of tokens.
        if src == "tokenizer" and self.context_used and int(occupancy) < int(self.context_used):
            occupancy = self.context_used
            src = self.context_source or src
        if occupancy:
            self.context_used = int(occupancy)
            self.context_source = src or "tokenizer"
        if out_tok is None:
            out_tok = tel.count_tokens_local(text or "")
            out_src = "tokenizer"
        elapsed = max(0.0, ended_ms - started_ms)
        if self._gen:
            self._gen["elapsed_ms"] = elapsed
            if blob.get("completion_tokens") is None and out_tok is not None:
                blob["completion_tokens"] = int(out_tok)
            if blob.get("total_tokens") is None and occupancy:
                blob["total_tokens"] = int(occupancy)
            self.finish_generation(blob, None, elapsed_ms=elapsed)
            return
        if out_tok and elapsed >= 80:
            # Keep llama.cpp decode tok/s; wall-clock including tools would under-report.
            if not (float(self.tps or 0) > 0 and self.speed_source == "local_runtime"):
                self.tps = float(out_tok) / (elapsed / 1000.0)
                self.speed_source = "local_runtime"
                self.token_source = out_src or "tokenizer"
        self.telemetry = {
            "current_tokens": self.context_used,
            "max_tokens": self.context_window,
            "percentage": tel.context_percentage(self.context_used, self.context_window),
            "context_source": self.context_source or "",
            "model": self.model,
            "output_tokens": int(out_tok or 0),
            "generation_duration_ms": elapsed if elapsed else None,
            "generation_tok_s": self.tps,
            "speed_source": self.speed_source,
            "token_source": self.token_source,
        }
        self._emit_usage(log=True)

    def telemetry_snapshot(self) -> dict[str, Any]:
        snap = dict(self.telemetry or {})
        snap.update(
            {
                "current_tokens": self.context_used,
                "max_tokens": self.context_window,
                "percentage": tel.context_percentage(self.context_used, self.context_window),
                "context_source": self.context_source or "",
                "model": self.model,
                "generation_tok_s": self.tps,
                "speed_source": self.speed_source,
                "token_source": self.token_source,
                "chat_id": self.chat_id,
                "session_id": self.acp.session_id if self.acp else None,
            }
        )
        return snap

    def apply_models(self, models: dict[str, Any]) -> None:
        # This host's config.toml is the picker. Never adopt hermes's advertised
        # currentModelId, and never mix ACP cloud ids into a local-only catalog.
        avail = models.get("availableModels") or models.get("available_models") or []
        acp_by_id: dict[str, dict[str, Any]] = {}
        for m in avail:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("modelId") or m.get("id") or "")
            if not mid:
                continue
            meta = m.get("_meta") or {}
            cw = meta.get("totalContextTokens") or m.get("context_window")
            acp_by_id[mid] = {"name": m.get("name") or mid, "context_window": tel._as_int(cw)}
        default, user_models = load_user_models()
        extra = [self.model] if self.model else []
        _, catalog = host_picker_models(user_models, default, extra_ids=extra)
        for row in catalog:
            mid = str(row.get("id") or "")
            acp = acp_by_id.get(mid) or {}
            if acp.get("context_window") and not row.get("context_window"):
                row["context_window"] = acp["context_window"]
            tbl = user_models.get(mid) if isinstance(user_models.get(mid), dict) else {}
            cw = (tbl or {}).get("context_window") or row.get("context_window")
            if cw and mid == self.model:
                self.context_window = tel.resolve_context_window(self.model, cw, self.context_window)
        if bot_kind_is_agent(self):
            have = {str(r.get("id") or "") for r in catalog}
            for mid, meta in acp_by_id.items():
                if not mid or mid in have:
                    continue
                tbl = user_models.get(mid) if isinstance(user_models.get(mid), dict) else {}
                if tbl and is_local_gpu_model(mid, tbl):
                    continue
                catalog.append(
                    {
                        "id": mid,
                        "name": meta.get("name") or mid,
                        "context_window": meta.get("context_window"),
                        "available": True,
                        "local": False,
                        "running": False,
                        "startable": False,
                    }
                )
                have.add(mid)
        self.models = catalog
        self.models_raw = {
            str(r.get("id") or ""): r for r in catalog if isinstance(r, dict)
        }
        if not str(getattr(self, "effort", "") or "").strip():
            hit = next((m for m in catalog if m.get("id") == self.model), {}) or {}
            self.effort = str(hit.get("reasoning_effort") or "")
        self.context_window = tel.resolve_context_window(
            self.model,
            next((m.get("context_window") for m in self.models if m.get("id") == self.model), None),
            self.context_window,
        )
        self._emit_usage()

    def _apply_effort(self, effort: str) -> None:
        level = normalize_reasoning_effort(effort)
        if not level:
            return
        if level not in _REASONING_EFFORTS:
            raise ValueError("effort must be off, none, minimal, low, medium, high, xhigh, or max")
        self.effort = level
        wire = agent_effort_wire(level)
        for row in self.models or []:
            if str(row.get("id") or "") == self.model:
                row["reasoning_effort"] = wire
                break

    def _catalog_with_effort(self, models: dict[str, Any]) -> dict[str, Any]:
        effort = agent_effort_wire(self.effort or "")
        out: dict[str, Any] = {}
        for mid, tbl in (models or {}).items():
            if not isinstance(tbl, dict):
                continue
            row = dict(tbl)
            if effort and mid == self.model:
                row["reasoning_effort"] = effort
            out[mid] = row
        return out

    def set_model(self, model_id: str, effort: str | None = None) -> dict[str, Any]:
        model_id = (model_id or "").strip()
        if not model_id:
            raise ValueError("model id required")
        changed = model_id != self.model
        if changed:
            ensure_model_on_host(model_id, allow_current=self.model, bot=self)
        _, user_models = load_user_models()
        if model_id not in user_models and looks_like_cloud_model(model_id):
            user_models = {
                **user_models,
                model_id: {
                    "model": model_id,
                    "name": model_id,
                    "api_backend": "responses",
                },
            }
        self.model = model_id
        tbl = user_models.get(model_id) if isinstance(user_models.get(model_id), dict) else {}
        if effort:
            self._apply_effort(effort)
        elif changed or not str(self.effort or "").strip():
            self.effort = str((tbl or {}).get("reasoning_effort") or self.effort or "")
        write_child_config(
            self.agent_home,
            self.model,
            self._catalog_with_effort(user_models),
            bot_id=self.id,
            permission_mode="always-approve" if bot_kind_has_host_coding(self) else "default",
            default_reasoning_effort=agent_effort_wire(self.effort or ""),
            inherit_mcp=bot_kind_is_agent(self),
        )
        if changed:
            user_cw = user_models.get(model_id, {}).get("context_window") if model_id in user_models else None
            catalog_cw = next((m.get("context_window") for m in self.models if m.get("id") == self.model), None)
            self.context_window = tel.resolve_context_window(self.model, user_cw, catalog_cw)
            self.reset_telemetry()
            self._seed_usage_from_disk()
            self._write_agents()
            # Keep PROFILE.toml model field in sync (rewrite name line).
            prof = self.root / "PROFILE.toml"
            if prof.is_file():
                lines = []
                for line in prof.read_text(encoding="utf-8").splitlines():
                    if line.startswith("model ="):
                        lines.append(f'model = "{self.model}"')
                    else:
                        lines.append(line)
                prof.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # Always new ACP session so AGENTS.md Runtime is reloaded into the model.
            self._restart_session()
            _, self.models = host_picker_models(user_models, extra_ids=[self.model])
            if self.effort:
                wire = agent_effort_wire(self.effort)
                for row in self.models or []:
                    if str(row.get("id") or "") == self.model:
                        row["reasoning_effort"] = wire
                        break
        elif self.acp and self.acp.session_id:
            try:
                self.acp._ensure_acp_model(self.model, effort=self.effort or "")
            except Exception:
                pass
        self._emit_usage()
        return {
            "model": self.model,
            "effort": self.effort or current_model_effort(self),
            "window": self.context_window,
            "models": self.models,
        }

    def ensure_home_dirs(self) -> None:
        """Ubuntu-style home folders inside the real bot workspace."""
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Trash"):
            (self.workspace / name).mkdir(exist_ok=True)

    def trash_item(self, rel: str) -> dict[str, Any]:
        self.ensure_home_dirs()
        src = workspace_target(self, rel, must_exist=True)
        root = self.workspace.resolve()
        rel_posix = src.relative_to(root).as_posix()
        if rel_posix in {".git", ".hermes"} or rel_posix.startswith(".git/") or rel_posix.startswith(".hermes/"):
            raise ValueError("protected path")
        trash = (root / "Trash").resolve()
        if src == trash:
            raise ValueError("cannot trash the Trash folder")
        if trash == src or trash in src.parents:
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
            self.detach_media(rel_posix)
            return {"ok": True, "deleted": rel_posix}
        dest = trash / src.name
        n = 1
        while dest.exists():
            dest = trash / f"{src.name}.{n}"
            n += 1
        shutil.move(str(src), str(dest))
        dest_rel = dest.relative_to(root).as_posix()
        self.detach_media(rel_posix)
        return {"ok": True, "from": rel_posix, "path": dest_rel}

    def delete_item(self, rel: str) -> dict[str, Any]:
        """Permanently delete a workspace file or folder."""
        self.ensure_home_dirs()
        src = workspace_target(self, rel, must_exist=True)
        root = self.workspace.resolve()
        rel_posix = src.relative_to(root).as_posix()
        if rel_posix in {".git", ".hermes", "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Trash"}:
            raise ValueError("protected path")
        if rel_posix.startswith(".git/") or rel_posix.startswith(".hermes/"):
            raise ValueError("protected path")
        if src.is_dir():
            shutil.rmtree(src)
        else:
            src.unlink()
        self.detach_media(rel_posix)
        return {"ok": True, "deleted": rel_posix}

    def empty_trash(self) -> dict[str, Any]:
        self.ensure_home_dirs()
        trash = (self.workspace / "Trash").resolve()
        removed: list[str] = []
        if trash.is_dir():
            for p in list(trash.iterdir()):
                rel = p.relative_to(self.workspace.resolve()).as_posix()
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
                self.detach_media(rel)
                removed.append(rel)
        return {"ok": True, "removed": len(removed), "paths": removed}

    def detach_media(self, rel: str) -> None:
        """Drop chat image/attachment refs to a workspace path (after trash/delete)."""
        want = (rel or "").strip()
        if not want:
            return
        name = Path(want).name
        changed = False
        for m in self.messages:
            imgs = m.get("images")
            if isinstance(imgs, list):
                kept = [i for i in imgs if isinstance(i, dict) and i.get("path") not in {want, name}]
                if len(kept) != len(imgs):
                    m["images"] = kept
                    changed = True
            atts = m.get("attachments")
            if isinstance(atts, list):
                kept_a = [
                    a
                    for a in atts
                    if not isinstance(a, dict) or (a.get("path") not in {want, name} and a.get("name") != name)
                ]
                if len(kept_a) != len(atts):
                    m["attachments"] = kept_a
                    changed = True
        if changed:
            self.persist_messages()

    def inspect_workspace(self) -> dict[str, Any]:
        self.ensure_home_dirs()
        files: list[dict[str, Any]] = []
        root = self.workspace
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                rel = p.relative_to(root).as_posix()
                if rel.startswith(".git/") or rel == ".git":
                    continue
                ext = p.suffix.lower()
                kind = "dir" if p.is_dir() else "file"
                if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
                    kind = "image"
                elif ext in {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"}:
                    kind = "video"
                elif ext in {".md", ".txt", ".py", ".rs", ".toml", ".json", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".yaml", ".yml"}:
                    kind = "doc"
                files.append(
                    {
                        "path": rel,
                        "dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else 0,
                        "kind": kind,
                    }
                )
        return {
            "id": self.workspace_id,
            "bot_id": self.id,
            "backend": "local",
            "path": str(root),
            "status": "ready" if self.acp.alive else "stopped",
            "control": self.control,
            "surface": self.surface,
            "desktop_cursor": dict(self.desktop_cursor),
            "files": files[:400],
            "tools": self.tool_log[-20:],
            "routines": self.routines,
            "dev": self.detect_dev_system(),
        }

    def detect_dev_system(self) -> dict[str, Any]:
        root = self.workspace
        commands: dict[str, str] = {}
        kind = "generic"
        if (root / "package.json").is_file():
            kind = "node"
            try:
                pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
                scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
            except Exception:
                scripts = {}
            runner = "npm"
            if (root / "pnpm-lock.yaml").is_file():
                runner = "pnpm"
            elif (root / "yarn.lock").is_file():
                runner = "yarn"
            if "build" in scripts:
                commands["build"] = f"{runner} run build"
            if "test" in scripts:
                commands["test"] = f"{runner} test"
            for key in ("dev", "start", "preview"):
                if key in scripts and "run" not in commands:
                    commands["run"] = f"{runner} run {key}"
        if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file() or any(root.glob("test*.py")) or (root / "tests").is_dir():
            if kind == "generic":
                kind = "python"
            commands.setdefault("test", "python3 -m unittest discover -s tests -v" if (root / "tests").is_dir() else "python3 -m pytest -q")
        if (root / "Cargo.toml").is_file():
            kind = "rust"
            commands.setdefault("build", "cargo build")
            commands.setdefault("test", "cargo test")
            commands.setdefault("run", "cargo run")
        if (root / "CMakeLists.txt").is_file():
            kind = "cmake"
            commands.setdefault("build", "cmake -S . -B build && cmake --build build")
            commands.setdefault("test", "ctest --test-dir build --output-on-failure")
        elif (root / "Makefile").is_file():
            kind = "make"
            commands.setdefault("build", "make")
            commands.setdefault("test", "make test")
        if not commands:
            commands["test"] = "python3 -m unittest discover -s tests -v" if (root / "tests").is_dir() else "git status --short"
        return {"kind": kind, "commands": commands, "process": self.dev_process_status()}

    def dev_process_status(self) -> dict[str, Any]:
        with self._dev_proc_lock:
            proc = self.dev_proc
            running = bool(proc and proc.poll() is None)
            return {
                "running": running,
                "pid": proc.pid if running and proc else None,
                "command": self.dev_proc_command,
                "cwd": self.dev_proc_cwd,
                "returncode": None if running or not proc else proc.poll(),
                "output": self.dev_proc_output[-120_000:],
            }

    def _read_dev_process(self, proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        try:
            if stream:
                for line in stream:
                    with self._dev_proc_lock:
                        if proc is not self.dev_proc:
                            return
                        self.dev_proc_output = (self.dev_proc_output + line)[-120_000:]
                    emit(
                        {
                            "type": "dev.process",
                            "bot_id": self.id,
                            **self.dev_process_status(),
                        }
                    )
        finally:
            try:
                proc.wait(timeout=0.1)
            except Exception:
                pass
            with self._dev_proc_lock:
                if proc is not self.dev_proc:
                    return
            emit({"type": "dev.process", "bot_id": self.id, **self.dev_process_status()})

    def start_dev_process(self, command: str, cwd: str = "") -> dict[str, Any]:
        command = (command or "").strip()
        if not command:
            raise ValueError("command required")
        work = workspace_target(self, cwd or ".")
        if not work.is_dir():
            raise ValueError("cwd must be a workspace directory")
        self.stop_dev_process()
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", f"exec {command}"],
            cwd=work,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
            start_new_session=True,
            bufsize=1,
        )
        with self._dev_proc_lock:
            self.dev_proc = proc
            self.dev_proc_command = command
            self.dev_proc_cwd = work.relative_to(self.workspace.resolve()).as_posix() if work != self.workspace.resolve() else "."
            self.dev_proc_output = ""
        threading.Thread(target=self._read_dev_process, args=(proc,), daemon=True).start()
        status = self.dev_process_status()
        emit({"type": "dev.process", "bot_id": self.id, **status})
        return {"ok": True, **status}

    def stop_dev_process(self) -> dict[str, Any]:
        with self._dev_proc_lock:
            proc = self.dev_proc
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=1)
                except Exception:
                    pass
            except (ProcessLookupError, OSError):
                pass
        status = self.dev_process_status()
        if proc:
            emit({"type": "dev.process", "bot_id": self.id, **status})
        return {"ok": True, **status}

    def run_dev_command(self, command: str, cwd: str = "", timeout: int = 120) -> dict[str, Any]:
        command = (command or "").strip()
        if not command:
            raise ValueError("command required")
        work = workspace_target(self, cwd or ".")
        if not work.is_dir():
            raise ValueError("cwd must be a workspace directory")
        timeout = max(1, min(int(timeout or 120), 300))
        started = time.time()
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=os.environ.copy(),
        )
        output = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else ""))[-120_000:]
        result = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "command": command,
            "cwd": work.relative_to(self.workspace.resolve()).as_posix() if work != self.workspace.resolve() else ".",
            "elapsed": round(time.time() - started, 3),
            "output": output,
        }
        emit({"type": "dev.result", "bot_id": self.id, **result})
        emit({"type": "workspace", "bot_id": self.id})
        return result

    def routines_path(self) -> Path:
        return self.root / "routines.json"

    def load_routines(self) -> None:
        p = self.routines_path()
        if p.is_file():
            try:
                self.routines = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.routines = []
        else:
            self.routines = []

    def save_routines(self) -> None:
        self.routines_path().write_text(json.dumps(self.routines, indent=2) + "\n", encoding="utf-8")

    def add_routine(self, body: dict[str, Any]) -> dict[str, Any]:
        name = (body.get("name") or "").strip()
        instruction = (body.get("instruction") or "").strip()
        schedule = (body.get("schedule") or "1h").strip()
        if not name or not instruction:
            raise ValueError("name and instruction are required")
        item = {
            "id": "r_" + uuid.uuid4().hex[:10],
            "name": name,
            "instruction": instruction,
            "schedule": schedule,
            "enabled": body.get("enabled", True) is not False,
            "last_run": None,
            "next_run": next_run_ts(schedule),
            "last_status": "idle",
        }
        for extra in ("type", "timezone", "time", "cron", "schedulePreset"):
            if body.get(extra) not in (None, ""):
                item[extra] = body[extra]
        with self._routine_lock:
            self.routines.append(item)
            self.save_routines()
        return item

    def patch_routine(self, rid: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._routine_lock:
            item = next((r for r in self.routines if r["id"] == rid), None)
            if not item:
                raise ValueError("routine not found")
            if "enabled" in body:
                item["enabled"] = bool(body["enabled"])
            if body.get("schedule"):
                item["schedule"] = body["schedule"]
                item["next_run"] = next_run_ts(body["schedule"])
            if body.get("instruction"):
                item["instruction"] = body["instruction"]
            if body.get("name"):
                item["name"] = body["name"]
            for extra in ("type", "timezone", "time", "cron", "schedulePreset"):
                if extra in body and body.get(extra) not in (None, ""):
                    item[extra] = body[extra]
            self.save_routines()
            return item

    def delete_routine(self, rid: str) -> None:
        with self._routine_lock:
            self.routines = [r for r in self.routines if r["id"] != rid]
            self.save_routines()

    def tick_routines(self) -> None:
        now = time.time()
        due: list[dict[str, Any]] = []
        with self._routine_lock:
            for r in self.routines:
                if r.get("enabled") and r.get("next_run") and r["next_run"] <= now:
                    due.append(r)
                    r["last_status"] = "running"
                    r["next_run"] = next_run_ts(r.get("schedule") or "1h")
            if due:
                self.save_routines()
        for r in due:
            text = (
                f"[Routine: {r['name']} — schedule {r.get('schedule')}]\n"
                f"{r.get('instruction')}"
            )
            try:
                self.append_msg("system", f"⏰ Routine “{r['name']}” started")
                self.status = f"Routine: {r['name']}"
                emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})
                emit({"type": "routines", "bot_id": self.id, "routines": self.routines})
                self.acp.prompt(text)
                self.close_chunk()
                r["last_run"] = time.time()
                r["last_status"] = "ok"
            except Exception as e:
                r["last_run"] = time.time()
                r["last_status"] = f"error: {e}"
            with self._routine_lock:
                self.save_routines()
            emit({"type": "routines", "bot_id": self.id, "routines": self.routines})
            self.status = "Ready"
            emit({"type": "status", "bot_id": self.id, "text": self.status, "surface": self.surface, "control": self.control})

    def save_paste_image(self, mime: str, data_b64: str, name: str | None = None) -> dict[str, Any]:
        mime = (mime or "image/png").split(";")[0].strip().lower()
        video = mime.startswith("video/")
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/heic": ".heic",
            "image/heif": ".heif",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }.get(mime, ".mp4" if video else ".png")
        raw = base64.b64decode(data_b64)
        folder = "Videos" if video else "Pictures"
        kind = "video" if video else "chat"
        rel = dated_media_relpath(folder, kind, ext, hint=name or "")
        path = unique_workspace_file(self.workspace, rel)
        path.write_bytes(raw)
        out_rel = path.relative_to(self.workspace).as_posix()
        return {"path": out_rel, "mime": mime, "data": data_b64, "name": path.name, "label": path.stem}

    def extract_video_stills(self, item: dict[str, Any], n: int = 6) -> list[dict[str, Any]]:
        """Turn a pasted video into a few JPEGs VL-8B can actually look at."""
        if not str(item.get("mime") or "").startswith("video/"):
            return []
        rel = str(item.get("path") or "")
        src = self.workspace / rel
        if not rel or not src.is_file() or not shutil.which("ffmpeg"):
            return []
        stem = src.stem
        pattern = src.parent / f"{stem}-still-%02d.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src),
                    "-vf", "fps=2,scale=480:-2",
                    "-frames:v", str(max(1, min(n, 8))),
                    str(pattern),
                ],
                check=False,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        out: list[dict[str, Any]] = []
        for still in sorted(src.parent.glob(f"{stem}-still-*.jpg")):
            out.append({
                "path": still.relative_to(self.workspace).as_posix(),
                "mime": "image/jpeg",
                "name": still.name,
                "label": still.stem,
            })
        return out


def _parse_hhmm(raw: str, default_h: int = 8, default_m: int = 0) -> tuple[int, int]:
    text = (raw or "").strip()
    if not text:
        return default_h, default_m
    if ":" in text:
        hh, _, mm = text.partition(":")
        try:
            return max(0, min(23, int(hh))), max(0, min(59, int(mm or "0")))
        except ValueError:
            return default_h, default_m
    try:
        return max(0, min(23, int(text))), default_m
    except ValueError:
        return default_h, default_m


def next_run_ts(schedule: str, from_ts: float | None = None) -> float:
    now = datetime.fromtimestamp(from_ts or time.time())
    s = (schedule or "1h").strip().lower()
    interval = re.match(r"^(\d+(?:\.\d+)?)(mo|[smhdwy])$", s)
    if interval:
        amount = max(1.0, float(interval.group(1))); unit = interval.group(2); base_ts = from_ts or time.time()
        if unit in {"s", "m", "h", "d", "w"}:
            units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
            return base_ts + max(60.0, amount * units[unit])
        import calendar
        whole = max(1, int(amount))
        if unit == "mo":
            idx = now.month - 1 + whole; year = now.year + idx // 12; month = idx % 12 + 1; day = min(now.day, calendar.monthrange(year, month)[1])
            return now.replace(year=year, month=month, day=day).timestamp()
        year = now.year + whole; day = min(now.day, calendar.monthrange(year, now.month)[1])
        return now.replace(year=year, day=day).timestamp()
    named = re.match(r"^(weekdays?|daily|weekly)(?:[-_ ](\d{1,2}(?::\d{2})?))?$", s)
    if named:
        kind, clock = named.group(1), named.group(2)
        hour, minute = _parse_hhmm(clock or "8")
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == "weekly":
            if candidate <= now:
                candidate += timedelta(days=7)
            return candidate.timestamp()
        if candidate <= now:
            candidate += timedelta(days=1)
        if kind.startswith("weekday"):
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
        return candidate.timestamp()
    return (from_ts or time.time()) + 3600


_AVATAR_SHAPES = {"", "blob", "drop", "square", "triangle"}


def _avatar_from_body(body: dict[str, Any]) -> tuple[str, str]:
    color = str(body.get("avatar_color") or "").strip()
    shape = str(body.get("avatar_shape") or "").strip()
    av = body.get("avatar")
    if isinstance(av, dict):
        color = color or str(av.get("color") or "").strip()
        shape = shape or str(av.get("shape") or "").strip()
    if color and not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", color):
        color = ""
    if shape not in _AVATAR_SHAPES:
        shape = ""
    return color, shape


def _avatar_from_profile_text(fields: dict[str, str], raw: str = "") -> tuple[str, str]:
    """Read color/shape from top-level keys or the avatar = { ... } inline table."""
    color = str(fields.get("avatar_color") or "").strip()
    shape = str(fields.get("avatar_shape") or "").strip()
    av = str(fields.get("avatar") or "")
    src = av if av else raw
    if not color:
        m = re.search(r"\bcolor\s*=\s*\"([^\"]*)\"", src)
        if m:
            color = m.group(1).strip()
    if not shape:
        m = re.search(r"\bshape\s*=\s*\"([^\"]*)\"", src)
        if m:
            shape = m.group(1).strip()
    if color and not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", color):
        color = ""
    if shape not in _AVATAR_SHAPES:
        shape = ""
    return color, shape


def load_existing() -> None:
    root = USER_AGENT_HOME / "bots"
    if not root.is_dir():
        return
    for d in root.iterdir():
        if not d.is_dir() or not (d / "PROFILE.toml").is_file():
            continue
        soul = (d / "SOUL.md").read_text(encoding="utf-8") if (d / "SOUL.md").is_file() else ""
        raw = (d / "PROFILE.toml").read_text(encoding="utf-8")
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" in line and not line.strip().startswith("#") and not line.strip().startswith("{"):
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip().strip('"')
        bid = fields.get("id") or d.name
        name = fields.get("name") or bid
        desc = fields.get("description") or ""
        model = fields.get("model") or load_user_models()[0]
        emoji = "◉"
        if "value" in raw:
            # avatar = { kind = "emoji", value = "🔎" }
            try:
                emoji = raw.split("value =")[1].split('"')[1]
            except Exception:
                pass
        bot = Bot(bid, name, desc, soul, model, emoji, kind=fields.get("kind") or "")
        bot.avatar_color, bot.avatar_shape = _avatar_from_profile_text(fields, raw)
        if occupies_teela_brain_slot(bot) and teela_brain_slot_taken():
            print(
                f"[deskd] skipping extra Teela Brain {bid}; only one is allowed per computer",
                flush=True,
            )
            continue
        bots[bid] = bot
        try:
            bot.provision()
            bot.acp.start(load_session_id=bot.last_acp_session_id())
            bot.status = "Ready"
        except Exception as e:
            bot.status = f"Error: {e}"


def create_bot(body: dict[str, Any]) -> Bot:
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    description = (body.get("description") or f"{name} — user-defined bot.").strip()
    kind = normalize_bot_kind(
        body.get("kind") or body.get("bot_kind") or body.get("type"),
        default=BOT_KIND_TEELA_BRAIN,
    )
    ensure_single_teela_brain(kind)
    soul = (body.get("soul") or default_soul_for_kind(kind, name, description)).strip()
    model = (body.get("model") or load_user_models()[0]).strip()
    if not model:
        raise ValueError("model is required")
    _default, _cat = load_user_models()
    _cat_tbl = _cat.get(model) if isinstance(_cat.get(model), dict) else {}
    # Reject a model this host cannot actually serve before spawning the agent.
    # A keyless cloud row (no xAI key) would otherwise make ACP's session/new
    # fail with an opaque "Internal error / No LLM provider configured".
    if not model_has_provider(model, _cat_tbl):
        raise ValueError(
            f"Model {model} has no usable provider on this host "
            "(cloud model with no API key, or local endpoint not reachable). "
            "Pick a model with a provider in Settings → Models, or add the API key."
        )
    ensure_model_on_host(model, bot=types.SimpleNamespace(kind=kind))
    emoji = (body.get("emoji") or "◉").strip()[:4]
    bid = bot_id()
    bot = Bot(bid, name, description, soul, model, emoji, kind=kind)
    color, shape = _avatar_from_body(body)
    bot.avatar_color = color
    bot.avatar_shape = shape
    bot.provision()
    if not Path(HERMES_BIN).is_file():
        raise FileNotFoundError(
            f"Hermes Agent CLI not found at {HERMES_BIN}. "
            "Do not start hermes-deskd as root. Install Hermes Agent and run ./start.sh as that user, "
            "or export HERMES_BIN=/path/to/hermes"
        )
    try:
        bot.acp.start()
    except BaseException as e:
        # Roll back before registering: a bot whose ACP session could not be
        # created (e.g. model has no provider) must not be persisted, or it
        # becomes a zombie that re-fails on every daemon restart.
        with lock:
            bots.pop(bid, None)
        try:
            bot.destroy()
        except Exception:
            pass
        if isinstance(e, FileNotFoundError):
            raise
        raise ValueError(
            f"Could not start the agent for {name!r} (model {model!r}): {e}. "
            "Pick a model with a provider in Settings → Models and try again."
        ) from e
    with lock:
        bots[bid] = bot
    emit({"type": "bot.created", "bot": bot.profile()})
    if cluster is not None:
        threading.Thread(target=cluster.announce_to_peers, daemon=True).start()
    emit({"type": "status", "bot_id": bid, "text": "Ready", "surface": "files", "control": "agent_controlled"})
    return bot


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".zip": "application/zip",
    ".bin": "application/octet-stream",
    ".gz": "application/octet-stream",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr = __import__("sys").stderr
        msg = fmt % args
        msg = re.sub(r"\?[^ ]*", "?[redacted]", msg)
        print(
            f"[deskd] {time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} {msg}",
            file=sys_stderr,
        )

    def _is_loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _require_loopback(self, role: str) -> bool:
        return role == "ui_local"

    def _secret_eq(self, given: str, expected: str) -> bool:
        if not given or not expected:
            return False
        a, b = given.encode("utf-8"), expected.encode("utf-8")
        if len(a) != len(b):
            hmac.compare_digest(b, b)
            return False
        return hmac.compare_digest(a, b)

    def _ui_ok(self) -> bool:
        token = desk_token()
        if not token:
            return False
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            if self._secret_eq(header[7:].strip(), token):
                return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == "hermes_desk_token" and self._secret_eq(value, token):
                return True
        # Backward compatibility for older Hermes Desk clients. New UI never puts
        # the long-lived token in a URL.
        q = parse_qs(urlparse(self.path).query)
        qt = q.get("token", [None])[0]
        if qt is not None and self._secret_eq(str(qt), token):
            return True
        return False

    def _cluster_header_present(self) -> bool:
        if self.headers.get("X-Hermes-Cluster-Token"):
            return True
        auth = self.headers.get("Authorization", "")
        return auth.lower().startswith("cluster ")

    def _cluster_ok(self) -> bool:
        token = cluster.token if cluster is not None else ""
        if not token:
            return False
        given = self.headers.get("X-Hermes-Cluster-Token", "") or ""
        if not given:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("cluster "):
                given = auth[8:]
        given = normalize_cluster_token(given)
        token = normalize_cluster_token(token)
        if not given:
            return False
        return self._secret_eq(given, token)

    def _ui_role(self) -> str:
        if not self._ui_ok():
            return "deny"
        return "ui_local" if self._is_loopback() else "ui"

    def _authorize(self) -> str:
        """Return 'public' | 'ui' | 'ui_local' | 'cluster' | 'deny'. Never OR secrets."""
        path = urlparse(self.path).path
        if path in (
            "/",
            "/index.html",
            "/app.js",
            "/styles.css",
            "/hermesbot.css",
            "/hermesbot-ui.js",
            "/working-memory.js",
        ) or path.startswith(
            ("/ui/", "/assets/", "/vendor/")
        ):
            return "public"
        if path.startswith("/v1/llm"):
            return "public" if self._is_loopback() else "deny"
        if path.startswith("/v1/memory"):
            if self._cluster_header_present():
                return "deny"
            if self._ui_ok() and self._is_loopback():
                return "ui_local"
            return "deny"
        if path.startswith("/v1/debug/context") or "/working-memory" in path:
            if self._cluster_header_present():
                return "deny"
            if self._ui_ok():
                return self._ui_role()
            return "deny"
        if path == "/v1/bootstrap":
            if self._cluster_header_present():
                return "deny"
            if self._is_loopback() or lan_mode():
                return "public"
            return "deny"
        if path in UI_LOCAL_ONLY:
            return "ui_local" if self._ui_ok() and self._is_loopback() else "deny"
        if path == "/v1/settings" or path in UI_CLUSTER_HELPERS:
            return self._ui_role()
        if path.startswith("/v1/cluster/"):
            return "cluster" if self._cluster_ok() else "deny"
        if path == "/v1/events":
            return self._ui_role() if self._ui_ok() else "deny"
        if self._ui_ok():
            return self._ui_role()
        if self._cluster_ok():
            return "cluster"
        return "deny"

    def _auth_ok(self) -> bool:
        return self._authorize() != "deny"

    def _drain(self) -> None:
        if self.command not in ("POST", "PUT", "PATCH"):
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            return
        if 0 < n <= 80 * 1024 * 1024:
            try:
                self.rfile.read(n)
            except Exception:
                pass

    def _deny(self, role: str | None = None) -> None:
        self._drain()
        path = urlparse(self.path).path
        if path == "/v1/cluster/token-new":
            return self._json(403, {"error": "cluster config is local-only"})
        if path.startswith("/v1/cluster/") and path not in UI_CLUSTER_HELPERS and path not in UI_LOCAL_ONLY:
            if cluster is None or not cluster.token:
                return self._json(403, {"error": "cluster not configured"})
        return self._json(401, {"error": "unauthorized"})

    def _proxy_or_local(self, path: str) -> bool:
        """If this path names a remote bot/workspace, proxy and return True."""
        if cluster is None:
            return False
        bid = bot_id_from_path(path)
        if not bid:
            return False
        own = cluster.owner(bid)
        if own == "local" or own is None:
            return False
        if getattr(own, "status", "") == "offline":
            self._json(503, {"error": "peer offline", "node": own.name, "peer": own.url})
            return True
        cluster.proxy(self, own, timeout=timeout_for(path, self.command))
        return True

    def _lookup_local_bot(self, spec: str) -> Bot | None:
        spec = (spec or "").strip()
        if not spec:
            return None
        bot = bots.get(spec)
        if bot:
            return bot
        return next((b for b in bots.values() if b.name.lower() == spec.lower()), None)

    def _settings_public(self) -> dict[str, Any]:
        out = public_listen()
        if cluster is not None:
            out["node_name"] = cluster.node_name
            out["cluster_token_set"] = bool(cluster.token)
            out["cluster_token_fp"] = cluster_token_fp(cluster.token)
            out["peers"] = [cluster.diagnostics_row(p) for p in cluster.peers]
        else:
            cfg = load_desk_config()
            out["node_name"] = cfg.get("node_name") or socket.gethostname()
            out["cluster_token_set"] = False
            out["cluster_token_fp"] = ""
            out["peers"] = []
        default, rows = settings_model_rows()
        out["default_model"] = default
        out["models"] = rows
        out["models_dir"] = str(MODELS_DIR)
        out["voice"] = voice_enabled()
        return out

    def _cluster_get(self, path: str, role: str) -> bool:
        if cluster is not None:
            cluster.note_inbound(self.client_address[0])
        if path == "/v1/cluster/hello":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            self._json(200, cluster.hello_payload())
            return True
        if path == "/v1/cluster/bots":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            with lock:
                self._json(200, {"node": cluster.node_name, "bots": cluster.local_roster()})
            return True
        if path == "/v1/cluster/events":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            self._sse(cluster_feed=True)
            return True
        if path == "/v1/cluster/links":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            self._json(200, cluster.list_links())
            return True
        if path == "/v1/cluster/robot/capabilities":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            self._json(200, motion.capabilities(cluster.node_name))
            return True
        if path == "/v1/cluster/peer-models":
            if cluster is None:
                self._json(503, {"error": "cluster not configured"})
                return True
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("peer", [""])[0] or "").strip()
            peer = cluster.peer_by_name(name)
            if not peer:
                self._json(404, {"error": "unknown peer"})
                return True
            try:
                self._json(200, cluster.peer_models(peer))
            except PermissionError:
                self._json(502, {"error": "cluster auth failed"})
            except ConnectionError as e:
                self._json(503, {"error": str(e), "node": peer.name, "peer": peer.url})
            except Exception as e:
                self._json(502, {"error": str(e)})
            return True
        if path.startswith("/v1/cluster/"):
            self._json(404, {"error": "not found"})
            return True
        return False

    def _cluster_settings_patch(self, body: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if "node_name" in body and body.get("node_name") not in (None, ""):
            patch["node_name"] = validate_node_name(str(body.get("node_name") or ""))
        current = ""
        if cluster is not None:
            current = cluster.token or ""
        if not current:
            current = str(read_desk_file().get("cluster_token") or "")
        incoming = body.get("cluster_token") if "cluster_token" in body else None
        clear = bool(body.get("cluster_token_clear"))
        if clear or (incoming not in (None, "")):
            # Mesh writes are already loopback-only. Do not require the old
            # token on localhost or a forgotten secret cannot be reset.
            if current and not self._is_loopback():
                confirm = str(body.get("cluster_token_confirm") or "")
                if not self._secret_eq(confirm, current):
                    raise PermissionError("cluster_token_confirm does not match")
            if clear:
                patch["cluster_token_clear"] = True
            elif incoming not in (None, ""):
                patch["cluster_token"] = normalize_cluster_token(incoming)
        if "peers" in body and body.get("peers_loaded") is True:
            raw = body.get("peers")
            if not isinstance(raw, list):
                raise ValueError("peers must be a list")
            if len(raw) > 8:
                raise ValueError("at most 8 peers")
            listen_host, listen_port, access = (
                LISTEN_HOST,
                LISTEN_PORT,
                access_host(),
            )
            peers = []
            for row in raw:
                if not isinstance(row, dict):
                    raise ValueError("peer must be an object")
                pname = validate_node_name(str(row.get("name") or ""))
                url = validate_peer_url(
                    str(row.get("url") or ""),
                    self_host=access or listen_host,
                    self_port=int(listen_port),
                )
                peers.append({"name": pname, "url": url})
            names = [p["name"] for p in peers]
            if len(names) != len(set(names)):
                raise ValueError("peer names must be unique")
            patch["peers"] = peers
        return patch

    def _cluster_post(self, path: str, body: dict[str, Any], role: str) -> bool:
        if path == "/v1/cluster/token-new":
            self._json(200, {"cluster_token": secrets.token_urlsafe(32)})
            return True
        if path == "/v1/cluster/self-test":
            if cluster is None:
                self._json(503, {"error": "cluster not configured"})
                return True
            self._json(200, cluster.self_test())
            return True
        if path == "/v1/cluster/create":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            name = (body.get("peer") or "").strip()
            peer = cluster.peer_by_name(name)
            if not peer:
                self._json(404, {"error": "unknown peer"})
                return True
            model = (body.get("model") or "").strip()
            if model:
                try:
                    catalog = cluster.peer_models(peer)
                except Exception as e:
                    self._json(503, {"error": str(e), "node": peer.name})
                    return True
                ids = {str(m.get("id")) for m in (catalog.get("models") or []) if isinstance(m, dict)}
                default = str(catalog.get("default") or "")
                if default:
                    ids.add(default)
                if model not in ids:
                    self._json(400, {"error": f"model {model} is not on {peer.name}"})
                    return True
            payload = {
                k: body.get(k)
                for k in ("name", "description", "soul", "model", "kind", "emoji", "avatar_color", "avatar_shape")
                if k in body or k == "name"
            }
            payload["name"] = body.get("name")
            try:
                result = cluster.create_on_peer(peer, payload)
            except PermissionError:
                self._json(502, {"error": "cluster auth failed"})
                return True
            except (ConnectionError, ValueError, RuntimeError) as e:
                code = 503 if isinstance(e, ConnectionError) else 400
                self._json(code, {"error": str(e), "node": peer.name})
                return True
            self._json(200, result)
            return True
        if path == "/v1/cluster/announce":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            cluster.note_inbound(self.client_address[0])
            try:
                result = cluster.accept_announce(str(body.get("node") or ""), body.get("bots") or [])
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return True
            except KeyError as e:
                self._json(404, {"error": str(e)})
                return True
            self._json(200, result)
            return True
        if path == "/v1/cluster/robot/execute":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            cluster.note_inbound(self.client_address[0])
            self._json(200, motion.handle_execute(cluster.node_name, body if isinstance(body, dict) else {}))
            return True
        if path == "/v1/cluster/robot/wbc":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            cluster.note_inbound(self.client_address[0])
            self._json(200, motion.handle_wbc(cluster.node_name, body if isinstance(body, dict) else {}))
            return True
        if path == "/v1/cluster/dm":
            if cluster is None or not cluster.token:
                self._json(403, {"error": "cluster not configured"})
                return True
            cluster.note_inbound(self.client_address[0])
            dest_spec = (body.get("to") or "").strip()
            text = (body.get("text") or "").strip()
            if not dest_spec or not text:
                self._json(400, {"error": "to and text required"})
                return True
            dest = self._lookup_local_bot(dest_spec)
            if not dest:
                self._json(404, {"error": f"no bot named {dest_spec}"})
                return True
            dest.deliver_remote_dm(
                str(body.get("from_id") or ""),
                str(body.get("from_name") or "peer"),
                str(body.get("from_node") or ""),
                text,
            )
            self._json(
                200,
                {
                    "ok": True,
                    "to": dest.id,
                    "to_name": dest.name,
                    "node": cluster.node_name,
                },
            )
            return True
        if path.startswith("/v1/cluster/"):
            self._json(404, {"error": "not found"})
            return True
        return False

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError):
            return

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj).encode()
        self._send(code, raw, "application/json")

    def _send_json_completion(
        self,
        body: bytes,
        allowed_tools: list[str],
        *,
        promote_json: bool = True,
        stream: bool = False,
    ) -> None:
        body = rewrite_llm_response_body(
            body, "application/json", allowed_tools, promote_json=promote_json
        )
        ctype = "application/json"
        if stream:
            body = openai_completion_to_sse(body)
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _pipe_agent_sse(
        self,
        resp: Any,
        allowed_tools: list[str],
        *,
        request_payload: dict[str, Any] | None,
        bot: Any,
        promote_json: bool,
    ) -> None:
        """Forward local thinking/content live; assemble tool_calls at the end."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        intent = last_user_intent_from_payload(request_payload) if request_payload else ""
        prior = last_assistant_tool_fingerprints(request_payload)
        known = known_background_task_ids(request_payload)
        stream_objs: list[dict[str, Any]] = []
        content_parts: list[str] = []
        saw_tool = False
        forwarded = False
        template: dict[str, Any] | None = None
        speed = tel.LocalStreamSpeed()
        fp = getattr(resp, "fp", None) or resp
        while True:
            if turn_was_cancelled(bot):
                break
            try:
                raw_line = fp.readline()
            except Exception:
                break
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
            if not line.startswith("data:"):
                continue
            payload, _ended = _sse_data_payload(line)
            if payload.strip() == "[DONE]":
                break
            if not payload.strip():
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            rewrite_completion_tool_names(
                obj,
                allowed_tools,
                promote_json=promote_json,
                intent=intent,
                fill_empty_shell=False,
            )
            if not isinstance(obj, dict):
                continue
            template = template or obj
            stream_objs.append(obj)
            visible = False
            toolish = False
            for choice in obj.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                if delta.get("tool_calls") or msg.get("tool_calls"):
                    toolish = True
                for block in (delta, msg):
                    for key in ("content", "reasoning_content", "reasoning", "thinking"):
                        val = block.get(key)
                        if isinstance(val, str) and val:
                            visible = True
                            content_parts.append(val)
                            live = speed.record(tel.count_tokens_local(val), time.monotonic())
                            if live is not None and bot is not None:
                                bot.note_local_stream_speed(live, speed.tokens)
            if toolish:
                saw_tool = True
                continue
            if not visible:
                continue
            if not forwarded:
                forwarded = True
                stop_local_prefill_progress(bot)
                emit_local_activity(bot, "Speaking…")
                emit_local_progress(bot, "")
            try:
                chunk = "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
        if bot is not None:
            final_speed = speed.final()
            if final_speed is not None:
                bot.note_local_stream_speed(final_speed, speed.tokens, log=True)
        if saw_tool:
            calls = assemble_stream_tool_calls(
                stream_objs,
                allowed_tools,
                intent=intent,
                extra_text="".join(content_parts),
                fill_empty_shell=False,
            )
            calls = _sanitize_local_tool_calls(calls, prior_tools=prior, known_task_ids=known)
            if calls:
                try:
                    self.wfile.write(sse_tool_calls_body(template, calls))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                return
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_llm_upstream_response(
        self,
        resp: Any,
        ctype: str,
        rest: str,
        allowed_tools: list[str],
        *,
        promote_json: bool = True,
        request_payload: dict[str, Any] | None = None,
        stream: bool = False,
        bot: Any = None,
    ) -> None:
        stop_local_prefill_progress(bot)
        ctype_l = (ctype or "").lower()
        if (
            "chat/completions" in rest
            and getattr(resp, "status", 200) == 200
            and bot_kind_is_agent(bot)
            and (stream or "event-stream" in ctype_l)
        ):
            self._pipe_agent_sse(
                resp,
                allowed_tools,
                request_payload=request_payload,
                bot=bot,
                promote_json=promote_json,
            )
            return
        body = resp.read()
        if "chat/completions" in rest and getattr(resp, "status", 200) == 200:
            intent = last_user_intent_from_payload(request_payload) if request_payload else ""
            body = rewrite_llm_response_body(
                body,
                ctype,
                allowed_tools,
                promote_json=promote_json,
                intent=intent,
                prior_tools=last_assistant_tool_fingerprints(request_payload),
                known_task_ids=(
                    known_background_task_ids(request_payload)
                    if bot_kind_is_agent(bot)
                    else None
                ),
                fill_empty_shell=not bot_kind_is_agent(bot),
            )
            if request_payload and not bot_kind_is_agent(bot):
                served = str((request_payload or {}).get("model") or "qwen38")
                checked = ensure_usable_system_check_completion(
                    body,
                    request_payload,
                    ctype,
                    served=served,
                    bot=bot,
                )
                if checked == body:
                    checked = ensure_usable_desktop_completion(
                        body,
                        request_payload,
                        ctype,
                        served=served,
                        bot=bot,
                    )
                if checked != body:
                    body = checked
                    if stream or "event-stream" in (ctype or "").lower() or body.lstrip().startswith(b"data:"):
                        ctype = "text/event-stream"
                    else:
                        ctype = "application/json"
                ensured = ensure_usable_motor_completion(
                    body,
                    request_payload,
                    ctype,
                    served=served,
                    bot=bot,
                )
                if ensured != body:
                    body = ensured
                    if stream or "event-stream" in (ctype or "").lower() or body.lstrip().startswith(b"data:"):
                        ctype = "text/event-stream"
                    else:
                        ctype = "application/json"
                else:
                    planned = prefer_inferred_plan_completion(
                        body,
                        request_payload,
                        ctype,
                        served=served,
                        bot=bot,
                    )
                    if planned:
                        body = planned
                        if stream or "event-stream" in (ctype or "").lower() or body.lstrip().startswith(b"data:"):
                            ctype = "text/event-stream"
                        else:
                            ctype = "application/json"
                    elif not response_has_robot_tool_call(body):
                        fb = fallback_motor_completion(request_payload, bot=bot)
                        if fb:
                            print("[deskd] replacing empty completion with motor fallback", flush=True)
                            body = rewrite_llm_response_body(
                                fb, "application/json", allowed_tools, promote_json=True
                            )
                            if stream or "event-stream" in (ctype or "").lower():
                                body = openai_completion_to_sse(body)
                                ctype = "text/event-stream"
                            else:
                                ctype = "application/json"
            if completion_truncated_by_max_tokens(body):
                motor = bool(request_payload) and local_llm_motor_turn(request_payload)
                moved = bool(request_payload) and payload_already_moved(request_payload, bot)
                fb = None
                if motor and not moved and not bot_kind_is_agent(bot):
                    fb = fallback_motor_completion(request_payload, bot=bot)
                if fb:
                    print("[deskd] truncated motor reply; using inferred tool", flush=True)
                    body = rewrite_llm_response_body(
                        fb, "application/json", allowed_tools, promote_json=True
                    )
                    if stream or "event-stream" in (ctype or "").lower() or body.lstrip().startswith(b"data:"):
                        body = openai_completion_to_sse(body)
                        ctype = "text/event-stream"
                    else:
                        ctype = "application/json"
                else:
                    print("[deskd] finish_reason=length rewritten to stop", flush=True)
                    body = rewrite_length_finish_to_stop(body)
        skip = {"transfer-encoding", "connection", "content-length"}
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        for k, v in resp.getheaders():
            if k.lower() in skip or k.lower() in {"content-type", "cache-control", "connection"}:
                continue
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _proxy_local_llm(self) -> None:
        """Loopback OpenAI shim: VL-8B for normal/motor/vision turns, 27B for hard think."""
        path = urlparse(self.path).path
        bid, rest = botmem.parse_llm_proxy_path(path)
        if not rest.startswith("/"):
            rest = "/" + rest
        n = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(n) if n > 0 else b""
        upstream_url = LOCAL_LLM_UPSTREAM
        served = ""
        allowed_tools: list[str] = []
        promote_json = True
        want_stream = False
        request_payload: dict[str, Any] | None = None
        bot = bots.get(bid) if bid else None
        if raw and "chat/completions" in rest and turn_was_cancelled(bot):
            self._send_json_completion(
                cancelled_llm_completion(served),
                allowed_tools,
                stream=want_stream,
            )
            return
        if raw and "chat/completions" in rest:
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                requested = str(payload.get("model") or "")
                payload = rewrite_local_llm_chat_payload(payload, bot=bot)
                want_stream = bool(payload.get("stream"))
                promote_json = not payload_already_moved(payload, bot)
                request_payload = payload
                if bot is not None:
                    payload = hydrate_local_mm_parts(payload, bot.workspace)
                    payload = attach_recent_chat_images(payload, bot)
                    motor = local_llm_motor_turn(payload)
                    if not motor:
                        try:
                            try:
                                cm.before_assemble(bot, "")
                                assembled = cm.assemble_for_bot(
                                    bot,
                                    payload.get("messages") or [],
                                    "",
                                )
                            except Exception:
                                assembled = bot.memory.observe_and_assemble(
                                    payload.get("messages") or [],
                                    "",
                                    botmem.TokenBudget.for_model(bot.model),
                                )
                            payload = botmem.inject_assembled_messages(payload, assembled)
                            try:
                                payload = cm.inject_checkpoint_message(payload, bot)
                            except Exception:
                                pass
                            try:
                                wm.capture_turn(bot, payload, assembled)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    payload = inject_live_body(payload, bot)
                    payload = apply_local_token_budget(payload, bot)
                    if not requested:
                        requested = str(bot.model or "")
                request_payload = payload
                promote_json = not payload_already_moved(payload, bot)
                upstream_url, served = pick_local_llm_route(
                    payload, requested_model=requested, bot=bot
                )
                payload["model"] = served
                allowed_tools = [
                    canonicalize_tool_name(n, None) for n in tool_names_from_payload(payload)
                ]
                if not bot_kind_is_agent(bot):
                    if not any("robot_pose" in n for n in allowed_tools):
                        allowed_tools = list(allowed_tools) + list(_DEFAULT_HERMES_MOTOR_TOOLS)
                    extra = (
                        "bot_desktop__desktop_state",
                        "bot_desktop__desktop_observe",
                        "search_tool",
                    )
                    for n in extra:
                        if n not in allowed_tools:
                            allowed_tools.append(n)
                mixed = orch.classify(last_user_intent_from_payload(payload))
                if mixed.get("mode") == "parallel" and bot is not None:
                    intent = last_user_intent_from_payload(payload)
                    hit = virtual_body.teela_args_from_motor(
                        robot_sim.infer_command(intent, getattr(bot, "robot_state", None)),
                        intent or "",
                    )
                    busy = any(
                        t.get("kind") == "body" and t.get("status") in {"running", "executing"}
                        for t in orch.snapshot(bot.id).get("tasks") or []
                    )
                    if hit and not busy:
                        _n, args = hit
                        skill = str(args.get("skill") or args.get("gesture") or "orient_head")
                        started = virtual_body.submit_action(bot.id, skill, args)
                        orch.note(
                            bot.id,
                            "body",
                            skill,
                            status="executing",
                            id=started.get("action_id"),
                        )
                        print(
                            f"[deskd] orchestrator parallel body {skill} {args}",
                            flush=True,
                        )
                intent_now = last_user_intent_from_payload(payload)
                mixed_now = orch.classify(intent_now).get("mode")
                if payload_already_moved(payload, bot) and mixed_now not in {"parallel", "after"}:
                    intent = intent_now
                    bid = str(getattr(bot, "id", "") or "") if bot is not None else ""
                    if bid:
                        virtual_body.wait_settle(bid)
                    st = getattr(bot, "robot_state", None) if bot is not None else None
                    virt = virtual_body.overlay(bid, st) if bot is not None else st
                    cmd = robot_sim.infer_command(intent, st) if intent else None
                    hit = virtual_body.teela_args_from_motor(cmd, intent or "")
                    still = bool(hit and virtual_body.move_still_needed(hit[1], virt))
                    matched = bool(
                        hit and virtual_body.same_request(_last_teela_args(payload), hit[1])
                    )
                    if not (still and not matched):
                        fb = fallback_moved_speech(
                            payload,
                            served=str(payload.get("model") or served or "qwen38"),
                            bot=bot,
                        )
                        print("[deskd] already moved; speaking so the turn can end", flush=True)
                        self._send_json_completion(
                            fb, allowed_tools, promote_json=False, stream=want_stream
                        )
                        return
                if (
                    not bot_kind_is_agent(bot)
                    and local_llm_motor_turn(payload)
                    and robot_tool_failed_this_turn(payload)
                ):
                    fb = fallback_motor_completion(
                        payload,
                        served=str(payload.get("model") or served or "qwen38"),
                        bot=bot,
                    )
                    if fb:
                        print("[deskd] retry motor after failed robot tool", flush=True)
                        self._send_json_completion(
                            fb, allowed_tools, promote_json=True, stream=want_stream
                        )
                        return
                direct = None if bot_kind_is_agent(bot) else local_llm_direct_completion(
                    payload,
                    served=str(payload.get("model") or served or "qwen38"),
                    bot=bot,
                )
                if direct:
                    print(
                        f"[deskd] motor direct after intent "
                        f"est={estimate_local_prompt_tokens(payload)} "
                        f"(skip llama prefill)",
                        flush=True,
                    )
                    self._send_json_completion(
                        direct, allowed_tools, promote_json=True, stream=want_stream
                    )
                    return
                if (
                    not bot_kind_is_agent(bot)
                    and local_llm_motor_turn(payload)
                    and not motion.needs_vision(last_user_intent_from_payload(payload))
                ):
                    fb = fallback_moved_speech(
                        payload,
                        served=str(payload.get("model") or served or "qwen38"),
                        bot=bot,
                    )
                    print("[deskd] motor follow-up skip llama; speaking", flush=True)
                    self._send_json_completion(
                        fb, allowed_tools, promote_json=False, stream=want_stream
                    )
                    return
                raw = json.dumps(payload).encode()
                est = estimate_local_prompt_tokens(payload)
                print(
                    f"[deskd] llm route {served} {upstream_url} motor={local_llm_motor_turn(payload)} "
                    f"hard={local_llm_hard_think(payload)} est={est} "
                    f"max_tokens={payload.get('max_tokens')} "
                    f"tools={tool_names_from_payload(payload)} "
                    f"choice={payload.get('tool_choice')!r}",
                    flush=True,
                )
                if bot_kind_is_agent(bot):
                    start_local_prefill_progress(bot, est)
        upstream = urlsplit(upstream_url)
        host = upstream.hostname or "127.0.0.1"
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        target = "/v1" + rest + (("?" + urlparse(self.path).query) if urlparse(self.path).query else "")
        hdrs = {
            "Content-Type": self.headers.get("Content-Type") or "application/json",
            "Accept": self.headers.get("Accept") or "application/json",
            "Connection": "close",
        }
        if raw:
            hdrs["Content-Length"] = str(len(raw))
        family = _hybrid_family(upstream_url, served) if (raw and "chat/completions" in rest) else None
        conn: HTTPConnection | None = None
        try:
            with hybrid_exclusive(family):
                try:
                    sock_timeout = ACP_PROMPT_MAX_SEC if "chat/completions" in rest else 10
                    conn = HTTPConnection(host, port, timeout=sock_timeout)
                    track_llm_conn(bid, conn)
                    conn.request(self.command, target, body=raw or None, headers=hdrs)
                    resp = conn.getresponse()
                except (ConnectionRefusedError, TimeoutError, OSError) as e:
                    if turn_was_cancelled(bot):
                        self._send_json_completion(
                            cancelled_llm_completion(served),
                            allowed_tools,
                            stream=want_stream,
                        )
                        return
                    fb = fallback_motor_completion(request_payload)
                    if fb:
                        print(f"[deskd] motor fallback after local llm down ({e})", flush=True)
                        self._send_json_completion(fb, allowed_tools, stream=want_stream)
                        return
                    if not (raw and "chat/completions" in rest and family == "think"):
                        if raw and "chat/completions" in rest:
                            print(f"[deskd] local llm {host}:{port} failed ({e})", flush=True)
                            self._send_json_completion(
                                fallback_engine_down_completion(request_payload, bot=bot),
                                allowed_tools,
                                stream=want_stream,
                            )
                            return
                        raise
                    print(f"[deskd] local llm {host}:{port} failed ({e}); retry VL-8B", flush=True)
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    fast = urlsplit(LOCAL_LLM_FAST_UPSTREAM)
                    host = fast.hostname or "127.0.0.1"
                    port = fast.port or 8001
                    try:
                        body_obj = json.loads(raw.decode() or "{}")
                    except json.JSONDecodeError:
                        body_obj = None
                    if isinstance(body_obj, dict):
                        body_obj["model"] = LOCAL_LLM_FAST_SERVED
                        raw = json.dumps(body_obj).encode()
                        hdrs["Content-Length"] = str(len(raw))
                    family = "fast"
                    try:
                        conn = HTTPConnection(host, port, timeout=ACP_PROMPT_MAX_SEC)
                        track_llm_conn(bid, conn)
                        conn.request(self.command, target, body=raw or None, headers=hdrs)
                        resp = conn.getresponse()
                    except (ConnectionRefusedError, TimeoutError, OSError) as e2:
                        print(f"[deskd] local llm down ({e2}); using fallback completion", flush=True)
                        self._send_json_completion(
                            fallback_engine_down_completion(request_payload, bot=bot),
                            allowed_tools,
                            stream=want_stream,
                        )
                        return
                skip = {"transfer-encoding", "connection", "content-length"}
                ctype = resp.getheader("Content-Type") or "application/json"
                if resp.status >= 500:
                    err_body = resp.read()
                    snippet = err_body[:800].decode("utf-8", "replace").replace("\n", " ")
                    print(f"[deskd] local llm HTTP {resp.status} {target}: {snippet}", flush=True)
                    fb = fallback_engine_down_completion(request_payload, bot=bot)
                    if fb:
                        print("[deskd] fallback after local llm HTTP 5xx", flush=True)
                        self._send_json_completion(fb, allowed_tools, stream=want_stream)
                        return
                    self.send_response(resp.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(err_body)))
                    self.end_headers()
                    self.wfile.write(err_body)
                    return
                if resp.status >= 400:
                    err_body = resp.read()
                    snippet = err_body[:800].decode("utf-8", "replace").replace("\n", " ")
                    print(f"[deskd] local llm HTTP {resp.status} {target}: {snippet}", flush=True)
                    overflow = (
                        parse_context_overflow_details(snippet)
                        if raw and "chat/completions" in rest and resp.status == 400
                        else None
                    )
                    if overflow:
                        try:
                            retry_payload = json.loads(raw.decode() or "{}")
                        except json.JSONDecodeError:
                            retry_payload = None
                        if isinstance(retry_payload, dict):
                            ctx, _out, inp = overflow
                            room = ctx - inp - 8
                            if room < 16:
                                retry_payload = apply_local_token_budget(
                                    trim_local_llm_payload(retry_payload, reserve=1024),
                                    bot,
                                )
                                print(
                                    f"[deskd] local llm retry after trimming prompt "
                                    f"(was {inp} input tokens / {ctx} window)",
                                    flush=True,
                                )
                            else:
                                retry_payload["max_tokens"] = room
                                retry_payload.pop("max_completion_tokens", None)
                                print(
                                    f"[deskd] local llm retry max_tokens={room} after context overflow",
                                    flush=True,
                                )
                            raw2 = json.dumps(retry_payload).encode()
                            hdrs2 = dict(hdrs)
                            hdrs2["Content-Length"] = str(len(raw2))
                            untrack_llm_conn(bid, conn)
                            conn.close()
                            conn = HTTPConnection(host, port, timeout=ACP_PROMPT_MAX_SEC)
                            track_llm_conn(bid, conn)
                            conn.request(self.command, target, body=raw2, headers=hdrs2)
                            resp = conn.getresponse()
                            ctype = resp.getheader("Content-Type") or "application/json"
                            if resp.status >= 400:
                                err_body = resp.read()
                                snippet = err_body[:800].decode("utf-8", "replace").replace("\n", " ")
                                print(f"[deskd] local llm overflow retry HTTP {resp.status}: {snippet}", flush=True)
                            else:
                                self._write_llm_upstream_response(
                                    resp,
                                    ctype,
                                    rest,
                                    allowed_tools,
                                    promote_json=promote_json,
                                    request_payload=request_payload,
                                    stream=want_stream,
                                    bot=bot,
                                )
                                return
                    need_retry = (
                        raw
                        and "chat/completions" in rest
                        and resp.status == 400
                        and b'"tools"' in raw
                        and (
                            "tool-call-parser" in snippet
                            or "tool_choice" in snippet.lower()
                            or "tool choice" in snippet.lower()
                        )
                    )
                    if need_retry:
                        try:
                            retry_payload = json.loads(raw.decode() or "{}")
                        except json.JSONDecodeError:
                            retry_payload = None
                        if isinstance(retry_payload, dict):
                            motor = local_llm_motor_turn(retry_payload)
                            retry_payload.pop("tool_choice", None)
                            if not motor:
                                retry_payload.pop("tools", None)
                            raw2 = json.dumps(retry_payload).encode()
                            hdrs2 = dict(hdrs)
                            hdrs2["Content-Length"] = str(len(raw2))
                            print(
                                "[deskd] local llm retry without tool_choice"
                                + ("" if motor else " or tools")
                                + " after 400",
                                flush=True,
                            )
                            untrack_llm_conn(bid, conn)
                            conn.close()
                            conn = HTTPConnection(host, port, timeout=ACP_PROMPT_MAX_SEC)
                            track_llm_conn(bid, conn)
                            conn.request(self.command, target, body=raw2, headers=hdrs2)
                            resp = conn.getresponse()
                            ctype = resp.getheader("Content-Type") or "application/json"
                            if resp.status >= 400:
                                err_body = resp.read()
                                snippet = err_body[:800].decode("utf-8", "replace").replace("\n", " ")
                                print(f"[deskd] local llm retry HTTP {resp.status}: {snippet}", flush=True)
                            else:
                                self._write_llm_upstream_response(
                                    resp,
                                    ctype,
                                    rest,
                                    allowed_tools,
                                    promote_json=promote_json,
                                    request_payload=request_payload,
                                    stream=want_stream,
                                    bot=bot,
                                )
                                return
                    fb = fallback_engine_down_completion(request_payload, bot=bot)
                    if fb:
                        print("[deskd] fallback after local llm 400", flush=True)
                        self._send_json_completion(fb, allowed_tools, stream=want_stream)
                        return
                    self.send_response(resp.status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(err_body)))
                    self.end_headers()
                    self.wfile.write(err_body)
                    return
                self._write_llm_upstream_response(
                    resp,
                    ctype,
                    rest,
                    allowed_tools,
                    promote_json=promote_json,
                    request_payload=request_payload,
                    stream=want_stream,
                    bot=bot,
                )
        except BrokenPipeError:
            pass
        except Exception as e:
            if turn_was_cancelled(bot):
                try:
                    self._send_json_completion(
                        cancelled_llm_completion(served),
                        allowed_tools,
                        stream=want_stream,
                    )
                except Exception:
                    pass
                return
            if raw and "chat/completions" in rest:
                print(f"[deskd] proxy error, using fallback completion: {e}", flush=True)
                try:
                    self._send_json_completion(
                        fallback_engine_down_completion(request_payload, bot=bot),
                        allowed_tools,
                        stream=want_stream,
                    )
                    return
                except Exception:
                    pass
            return self._json(502, {"error": f"local model proxy: {e}"})
        finally:
            stop_local_prefill_progress(bot)
            untrack_llm_conn(bid, conn)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_working_memory(self, path: str) -> None:
        """UI-auth read-only manifest. Not loopback-only (LAN UI must reach it). Cluster denied."""
        if self._cluster_header_present():
            return self._json(403, {"error": "working memory is host-local"})
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]
        bot = None
        rest = ""
        if len(parts) >= 2 and parts[0] == "v1" and parts[1] == "debug" and (len(parts) == 2 or parts[2] == "context"):
            bid = (qs.get("bot_id") or qs.get("bot") or [""])[0].strip()
            if not bid:
                return self._json(400, {"error": "bot_id required"})
            bot = bots.get(bid)
            rest = "/".join(parts[3:]) if len(parts) > 3 else ""
        elif len(parts) >= 4 and parts[0] == "v1" and parts[1] == "bots" and parts[3] == "working-memory":
            bot = bots.get(parts[2])
            rest = "/".join(parts[4:])
        else:
            return self._json(404, {"error": "not found"})
        if bot is None:
            return self._json(404, {"error": "not found"})
        if self.command != "GET":
            self._drain()
            return self._json(405, {"error": "method not allowed"})
        try:
            payload = wm.handle_get(bot, rest, qs)
        except Exception as e:
            print(f"[deskd] working-memory GET failed: {e}", flush=True)
            return self._json(500, {"error": "working memory unavailable"})
        if isinstance(payload, dict) and payload.get("error") == "not found":
            return self._json(404, payload)
        if isinstance(payload, dict) and payload.get("status") == "not_found":
            return self._json(404, payload)
        return self._json(200, payload)

    def _handle_memory(self) -> None:
        """Loopback-only per-bot facts. Never a cluster or UI-origin resource."""
        if self._cluster_header_present() or not self._is_loopback():
            return self._json(403, {"error": "memory is loopback-only and host-local"})
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        # v1 / memory / <bot_id> / write|retrieve
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != "memory":
            self._drain()
            return self._json(404, {"error": "not found"})
        bid, action = parts[2], parts[3]
        if action not in {"write", "retrieve"}:
            self._drain()
            return self._json(404, {"error": "not found"})
        bot = bots.get(bid)
        if bot is None:
            self._drain()
            return self._json(404, {"error": "not found"})
        if self.command == "GET":
            if action != "retrieve":
                return self._json(405, {"error": "method not allowed"})
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("query") or qs.get("q") or [""])[0]
            try:
                limit = int((qs.get("limit") or ["8"])[0])
            except (TypeError, ValueError):
                limit = 8
            rows = [r.to_dict() for r in bot.memory.retrieve(query, limit=limit)]
            return self._json(200, {"records": rows})
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            body = json.loads(raw.decode() or "{}") if raw else {}
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "invalid json"})
        if action == "write":
            try:
                fid = bot.memory.write(
                    str(body.get("text") or ""),
                    [str(t) for t in (body.get("tags") or [])],
                    media_path=body.get("media_path"),
                    media_type=body.get("media_type"),
                    workspace=bot.workspace,
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"id": fid})
        query = str(body.get("query") or "")
        try:
            limit = int(body.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        rows = [r.to_dict() for r in bot.memory.retrieve(query, limit=limit)]
        return self._json(200, {"records": rows})

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or "0")
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _voice_upstream(self, method: str, url: str, data: bytes | None,
                        content_type: str | None, timeout: float,
                        headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        return vu.fetch(method, url, data=data, content_type=content_type, timeout=timeout, headers=headers)

    def _voice_health(self) -> None:
        snap = vu.health_snapshot()
        self._json(200, {**snap, "voice": voice_enabled()})

    def _tts_request(self) -> None:
        body = self._read_json()
        raw_text = str(body.get("text") or "").strip()
        user_text = str(body.get("user_text") or "")
        emotion, params = tts_emotion_params(raw_text, str(body.get("emotion") or ""), user_text)
        text = sanitize_chatterbox_text(raw_text, user_text)
        if not text:
            return self._json(400, {"error": "text required"})
        text = apply_tts_emotion_tag(text, emotion)
        payload = {"text": text, "emotion": emotion, **params}
        code, raw = self._voice_upstream(
            "POST", f"{vu.tts_url()}/tts", json.dumps(payload).encode(),
            "application/json", vu.TTS_TIMEOUT_S,
        )
        if code != 200 or not raw:
            try:
                err = json.loads(raw or b"{}").get("error", "tts failed")
            except json.JSONDecodeError:
                err = "tts failed"
            return self._json(code if 400 <= code < 600 else 502, {"error": str(err)})
        self._send(200, raw, "audio/wav")

    def _stt_request(self) -> None:
        n = int(self.headers.get("Content-Length") or "0")
        if n <= 0:
            return self._json(400, {"error": "empty audio"})
        if n > 32 * 1024 * 1024:
            return self._json(413, {"error": "audio too large (32 MB max)"})
        audio = self.rfile.read(n)
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        extra = {}
        if (self.headers.get("X-STT-Partial") or "").strip() == "1":
            extra["X-STT-Partial"] = "1"
        code, raw = self._voice_upstream(
            "POST", f"{vu.stt_url()}/transcribe", audio, ctype, vu.STT_TIMEOUT_S, headers=extra or None
        )
        try:
            out = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(502, {"error": "stt failed"})
        if code != 200:
            return self._json(code if 400 <= code < 600 else 502, out if isinstance(out, dict) else {"error": "stt failed"})
        text = str(out.get("text") or "") if isinstance(out, dict) else ""
        print(f"[stt] {n}B -> {len(text)} chars {text[:80]!r}", flush=True)
        self._json(200, out)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Hermes-Cluster-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        role = self._authorize()
        if role == "deny":
            return self._deny(role)
        path = urlparse(self.path).path
        if path == "/v1/local-llm/status":
            return self._json(200, local_llm_status())
        if path.startswith("/v1/llm"):
            return self._proxy_local_llm()
        if path.startswith("/v1/memory"):
            return self._handle_memory()
        if self._proxy_or_local(path):
            return
        if path == "/v1/bootstrap":
            token = TOKEN_PATH.read_text(encoding="utf-8").strip()
            out = {
                "token": token,
                "port": int(LISTEN_PORT),
                "node_name": cluster.node_name if cluster is not None else socket.gethostname(),
            }
            out.update(public_listen())
            raw = json.dumps(out).encode()
            return self._send(
                200,
                raw,
                "application/json",
                extra={"Set-Cookie": f"hermes_desk_token={token}; Path=/; HttpOnly; SameSite=Strict"},
            )
        if path == "/v1/settings":
            return self._json(200, self._settings_public())
        if path == "/v1/voice/health":
            return self._voice_health()
        if self._cluster_get(path, role):
            return
        if path in ("/", "/index.html"):
            return self._static(UI_ROOT / "index.html")
        if path.startswith("/ui/"):
            return self._static(UI_ROOT / path[4:])
        if path.startswith("/vendor/"):
            return self._static(UI_ROOT / path.lstrip("/"))
        if path in ("/app.js", "/styles.css", "/hermesbot.css", "/hermesbot-ui.js", "/working-memory.js"):
            return self._static(UI_ROOT / path.lstrip("/"))
        if path == "/v1/models":
            default, catalog = load_user_models()
            default, models = host_picker_models(catalog, default)
            node = cluster.node_name if cluster is not None else socket.gethostname()
            return self._json(200, {"default": default, "node": node, "models": models})
        if path == "/v1/bots":
            if cluster is None:
                with lock:
                    slim = []
                    for b in bots.values():
                        p = b.profile()
                        p.pop("messages", None)
                        p.pop("workspace", None)
                        slim.append(p)
                    return self._json(200, {"bots": slim, "teela_brain_taken": teela_brain_slot_taken()})
            if role == "cluster":
                with lock:
                    return self._json(
                        200,
                        {"node": cluster.node_name, "bots": cluster.local_roster()},
                    )
            with lock:
                local = cluster.local_roster()
            remote, diagnostics = cluster.fetch_remote_rosters(timeout=0.35)
            return self._json(
                200,
                {
                    "bots": cluster.merge(local, remote, diagnostics),
                    "node": cluster.node_name,
                    "cluster_token_fp": cluster_token_fp(cluster.token),
                    "peers": diagnostics,
                    "teela_brain_taken": teela_brain_slot_taken(),
                },
            )
        if path.startswith("/v1/bots/") and path.endswith("/chats") and path.count("/") == 4:
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            query = (q.get("q", [""])[0] or "").strip()
            return self._json(200, bot.list_chats(query))
        if path.startswith("/v1/bots/") and path.endswith("/soul"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"body": bot.soul, "revision": 1})
        if path.startswith("/v1/bots/") and path.endswith("/export"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            fmt = (q.get("format", ["json"])[0] or "json").strip().lower()
            try:
                payload, filename, mime = conv_io.export_payload(bot, fmt)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._send(
                200,
                payload,
                mime,
                extra={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        if path.startswith("/v1/bots/") and path.endswith("/telemetry"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, bot.telemetry_snapshot())
        if path.startswith("/v1/bots/") and "/working-memory" in path:
            return self._handle_working_memory(path)
        if path.startswith("/v1/debug/context"):
            return self._handle_working_memory(path)
        if path.startswith("/v1/bots/") and path.endswith("/workspace-shares"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, bot.share_state())
        if path.startswith("/v1/bots/") and path.endswith("/shared-desks"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"desks": bot.list_shared_desks_for()})
        if path.startswith("/v1/bots/") and path.endswith("/shared-files"):
            bid = path.split("/")[3]
            viewer = bots.get(bid)
            if not viewer:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            owner_id = (q.get("owner") or [""])[0].strip()
            rel = (q.get("path") or ["."])[0]
            owner = bots.get(owner_id)
            if not owner:
                return self._json(404, {"error": "owner not found"})
            try:
                return self._json(200, {"owner": owner.id, "path": rel, "entries": viewer.list_shared_paths(owner, rel)})
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
        if path.startswith("/v1/bots/") and path.endswith("/shared-file"):
            bid = path.split("/")[3]
            viewer = bots.get(bid)
            if not viewer:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            owner_id = (q.get("owner") or [""])[0].strip()
            rel = (q.get("path") or [""])[0].strip()
            owner = bots.get(owner_id)
            if not owner or not rel:
                return self._json(400, {"error": "owner and path required"})
            try:
                return self._json(200, viewer.read_shared_file(owner, rel))
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
            except (IsADirectoryError, ValueError) as e:
                return self._json(400, {"error": str(e)})
        if path.startswith("/v1/bots/") and path.count("/") == 3:
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            prof = bot.profile()
            if cluster is not None:
                prof = cluster.annotate_local(prof)
            return self._json(200, prof)
        if path.startswith("/v1/bots/") and path.endswith("/routines"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"routines": bot.routines})
        if path == "/v1/runtime/hermes":
            return self._json(200, agent_capabilities())
        if path.startswith("/v1/bots/") and path.endswith("/dev"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, bot.detect_dev_system())
        if path.startswith("/v1/workspaces/"):
            parts = path.strip("/").split("/")
            wid = parts[2] if len(parts) > 2 else ""
            bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
            if not bot:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            file_path = q.get("file", [None])[0]
            raw = len(parts) > 3 and parts[3] == "raw"
            if file_path:
                try:
                    target = workspace_target(bot, file_path, must_exist=True)
                except (ValueError, FileNotFoundError):
                    return self._json(404, {"error": "file not found"})
                if not target.is_file():
                    return self._json(404, {"error": "file not found"})
                if raw:
                    data = target.read_bytes()
                    ext = target.suffix.lower()
                    mime = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".gif": "image/gif",
                        ".webp": "image/webp",
                        ".svg": "image/svg+xml",
                        ".mp4": "video/mp4",
                        ".webm": "video/webm",
                        ".mov": "video/quicktime",
                        ".html": "text/html; charset=utf-8",
                        ".htm": "text/html; charset=utf-8",
                        ".css": "text/css; charset=utf-8",
                        ".js": "text/javascript; charset=utf-8",
                    }.get(ext, "application/octet-stream")
                    extra = {"Content-Disposition": f'inline; filename="{target.name}"'}
                    return self._send(200, data, mime, extra=extra)
                if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    return self._json(200, {"path": file_path, "kind": "image"})
                text = target.read_text(encoding="utf-8", errors="replace")[:200_000]
                return self._json(200, {"path": file_path, "text": text, "kind": "doc"})
            return self._json(200, bot.inspect_workspace())
        if path == "/v1/embodiment/schema":
            return self._json(
                200,
                {
                    "phase": 1,
                    "joints": list(JOINT_NAMES),
                    "skills": list(SKILLS),
                    "teela_body_action": BODY_ACTION_SCHEMA,
                    "rule": "REAL→VIRTUAL mirrors observed. VIRTUAL→REAL is rejected until the action gate exists.",
                },
            )
        if path.startswith("/v1/bots/") and path.endswith("/embodiment/state"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, bot.embodiment.snapshot(bot.robot_state))
        if path.startswith("/v1/bots/") and path.endswith("/virtual-body/ws"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            key = self.headers.get("Sec-WebSocket-Key") or ""
            if not key:
                return self._json(400, {"error": "websocket upgrade required"})
            accept = virtual_body.ws_accept_key(key)
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            sock = self.connection
            virtual_body.register_ws(bid, sock)
            try:
                while True:
                    text = virtual_body.read_ws_text(self.rfile)
                    if text is None:
                        break
                    if not str(text).strip():
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        virtual_body.handle_client_message(bid, obj)
                        if bot is not None:
                            virtual_body.apply_to_robot(
                                bot.robot_state, virtual_body.latest_state(bid)
                            )
            finally:
                virtual_body.unregister_ws(bid, sock)
            return
        if path.startswith("/v1/bots/") and path.endswith("/virtual-body/state"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"ok": True, **virtual_body.latest_state(bid)})
        if path.startswith("/v1/bots/") and path.endswith("/body/state"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, teela_body_snapshot(bot))
        if path.startswith("/v1/bots/") and path.endswith("/activity"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"ok": True, **orch.snapshot(bid)})
        if path.startswith("/v1/bots/") and path.endswith("/system-check"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, teela_system_report(bot))
        if path.startswith("/v1/bots/") and path.endswith("/desktop/observe"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            q = parse_qs(urlparse(self.path).query)
            try:
                after = int((q.get("after", ["0"])[0] or "0"))
            except ValueError:
                after = 0
            try:
                wait = float((q.get("wait", ["0"])[0] or "0"))
            except ValueError:
                wait = 0.0
            return self._json(200, bot.observer_state(after=after, wait=wait))
        if path.startswith("/v1/bots/") and path.endswith("/desktop/frame"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            try:
                frame, seq = bot.observer_frame()
            except Exception as e:
                return self._json(503, {"error": str(e)})
            if not frame:
                self.send_response(204)
                self.end_headers()
                return
            return self._send(200, frame, "image/jpeg", extra={"X-Frame-Seq": str(seq), "Cache-Control": "no-store"})
        if path.endswith("/browser/frame"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            if bot.browser is None:
                bot.ensure_browser()
            br = bot.browser
            frame = br.frame_jpeg if br else b""
            seq = br.frame_seq if br else 0
            etag = f'"{seq}"'
            extra = {
                "ETag": etag,
                "X-Frame-Seq": str(seq),
                "X-View-Width": str(br.view_w if br else 1280),
                "X-View-Height": str(br.view_h if br else 800),
            }
            if self.headers.get("If-None-Match") == etag and frame:
                self.send_response(304)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not frame:
                self.send_response(204)
                self.end_headers()
                return
            return self._send(200, frame, "image/jpeg", extra=extra)
        if path.endswith("/browser/info"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            if bot.browser is None:
                bot.ensure_browser()
            return self._json(
                200,
                {
                    "url": bot.browser.url if bot.browser else "",
                    "alive": bool(bot.browser and bot.browser.alive),
                    "site": f"http://127.0.0.1:{bot.www_port}/",
                    "width": bot.browser.view_w if bot.browser else 1280,
                    "height": bot.browser.view_h if bot.browser else 800,
                    "tab": bot.browser.page_id if bot.browser else "",
                    "tabs": bot.browser.list_tabs() if bot.browser else [],
                },
            )
        if path.endswith("/browser/snapshot"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, bot.browser_snapshot())
        if "/shell/pull" in path or "/tui/pull" in path:
            bid = path.split("/")[3]
            kind = "tui" if "/tui/" in path else "shell"
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            pty_s = bot.ensure_tui() if kind == "tui" else bot.ensure_shell()
            q = parse_qs(urlparse(self.path).query)
            after = int(q.get("after", ["0"])[0] or 0)
            seq, data = pty_s.pull(after, wait=12)
            return self._json(200, {"seq": seq, "data": base64.b64encode(data).decode(), "alive": pty_s.alive})
        if path == "/v1/events":
            return self._sse()
        self._json(404, {"error": "not found"})

    def _static(self, p: Path) -> None:
        if not p.is_file():
            return self._json(404, {"error": "not found"})
        data = p.read_bytes()
        extra = None
        if p.name.endswith(".bin.gz") or p.suffix.lower() in {".bin", ".gz"}:
            extra = {"Access-Control-Allow-Origin": "*"}
        # UI files must never be cached: a stale app.js on one device keeps
        # speaking replies that a newer UI already left to the sender's page.
        if p.suffix.lower() in {".html", ".htm", ".js", ".css"}:
            extra = dict(extra or {})
            extra["Cache-Control"] = "no-store"
        if p.suffix.lower() in {".html", ".htm"}:
            if p.name == "robot-simulator.html":
                extra = {
                    "Content-Security-Policy": (
                        "default-src 'none'; "
                        "script-src 'unsafe-inline'; "
                        "style-src 'unsafe-inline'; "
                        "img-src 'self' data: blob:; "
                        "connect-src 'self'; "
                        "frame-src 'none'; "
                        "object-src 'none'; base-uri 'none'; form-action 'none'"
                    )
                }
            else:
                extra = {
                    "Content-Security-Policy": (
                        "default-src 'self'; "
                        "script-src 'self'; "
                        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                        "font-src 'self' https://fonts.gstatic.com; "
                        "img-src 'self' data: blob:; "
                        "connect-src 'self'; "
                        "frame-src 'self'; "
                        "object-src 'none'; base-uri 'none'; form-action 'self'"
                    )
                }
        self._send(200, data, MIME.get(p.suffix, "application/octet-stream"), extra)

    def _sse(self, cluster_feed: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        wake = threading.Event()
        q: list[dict[str, Any]] = []
        with lock:
            subscribers.append((wake, q, self))
        try:
            # A stalled client (phone Wi-Fi drop, screen off) must not hold a
            # write open forever: fail the write so the client can reconnect.
            self.connection.settimeout(20.0)
            self.wfile.write(b":ok\n\n")
            self.wfile.flush()
            while True:
                wake.wait(timeout=15)
                wake.clear()
                batch = []
                with lock:
                    batch.extend(q)
                    q.clear()
                if not batch:
                    self.wfile.write(b":keepalive\n\n")
                    self.wfile.flush()
                    continue
                for ev in batch:
                    if cluster_feed and ev.get("proxied"):
                        continue
                    payload = json.dumps(ev).encode()
                    self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError, TimeoutError):
            pass
        finally:
            with lock:
                try:
                    subscribers.remove((wake, q, self))
                except ValueError:
                    pass

    def do_POST(self) -> None:  # noqa: N802
        role = self._authorize()
        if role == "deny":
            return self._deny(role)
        path = urlparse(self.path).path
        if path.startswith("/v1/llm"):
            return self._proxy_local_llm()
        if path.startswith("/v1/memory"):
            return self._handle_memory()
        if self._proxy_or_local(path):
            return
        if path == "/v1/tts":
            return self._tts_request()
        if path == "/v1/stt":
            return self._stt_request()
        n = int(self.headers.get("Content-Length") or "0")
        if n > 80 * 1024 * 1024:
            return self._json(413, {"error": "file too large (80 MB max)"})
        try:
            parsed = self._read_json()
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        body: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        try:
            if path == "/v1/local-llm/control":
                try:
                    return self._json(200, local_llm_control(body))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            if path == "/v1/settings":
                if any(k in body for k in CLUSTER_KEYS) and not self._require_loopback(role):
                    return self._json(403, {"error": "cluster config is local-only"})
                cluster_patch: dict[str, Any] = {}
                if any(k in body for k in CLUSTER_KEYS):
                    try:
                        cluster_patch = self._cluster_settings_patch(body)
                    except PermissionError as e:
                        return self._json(403, {"error": str(e)})
                listen_out: dict[str, Any] | None = None
                if "listen_host" in body or "listen_port" in body or "host" in body or "port" in body:
                    host = body.get("listen_host") or body.get("host") or LISTEN_HOST
                    port = body.get("listen_port") if "listen_port" in body else body.get("port")
                    listen_out = apply_listen(str(host), port)
                if cluster_patch:
                    patch_desk_config(cluster_patch)
                    if cluster is not None:
                        cluster.reload()
                        cluster.stop_fanin()
                        cluster.start_fanin()
                    lan_out = ensure_lan_listen_for_cluster()
                    if lan_out:
                        listen_out = lan_out
                if "models" in body and isinstance(body.get("models"), list):
                    save_user_model_catalog(
                        body["models"],
                        str(body.get("default_model") or body.get("default") or ""),
                    )
                if isinstance(body.get("voice"), bool):
                    patch_desk_config({"voice": body["voice"]})
                    emit({"type": "voice", "voice": body["voice"]})
                out = self._settings_public()
                if listen_out:
                    out = {**out, **listen_out}
                return self._json(200, out)
            if self._cluster_post(path, body, role):
                return
            if path.startswith("/v1/bots/") and path.endswith("/import"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                filename = (body.get("filename") or "").strip()
                mode = (body.get("mode") or "append").strip().lower()
                if mode not in ("append", "replace"):
                    mode = "append"
                raw: bytes
                if body.get("content_b64") or body.get("b64"):
                    try:
                        raw = base64.b64decode(body.get("content_b64") or body.get("b64"))
                    except Exception:
                        return self._json(400, {"error": "invalid base64"})
                elif isinstance(body.get("text"), str) or isinstance(body.get("content"), str):
                    raw = (body.get("text") or body.get("content") or "").encode("utf-8")
                elif isinstance(parsed, list) or any(
                    k in body
                    for k in (
                        "mapping",
                        "chat_messages",
                        "conversations",
                        "messages",
                        "source",
                    )
                ):
                    raw = json.dumps(parsed).encode("utf-8")
                    filename = filename or "conversation.json"
                else:
                    return self._json(400, {"error": "filename + text/content_b64 required"})
                result = bot.import_conversation(raw, filename=filename, mode=mode)
                emit({"type": "conversation.imported", "bot_id": bid, "bot": bot.profile(), **result})
                return self._json(200, {**result, "bot": bot.profile()})
            if path == "/v1/bots":
                bot = create_bot(body)
                return self._json(200, {"bot_id": bot.id, "bot": bot.profile()})
            if path.startswith("/v1/bots/") and path.endswith("/check-confirm"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, teela_check_confirm_payload(str(body.get("text") or "")))
            if path.startswith("/v1/bots/") and path.endswith("/clarify"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                hold = getattr(bot, "_minios_hold", None)
                cid = str(body.get("id") or body.get("clarify_id") or "")
                if not isinstance(hold, dict) or (cid and hold.get("id") != cid):
                    return self._json(409, {"error": "no pending question"})
                choice = str(body.get("choice") or body.get("label") or "")
                cancel = bool(body.get("cancel"))

                def _resume() -> None:
                    try:
                        line = resume_teela_minios_after_clarify(bot, choice, cancel=cancel)
                    except MiniOSClarify:
                        bot.status = "Waiting for you…"
                        emit(
                            {
                                "type": "status",
                                "bot_id": bot.id,
                                "text": bot.status,
                                "surface": bot.surface,
                                "control": bot.control,
                            }
                        )
                        return
                    if not line:
                        line = "Okay."
                    nxt = self._finish_fast_chat(bot, line)
                    if nxt:
                        self._run_prompt(bot, nxt[0], nxt[1])

                threading.Thread(target=_resume, daemon=True).start()
                return self._json(200, {"ok": True})
            if path.startswith("/v1/bots/") and path.endswith("/chats") and path.count("/") == 4:
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, bot.new_chat())
            if path.startswith("/v1/bots/") and "/chats/" in path and path.endswith("/open"):
                parts = path.strip("/").split("/")
                if len(parts) == 6:
                    bid, cid = parts[2], parts[4]
                    bot = bots.get(bid)
                    if not bot:
                        return self._json(404, {"error": "not found"})
                    try:
                        return self._json(200, bot.open_chat(cid))
                    except ValueError as e:
                        return self._json(404, {"error": str(e)})
            if path.startswith("/v1/bots/") and path.count("/") == 3:
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, {"ok": True, "bot": bot.update_identity(body)})
            if path.startswith("/v1/bots/") and path.endswith("/clear"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, bot.clear_chat())
            if path.startswith("/v1/bots/") and path.endswith("/undo"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                result = bot.undo_last_turn()
                return self._json(200, {**result, "bot": bot.profile()})
            if path.startswith("/v1/bots/") and path.endswith("/model"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                mid = (body.get("model") or body.get("modelId") or "").strip() or bot.model
                effort = str(body.get("effort") or body.get("reasoning_effort") or "").strip()
                _mdl_tbl = (load_user_models()[1] or {}).get(mid)
                if not model_has_provider(mid, _mdl_tbl if isinstance(_mdl_tbl, dict) else {}):
                    return self._json(
                        400,
                        {
                            "error": (
                                f"Model {mid} has no usable provider on this host "
                                "(cloud model with no API key, or local endpoint not reachable). "
                                "Pick a model with a provider in Settings → Models, or add the API key."
                            )
                        },
                    )
                return self._json(200, bot.set_model(mid, effort=effort or None))
            if path.startswith("/v1/bots/") and path.endswith("/workspace-shares"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                ids = body.get("share_with")
                if not isinstance(ids, list):
                    return self._json(400, {"error": "share_with list required"})
                return self._json(200, {"ok": True, **bot.set_workspace_share_with([str(x) for x in ids])})
            if path.startswith("/v1/bots/") and path.endswith("/workspace-shares/request"):
                bid = path.split("/")[3]
                viewer = bots.get(bid)
                if not viewer:
                    return self._json(404, {"error": "not found"})
                spec = (body.get("owner") or body.get("to") or "").strip()
                owner = bots.get(spec) or next((b for b in bots.values() if b.name.lower() == spec.lower()), None)
                if not owner:
                    return self._json(404, {"error": "owner not found"})
                if getattr(owner, "remote", False):
                    return self._json(400, {"error": "can only request local desks"})
                try:
                    return self._json(200, owner.add_share_request(viewer))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            if path.startswith("/v1/bots/") and path.endswith("/dm"):
                src_id = path.split("/")[3]
                src = bots.get(src_id)
                if not src:
                    return self._json(404, {"error": "sender not found"})
                spec = (body.get("to") or "").strip()
                text = (body.get("text") or "").strip()
                if not spec or not text:
                    return self._json(400, {"error": "to and text required"})
                dest = bots.get(spec)
                if not dest:
                    dest = next((b for b in bots.values() if b.name.lower() == spec.lower()), None)
                if dest:
                    if dest.id == src.id:
                        return self._json(400, {"error": "cannot DM yourself"})
                    dest.deliver_dm(src, text)
                    src.append_msg("assistant", f"Sent to {dest.name}: {text}", via="dm", peer=dest.name)
                    emit({"type": "chat", "bot_id": src.id, "role": "assistant", "text": f"Sent to {dest.name}: {text}", "via": "dm", "peer": dest.name})
                    node = cluster.node_name if cluster is not None else socket.gethostname()
                    return self._json(200, {"ok": True, "to": dest.id, "to_name": dest.name, "to_node": node})
                if cluster is None:
                    return self._json(404, {"error": f"no bot named {spec}"})
                try:
                    result = cluster.forward_dm(
                        {
                            "from_id": src.id,
                            "from_name": src.name,
                            "from_node": cluster.node_name,
                            "to": spec,
                            "text": text,
                        }
                    )
                except KeyError:
                    return self._json(404, {"error": f"no bot named {spec}"})
                except PermissionError:
                    return self._json(502, {"error": "cluster auth failed"})
                except ConnectionError as e:
                    return self._json(503, {"error": str(e)})
                except Exception as e:
                    return self._json(502, {"error": str(e)})
                to_name = result.get("to_name") or spec
                src.append_msg("assistant", f"Sent to {to_name}: {text}", via="dm", peer=to_name)
                emit({"type": "chat", "bot_id": src.id, "role": "assistant", "text": f"Sent to {to_name}: {text}", "via": "dm", "peer": to_name})
                return self._json(
                    200,
                    {
                        "ok": True,
                        "to": result.get("to") or spec,
                        "to_name": to_name,
                        "to_node": result.get("node"),
                    },
                )
            if path.startswith("/v1/agent/") and path.endswith("/prompt"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                if bot.control != "agent_controlled":
                    return self._json(409, {"error": "user_controlled"})
                page_id = str(body.get("page_id") or "")[:48]
                if page_id:
                    bot._voice_page = page_id
                scope = str(body.get("confirmed_scope") or "").strip().lower()
                bot._check_scope = scope if scope in {"minios", "host", "both"} else ""
                bot._voice_chat = bool(body.get("voice_chat"))
                text = visible_user_text((body.get("text") or "").strip())
                if not text:
                    blocks = body.get("blocks") or []
                    text = visible_user_text(
                        "".join(b.get("text") or "" for b in blocks if isinstance(b, dict)).strip()
                    )
                images_in = list(body.get("images") or []) + list(body.get("attachments") or [])
                saved: list[dict[str, Any]] = []
                for img in images_in:
                    if not isinstance(img, dict) or not img.get("data"):
                        continue
                    saved.append(
                        bot.save_paste_image(
                            img.get("mime") or "image/png",
                            img["data"],
                            img.get("name"),
                        )
                    )
                    saved.extend(bot.extract_video_stills(saved[-1]))
                if not text and not saved:
                    return self._json(400, {"error": "empty prompt"})
                bot.append_msg("user", text, images=saved)
                emit(
                    {
                        "type": "chat",
                        "bot_id": bid,
                        "role": "user",
                        "text": text,
                        "images": [{"path": s.get("path"), "mime": s.get("mime")} for s in saved],
                    }
                )
                bot.status = "Working…"
                emit({"type": "status", "bot_id": bid, "text": "Working…", "surface": bot.surface, "control": bot.control})
                prompt_text = text or ("Look at the attached media." if saved else "")
                if not bot.take_prompt_turn():
                    bot.queue_prompt(prompt_text, saved)
                    return self._json(200, {"turn_id": uuid.uuid4().hex, "queued": True, "images": [s["path"] for s in saved]})
                threading.Thread(target=self._run_prompt, args=(bot, prompt_text, saved), daemon=True).start()
                return self._json(200, {"turn_id": uuid.uuid4().hex, "images": [s["path"] for s in saved]})
            if path.startswith("/v1/bots/") and path.endswith("/routines") and path.count("/") == 4:
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                item = bot.add_routine(body)
                emit({"type": "routines", "bot_id": bid, "routines": bot.routines})
                return self._json(200, {"routine": item})
            if path.startswith("/v1/bots/") and "/routines/" in path and path.count("/") == 5:
                _, _, _, bid, _, rid = path.strip("/").split("/")
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                item = bot.patch_routine(rid, body)
                emit({"type": "routines", "bot_id": bid, "routines": bot.routines})
                return self._json(200, {"routine": item})
            if path.startswith("/v1/agent/") and path.endswith("/cancel"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.acp.cancel()
                bot.cancel_prompt_queue()
                bot.close_chunk()
                bot.status = "Ready"
                emit(
                    {
                        "type": "session.update",
                        "bot_id": bid,
                        "update": {"sessionUpdate": "turn_completed", "stop_reason": "cancelled"},
                    }
                )
                emit({"type": "status", "bot_id": bid, "text": "Ready", "surface": bot.surface, "control": bot.control})
                return self._json(200, {"ok": True, "bot": bot.profile()})
            if "/browser/tab/" in path:
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.ensure_browser()
                if not bot.browser:
                    return self._json(503, {"error": "browser not ready"})
                action = path.rstrip("/").split("/")[-1]
                if action == "new":
                    url = body.get("url") or "about:blank"
                    if url and not url.startswith(("http://", "https://", "about:")):
                        url = bot.resolve_browser_url(url)
                    out = bot.browser.new_tab(url)
                    bot.surface = "browser"
                    emit({"type": "status", "bot_id": bid, "text": f"Browser tab: {out.get('url')}", "surface": "browser", "control": bot.control})
                    return self._json(200, out)
                if action == "focus":
                    tid = (body.get("id") or "").strip()
                    if not tid:
                        return self._json(400, {"error": "id required"})
                    out = bot.browser.activate_tab(tid)
                    bot.surface = "browser"
                    return self._json(200, out)
                if action == "close":
                    tid = (body.get("id") or "").strip() or None
                    out = bot.browser.close_tab(tid)
                    return self._json(200, out)
                return self._json(404, {"error": "unknown tab action"})
            if path.startswith("/v1/bots/") and path.endswith("/desktop/view"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                wins = body.get("windows") if isinstance(body.get("windows"), list) else []
                try:
                    width = int(body.get("width") or 0)
                    height = int(body.get("height") or 0)
                except (TypeError, ValueError):
                    width, height = 0, 0
                dock = str(body.get("dock") or "").strip().lower()
                if dock not in {"left", "right", "top", "bottom"}:
                    dock = "left"
                bot.desktop_view = {
                    "width": width,
                    "height": height,
                    "surface": str(body.get("surface") or bot.surface),
                    "dock": dock,
                    "wallpaper": str(body.get("wallpaper") or ""),
                    "wallpaperStyle": str(body.get("wallpaperStyle") or ""),
                    "windows": [
                        {
                            "app": str(w.get("app") or ""),
                            "open": bool(w.get("open")),
                            "minimized": bool(w.get("minimized")),
                            "maximized": bool(w.get("maximized")),
                            "focused": bool(w.get("focused")),
                        }
                        for w in wins
                        if isinstance(w, dict) and w.get("app")
                    ],
                }
                bot.desktop_view_seq = int(bot.desktop_view_seq or 0) + 1
                bot.desktop_view_event.set()
                return self._json(200, {"ok": True, "seq": bot.desktop_view_seq})
            if path.startswith("/v1/bots/") and path.endswith("/embodiment/observe"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                live = body.get("joints") if isinstance(body.get("joints"), dict) else body.get("live")
                if not isinstance(live, dict) or not live:
                    return self._json(400, {"error": "joints or live required"})
                robot_sim._ingest_live(bot.robot_state, live)
                bot.embodiment.observe(
                    live,
                    pose=str(body.get("pose") or bot.robot_state.get("pose") or "home"),
                    motion=str(body.get("motion") or bot.robot_state.get("motion") or "idle"),
                    seq=body.get("seq"),
                    contacts=body.get("contacts") if isinstance(body.get("contacts"), dict) else None,
                )
                bot.save_robot_state()
                return self._json(200, {"ok": True, **bot.embodiment.snapshot(bot.robot_state)})
            if path.startswith("/v1/bots/") and path.endswith("/virtual-body/action"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                if bot_kind_is_agent(bot):
                    return self._json(403, {"error": "Hermes Agent bots do not have the Robot Simulator"})
                skill = str(body.get("skill") or "").strip()
                params = {
                    k: v
                    for k, v in body.items()
                    if k not in {"skill", "parameters", "action"}
                }
                extra = body.get("parameters")
                if isinstance(extra, dict):
                    params.update(extra)
                timeout = float(body.get("timeout") or 0)
                if timeout <= 0:
                    timeout = virtual_body.settle_timeout(skill)
                result = virtual_body.submit_action(bid, skill, params, timeout=timeout)
                virtual_body.apply_to_robot(
                    bot.robot_state,
                    result.get("body_state")
                    if isinstance(result.get("body_state"), dict)
                    else virtual_body.latest_state(bid),
                )
                orch.note(
                    bid,
                    "body",
                    str(params.get("gesture") or skill),
                    status=str(result.get("status") or "executing"),
                    id=result.get("action_id"),
                )
                return self._json(200, result)
            if path.startswith("/v1/bots/") and path.endswith("/virtual-body/result"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                virtual_body.handle_client_message(bid, body)
                virtual_body.apply_to_robot(bot.robot_state, virtual_body.latest_state(bid))
                aid = str(body.get("action_id") or "")
                if aid:
                    orch.complete(bid, aid, str(body.get("status") or "completed"))
                try:
                    bot.save_robot_state()
                except Exception:
                    pass
                return self._json(200, {"ok": True})
            if path.startswith("/v1/bots/") and path.endswith("/desktop/action"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                action = str(body.get("action") or "").strip()
                if action == "screenshot":
                    try:
                        saved = bot.screenshot_to_chat(str(body.get("caption") or ""))
                    except Exception as e:
                        return self._json(503, {"error": str(e)})
                    return self._json(200, {"ok": True, "action": "screenshot", **saved})
                allowed = {"open_app","focus_window","minimize_window","maximize_window","close_window","move_cursor","click","double_click","click_object","scroll","type_text","key","open_file","open_preview","browser_navigate","browser_back","browser_forward","run_tests","run_app","stop_app","robot"}
                if action not in allowed:
                    return self._json(400, {"error": "unknown desktop action"})
                aliases = {"code":"editor","test":"dev","workspace":"files","text":"notepad","texteditor":"notepad","notes":"notepad"}
                ev = {"type":"desktop.action","bot_id":bid,"action":action}
                result: dict[str, Any] = {"ok":True,"action":action}
                if action in {"move_cursor","click","double_click"}:
                    try:
                        x=max(0.0,min(1000.0,float(body.get("x",bot.desktop_cursor["x"])))); y=max(0.0,min(1000.0,float(body.get("y",bot.desktop_cursor["y"]))))
                    except (TypeError,ValueError):
                        return self._json(400,{"error":"x and y must be numeric coordinates from 0 to 1000"})
                    bot.desktop_cursor={"x":x,"y":y}; ev.update({"x":x,"y":y})  # compatibility: bot.desktop_cursor = {"x": x, "y": y}
                elif action == "click_object":
                    oid=str(body.get("object_id") or body.get("id") or "").strip()
                    if not bot.desktop_objects: bot.observer_state()
                    obj=next((o for o in bot.desktop_objects if str(o.get("id"))==oid or str(o.get("ordinal"))==oid),None)
                    if not obj: return self._json(404,{"error":f"desktop object {oid!r} not found; call desktop_state again"})
                    b=obj.get("bounds") or []
                    if len(b)!=4: return self._json(409,{"error":"desktop object has no clickable bounds"})
                    x=(float(b[0])+float(b[2]))/2; y=(float(b[1])+float(b[3]))/2; bot.desktop_cursor={"x":x,"y":y}
                    ev={"type":"desktop.action","bot_id":bid,"action":"click","x":x,"y":y,"object_id":oid}; result.update({"object":obj,"cursor":dict(bot.desktop_cursor)})
                elif action == "open_app":
                    raw=str(body.get("app") or body.get("app_id") or "").replace("app_","").lower(); app=aliases.get(raw,raw)
                    if app not in {"hermes","browser","files","notepad","editor","terminal","preview","dev","settings"}: return self._json(400,{"error":"unknown MiniOS app"})
                    ev["app"]=app; bot.surface={"hermes":"tui","browser":"browser","files":"desktop","editor":"editor","terminal":"shell","preview":"preview","dev":"dev","notepad":"notepad"}.get(app,bot.surface); result["app"]=app
                elif action in {"focus_window","minimize_window","maximize_window","close_window"}:
                    raw=str(body.get("window_id") or body.get("window") or "").removeprefix("win_").lower(); app=aliases.get(raw,raw)
                    if app not in {"hermes","browser","files","notepad","editor","terminal","preview","dev","settings"}: return self._json(400,{"error":"unknown window_id"})
                    ev.update({"window_id":f"win_{app}","app":app}); result["window_id"]=f"win_{app}"
                elif action in {"open_file","open_preview"}:
                    rel=str(body.get("path") or "").strip()
                    try: target=workspace_target(bot,rel,must_exist=True)
                    except (ValueError,FileNotFoundError): return self._json(404,{"error":"file not found"})
                    ev["path"]=target.relative_to(bot.workspace.resolve()).as_posix()
                    kind=workspace_media_kind(ev["path"])
                    result["path"]=ev["path"]
                    result["kind"]=kind or "file"
                    ev["kind"]=result["kind"]
                    if kind=="image":
                        bot.surface="preview"
                    elif kind=="video":
                        bot.surface="browser"
                        try:
                            result.update(bot.show_in_browser(ev["path"]))
                        except Exception as e:
                            result["browser_error"]=str(e)
                        stills=bot.extract_video_stills({"path":ev["path"],"mime":"video/mp4"})
                        result["stills"]=[s.get("path") for s in stills if s.get("path")]
                    elif kind=="notepad" and action=="open_file":
                        bot.surface="notepad"
                        ev["app"]="notepad"
                    else:
                        bot.surface="editor" if action=="open_file" else "preview"
                elif action == "browser_navigate":
                    result.update(bot.show_in_browser(str(body.get("url") or "about:blank"))); ev={"type":"desktop.action","bot_id":bid,"action":"open_app","app":"browser"}
                elif action in {"browser_back","browser_forward"}:
                    bot.ensure_browser()
                    if bot.browser:
                        bot.browser.evaluate("history.back()" if action=="browser_back" else "history.forward()"); time.sleep(0.15); result.update(bot.browser_snapshot())
                    bot.surface="browser"; ev={"type":"desktop.action","bot_id":bid,"action":"open_app","app":"browser"}
                elif action == "run_tests":
                    cmd=str(bot.detect_dev_system().get("commands",{}).get("test") or "")
                    if not cmd: return self._json(409,{"error":"no test command detected"})
                    result.update(bot.run_dev_command(cmd,"",int(body.get("timeout") or 120))); bot.surface="dev"; ev={"type":"desktop.action","bot_id":bid,"action":"open_app","app":"dev"}
                elif action == "run_app":
                    cmd=str(body.get("command") or bot.detect_dev_system().get("commands",{}).get("run") or "")
                    if not cmd: return self._json(409,{"error":"no run command detected"})
                    result.update(bot.start_dev_process(cmd,str(body.get("cwd") or ""))); bot.surface="dev"; ev={"type":"desktop.action","bot_id":bid,"action":"open_app","app":"dev"}
                elif action == "stop_app":
                    result.update(bot.stop_dev_process()); bot.surface="dev"; ev={"type":"desktop.action","bot_id":bid,"action":"open_app","app":"dev"}
                elif action == "robot":
                    if bot_kind_is_agent(bot):
                        return self._json(403, {"error": "Hermes Agent bots do not have the Robot Simulator"})
                    body = fill_robot_action_from_intent(bot, body)
                    cmd = str(body.get("cmd") or "")
                    if getattr(bot, "_motor_hold", False) and cmd not in {"status", "state", "live", "telemetry", ""}:
                        result = robot_sim.public_status(bot.robot_state)
                        result["ok"] = True
                        result["held"] = True
                        result["app"] = "preview"
                    else:
                        result, robot_ev = bot.apply_robot(body)
                        ev.update({k: v for k, v in robot_ev.items() if k != "type"})
                        if cmd not in {"status", "state", "live", "telemetry", ""}:
                            ev["action"] = "robot"
                elif action == "scroll":
                    try: ev.update({"dx":float(body.get("dx") or 0),"dy":float(body.get("dy") or 0)})
                    except (TypeError,ValueError): return self._json(400,{"error":"dx/dy must be numeric"})
                elif action == "type_text":
                    text=str(body.get("text") or "")
                    app=str(body.get("app") or "").replace("app_","").lower()
                    ev["text"]=text
                    if app=="notepad" or (not app and bot.surface=="notepad"):
                        bot.surface="notepad"
                        ev["app"]="notepad"
                        result["typed"]="notepad"
                    elif app=="browser" or bot.surface=="browser":
                        bot.ensure_browser()
                        if bot.browser:
                            bot.browser.type_text(text, submit=bool(body.get("submit")))
                        bot.surface="browser"
                        ev["app"]="browser"
                        result["typed"]="browser"
                    else:
                        result["typed"]="minios"
                else:
                    for k in ("text","key"):
                        if k in body: ev[k]=body[k]
                bot.desktop_last_action={"action":action,"at":time.time(),**{k:v for k,v in result.items() if k in {"app","window_id","path","object"}}}
                emit(ev); result.update({"surface":bot.surface,"cursor":dict(bot.desktop_cursor),"next":"Call desktop_watch to verify the visual change."}); return self._json(200,result)
            if path.startswith("/v1/bots/") and path.endswith("/dev/run"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                command = str(body.get("command") or "")
                preset = str(body.get("preset") or "")
                if preset and not command:
                    command = str(bot.detect_dev_system().get("commands", {}).get(preset) or "")
                try:
                    return self._json(200, bot.run_dev_command(command, str(body.get("cwd") or ""), int(body.get("timeout") or 120)))
                except subprocess.TimeoutExpired:
                    return self._json(408, {"error": "command timed out", "command": command})
                except (ValueError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e)})
            if path.startswith("/v1/bots/") and path.endswith("/dev/start"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                command = str(body.get("command") or "")
                preset = str(body.get("preset") or "")
                if preset and not command:
                    command = str(bot.detect_dev_system().get("commands", {}).get(preset) or "")
                try:
                    return self._json(200, bot.start_dev_process(command, str(body.get("cwd") or "")))
                except (ValueError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e)})
            if path.startswith("/v1/bots/") and path.endswith("/dev/stop"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, bot.stop_dev_process())
            if path.startswith("/v1/workspaces/") and path.endswith("/trash/empty"):
                parts = path.strip("/").split("/")
                wid = parts[2] if len(parts) > 2 else ""
                bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
                if not bot:
                    return self._json(404, {"error": "not found"})
                out = bot.empty_trash()
                emit({"type": "workspace", "bot_id": bot.id})
                return self._json(200, out)
            if path.startswith("/v1/workspaces/") and path.endswith("/delete"):
                parts = path.strip("/").split("/")
                wid = parts[2] if len(parts) > 2 else ""
                bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
                if not bot:
                    return self._json(404, {"error": "not found"})
                rel = str(body.get("path") or "").strip()
                if not rel:
                    return self._json(400, {"error": "path required"})
                try:
                    out = bot.delete_item(rel)
                except (ValueError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e)})
                emit({"type": "workspace", "bot_id": bot.id})
                return self._json(200, out)
            if path.startswith("/v1/workspaces/") and path.endswith("/trash"):
                parts = path.strip("/").split("/")
                wid = parts[2] if len(parts) > 2 else ""
                bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
                if not bot:
                    return self._json(404, {"error": "not found"})
                rel = str(body.get("path") or "").strip()
                if not rel:
                    return self._json(400, {"error": "path required"})
                try:
                    out = bot.trash_item(rel)
                except (ValueError, FileNotFoundError) as e:
                    return self._json(400, {"error": str(e)})
                emit({"type": "workspace", "bot_id": bot.id})
                return self._json(200, out)
            if path.startswith("/v1/workspaces/") and path.endswith("/file"):
                parts = path.strip("/").split("/")
                wid = parts[2] if len(parts) > 2 else ""
                bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
                if not bot:
                    return self._json(404, {"error": "not found"})
                rel = str(body.get("path") or "").strip()
                if not rel:
                    return self._json(400, {"error": "path required"})
                try:
                    target = workspace_target(bot, rel)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                target.parent.mkdir(parents=True, exist_ok=True)
                text = body.get("text")
                if not isinstance(text, str):
                    return self._json(400, {"error": "text required"})
                if len(text.encode("utf-8")) > 2_000_000:
                    return self._json(413, {"error": "editor file too large (2 MB max)"})
                target.write_text(text, encoding="utf-8")
                emit({"type": "workspace", "bot_id": bot.id})
                return self._json(200, {"ok": True, "path": target.relative_to(bot.workspace.resolve()).as_posix(), "size": target.stat().st_size})
            if path.endswith("/browser/navigate"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                url = body.get("url") or body.get("query") or "about:blank"
                return self._json(200, bot.show_in_browser(url))
            if path.endswith("/browser/search"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                q = (body.get("query") or body.get("q") or "").strip()
                if not q:
                    return self._json(400, {"error": "query required"})
                return self._json(200, bot.show_in_browser(search_url(q)))
            if path.endswith("/browser/open"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                rel = (body.get("path") or body.get("file") or "").strip()
                if not rel:
                    return self._json(400, {"error": "path required"})
                return self._json(200, bot.show_in_browser(rel))
            if path.endswith("/browser/click"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.ensure_browser()
                sel = (body.get("selector") or body.get("css") or "").strip()
                if not sel or not bot.browser:
                    return self._json(400, {"error": "selector required"})
                out = bot.browser.click_selector(sel)
                bot.surface = "browser"
                return self._json(200, out)
            if path.endswith("/browser/type"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.ensure_browser()
                text = body.get("text") or ""
                if bot.browser:
                    bot.browser.type_text(text, submit=bool(body.get("submit")))
                bot.surface = "browser"
                return self._json(200, {"ok": True})
            if path.endswith("/browser/input"):
                bid = path.split("/")[3]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.ensure_browser()
                br = bot.browser
                if not br:
                    return self._json(503, {"error": "browser not ready"})
                events = body.get("events")
                if not isinstance(events, list):
                    events = [body]
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    kind = ev.get("type") or ev.get("kind") or ""
                    mods = int(ev.get("modifiers") or 0)
                    if kind in ("mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"):
                        br.dispatch_mouse(
                            kind,
                            ev.get("x") or 0,
                            ev.get("y") or 0,
                            button=ev.get("button") or "left",
                            click_count=int(ev.get("clickCount") or 1),
                            delta_x=float(ev.get("deltaX") or 0),
                            delta_y=float(ev.get("deltaY") or 0),
                            modifiers=mods,
                        )
                    elif kind in ("keyDown", "keyUp", "rawKeyDown", "char"):
                        br.dispatch_key(
                            kind,
                            key=ev.get("key") or "",
                            code=ev.get("code") or "",
                            text=ev.get("text") or "",
                            vk=int(ev.get("vk") or ev.get("keyCode") or 0),
                            modifiers=mods,
                        )
                    elif kind == "insertText":
                        br.insert_text(ev.get("text") or "")
                bot.surface = "browser"
                return self._json(200, {"ok": True, "n": len(events)})
            if "/shell/input" in path or "/tui/input" in path:
                bid = path.split("/")[3]
                kind = "tui" if "/tui/" in path else "shell"
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                pty_s = bot.ensure_tui() if kind == "tui" else bot.ensure_shell()
                raw = body.get("data") or ""
                try:
                    pty_s.write(base64.b64decode(raw) if body.get("b64") else raw.encode())
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})
            if "/shell/resize" in path or "/tui/resize" in path:
                bid = path.split("/")[3]
                kind = "tui" if "/tui/" in path else "shell"
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                pty_s = bot.ensure_tui() if kind == "tui" else bot.ensure_shell()
                pty_s.resize(int(body.get("cols") or 80), int(body.get("rows") or 24))
                return self._json(200, {"ok": True})
            if path.startswith("/v1/control/"):
                wid = path.split("/")[3]
                bot = next((b for b in bots.values() if b.workspace_id == wid or b.id == wid), None)
                if not bot:
                    return self._json(404, {"error": "not found"})
                state = body.get("state") or ""
                if state == "user_controlled":
                    bot.control = "user_controlled"
                    bot.status = "Waiting for you…"
                    bot.surface = "browser"
                    try:
                        bot.ensure_browser()
                    except Exception:
                        pass
                    try:
                        bot.acp.cancel()
                    except Exception:
                        pass
                    emit({"type": "handoff.requested", "workspace_id": bot.workspace_id, "reason": "user"})
                elif state == "agent_controlled":
                    bot.control = "agent_controlled"
                    bot.status = "Ready"
                emit({"type": "status", "bot_id": bot.id, "text": bot.status, "surface": bot.surface, "control": bot.control})
                return self._json(200, {"control": bot.control})
            if "/pin" in path or "/hide" in path:
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})
        except FileNotFoundError as e:
            self._json(400, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": str(e)})

    def _stream_fast_chat(self, bot: Bot, text: str, said: str) -> tuple[str | None, bool]:
        """Local Qwen chat with live tok/s + context meters."""
        started = time.time() * 1000.0
        streamed = {"on": False}

        def on_delta(full: str, piece: str = "") -> None:
            streamed["on"] = True
            now = time.time() * 1000.0
            bot.note_generation_chunk(
                full,
                {
                    "streamStartMs": started,
                    "turnStartMs": started,
                    "agentTimestampMs": now,
                    "chunkId": "local",
                },
            )
            bot.append_msg("assistant", piece or full, chunk=True)
            emit(
                {
                    "type": "session.update",
                    "bot_id": bot.id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": piece or full},
                    },
                }
            )

        line = fast_local_chat(bot, text, on_delta=on_delta)
        if line is None:
            return None, False
        line = ground_chat_speech(line, said or text, bot.robot_state, acted=False)
        if streamed["on"] and bot.messages and bot.messages[-1].get("open") and line:
            bot.messages[-1]["text"] = line
        return line, streamed["on"]

    def _finish_fast_chat(
        self, bot: Bot, text: str, *, streamed: bool = False
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Reply without ACP. Returns the next queued turn, or None if idle."""
        user_line = ""
        for m in reversed(list(getattr(bot, "messages", None) or [])):
            if m.get("role") == "user":
                user_line = visible_user_text(str(m.get("text") or ""))
                break
        text = filter_unwarranted_laughs(strip_model_think_tags(text or ""), user_line)
        if streamed:
            bot.close_chunk()
            emit(
                {
                    "type": "session.update",
                    "bot_id": bot.id,
                    "update": {"sessionUpdate": "turn_completed"},
                }
            )
        else:
            bot.append_msg("assistant", text)
            emit({"type": "chat", "bot_id": bot.id, "role": "assistant", "text": text})
        try:
            started = getattr(bot, "_llama_started_ms", None)
            usage = getattr(bot, "_llama_usage", None)
            if started is not None:
                bot.record_local_generation(text or "", usage, started_ms=float(started))
            else:
                bot.record_local_generation(text or "", started_ms=time.time() * 1000.0)
        except Exception:
            pass
        bot._llama_started_ms = None
        bot._llama_usage = None
        bot._motor_hold = False
        nxt = bot.finish_prompt_turn()
        if nxt:
            bot.status = "Working…"
            emit({"type": "status", "bot_id": bot.id, "text": "Working…", "surface": bot.surface, "control": bot.control})
            return nxt
        release_voice_page(bot)
        bot.status = "Ready"
        emit({"type": "status", "bot_id": bot.id, "text": bot.status, "surface": bot.surface, "control": bot.control})
        emit({"type": "workspace", "bot_id": bot.id})
        return None

    def _run_prompt(self, bot: Bot, text: str, images: list[dict[str, Any]] | None = None) -> None:
        try:
            self._run_prompt_loop(bot, text, images)
        except MiniOSClarify:
            bot.status = "Waiting for you…"
            emit(
                {
                    "type": "status",
                    "bot_id": bot.id,
                    "text": bot.status,
                    "surface": bot.surface,
                    "control": bot.control,
                }
            )
        except Exception as e:
            try:
                bot.close_chunk()
            except Exception:
                pass
            err = str(e)
            try:
                bot.append_msg("system", err)
            except Exception:
                pass
            emit({"type": "chat", "bot_id": bot.id, "role": "system", "text": err})
            bot.status = f"Error: {e}"
            emit({"type": "status", "bot_id": bot.id, "text": bot.status, "surface": bot.surface, "control": bot.control})
            emit({"type": "workspace", "bot_id": bot.id})
        finally:
            with bot._prompt_lock:
                if getattr(bot, "_minios_hold", None):
                    bot._prompt_busy = True
                elif not bot._prompt_queue:
                    bot._prompt_busy = False

    def _run_prompt_loop(self, bot: Bot, text: str, images: list[dict[str, Any]] | None = None) -> None:
        while True:
            turn_text = text
            bot._motor_hold = False
            bot._motor_user = turn_text
            said = visible_user_text(user_intent_text(turn_text))
            prior = prior_user_intent(bot)
            learn_from_user(bot, said, prior)
            if bot_kind_is_teela(bot):
                try:
                    line = run_teela_executive_turn(bot, said or turn_text, images=images)
                except MiniOSClarify:
                    bot.status = "Waiting for you…"
                    emit(
                        {
                            "type": "status",
                            "bot_id": bot.id,
                            "text": bot.status,
                            "surface": bot.surface,
                            "control": bot.control,
                        }
                    )
                    return
                if not line:
                    line = "One second — my local brain stalled. Say that again?"
                nxt = self._finish_fast_chat(bot, line)
                if nxt:
                    text, images = nxt
                    continue
                return
            turn_text = with_voice_note(turn_text, tui=True, bot=bot)
            wait_for_local_model(str(getattr(bot, "model", "") or ""), bot)
            if str(getattr(bot, "status", "") or "").startswith("Starting "):
                bot.status = "Working…"
                emit(
                    {
                        "type": "status",
                        "bot_id": bot.id,
                        "text": bot.status,
                        "surface": bot.surface,
                        "control": bot.control,
                    }
                )
            err_status = ""
            try:
                bot.acp.prompt(turn_text, images=images)
                bot.close_chunk()
            except Exception as e:
                err = str(e)
                cancelled = "cancel" in err.lower()
                bot.close_chunk()
                if cancelled:
                    nxt = bot.finish_prompt_turn()
                    bot._motor_hold = False
                    if nxt:
                        text, images = nxt
                        bot.status = "Working…"
                        emit({"type": "status", "bot_id": bot.id, "text": "Working…", "surface": bot.surface, "control": bot.control})
                        continue
                    bot.status = "Ready"
                    emit({"type": "status", "bot_id": bot.id, "text": bot.status, "surface": bot.surface, "control": bot.control})
                    emit({"type": "workspace", "bot_id": bot.id})
                    return
                transient = "agent exited" in err.lower() or "stdin closed" in err.lower()
                if transient:
                    print(f"[deskd] ACP {err}; retrying prompt", flush=True)
                    try:
                        bot.acp.ensure()
                        bot.acp.prompt(turn_text, images=images)
                        bot.close_chunk()
                    except Exception as e2:
                        err = str(e2)
                        err_status = f"Error: {e2}"
                        bot.append_msg("system", err)
                        emit({"type": "chat", "bot_id": bot.id, "role": "system", "text": err})
                else:
                    err_status = f"Error: {e}"
                    bot.append_msg("system", err)
                    emit({"type": "chat", "bot_id": bot.id, "role": "system", "text": err})
            nxt = bot.finish_prompt_turn()
            bot._motor_hold = False
            stop_local_prefill_progress(bot)
            if nxt:
                text, images = nxt
                bot.status = "Working…"
                emit({"type": "status", "bot_id": bot.id, "text": "Working…", "surface": bot.surface, "control": bot.control})
                continue
            release_voice_page(bot)
            bot.status = err_status or "Ready"
            emit({"type": "status", "bot_id": bot.id, "text": bot.status, "surface": bot.surface, "control": bot.control})
            emit({"type": "workspace", "bot_id": bot.id})
            return

    def do_PUT(self) -> None:  # noqa: N802
        role = self._authorize()
        if role == "deny":
            return self._deny(role)
        path = urlparse(self.path).path
        if path.startswith("/v1/llm"):
            return self._proxy_local_llm()
        if self._proxy_or_local(path):
            return
        if path.startswith("/v1/bots/") and path.endswith("/soul"):
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            body = self._read_json()
            soul = body.get("body") or body.get("soul") or ""
            return self._json(200, {"ok": True, "revision": 1, "bot": bot.save_soul(soul) or bot.profile()})
        if path.startswith("/v1/bots/") and path.count("/") == 3:
            bid = path.split("/")[3]
            bot = bots.get(bid)
            if not bot:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"ok": True, "bot": bot.update_identity(self._read_json())})
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        role = self._authorize()
        if role == "deny":
            return self._deny(role)
        path = urlparse(self.path).path
        if path.startswith("/v1/llm"):
            return self._proxy_local_llm()
        if self._proxy_or_local(path):
            return
        if path.startswith("/v1/bots/") and "/chats/" in path:
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[3] == "chats":
                bid, cid = parts[2], parts[4]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                try:
                    return self._json(200, bot.delete_chat(cid))
                except ValueError as e:
                    return self._json(404, {"error": str(e)})
        if path.startswith("/v1/bots/") and "/routines/" in path:
            parts = path.strip("/").split("/")
            if len(parts) == 5:
                bid, rid = parts[2], parts[4]
                bot = bots.get(bid)
                if not bot:
                    return self._json(404, {"error": "not found"})
                bot.delete_routine(rid)
                emit({"type": "routines", "bot_id": bid, "routines": bot.routines})
                return self._json(200, {"ok": True})
        if path.startswith("/v1/bots/") and path.count("/") == 3:
            bid = path.split("/")[3]
            bot = bots.pop(bid, None)
            if not bot:
                return self._json(404, {"error": "not found"})
            bot.destroy()
            emit({"type": "bot.deleted", "bot_id": bid})
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})


def _lookup_bot(spec: str) -> Bot | None:
    spec = (spec or "").strip()
    if not spec:
        return None
    bot = bots.get(spec)
    if bot:
        return bot
    return next((b for b in bots.values() if b.name.lower() == spec.lower()), None)


def _on_cluster_dm(ev: dict[str, Any]) -> None:
    to = str(ev.get("to") or "").strip()
    text = str(ev.get("text") or "").strip()
    dest = _lookup_bot(to)
    if not dest or not text:
        return
    dest.deliver_remote_dm(
        str(ev.get("from_id") or ""),
        str(ev.get("from_name") or "peer"),
        str(ev.get("from_node") or ""),
        text,
    )


def ensure_https_cert() -> Path | None:
    """Self-signed cert so phones/other PCs get a secure context (mic needs it)."""
    cert_dir = USER_AGENT_HOME / "certs"
    cert = cert_dir / "desk-https.pem"
    key = cert_dir / "desk-https.key"
    if not (cert.is_file() and key.is_file()):
        cert_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert),
                    "-days", "825", "-subj", f"/CN={LISTEN_HOST or 'hermes-desk'}",
                    "-addext", f"subjectAltName=IP:{LISTEN_HOST or '127.0.0.1'},IP:127.0.0.1,DNS:localhost",
                ],
                check=True, capture_output=True, timeout=60,
            )
            os.chmod(key, 0o600)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"[deskd] HTTPS cert generation failed: {e}", flush=True)
            return None
    return cert


def main() -> None:
    global LISTEN_HOST, LISTEN_PORT, cluster
    claim_deskd_pidfile()
    atexit.register(release_deskd_pidfile)
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    if not TOKEN_PATH.is_file():
        TOKEN_PATH.write_text(uuid.uuid4().hex, encoding="utf-8")
        os.chmod(TOKEN_PATH, 0o600)
    HERMES_DESKS.mkdir(parents=True, exist_ok=True)
    (USER_AGENT_HOME / "bots").mkdir(parents=True, exist_ok=True)
    cfg = load_desk_config()
    LISTEN_HOST = cfg["listen_host"]
    LISTEN_PORT = int(cfg["listen_port"])
    chosen = desired_listen_host(LISTEN_HOST)
    if chosen != LISTEN_HOST:
        print(f"[deskd] LAN cluster peers set; listen_host {LISTEN_HOST} → {chosen} (bind 0.0.0.0)", flush=True)
        LISTEN_HOST = chosen
        patch_desk_config({"listen_host": LISTEN_HOST, "listen_port": LISTEN_PORT})
    if not read_desk_file().get("node_name"):
        try:
            patch_desk_config({"node_name": validate_node_name(str(cfg.get("node_name") or socket.gethostname()))})
        except ValueError:
            safe = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:64] or "desk"
            patch_desk_config({"node_name": safe})
            cfg["node_name"] = safe
    cluster = Cluster(
        emit=emit,
        lookup_local_bot=_lookup_bot,
        local_profiles=lambda: [b.profile() for b in bots.values()],
        is_local_id=lambda bid: bid in bots,
        load_config=load_desk_config,
        listen_info=lambda: (LISTEN_HOST, int(LISTEN_PORT), access_host()),
        on_cluster_dm=_on_cluster_dm,
    )
    load_existing()

    def _observer_loop() -> None:
        # The HTTP UI must exist before a MiniOS mirror can navigate to it.
        while not _stop_http.is_set():
            if _httpd is None:
                time.sleep(0.5)
                continue
            with lock:
                snapshot = list(bots.values())
            for b in snapshot:
                try:
                    if not b.observer or not b.observer.healthy():
                        b.ensure_observer()
                except Exception:
                    # Chrome may not be installed yet; desktop_observe will return the concrete error.
                    pass
            time.sleep(5.0)

    def _routine_loop() -> None:
        while True:
            time.sleep(20)
            snapshot = []
            with lock:
                snapshot = list(bots.values())
            for b in snapshot:
                try:
                    b.tick_routines()
                except Exception:
                    traceback.print_exc()

    threading.Thread(target=_routine_loop, daemon=True).start()
    threading.Thread(target=_observer_loop, daemon=True, name="minios-observers").start()
    print(f"hermes-deskd {access_url()}  (token in {TOKEN_PATH})")
    print(f"hermes:  {HERMES_BIN}")
    print(f"open: {access_url()}")
    print(f"bind: {bind_address(LISTEN_HOST)}:{LISTEN_PORT}")
    print(f"desks: {HERMES_DESKS}")
    print(f"bots:  {USER_AGENT_HOME / 'bots'}")
    print(f"sandbox: {SANDBOX}")

    def _http_loop() -> None:
        global _httpd, LISTEN_HOST, LISTEN_PORT
        while not _stop_http.is_set():
            bind = bind_address(LISTEN_HOST)
            try:
                srv = DeskHTTPServer((bind, int(LISTEN_PORT)), Handler)
            except OSError as e:
                print(f"listen failed {bind}:{LISTEN_PORT}: {e}")
                _rebind_http.clear()
                time.sleep(1.5)
                continue
            _httpd = srv
            print(f"listening {bind}:{LISTEN_PORT}  access {access_url()}")
            _rebind_http.clear()
            if cluster is not None:
                cluster.stop_fanin()
                cluster.start_fanin()
            thread = threading.Thread(target=srv.serve_forever, daemon=True, name="deskd-http")
            thread.start()
            tls_srv: DeskHTTPServer | None = None
            tls_thread: threading.Thread | None = None
            try:
                cert = ensure_https_cert()
                if cert is not None:
                    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    ctx.load_cert_chain(certfile=str(cert), keyfile=str(cert.parent / "desk-https.key"))
                    tls_srv = DeskHTTPServer((bind, int(LISTEN_PORT) + 1), Handler)
                    tls_srv.socket = ctx.wrap_socket(tls_srv.socket, server_side=True)
                    tls_thread = threading.Thread(target=tls_srv.serve_forever, daemon=True, name="deskd-https")
                    tls_thread.start()
                    print(f"listening https {bind}:{int(LISTEN_PORT) + 1}  (self-signed; accept the cert once per device)", flush=True)
            except OSError as e:
                print(f"https listen failed {bind}:{int(LISTEN_PORT) + 1}: {e}", flush=True)
                tls_srv = None
            while not _stop_http.is_set() and not _rebind_http.is_set():
                _stop_http.wait(0.4)
            if tls_srv is not None:
                try:
                    tls_srv.shutdown()
                    tls_srv.server_close()
                except Exception:
                    pass
                if tls_thread is not None:
                    tls_thread.join(timeout=2)
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
            thread.join(timeout=2)
            _httpd = None
            if _rebind_http.is_set() and not _stop_http.is_set():
                time.sleep(0.2)

    http_thread = threading.Thread(target=_http_loop, daemon=True, name="deskd-http-loop")
    http_thread.start()
    try:
        while http_thread.is_alive():
            http_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("stopping")
        _stop_http.set()
        _rebind_http.set()
        if cluster is not None:
            cluster.stop_fanin()
        for b in list(bots.values()):
            b.acp.stop()
        http_thread.join(timeout=3)
        release_deskd_pidfile()


if __name__ == "__main__":
    main()
