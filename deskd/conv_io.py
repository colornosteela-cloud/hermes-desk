"""Conversation export/import for Hermes Desk.

Understands native Hermes Desk JSON, ChatGPT data-export trees, Claude
chat_messages, OpenAI {role,content} arrays, hermes.com / xAI account dumps,
markdown transcripts, jsonl, and ZIP wrappers of those files.
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_CONVERSATIONS = 50
VISIBLE_ROLES = {"user", "assistant", "system", "human", "hermes", "you", "model"}


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    t = datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc)
    return t.isoformat().replace("+00:00", "Z")


def unix(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        return n
    if isinstance(value, dict):
        if "$date" in value:
            inner = value["$date"]
            if isinstance(inner, dict) and "$numberLong" in inner:
                try:
                    return int(inner["$numberLong"]) / 1000.0
                except (TypeError, ValueError):
                    return None
            return unix(inner)
        return unix(value.get("$numberLong") or value.get("seconds"))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.replace(".", "", 1).isdigit():
            return unix(float(s))
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        bits = [extract_text(p) for p in content]
        return "\n".join(b for b in bits if b)
    if isinstance(content, dict):
        if "parts" in content:
            return extract_text(content.get("parts"))
        for key in ("text", "message", "content", "value", "body"):
            if key in content and content[key] is not None and key != "content":
                t = extract_text(content[key])
                if t:
                    return t
        if "content" in content:
            t = extract_text(content["content"])
            if t:
                return t
        if content.get("type") in ("image", "image_url", "image_file"):
            return ""
        return ""
    return str(content)


def canon_role(role: str | None) -> str:
    r = (role or "").strip().lower()
    if r in ("user", "human", "you", "customer", "prompter"):
        return "user"
    if r in ("assistant", "hermes", "model", "bot", "ai", "chatgpt", "claude", "gpt"):
        return "assistant"
    if r in ("system", "developer"):
        return "system"
    if r in ("tool", "function"):
        return "tool"
    return r or "assistant"


def normalize_msg(role: str, text: str, ts: Any = None, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    role_n = canon_role(role)
    if role_n not in ("user", "assistant", "system"):
        return None
    msg: dict[str, Any] = {"role": role_n, "text": text}
    t = unix(ts)
    if t is not None:
        msg["ts"] = t
    if extra:
        for k, v in extra.items():
            if v is not None and k not in msg:
                msg[k] = v
    return msg


# ---------------------------------------------------------------------------
# Import parsers
# ---------------------------------------------------------------------------

def chatgpt_linear(conv: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = conv.get("mapping") or {}
    if not isinstance(mapping, dict):
        return []
    node_id = conv.get("current_node")
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while node_id and node_id not in seen:
        seen.add(str(node_id))
        node = mapping.get(node_id) or {}
        if not isinstance(node, dict):
            break
        chain.append(node)
        node_id = node.get("parent")
    if not chain:
        nodes = [n for n in mapping.values() if isinstance(n, dict)]
        nodes.sort(key=lambda n: unix((n.get("message") or {}).get("create_time")) or 0)
        chain = nodes
    else:
        chain.reverse()
    out: list[dict[str, Any]] = []
    for node in chain:
        m = node.get("message")
        if not isinstance(m, dict):
            continue
        meta = m.get("metadata") or {}
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        role = (m.get("author") or {}).get("role") or m.get("role")
        if canon_role(role) == "tool":
            continue
        text = extract_text(m.get("content"))
        nm = normalize_msg(role, text, m.get("create_time") or node.get("create_time"))
        if nm:
            out.append(nm)
    return out


def claude_linear(conv: dict[str, Any]) -> list[dict[str, Any]]:
    msgs = conv.get("chat_messages") or conv.get("messages") or []
    if not isinstance(msgs, list):
        return []
    by_id = {m.get("uuid") or m.get("id"): m for m in msgs if isinstance(m, dict) and (m.get("uuid") or m.get("id"))}
    parents = {m.get("parent_message_uuid") or m.get("parent") for m in msgs if isinstance(m, dict)}
    leaves = [
        m
        for m in msgs
        if isinstance(m, dict) and (m.get("uuid") or m.get("id")) not in parents
    ]
    if leaves:
        def leaf_key(m: dict[str, Any]) -> float:
            return unix(m.get("created_at") or m.get("updated_at")) or 0.0

        cur: dict[str, Any] | None = max(leaves, key=leaf_key)
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        while cur:
            uid = str(cur.get("uuid") or cur.get("id") or "")
            if uid in seen:
                break
            if uid:
                seen.add(uid)
            chain.append(cur)
            pid = cur.get("parent_message_uuid") or cur.get("parent")
            cur = by_id.get(pid) if pid else None
        chain.reverse()
        msgs = chain
    else:
        msgs = sorted(
            [m for m in msgs if isinstance(m, dict)],
            key=lambda m: unix(m.get("created_at") or m.get("updated_at")) or 0.0,
        )
    out: list[dict[str, Any]] = []
    for m in msgs:
        role = m.get("sender") or m.get("role") or m.get("author")
        text = m.get("text") or extract_text(m.get("content"))
        nm = normalize_msg(role, text, m.get("created_at") or m.get("updated_at"))
        if nm:
            out.append(nm)
    return out


def agent_linear(item: dict[str, Any]) -> list[dict[str, Any]]:
    conv = item.get("conversation") if isinstance(item.get("conversation"), dict) else item
    responses = item.get("responses")
    if not isinstance(responses, list):
        responses = conv.get("responses") if isinstance(conv, dict) else None
    out: list[dict[str, Any]] = []
    if isinstance(responses, list) and responses:
        for wrap in responses:
            if not isinstance(wrap, dict):
                continue
            resp = wrap.get("response") if isinstance(wrap.get("response"), dict) else wrap
            sender = resp.get("sender") or resp.get("role") or resp.get("speaker")
            text = resp.get("message") or extract_text(resp.get("content")) or resp.get("text") or ""
            nm = normalize_msg(sender, text, resp.get("create_time") or resp.get("created_at"))
            if nm:
                out.append(nm)
        return out
    for m in conv.get("messages") or item.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or m.get("sender") or m.get("speaker")
        text = m.get("content") or m.get("message") or m.get("text") or ""
        nm = normalize_msg(role, extract_text(text), m.get("timestamp") or m.get("create_time") or m.get("ts"))
        if nm:
            out.append(nm)
    return out


def openai_linear(obj: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        items = obj
    else:
        items = obj.get("messages") or obj.get("data") or []
    out: list[dict[str, Any]] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or m.get("sender") or (m.get("author") or {}).get("role")
        text = m.get("text") or extract_text(m.get("content")) or m.get("message") or ""
        nm = normalize_msg(role, text, m.get("created_at") or m.get("create_time") or m.get("ts"))
        if nm:
            extra = {}
            if m.get("images"):
                extra["images"] = m["images"]
            if m.get("via"):
                extra["via"] = m["via"]
            if m.get("peer"):
                extra["peer"] = m["peer"]
            if extra:
                nm.update(extra)
            out.append(nm)
    return out


MD_SPLIT = re.compile(
    r"(?im)^(?:#{1,3}\s+)?(?:\*\*)?(user|you|human|assistant|chatgpt|claude|hermes|system|model)(?:\*\*)?\s*:?\s*$"
)
MD_INLINE = re.compile(
    r"(?im)^(?:#{1,3}\s+)?(?:\*\*)?(user|you|human|assistant|chatgpt|claude|hermes|system|model)(?:\*\*)?\s*:\s+(.*)$"
)


def markdown_linear(text: str) -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[dict[str, Any]] = []
    role: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal role, buf
        if role:
            nm = normalize_msg(role, "\n".join(buf))
            if nm:
                out.append(nm)
        role, buf = None, []

    for line in lines:
        m = MD_SPLIT.match(line.strip())
        if m:
            flush()
            role = m.group(1)
            continue
        m = MD_INLINE.match(line)
        if m:
            flush()
            role = m.group(1)
            rest = m.group(2)
            buf = [rest] if rest else []
            continue
        if role is not None:
            buf.append(line)
    flush()
    return out


def jsonl_linear(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role") or obj.get("sender") or obj.get("speaker")
        text_v = obj.get("text") or extract_text(obj.get("content") or obj.get("message"))
        nm = normalize_msg(role, text_v, obj.get("ts") or obj.get("create_time"))
        if nm:
            out.append(nm)
    return out


def titled(title: str, source: str, msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not msgs:
        return []
    head = normalize_msg("system", f"Imported conversation: {title} ({source})")
    return ([head] if head else []) + msgs


def from_object(data: Any, filename: str = "") -> tuple[list[dict[str, Any]], str, int]:
    """Return (messages, detected_source, conversation_count)."""
    name = (filename or "").lower()

    if isinstance(data, dict) and data.get("source") == "hermes-desk":
        msgs = openai_linear(data)
        return msgs, "hermes-desk", 1

    if isinstance(data, dict) and isinstance(data.get("mapping"), dict):
        title = data.get("title") or filename or "ChatGPT conversation"
        return titled(title, "ChatGPT", chatgpt_linear(data)), "chatgpt", 1

    if isinstance(data, list) and data and isinstance(data[0], dict) and "mapping" in data[0]:
        bundled: list[dict[str, Any]] = []
        convs = sorted(data, key=lambda c: unix(c.get("update_time") or c.get("create_time")) or 0, reverse=True)
        for conv in convs[:MAX_CONVERSATIONS]:
            title = conv.get("title") or "ChatGPT conversation"
            bundled.extend(titled(title, "ChatGPT", chatgpt_linear(conv)))
        return bundled, "chatgpt", min(len(data), MAX_CONVERSATIONS)

    if isinstance(data, dict) and ("chat_messages" in data or (isinstance(data.get("messages"), list) and any(
        isinstance(m, dict) and m.get("sender") in ("human", "assistant") for m in data.get("messages") or []
    ))):
        title = data.get("name") or data.get("title") or filename or "Claude conversation"
        return titled(title, "Claude", claude_linear(data)), "claude", 1

    if isinstance(data, list) and data and isinstance(data[0], dict) and (
        "chat_messages" in data[0] or (data[0].get("uuid") and data[0].get("name") is not None)
    ):
        bundled = []
        convs = sorted(
            data,
            key=lambda c: unix(c.get("updated_at") or c.get("created_at")) or 0,
            reverse=True,
        )
        for conv in convs[:MAX_CONVERSATIONS]:
            title = conv.get("name") or conv.get("title") or "Claude conversation"
            bundled.extend(titled(title, "Claude", claude_linear(conv)))
        return bundled, "claude", min(len(data), MAX_CONVERSATIONS)

    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        convs = data["conversations"]
        if convs and isinstance(convs[0], dict) and ("conversation" in convs[0] or "responses" in convs[0]):
            bundled = []
            for item in convs[:MAX_CONVERSATIONS]:
                conv = item.get("conversation") if isinstance(item.get("conversation"), dict) else item
                title = (conv or {}).get("title") or "Hermes conversation"
                bundled.extend(titled(title, "Hermes", agent_linear(item)))
            return bundled, "hermes", min(len(convs), MAX_CONVERSATIONS)
        bundled = []
        for item in convs[:MAX_CONVERSATIONS]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or "conversation"
            msgs = agent_linear(item) or openai_linear(item)
            src = "hermes" if item.get("messages") else "openai"
            bundled.extend(titled(title, src, msgs))
        return bundled, "hermes", min(len(convs), MAX_CONVERSATIONS)

    if isinstance(data, dict) and isinstance(data.get("conversation"), list):
        # userscript hermes export: {platform: hermes, conversation: [{speaker, content}]}
        msgs = []
        for m in data["conversation"]:
            if not isinstance(m, dict):
                continue
            nm = normalize_msg(m.get("speaker") or m.get("role"), m.get("content") or m.get("message") or m.get("text") or "")
            if nm:
                msgs.append(nm)
        if msgs:
            return msgs, "hermes", 1

    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return openai_linear(data), "openai", 1

    if isinstance(data, list) and data and isinstance(data[0], dict) and (
        "role" in data[0] or "content" in data[0] or "text" in data[0]
    ):
        return openai_linear(data), "openai", 1

    if "prod-hermes" in name or name.endswith("backend.json"):
        if isinstance(data, dict):
            return from_object({"conversations": data.get("conversations") or []}, filename)

    return [], "unknown", 0


def from_bytes(raw: bytes, filename: str = "") -> tuple[list[dict[str, Any]], str, int]:
    name = (filename or "").lower()
    if name.endswith(".zip") or raw[:2] == b"PK":
        return from_zip(raw, filename)
    text = raw.decode("utf-8", errors="replace")
    return from_text(text, filename)


def from_zip(raw: bytes, filename: str = "") -> tuple[list[dict[str, Any]], str, int]:
    bundled: list[dict[str, Any]] = []
    source = "zip"
    count = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")]
        preferred = []
        rest = []
        for n in names:
            ln = n.lower()
            if ln.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".html", ".css", ".txt")):
                continue
            if any(
                key in ln
                for key in (
                    "conversations.json",
                    "claude",
                    "prod-hermes-backend",
                    "chat.json",
                    "messages.json",
                )
            ):
                preferred.append(n)
            elif ln.endswith((".json", ".jsonl", ".md")):
                rest.append(n)
        for n in preferred + rest:
            try:
                payload = zf.read(n)
            except Exception:
                continue
            try:
                msgs, src, nconv = from_bytes(payload, n.split("/")[-1])
            except Exception:
                continue
            if msgs:
                bundled.extend(msgs)
                source = src
                count += nconv
                if count >= MAX_CONVERSATIONS:
                    break
    return bundled, source, count or (1 if bundled else 0)


def from_text(text: str, filename: str = "") -> tuple[list[dict[str, Any]], str, int]:
    name = (filename or "").lower()
    stripped = text.lstrip()
    if name.endswith(".jsonl") or (stripped.startswith("{") and "\n{" in text[:2000] and stripped.count("\n") > 0):
        msgs = jsonl_linear(text)
        if msgs:
            return msgs, "jsonl", 1
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            msgs, src, n = from_object(data, filename)
            if msgs:
                return msgs, src, n
            if name.endswith(".jsonl"):
                msgs = jsonl_linear(text)
                if msgs:
                    return msgs, "jsonl", 1
    if name.endswith((".md", ".txt", ".markdown")) or MD_SPLIT.search(text) or MD_INLINE.search(text):
        msgs = markdown_linear(text)
        if msgs:
            return msgs, "markdown", 1
    msgs = jsonl_linear(text)
    if msgs:
        return msgs, "jsonl", 1
    return [], "unknown", 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

EXPORT_FORMATS = ("json", "md", "chatgpt", "claude", "openai", "hermes", "system")

SYSTEM_SKIP_DIRS = {
    "browser-profile",
    "observer-profile",
    "bundled",
    "vendor",
    "node_modules",
    "__pycache__",
    "GPUCache",
    "Cache",
    "Code Cache",
    "marketplace-cache",
    ".cache",
}
SYSTEM_SKIP_FILES = {
    "auth.json",
    "auth.json.lock",
    "token",
    ".token",
}
SYSTEM_SKIP_SUFFIXES = {".lock", ".pem", ".key", ".p12"}


def visible(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("open") and not (m.get("text") or "").strip():
            continue
        role = canon_role(m.get("role"))
        if role not in ("user", "assistant", "system"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        item = {"role": role, "text": text}
        if m.get("ts"):
            item["ts"] = m["ts"]
        if m.get("images"):
            item["images"] = m["images"]
        if m.get("via"):
            item["via"] = m["via"]
        if m.get("peer"):
            item["peer"] = m["peer"]
        out.append(item)
    return out


def export_payload(bot: Any, fmt: str) -> tuple[bytes, str, str]:
    fmt = (fmt or "json").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown format {fmt}; use {', '.join(EXPORT_FORMATS)}")
    msgs = visible(getattr(bot, "messages", []) or [])
    name = getattr(bot, "name", "bot") or "bot"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "bot"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if fmt == "json":
        body = {
            "source": "hermes-desk",
            "version": 1,
            "exported_at": iso(),
            "bot": {
                "id": getattr(bot, "id", ""),
                "name": name,
                "model": getattr(bot, "model", ""),
            },
            "messages": msgs,
        }
        raw = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, f"{slug}-chat-{stamp}.json", "application/json"
    if fmt == "md":
        lines = [
            f"# Conversation with {name}",
            f"Exported {iso()} · Hermes Desk",
            "",
        ]
        labels = {"user": "User", "assistant": "Assistant", "system": "System"}
        for m in msgs:
            lines.append(f"## {labels.get(m['role'], m['role'].title())}")
            lines.append("")
            lines.append(m["text"])
            lines.append("")
        raw = "\n".join(lines).encode("utf-8")
        return raw, f"{slug}-chat-{stamp}.md", "text/markdown; charset=utf-8"
    if fmt == "openai":
        body = {
            "messages": [
                {"role": m["role"], "content": m["text"]}
                for m in msgs
                if m["role"] in ("user", "assistant", "system")
            ]
        }
        raw = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")
        return raw, f"{slug}-openai-{stamp}.json", "application/json"
    if fmt == "chatgpt":
        raw = json.dumps(_as_chatgpt(name, msgs), indent=2, ensure_ascii=False).encode("utf-8")
        return raw, f"{slug}-chatgpt-{stamp}.json", "application/json"
    if fmt == "claude":
        raw = json.dumps(_as_claude(name, msgs), indent=2, ensure_ascii=False).encode("utf-8")
        return raw, f"{slug}-claude-{stamp}.json", "application/json"
    if fmt == "hermes":
        raw = json.dumps(_as_agent(name, msgs), indent=2, ensure_ascii=False).encode("utf-8")
        return raw, f"{slug}-hermes-{stamp}.json", "application/json"
    if fmt == "system":
        return export_system_zip(bot, slug, stamp)
    raise ValueError(f"unknown format {fmt}")


def _system_skip(rel: Path) -> bool:
    if any(part in SYSTEM_SKIP_DIRS for part in rel.parts):
        return True
    if rel.name in SYSTEM_SKIP_FILES:
        return True
    if rel.suffix.lower() in SYSTEM_SKIP_SUFFIXES:
        return True
    return False


def _zip_tree(zf: zipfile.ZipFile, src: Path, prefix: str) -> int:
    if not src.exists():
        return 0
    added = 0
    if src.is_file():
        if not _system_skip(Path(src.name)):
            zf.write(src, f"{prefix}/{src.name}")
            added += 1
        return added
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(src)
        except ValueError:
            continue
        if _system_skip(rel):
            continue
        zf.write(path, f"{prefix}/{rel.as_posix()}")
        added += 1
    return added


def export_system_zip(bot: Any, slug: str = "bot", stamp: str = "") -> tuple[bytes, str, str]:
    """Zip identity, MiniOS workspace, live body, memory, and MiniOS source for offline review."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    name = getattr(bot, "name", "bot") or "bot"
    buf = io.BytesIO()
    files = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "source": "hermes-desk",
            "kind": "system",
            "version": 1,
            "exported_at": iso(),
            "bot": {
                "id": getattr(bot, "id", ""),
                "name": name,
                "model": getattr(bot, "model", ""),
                "emoji": getattr(bot, "emoji", ""),
            },
            "paths": {
                "bot_root": str(getattr(bot, "root", "") or ""),
                "workspace": str(getattr(bot, "workspace", "") or ""),
                "desk": str(getattr(bot, "desk", "") or ""),
            },
            "skipped": sorted(SYSTEM_SKIP_DIRS | SYSTEM_SKIP_FILES),
            "layout": {
                "README.md": "How to read this archive",
                "manifest.json": "Bot identity and original disk paths",
                "bot/": "SOUL, agent profile, chats, robot_state, Hermes home (no auth)",
                "workspace/": "MiniOS desktop files, BODY.md, AGENTS.md, memory",
                "minios/": "Hermes Desk MiniOS source used to run this bot",
            },
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        zf.writestr("README.md", _SYSTEM_README)
        live = getattr(bot, "robot_state", None)
        if isinstance(live, dict):
            zf.writestr("bot/robot_state.live.json", json.dumps(live, indent=2, default=str) + "\n")
        root = getattr(bot, "root", None)
        if root:
            files += _zip_tree(zf, Path(root), "bot")
        workspace = getattr(bot, "workspace", None)
        if workspace:
            files += _zip_tree(zf, Path(workspace), "workspace")
        desk_src = Path(__file__).resolve().parent.parent
        if (desk_src / "deskd").is_dir() and (desk_src / "ui").is_dir():
            for rel in (
                "VERSION",
                "README.md",
                "RELEASE_NOTES.md",
                "start.sh",
            ):
                p = desk_src / rel
                if p.is_file():
                    zf.write(p, f"minios/{rel}")
                    files += 1
            for py in sorted((desk_src / "deskd").glob("*.py")):
                zf.write(py, f"minios/deskd/{py.name}")
                files += 1
            ui = desk_src / "ui"
            for ui_name in (
                "index.html",
                "app.js",
                "hermesbot-ui.js",
                "hermesbot.css",
                "styles.css",
                "robot-simulator.html",
            ):
                p = ui / ui_name
                if p.is_file():
                    zf.write(p, f"minios/ui/{ui_name}")
                    files += 1
            test = desk_src / "tests" / "test_minios.py"
            if test.is_file():
                zf.write(test, "minios/tests/test_minios.py")
                files += 1
        zf.writestr("manifest.files.txt", f"{files} files packed (plus README and manifest)\n")
    return buf.getvalue(), f"{slug}-system-{stamp}.zip", "application/zip"


