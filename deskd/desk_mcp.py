#!/usr/bin/env python3
"""MCP: bots list / message / create — Hermes Bot style teammates, isolated desks."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BOT = ""
BASE = os.environ.get("HERMES_DESK_URL", "http://127.0.0.1:8742")
TOKEN = os.environ.get("HERMES_DESK_TOKEN", "")


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


TOOLS = [
    {
        "name": "list_teammates",
        "description": "List other bots on this LAN desk cluster (pass bot id; names collide local-first then peers-list order). They have separate workspaces and memories.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "message_teammate",
        "description": "Send a message to another bot by id (preferred) or exact bot name from list_teammates. Do not use a computer/node name like teela-body as `to`. Names may collide across nodes (local-first, then peers order). They receive it in their own conversation. Do not assume shared files unless list_shared_desks says you were granted read access.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Bot name or id"},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "create_teammate",
        "description": "Create a new named helper bot on THIS computer with its own SOUL and workspace. Default kind is hermes. Only one teela-brain is allowed per computer — do not create a second Teela Brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "soul": {"type": "string"},
                "kind": {
                    "type": "string",
                    "description": "hermes (default, full Hermes Agent tools). teela-brain is rejected if one already exists.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "delete_teammate",
        "description": "Delete a helper bot on THIS computer by id or exact name. Cannot delete yourself, Teela Brain, or a remote bot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Bot id or exact name"},
            },
            "required": ["to"],
        },
    },
    {
        "name": "list_shared_desks",
        "description": "List teammate workspaces the user granted you read access to. Empty unless permission was given in Edit Agent → Workspace sharing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_shared_files",
        "description": "List files in a teammate workspace you were granted read access to. path is relative to that workspace (default '.').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "string", "description": "Owner bot id from list_shared_desks"},
                "path": {"type": "string", "description": "Relative folder, default workspace root"},
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "read_shared_file",
        "description": "Read a text file from a teammate workspace you were granted read access to. Private .memory is never readable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["bot_id", "path"],
        },
    },
    {
        "name": "request_workspace_share",
        "description": "Ask the user to grant you read-only access to another bot's workspace. The user must approve in Edit Agent → Workspace sharing. Do not claim you can see their files until list_shared_desks includes them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "string", "description": "Owner bot id or exact name"},
            },
            "required": ["bot_id"],
        },
    },
]


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
                    "serverInfo": {"name": "desk_team", "version": "0.1"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "list_teammates":
                res = _http("GET", "/v1/bots")
                others = [
                    {
                        "id": b.get("id"),
                        "name": b.get("name"),
                        "description": b.get("description"),
                        "node": b.get("node"),
                        "remote": b.get("remote"),
                        "can_read_workspace": BOT in (b.get("workspace_share_with") or []),
                    }
                    for b in (res.get("bots") or [])
                    if b.get("id") != BOT
                ]
                text = json.dumps({"teammates": others}, indent=2)
            elif name == "message_teammate":
                res = _http(
                    "POST",
                    f"/v1/bots/{BOT}/dm",
                    {"to": args.get("to"), "text": args.get("text")},
                )
                text = json.dumps(res)
            elif name == "create_teammate":
                res = _http(
                    "POST",
                    "/v1/bots",
                    {
                        "name": args.get("name"),
                        "description": args.get("description"),
                        "soul": args.get("soul") or "",
                        "kind": args.get("kind") or "hermes",
                    },
                )
                text = json.dumps(res)
            elif name == "delete_teammate":
                dest = str(args.get("to") or args.get("name") or "").strip()
                listed = _http("GET", "/v1/bots")
                bid = dest
                for b in listed.get("bots") or []:
                    if b.get("id") == dest or str(b.get("name") or "").lower() == dest.lower():
                        bid = b.get("id") or dest
                        break
                res = _http("DELETE", f"/v1/bots/{bid}")
                text = json.dumps(res)
            elif name == "list_shared_desks":
                res = _http("GET", f"/v1/bots/{BOT}/shared-desks")
                text = json.dumps(res, indent=2)
            elif name == "list_shared_files":
                owner = urllib.parse.quote(str(args.get("bot_id") or ""))
                rel = urllib.parse.quote(str(args.get("path") or "."))
                res = _http("GET", f"/v1/bots/{BOT}/shared-files?owner={owner}&path={rel}")
                text = json.dumps(res, indent=2)
            elif name == "read_shared_file":
                owner = urllib.parse.quote(str(args.get("bot_id") or ""))
                rel = urllib.parse.quote(str(args.get("path") or ""))
                res = _http("GET", f"/v1/bots/{BOT}/shared-file?owner={owner}&path={rel}")
                text = json.dumps(res, indent=2)
            elif name == "request_workspace_share":
                res = _http(
                    "POST",
                    f"/v1/bots/{BOT}/workspace-shares/request",
                    {"owner": args.get("bot_id")},
                )
                text = json.dumps(res, indent=2)
            else:
                text = f"unknown tool {name}"
            reply(mid, {"content": [{"type": "text", "text": text}]})
        elif mid is not None:
            reply(mid, error=f"unknown method {method}")


if __name__ == "__main__":
    main()
