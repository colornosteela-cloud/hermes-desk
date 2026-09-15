#!/usr/bin/env python3
"""MCP: list / register / start / stop local models for Hermes Agent bots."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BOT = ""
BASE = os.environ.get("HERMES_DESK_URL", "http://127.0.0.1:8742")
TOKEN = os.environ.get("HERMES_DESK_TOKEN", "")


def _http(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:800]}
    except Exception as e:
        return {"error": str(e)}


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
        "name": "list_local_models",
        "description": "List picker models on this host: cloud and local, which are running, weights paths, and base URLs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "register_local_model",
        "description": "After downloading weights into ~/models, add the model to the user's picker. They can then start and select it. id is the picker key. weights is a folder or GGUF under ~/models. base_url optional (default next free 127.0.0.1 port /v1).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "weights": {"type": "string"},
                "base_url": {"type": "string"},
                "model": {"type": "string"},
                "context_window": {"type": "integer"},
            },
            "required": ["id", "weights"],
        },
    },
    {
        "name": "start_local_model",
        "description": "Start serving a registered local model so the user can select it.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "stop_local_model",
        "description": "Stop a running local model without removing it from the picker.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]


def main() -> None:
    global BOT
    argv = sys.argv[1:]
    if "--bot" in argv:
        BOT = argv[argv.index("--bot") + 1]
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
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
                    "serverInfo": {"name": "desk_models", "version": "0.1.0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "list_local_models":
                res = _http("GET", "/v1/local-llm/status")
            elif name == "register_local_model":
                res = _http(
                    "POST",
                    "/v1/local-llm/control",
                    {
                        "action": "register",
                        "id": args.get("id"),
                        "name": args.get("name") or args.get("id"),
                        "weights": args.get("weights"),
                        "base_url": args.get("base_url"),
                        "model": args.get("model") or args.get("id"),
                        "context_window": args.get("context_window"),
                    },
                )
            elif name == "start_local_model":
                res = _http(
                    "POST",
                    "/v1/local-llm/control",
                    {"action": "start", "id": args.get("id")},
                    timeout=90,
                )
            elif name == "stop_local_model":
                res = _http(
                    "POST",
                    "/v1/local-llm/control",
                    {"action": "stop", "id": args.get("id")},
                    timeout=60,
                )
            else:
                res = {"error": f"unknown tool {name}"}
            text = json.dumps(res, indent=2)[:30000]
            reply(mid, {"content": [{"type": "text", "text": text}]})
        elif mid is not None:
            reply(mid, error=f"unknown method {method}")


if __name__ == "__main__":
    main()