_SYSTEM_README = """# Hermes Desk bot system export

This zip is the bot as a MiniOS system, not just the chat.

## Layout

- `manifest.json` — bot id, name, model, original disk paths
- `bot/` — identity (`SOUL.md`, `agent.md`, `PROFILE.toml`), chats, `robot_state.json`, memory
- `workspace/` — MiniOS home: `BODY.md`, `AGENTS.md`, Desktop/Documents/Pictures, `.memory`
- `minios/` — Hermes Desk source that runs the body and desktop (`deskd/`, `ui/robot-simulator.html`)

Chrome profiles, API tokens (`auth.json`), vendor bundles, and caches are omitted.

## Review / debug

1. Read `workspace/BODY.md` and `bot/SOUL.md` for who the bot is and how the body is supposed to look.
2. Read `bot/robot_state.json` (or `bot/robot_state.live.json`) for the last pose.
3. Read `minios/ui/robot-simulator.html` and `minios/deskd/robot_sim.py` for Body Actions / walk / wave.
4. Chats are under `bot/conversations/` (`chat.jsonl`, `log.jsonl`, `archive/`).

This archive is for reading and debugging. It is not a one-click restore into another desk.
"""


def _nid() -> str:
    return str(uuid.uuid4())


def _as_chatgpt(title: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
    root = _nid()
    mapping: dict[str, Any] = {root: {"id": root, "message": None, "parent": None, "children": []}}
    parent = root
    t0 = now_ts()
    for i, m in enumerate(msgs):
        nid = _nid()
        ts = m.get("ts") or (t0 + i)
        mapping[parent]["children"].append(nid)
        mapping[nid] = {
            "id": nid,
            "parent": parent,
            "children": [],
            "message": {
                "id": nid,
                "author": {"role": m["role"]},
                "create_time": ts,
                "content": {"content_type": "text", "parts": [m["text"]]},
                "status": "finished_successfully",
                "metadata": {},
            },
        }
        parent = nid
    return {
        "title": title,
        "create_time": t0,
        "update_time": now_ts(),
        "mapping": mapping,
        "current_node": parent,
    }


def _as_claude(title: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
    root = "00000000-0000-0000-0000-000000000000"
    items = []
    parent = root
    t0 = now_ts()
    for i, m in enumerate(msgs):
        uid = _nid()
        sender = "human" if m["role"] == "user" else ("assistant" if m["role"] == "assistant" else "human")
        if m["role"] == "system":
            sender = "human"
        ts = iso(m.get("ts") or (t0 + i))
        items.append(
            {
                "uuid": uid,
                "text": m["text"],
                "sender": sender,
                "parent_message_uuid": parent,
                "created_at": ts,
                "content": [{"type": "text", "text": m["text"]}],
            }
        )
        parent = uid
    return {"uuid": _nid(), "name": title, "chat_messages": items}


def _as_agent(title: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
    t0 = iso()
    responses = []
    parent = None
    for i, m in enumerate(msgs):
        rid = _nid()
        sender = "user" if m["role"] == "user" else "hermes"
        if m["role"] == "system":
            sender = "user"
        responses.append(
            {
                "response": {
                    "_id": rid,
                    "parent_response_id": parent,
                    "sender": sender,
                    "message": m["text"],
                    "create_time": iso(m.get("ts")),
                    "model": "",
                }
            }
        )
        parent = rid
    return {
        "conversations": [
            {
                "conversation": {
                    "id": _nid(),
                    "title": title,
                    "create_time": t0,
                    "modify_time": t0,
                },
                "responses": responses,
            }
        ]
    }


def to_markdown(title: str, msgs: list[dict[str, Any]]) -> str:
    labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    parts = [f"# {title}", ""]
    for m in msgs:
        parts.append(f"## {labels.get(m['role'], m['role'].title())}")
        parts.append("")
        parts.append(m["text"])
        parts.append("")
    return "\n".join(parts)
