#!/usr/bin/env python3
"""MCP: memory_write / memory_retrieve — loopback only, this bot's store."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BOT = ""
BASE = os.environ.get("HERMES_DESK_URL", "http://127.0.0.1:8742")
TOKEN = os.environ.get("HERMES_DESK_TOKEN", "")

TOOLS = [
    {
        "name": "memory_write",
        "description": (
            "Persist a durable project fact in this bot's long-term memory. "
            "Use for architectural decisions, dead ends worth remembering across "
            "sessions, and anything expensive to re-derive. Do not store current "
            "file contents or test output. Optional media_path is a workspace path "
            "to a screenshot (copied) or a video artifact (pointer only)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The fact, or a caption for media"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords for FTS5 retrieval",
                },
                "media_path": {
                    "type": "string",
                    "description": "Workspace-relative path to image or video",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video"],
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_retrieve",
        "description": (
            "Search this bot's long-term facts with FTS5. Returns text, tags, "
            "and optional media_path — never image or video bytes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
]


def _http(method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500]}


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = {"code": -32000, "message": str(error)}
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main() -> None:
    global BOT
    args = sys.argv[1:]
    if "--bot" in args:
        BOT = args[args.index("--bot") + 1]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            reply(
                mid,
                {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bot_memory", "version": "0.1"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            targs = params.get("arguments") or {}
            if not BOT:
                reply(mid, error="bot id missing from MCP process")
                continue
            if name == "memory_write":
                res = _http(
                    "POST",
                    f"/v1/memory/{BOT}/write",
                    {
                        "text": targs.get("text"),
                        "tags": targs.get("tags") or [],
                        "media_path": targs.get("media_path"),
                        "media_type": targs.get("media_type"),
                    },
                )
                text = json.dumps(res)
            elif name == "memory_retrieve":
                res = _http(
                    "POST",
                    f"/v1/memory/{BOT}/retrieve",
                    {
                        "query": targs.get("query"),
                        "limit": targs.get("limit") or 8,
                    },
                )
                text = json.dumps(res)
            else:
                text = f"unknown tool {name}"
            reply(mid, {"content": [{"type": "text", "text": text}]})
        elif mid is not None:
            reply(mid, error=f"unknown method {method}")


if __name__ == "__main__":
    main()
