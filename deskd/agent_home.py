"""Per-bot Hermes Agent home management.

Each bot runs its own ``hermes acp`` / ``hermes --tui`` child under an isolated
``HERMES_HOME`` (``<bot>/agent-home``). The home is a lightweight overlay:
config.yaml is generated, while credentials (.env), auth.json, and the shared
skills/memories/plugins trees are symlinked from the host ``~/.hermes`` so a
refresh of the host login (OIDC / Nous) is never forked.

Model picker: the desk keeps its own shared catalog in
``~/.hermes-desk/models.json`` (local + remote rows the user registered,
exactly like the old ``~/.grok/config.toml`` [model.*] tables). Each bot's
child config.yaml is regenerated from that catalog (with the bot's own
default + effort) on every start/model change, so the picker stays the source
of truth — the child never rewrites it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

# Items shared (symlinked) from the host Hermes home into each bot home.
_SHARED_FILES = (".env", "auth.json", "SOUL.md", "install_id")
_SHARED_DIRS = ("skills", "memories", "plugins", "desktop-plugins", "skins")

# Never symlink state that must stay per-bot.
_PER_BOT = ("state.db", "config.yaml", "models.json", "last_acp_session.json")


def host_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_DESK_HOME") or (Path.home() / ".hermes")).expanduser()


def models_catalog_path() -> Path:
    """Shared desk model catalog (all bots, one picker)."""
    return host_hermes_home() / "models.json"


def _link_or_copy(src: Path, dest: Path) -> None:
    try:
        if dest.is_symlink():
            if dest.resolve() == src.resolve():
                return
            dest.unlink()
        elif dest.exists():
            return  # already present and real; don't clobber bot data
    except OSError:
        return
    if not src.exists():
        return
    try:
        dest.symlink_to(src)
    except OSError:
        if src.is_file():
            try:
                shutil.copy2(src, dest)
                os.chmod(dest, 0o600)
            except OSError:
                pass


def ensure_agent_home(bot_home: Path) -> None:
    """Create the per-bot HERMES_HOME overlay and link shared host resources."""
    bot_home.mkdir(parents=True, exist_ok=True)
    host = host_hermes_home()
    for name in _SHARED_FILES:
        _link_or_copy(host / name, bot_home / name)
    for name in _SHARED_DIRS:
        src = host / name
        dest = bot_home / name
        if dest.is_symlink():
            if dest.resolve() == src.resolve():
                continue
            dest.unlink()
        elif dest.exists():
            continue
        if src.exists():
            try:
                dest.symlink_to(src)
            except OSError:
                pass
    (bot_home / "logs").mkdir(exist_ok=True)
    (bot_home / "sessions").mkdir(exist_ok=True)


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def load_bot_catalog() -> tuple[str, dict[str, Any]]:
    """(default_model_id, catalog) from the shared desk models.json."""
    p = models_catalog_path()
    if not p.is_file():
        return "", {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", {}
    catalog = data.get("catalog") or {}
    default = str(data.get("default") or "")
    if not isinstance(catalog, dict):
        catalog = {}
    if default not in catalog and catalog:
        default = next(iter(catalog))
    return default, catalog


def save_bot_catalog(default: str, catalog: dict[str, Any]) -> None:
    p = models_catalog_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not catalog:
        raise ValueError("keep at least one model in the picker")
    if default not in catalog:
        default = next(iter(catalog))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"default": default, "catalog": catalog}, indent=2), encoding="utf-8")
    tmp.replace(p)




def _yaml_key(k: str) -> str:
    k = str(k or "").strip()
    if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_child_hermes_home(
    bot_home: Path,
    default_model: str,
    catalog: dict[str, Any],
    *,
    reasoning_effort: str = "",
    permission_mode: str = "default",
) -> None:
    """Regenerate the bot's config.yaml from the picker catalog.

    Model rows in the catalog are OpenAI-compatible endpoints (the desk's local
    llama.cpp/vLLM upstreams). The default model uses the bare ``custom``
    provider via the inline ``model:`` block (the shape the host's own working
    config.yaml uses). Every other picker row is registered as a named
    ``providers:`` entry keyed by its picker id so ``session/set_model`` can
    round-trip through the ACP-advertised ``custom:<key>:<model>`` id.
    """
    ensure_agent_home(bot_home)
    model = str(default_model or "").strip()
    raw_row = catalog.get(model)
    row = raw_row if isinstance(raw_row, dict) else {}
    base_url = str(row.get("base_url") or "").strip()
    api_key = str(row.get("api_key") or "").strip()
    served = str(row.get("model") or model).strip() or model
    effort = (reasoning_effort or "").strip().lower()

    mode = "acceptEdits" if permission_mode in {"always-approve", "acceptEdits"} else "default"

    lines = [
        "# Generated by hermes-deskd for this bot. Do not edit by hand.",
        "model:",
        f"  default: {_yaml_scalar(served)}",
        "  provider: custom",
    ]
    if base_url:
        lines.append(f"  base_url: {_yaml_scalar(base_url)}")
    if api_key:
        lines.append(f"  api_key: {_yaml_scalar(api_key)}")
    lines.append("agent:")
    if effort in {"minimal", "low", "medium", "high", "xhigh"}:
        lines.append(f"  reasoning_effort: {effort}")
    lines += [
        f"  permission_mode: {mode}",
        "  max_turns: 150",
        "terminal:",
        "  backend: local",
        "compression:",
        "  enabled: true",
    ]
    # Named provider per picker row (except the default, already inline above)
    # so every selectable model round-trips through session/set_model.
    providers: list[tuple[str, dict[str, Any]]] = []
    for mid, tbl in catalog.items():
        if not isinstance(tbl, dict) or mid == model:
            continue
        burl = str(tbl.get("base_url") or "").strip()
        if not burl:
            continue
        served_m = str(tbl.get("model") or mid).strip() or mid
        entry: dict[str, Any] = {
            "name": mid,
            "base_url": burl,
            "models": [served_m],
        }
        k = str(tbl.get("api_key") or "").strip()
        if k:
            entry["api_key"] = k
        cw = tbl.get("context_window")
        if isinstance(cw, (int, float)) and cw > 0:
            entry["context_length"] = int(cw)
        providers.append((mid, entry))
    if providers:
        lines.append("providers:")
        for key, entry in providers:
            lines.append(f"  {_yaml_key(key)}:")
            for k in ("name", "base_url", "api_key", "context_length"):
                if k in entry:
                    lines.append(f"    {k}: {_yaml_scalar(entry[k])}")
            lines.append("    models:")
            for m in entry["models"]:
                lines.append(f"      - {_yaml_scalar(m)}")
    (bot_home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def newest_hermes_session(bot_home: Path) -> str | None:
    """Most recent session id with messages in the bot's state.db (bootstrap only)."""
    db = bot_home / "state.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "select s.id from sessions s "
                "where s.id in (select distinct session_id from messages) "
                "order by (select max(timestamp) from messages where session_id = s.id) desc "
                "limit 1"
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None


def hermes_session_ids(bot_home: Path) -> list[tuple[str, float]]:
    """(session_id, last_activity_ts) for sessions that have messages."""
    db = bot_home / "state.db"
    if not db.is_file():
        return []
    out: list[tuple[str, float]] = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "select s.id, max(m.timestamp) from sessions s "
                "join messages m on m.session_id = s.id group by s.id"
            )
            for sid, ts in cur.fetchall():
                out.append((str(sid), float(ts or 0)))
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return []
    return out
