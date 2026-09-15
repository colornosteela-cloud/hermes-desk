"""Bounded Hermes Agent one-shot for Teela: system info and read-only commands."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+|authorization\s*[:=]\s*\S+|token\s*[:=]\s*\S+)"
)

_SAFE_COMMAND = re.compile(
    r"^(?:"
    r"uname(?:\s+-[a-zA-Z]+)?|"
    r"hostname|"
    r"date|"
    r"uptime|"
    r"whoami|"
    r"df(?:\s+-h)?|"
    r"free(?:\s+-h)?|"
    r"lscpu|"
    r"nvidia-smi(?:\s+-L)?|"
    r"cat /proc/(?:meminfo|cpuinfo|version)|"
    r"systemctl --user (?:status|is-active)(?:\s+[a-z0-9@._-]+)*|"
    r"curl -sS http://127\.0\.0\.1:8081/v1/models"
    r")$",
    re.I,
)

_EXTRACT = [
    re.compile(p, re.I)
    for p in (
        r"\buname(?:\s+-[a-zA-Z]+)?\b",
        r"\bhostname\b",
        r"\buptime\b",
        r"\bwhoami\b",
        r"\bdf(?:\s+-h)?\b",
        r"\bfree(?:\s+-h)?\b",
        r"\blscpu\b",
        r"\bnvidia-smi(?:\s+-L)?\b",
        r"\bcat /proc/(?:meminfo|cpuinfo|version)\b",
        r"\bsystemctl --user (?:status|is-active)(?:\s+[a-z0-9@._-]+)*",
        r"\bcurl -sS http://127\.0\.0\.1:8081/v1/models\b",
    )
]


def command_is_safe(command: str) -> bool:
    return bool(_SAFE_COMMAND.match(" ".join((command or "").split())))


def extract_command(text: str) -> str | None:
    t = " ".join((text or "").split())
    if not t:
        return None
    for rx in _EXTRACT:
        m = rx.search(t)
        if m:
            cmd = " ".join(m.group(0).split())
            if command_is_safe(cmd):
                return cmd
    return None


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text or "")


def build_prompt(task: str, command: str | None) -> str:
    if command:
        return (
            "Use Hermes Agent's shell tool to run this exact command. "
            "Return only the command stdout. Do not modify files, do not run anything else, "
            "and do not print secrets.\n"
            f"Command: {command}"
        )
    return (task or "").strip()


def run_agent_oneshot(
    *,
    task: str = "",
    command: str | None = None,
    cwd: str | None = None,
    timeout: float = 90,
    agent_bin: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One-shot `hermes --single`: read-only probes, or a coding task with no command."""
    cmd = " ".join((command or "").split()) or None
    if cmd and not command_is_safe(cmd):
        return {"ok": False, "error": "command is not an allowed read-only probe"}
    if cmd:
        prompt = build_prompt(task, cmd)
    else:
        prompt = (
            "Use Hermes Agent coding tools (shell, files, grep, search_replace, skills, MCP) as needed.\n"
            + (task or "").strip()
        )
    if not prompt.strip():
        return {"ok": False, "error": "task or command required"}
    binary = agent_bin or os.environ.get("HERMES_BIN") or str(Path.home() / ".hermes/bin/hermes")
    if not Path(binary).is_file() and runner is None:
        return {"ok": False, "error": f"Hermes Agent CLI not found at {binary}"}
    argv = [binary]
    if cwd:
        argv += ["--cwd", str(cwd)]
    sock_root = Path(cwd) if cwd else Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    sock = sock_root / f"hermes-{os.getpid()}.sock"
    argv += [
        "--leader-socket",
        str(sock),
        "--max-turns",
        "12",
        "--output-format",
        "plain",
        "--permission-mode",
        "bypassPermissions",
        "--single",
        prompt,
    ]
    run = runner or subprocess.run
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Hermes Agent timed out", "via": "hermes"}
    except Exception as e:
        return {"ok": False, "error": str(e), "via": "hermes"}
    stdout = redact((getattr(proc, "stdout", None) or "").strip())
    stderr = redact((getattr(proc, "stderr", None) or "").strip())
    code = int(getattr(proc, "returncode", 1) or 0)
    output = stdout or stderr
    return {
        "ok": code == 0 and bool(output),
        "output": output[:8000],
        "command": cmd,
        "via": "hermes",
        "returncode": code,
        "error": "" if code == 0 else (stderr or stdout or "hermes failed")[:500],
    }
