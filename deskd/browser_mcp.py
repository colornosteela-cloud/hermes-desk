#!/usr/bin/env python3
"""MCP stdio helper: bot_browser tools talk to hermes-deskd."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BOT = ""
BASE = os.environ.get("HERMES_DESK_URL", "http://127.0.0.1:8742")
TOKEN = os.environ.get("HERMES_DESK_TOKEN", "")


def _http(method: str, path: str, body: dict | None = None, timeout: int = 45) -> dict:
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
        return {"error": e.read().decode()[:400]}
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
        "name": "web_search",
        "description": (
            "Search the web in this bot's live Chromium. The user sees the results on the Agent Computer. "
            "Always use this for current information; do not claim you cannot browse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "navigate",
        "description": (
            "Open a URL, domain, or workspace file (e.g. Desktop/hello.html) in this bot's Chromium. "
            "The user sees it live on the bot's screen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "open_local_page",
        "description": (
            "Open an HTML file from this workspace in the live browser (path relative to the workspace, "
            "e.g. Desktop/hello.html or www/index.html). Write the file first, then call this so the user sees it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Read the current page title, URL, and visible text from the live Chromium (the user already sees the pixels).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click",
        "description": "Click a CSS selector on the current page in the live browser.",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type into the focused field on the live page. Set submit true to press Enter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "submit": {"type": "boolean"},
            },
            "required": ["text"],
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
                    "serverInfo": {"name": "bot_browser", "version": "0.2"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "web_search":
                q = args.get("query") or args.get("q") or ""
                res = _http("POST", f"/v1/bots/{BOT}/browser/search", {"query": q})
                text = (
                    f"Searched the live browser for {q!r}. "
                    f"URL: {res.get('url')}. Title: {res.get('title')}. "
                    f"Visible text:\n{(res.get('text') or '')[:1800]}"
                )
            elif name == "navigate":
                res = _http("POST", f"/v1/bots/{BOT}/browser/navigate", {"url": args.get("url") or "about:blank"})
                text = (
                    f"Opened {res.get('url')} on the bot screen. Title: {res.get('title')}. "
                    f"{(res.get('text') or '')[:1200]}"
                )
            elif name == "open_local_page":
                res = _http("POST", f"/v1/bots/{BOT}/browser/open", {"path": args.get("path") or ""})
                text = (
                    f"Opened local page {args.get('path')} at {res.get('url')} on the bot screen. "
                    f"Title: {res.get('title')}."
                )
            elif name == "browser_snapshot":
                res = _http("GET", f"/v1/bots/{BOT}/browser/snapshot")
                text = (
                    f"Browser showing {res.get('url')} — {res.get('title')}.\n"
                    f"{(res.get('text') or '')[:2500]}"
                )
            elif name == "browser_click":
                res = _http("POST", f"/v1/bots/{BOT}/browser/click", {"selector": args.get("selector") or ""})
                text = f"Click: {res}"
            elif name == "browser_type":
                res = _http(
                    "POST",
                    f"/v1/bots/{BOT}/browser/type",
                    {"text": args.get("text") or "", "submit": bool(args.get("submit"))},
                )
                text = f"Typed on the live page. {res}"
            else:
                text = f"unknown tool {name}"
            reply(mid, {"content": [{"type": "text", "text": text}]})
        elif mid is not None:
            reply(mid, error=f"unknown method {method}")


if __name__ == "__main__":
    main()
