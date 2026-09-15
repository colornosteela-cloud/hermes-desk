#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Do not run hermes-deskd as root. Use the same user that installed Hermes Agent (hermes login)." >&2
  echo "The CLI is ~/.local/bin/hermes or ~/.hermes/bin/hermes for that user — not /root/.local/bin/hermes." >&2
  echo "If you must, set HERMES_BIN=/path/to/hermes and HERMES_DESK_HOME=/home/<user>/.hermes" >&2
  exit 1
fi
if [[ -z "${HERMES_BIN:-}" ]]; then
  if command -v hermes >/dev/null 2>&1; then
    HERMES_BIN="$(command -v hermes)"
  elif [[ -x "${HOME}/.hermes/bin/hermes" ]]; then
    HERMES_BIN="${HOME}/.hermes/bin/hermes"
  elif [[ -x "${HOME}/.local/bin/hermes" ]]; then
    HERMES_BIN="${HOME}/.local/bin/hermes"
  else
    HERMES_BIN="${HOME}/.local/bin/hermes"
  fi
fi
export HERMES_BIN
export HERMES_DESK_PORT="${HERMES_DESK_PORT:-8742}"
export HERMES_DESKS="${HERMES_DESKS:-$HOME/hermes-desks}"
export HERMES_DESK_SANDBOX="${HERMES_DESK_SANDBOX:-off}"
# Local model upstream (llama.cpp, vLLM, or a key proxy in front of them).
# Renamed from HERMES_DESK_VLLM*; the old names are still honored by deskd.
export HERMES_DESK_LLM="${HERMES_DESK_LLM:-${HERMES_DESK_VLLM:-http://127.0.0.1:8081}}"
export HERMES_DESK_MODEL="${HERMES_DESK_MODEL:-${HERMES_DESK_VLLM_MODEL:-Qwen3.8-27B}}"
export HERMES_DESK_MAX_LEN="${HERMES_DESK_MAX_LEN:-${HERMES_DESK_VLLM_MAX_LEN:-262144}}"
export HERMES_DESK_MAX_TOKENS="${HERMES_DESK_MAX_TOKENS:-${HERMES_DESK_VLLM_MAX_TOKENS:-32768}}"

runtime="${XDG_RUNTIME_DIR:-/tmp}/hermes-desk"
mkdir -p "$runtime"
pidfile="$runtime/deskd.pid"

deskd_already_running() {
  if [[ -f "$pidfile" ]]; then
    old="$(tr -d ' \n' < "$pidfile" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      cmd="$(tr '\0' ' ' < "/proc/${old}/cmdline" 2>/dev/null || true)"
      if [[ "$cmd" == *deskd.py* ]]; then
        echo "hermes-deskd already running (pid ${old})"
        return 0
      fi
    fi
  fi
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE ":${HERMES_DESK_PORT}[[:space:]]"; then
    echo "hermes-deskd port ${HERMES_DESK_PORT} already in use"
    return 0
  fi
  return 1
}

if deskd_already_running; then
  exit 0
fi

# Nested under a Hermes Agent agent, detach so the parent cannot SIGTERM our hermes children.
if [[ -n "${HERMES_AGENT:-${GROK_AGENT:-}}" && -z "${HERMES_DESK_FOREGROUND:-}" ]] && command -v setsid >/dev/null 2>&1; then
  echo "hermes-deskd detaching on http://127.0.0.1:${HERMES_DESK_PORT}/  (log $runtime/deskd.log)"
  nohup setsid python3 deskd/deskd.py </dev/null >"$runtime/deskd.log" 2>&1 &
  echo "pid $!"
  exit 0
fi
exec python3 deskd/deskd.py
