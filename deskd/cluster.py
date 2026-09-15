#!/usr/bin/env python3
"""LAN mesh: peer HTTP, roster merge, reverse proxy, SSE fan-in.

No imports of deskd / deskd.py. Callbacks are injected by main().
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

VERSION = "0.1.0"
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_PEERS = 8
ROSTER_TIMEOUT = 0.35
DM_TIMEOUT = 2.0
HELLO_TIMEOUT = 0.35

_RFC1918 = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
_LINK_LOCAL = ipaddress.IPv4Network("169.254.0.0/16")

SLIM_KEYS = (
    "id",
    "name",
    "description",
    "avatar",
    "avatar_color",
    "avatar_shape",
    "model",
    "effort",
    "models",
    "kind",
    "permission_mode",
    "browser",
    "terminal",
    "host_access",
    "inherit_user_skills",
    "inherit_user_mcp",
    "status",
    "surface",
    "control",
    "context_used",
    "context_window",
    "context_source",
    "tps",
    "speed_source",
    "token_source",
    "workspace_id",
    "can_undo",
    "chat_id",
    "chat",
    "slash_commands",
    "node",
    "remote",
    "node_status",
)

HOP_BY_HOP = {
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
}


def normalize_cluster_token(value: Any) -> str:
    t = str(value or "").strip()
    for ch in ("\r", "\n", "\t", "\u200b", "\u00a0"):
        t = t.replace(ch, "")
    return t.strip()


def cluster_token_fp(token: str) -> str:
    t = normalize_cluster_token(token)
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:8]


def validate_node_name(name: str) -> str:
    n = (name or "").strip()
    if not NODE_NAME_RE.fullmatch(n):
        raise ValueError("node_name must be 1–64 chars [A-Za-z0-9._-]")
    return n


def is_rfc1918_ipv4(host: str) -> bool:
    try:
        ip = ipaddress.IPv4Address((host or "").strip())
    except ipaddress.AddressValueError:
        return False
    return any(ip in net for net in _RFC1918)


def has_lan_peers(peers: Any) -> bool:
    """True if any configured peer URL is an RFC1918 IPv4 (not loopback)."""
    if not isinstance(peers, list):
        return False
    for row in peers:
        url = ""
        if isinstance(row, dict):
            url = str(row.get("url") or "")
        else:
            url = str(getattr(row, "url", "") or "")
        host = urlparse(url).hostname or ""
        if is_rfc1918_ipv4(host):
            return True
    return False


def _ipv4_allowed(ip: ipaddress.IPv4Address) -> bool:
    if ip in _LINK_LOCAL:
        return False
    if ip in _LOOPBACK:
        return True
    return any(ip in net for net in _RFC1918)


def validate_peer_url(url: str, *, self_host: str | None = None, self_port: int | None = None) -> str:
    """Normalize to http://ipv4:port. Reject hostnames, userinfo, TLS, 169.254, self."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("peer url required")
    p = urlparse(raw)
    if p.scheme != "http":
        raise ValueError("peer url must be http")
    if p.username is not None or p.password is not None or "@" in (p.netloc or ""):
        raise ValueError("peer url must not include userinfo")
    host = p.hostname or ""
    if not host:
        raise ValueError("peer url host required")
    if ":" in host:
        raise ValueError("IPv6 peer urls are not allowed")
    try:
        ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as e:
        raise ValueError("peer url host must be an IPv4 literal") from e
    if ip in _LINK_LOCAL:
        raise ValueError("link-local peer url is not allowed")
    if not _ipv4_allowed(ip):
        raise ValueError("peer url must be RFC1918 or loopback")
    port = p.port or 8742
    if not 1 <= int(port) <= 65535:
        raise ValueError("peer port must be 1–65535")
    port = int(port)
    if self_port:
        self_port = int(self_port)
        if host == "127.0.0.1" and port == self_port:
            raise ValueError("peer url must not point at this process")
        sh = (self_host or "").strip()
        if sh and host == sh and port == self_port:
            raise ValueError("peer url must not point at this process")
    return f"http://{host}:{port}"


def bot_id_from_path(path: str) -> str | None:
    """Segment rule matching Handler: parts[2], never parts[-1]."""
    parts = urlparse(path).path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "v1":
        return None
    if parts[1] in ("bots", "agent"):
        return parts[2] or None
    if parts[1] in ("workspaces", "control"):
        wid = parts[2]
        if wid.startswith("ws_") and len(wid) > 3:
            return "b_" + wid[3:]
        if wid.startswith("b_"):
            return wid
        return None
    return None


def timeout_for(path: str, method: str = "GET") -> float:
    p = urlparse(path).path
    m = (method or "GET").upper()
    if m == "POST" and p.endswith("/prompt"):
        return 10.0
    if m == "POST" and p.endswith("/import"):
        return 120.0
    if p.endswith("/export"):
        return 60.0
    if p.endswith("/local-llm/control"):
        return 45.0
    if p.endswith("/browser/frame"):
        return 10.0
    if "/shell/pull" in p or "/tui/pull" in p:
        return 15.0
    if "/cluster/robot/" in p:
        return 0.8
    return 2.0


def _strip_token_query(path: str) -> str:
    p = urlparse(path)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "token"]
    query = urlencode(pairs)
    return urlunparse(("", "", p.path, "", query, ""))


@dataclass
class Peer:
    name: str
    url: str
    status: str = "unconfigured"
    last_err: str = ""
    last_hello_ms: float = 0.0
    latency_ms: float | None = None
    hello_node: str = ""
    inbound_seen: bool = False


class Cluster:
    def __init__(
        self,
        *,
        emit: Callable[[dict], None],
        lookup_local_bot: Callable[[str], Any | None],
        local_profiles: Callable[[], list[dict]],
        is_local_id: Callable[[str], bool],
        load_config: Callable[[], dict[str, Any]] | None = None,
        listen_info: Callable[[], tuple[str, int, str]] | None = None,
        on_cluster_dm: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._emit = emit
        self.lookup_local_bot = lookup_local_bot
        self.local_profiles = local_profiles
        self.is_local_id = is_local_id
        self._on_cluster_dm = on_cluster_dm
        self._load_config = load_config or (lambda: {})
        self._listen_info = listen_info or (lambda: ("127.0.0.1", 8742, "127.0.0.1"))
        self.node_name = socket.gethostname()
        self.token = ""
        self.peers: list[Peer] = []
        self.index: dict[str, str] = {}
        self.roster_cache: dict[str, list] = {}
        self.lock = threading.RLock()
        self._fanin_stop = threading.Event()
        self._fanin_threads: list[threading.Thread] = []
        self._fanin_conns: list[Any] = []
        self.reload()

    def listen_tuple(self) -> tuple[str, int, str]:
        try:
            host, port, access = self._listen_info()
            return str(host), int(port), str(access)
        except Exception:
            return "127.0.0.1", 8742, "127.0.0.1"

    def reload(self, cfg: dict[str, Any] | None = None) -> None:
        data = cfg if cfg is not None else (self._load_config() or {})
        name = str(data.get("node_name") or "").strip() or socket.gethostname()
        try:
            name = validate_node_name(name)
        except ValueError:
            name = re.sub(r"[^A-Za-z0-9._-]", "-", name)[:64] or socket.gethostname()
        token = normalize_cluster_token(data.get("cluster_token") or "")
        raw_peers = data.get("peers") if isinstance(data.get("peers"), list) else []
        if os.environ.get("HERMES_DESK_CLUSTER") == "0":
            raw_peers = []
        listen_host, listen_port, access = self.listen_tuple()
        built: list[Peer] = []
        prev = {p.name: p for p in self.peers}
        for row in raw_peers[:MAX_PEERS]:
            if not isinstance(row, dict):
                continue
            pname = str(row.get("name") or "").strip()
            try:
                pname = validate_node_name(pname)
                url = validate_peer_url(
                    str(row.get("url") or ""),
                    self_host=access or listen_host,
                    self_port=listen_port,
                )
            except ValueError:
                continue
            old = prev.get(pname)
            if old and old.url == url:
                built.append(old)
            else:
                built.append(Peer(name=pname, url=url))
        with self.lock:
            self.node_name = name
            self.token = token
            self.peers = built
            keep = {p.name for p in built}
            self.roster_cache = {k: v for k, v in self.roster_cache.items() if k in keep}
            self.index = {bid: n for bid, n in self.index.items() if n in keep}

    def peer_by_name(self, name: str) -> Peer | None:
        n = (name or "").strip()
        for p in self.peers:
            if p.name == n:
                return p
        return None

    def _named_request(
        self,
        method: str,
        name: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 0.45,
    ) -> dict[str, Any]:
        """HTTP JSON to a configured peer. Missing/down peers are skipped, not raised."""
        peer = self.peer_by_name(name)
        if peer is None:
            return {"ok": False, "skipped": True, "reason": f"peer {name} not configured"}
        if not self.token:
            return {"ok": False, "skipped": True, "reason": "cluster not configured"}
        try:
            code, data, ms = self._json_request(peer, method, path, body=body, timeout=timeout)
        except Exception as e:
            return {"ok": False, "error": str(e), "peer": name, "url": peer.url}
        row = data if isinstance(data, dict) else {"data": data}
        row.setdefault("ok", bool(code is not None and code < 400))
        row["http"] = code
        row["peer"] = name
        row["latency_ms"] = None if ms is None else round(ms, 1)
        if code is not None and code >= 400:
            row["ok"] = False
        return row

    def post_named(
        self,
        name: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 0.45,
    ) -> dict[str, Any]:
        """POST JSON to a configured peer. Missing/down peers are skipped, not raised."""
        return self._named_request("POST", name, path, body=body, timeout=timeout)

    def get_named(
        self,
        name: str,
        path: str,
        timeout: float = 0.45,
    ) -> dict[str, Any]:
        """GET JSON from a configured peer. Missing/down peers are skipped, not raised."""
        return self._named_request("GET", name, path, body=None, timeout=timeout)

    def configured(self) -> bool:
        return bool(self.token)

    def annotate_local(self, profile: dict[str, Any]) -> dict[str, Any]:
        p = dict(profile)
        p["node"] = self.node_name
        p["remote"] = False
        p["node_status"] = "ok"
        return p

    def slim(self, profile: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in SLIM_KEYS:
            if k in profile:
                out[k] = profile[k]
        out.pop("messages", None)
        out.pop("workspace", None)
        return out

    def local_roster(self) -> list[dict[str, Any]]:
        return [self.slim(self.annotate_local(p)) for p in (self.local_profiles() or [])]

    def merge(
        self,
        local: list[dict[str, Any]],
        remote: list[dict[str, Any]],
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        seen = {b.get("id") for b in local if b.get("id")}
        out = list(local)
        for b in remote:
            bid = b.get("id")
            if not bid:
                continue
            if bid in seen:
                node = b.get("node") or ""
                if diagnostics is not None:
                    for d in diagnostics:
                        if d.get("name") == node:
                            d["warning"] = f"id collision on {bid}; local wins"
                continue
            seen.add(bid)
            out.append(b)
        return out

    def merged_roster(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        local = self.local_roster()
        remote, diagnostics = self.fetch_remote_rosters(timeout=ROSTER_TIMEOUT)
        return self.merge(local, remote, diagnostics), diagnostics

    def hello_payload(self) -> dict[str, Any]:
        listen_host, listen_port, access = self.listen_tuple()
        host = access or listen_host
        try:
            n = len(self.local_profiles() or [])
        except Exception:
            n = 0
        return {
            "node": self.node_name,
            "listen_host": host,
            "listen_port": int(listen_port),
            "version": VERSION,
            "bots": n,
            "peer_names": [p.name for p in self.peers],
        }

    def diagnostics_row(self, p: Peer) -> dict[str, Any]:
        return {
            "name": p.name,
            "url": p.url,
            "status": p.status,
            "latency_ms": p.latency_ms,
            "hello_node": p.hello_node or None,
            "inbound_seen": bool(p.inbound_seen),
        }

    def note_inbound(self, ip: str) -> None:
        host = (ip or "").split("%")[0].strip()
        if host.startswith("::ffff:"):
            host = host[7:]
        if not host:
            return
        with self.lock:
            for p in self.peers:
                ph = urlparse(p.url).hostname or ""
                if ph == host:
                    p.inbound_seen = True

    def accept_announce(self, node: str, bots: list[Any]) -> dict[str, Any]:
        name = validate_node_name(str(node or ""))
        peer = self.peer_by_name(name)
        if peer is None:
            raise KeyError(f"unknown peer {name}")
        slimmed: list[dict[str, Any]] = []
        with self.lock:
            for b in bots or []:
                if not isinstance(b, dict) or not b.get("id"):
                    continue
                row = self.slim(b)
                row["remote"] = True
                row["node"] = peer.name
                row["node_status"] = "ok"
                self.index[str(row["id"])] = peer.name
                slimmed.append(row)
            self.roster_cache[peer.name] = slimmed
        return {"ok": True, "accepted": len(slimmed), "node": peer.name}

    def announce_to_peers(self) -> None:
        if not self.token or not self.peers:
            return
        payload = {"node": self.node_name, "bots": self.local_roster()}
        for p in list(self.peers):
            try:
                self._json_request(p, "POST", "/v1/cluster/announce", body=payload, timeout=2.0)
            except Exception:
                continue

    def _peer_host_port(self, peer: Peer) -> tuple[str, int]:
        u = urlparse(peer.url)
        return u.hostname or "127.0.0.1", int(u.port or 8742)

    def _json_request(
        self,
        peer: Peer,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> tuple[int, dict[str, Any] | list | None, float]:
        host, port = self._peer_host_port(peer)
        raw = None if body is None else json.dumps(body).encode()
        hdrs = {
            "X-Hermes-Cluster-Token": self.token,
            "Accept": "application/json",
            "Connection": "close",
        }
        if raw is not None:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(raw))
        t0 = time.monotonic()
        conn = HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request(method, path, body=raw, headers=hdrs)
            resp = conn.getresponse()
            blob = resp.read()
            ms = (time.monotonic() - t0) * 1000
            data: dict[str, Any] | list | None
            try:
                data = json.loads(blob.decode() or "null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None
            return resp.status, data, ms
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _classify(self, status: int | None, err: str | None) -> str:
        if status in (401, 403):
            return "auth"
        if status is None or err or status >= 500:
            return "offline"
        if status >= 400:
            return "offline"
        return "ok"

    def _hello_peer(self, peer: Peer, timeout: float = HELLO_TIMEOUT) -> dict[str, Any]:
        err = ""
        status_code: int | None = None
        data: dict[str, Any] | list | None = None
        ms: float | None = None
        try:
            status_code, data, ms = self._json_request(peer, "GET", "/v1/cluster/hello", timeout=timeout)
        except Exception as e:
            err = str(e)
        kind = self._classify(status_code, err or None)
        hello = data if isinstance(data, dict) else {}
        hello_node = str(hello.get("node") or "")
        with self.lock:
            peer.last_hello_ms = time.time()
            peer.latency_ms = None if ms is None or kind != "ok" else round(ms, 1)
            peer.last_err = err or ("" if kind == "ok" else f"HTTP {status_code}")
            peer.hello_node = hello_node
            if kind == "ok" and hello_node and hello_node != peer.name:
                peer.status = "name_mismatch"
            else:
                peer.status = kind
        return {
            "status": peer.status,
            "forward": kind,
            "hello": hello,
            "latency_ms": peer.latency_ms,
            "hello_node": hello_node or None,
        }

    def list_links(self) -> dict[str, Any]:
        """Hello this node's peers[] only. Query/body ignored by the caller."""
        links = []
        for p in list(self.peers):
            info = self._hello_peer(p)
            links.append(
                {
                    "name": p.name,
                    "status": info["forward"],
                    "latency_ms": info["latency_ms"],
                    "hello_node": info["hello_node"],
                }
            )
        return {"node": self.node_name, "links": links}

    def self_test(self) -> dict[str, Any]:
        results = []
        for p in list(self.peers):
            info = self._hello_peer(p)
            hello = info.get("hello") or {}
            peer_names = hello.get("peer_names") if isinstance(hello, dict) else None
            if not isinstance(peer_names, list):
                peer_names = []
            reverse_config = self.node_name in [str(x) for x in peer_names]
            reverse = "skipped"
            if info["forward"] == "ok":
                try:
                    code, data, _ms = self._json_request(p, "GET", "/v1/cluster/links", timeout=HELLO_TIMEOUT)
                    kind = self._classify(code, None)
                    reverse = kind
                    if kind == "ok" and isinstance(data, dict):
                        rows = data.get("links") or []
                        hit = next((r for r in rows if isinstance(r, dict) and r.get("name") == self.node_name), None)
                        if hit:
                            reverse = str(hit.get("status") or kind)
                        else:
                            reverse = "offline"
                except Exception:
                    reverse = "offline"
            results.append(
                {
                    "name": p.name,
                    "url": p.url,
                    "latency_ms": info["latency_ms"],
                    "hello_node": info["hello_node"],
                    "forward": info["forward"],
                    "reverse": reverse,
                    "reverse_config": reverse_config if info["forward"] == "ok" else False,
                    "warning": (
                        f"hello_node {info['hello_node']!r} != configured {p.name!r}"
                        if info["hello_node"] and info["hello_node"] != p.name
                        else (None if reverse_config or info["forward"] != "ok" else "missing_peer")
                    ),
                }
            )
        hint = None
        if not self.token:
            hint = "No cluster token is saved on this host. Paste or Generate, then Save. Test uses the last Save, not the text in the boxes."
        elif not self.peers:
            hint = "Token is saved, but there are no peers. Add a peer (name + http://<LAN-IP>:8742), Save, then Test."
        return {
            "node": self.node_name,
            "token_set": bool(self.token),
            "peer_count": len(self.peers),
            "hint": hint,
            "results": results,
        }

    def fetch_remote_rosters(self, timeout: float = ROSTER_TIMEOUT) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.token or not self.peers:
            return [], []
        collected: dict[str, list[dict[str, Any]]] = {}
        diagnostics: list[dict[str, Any]] = []
        threads: list[threading.Thread] = []

        def _one(peer: Peer) -> None:
            bots: list[dict[str, Any]] = []
            err = ""
            code: int | None = None
            ms: float | None = None
            try:
                code, data, ms = self._json_request(peer, "GET", "/v1/cluster/bots", timeout=timeout)
                if code == 200 and isinstance(data, dict):
                    rows = data.get("bots") or []
                    if isinstance(rows, list):
                        bots = [r for r in rows if isinstance(r, dict)]
                else:
                    err = f"HTTP {code}"
            except Exception as e:
                err = str(e)
            kind = self._classify(code, err or None)
            with self.lock:
                peer.last_err = err
                peer.latency_ms = None if ms is None or kind != "ok" else round(ms, 1)
                if kind == "ok":
                    peer.status = "ok"
                    node = peer.name
                    slimmed = []
                    for b in bots:
                        row = self.slim(b)
                        row["remote"] = True
                        row["node"] = node
                        row["node_status"] = "ok"
                        bid = row.get("id")
                        if bid:
                            self.index[str(bid)] = peer.name
                        slimmed.append(row)
                    self.roster_cache[peer.name] = slimmed
                    collected[peer.name] = slimmed
                else:
                    peer.status = kind
                    cached = self.roster_cache.get(peer.name) or []
                    stale = []
                    for b in cached:
                        row = dict(b)
                        row["remote"] = True
                        row["node"] = peer.name
                        row["node_status"] = "offline" if kind == "offline" else kind
                        stale.append(row)
                    collected[peer.name] = stale

        for p in list(self.peers):
            t = threading.Thread(target=_one, args=(p,), daemon=True)
            threads.append(t)
            t.start()
        deadline = time.monotonic() + timeout + 0.05
        for t in threads:
            remain = max(0.0, deadline - time.monotonic())
            t.join(timeout=remain)
        remote: list[dict[str, Any]] = []
        for p in list(self.peers):
            remote.extend(collected.get(p.name) or [])
            diagnostics.append(self.diagnostics_row(p))
        return remote, diagnostics

    def owner(self, bot_id: str) -> Any:
        bid = (bot_id or "").strip()
        if not bid:
            return None
        if self.is_local_id(bid):
            return "local"
        with self.lock:
            name = self.index.get(bid)
        if name:
            p = self.peer_by_name(name)
            if p:
                return p
        self.fetch_remote_rosters(timeout=ROSTER_TIMEOUT)
        with self.lock:
            name = self.index.get(bid)
        if name:
            p = self.peer_by_name(name)
            if p:
                return p
        return None

    def find_remote(self, spec: str) -> tuple[Peer, dict[str, Any]] | None:
        """Resolve a remote bot by id, else case-insensitive name in peers[] order."""
        spec = (spec or "").strip()
        if not spec:
            return None

        def _scan() -> tuple[Peer, dict[str, Any]] | None:
            with self.lock:
                for p in self.peers:
                    for b in self.roster_cache.get(p.name) or []:
                        if b.get("id") == spec:
                            return p, b
                low = spec.lower()
                for p in self.peers:
                    for b in self.roster_cache.get(p.name) or []:
                        if str(b.get("name") or "").lower() == low:
                            return p, b
            own = self.owner(spec) if spec.startswith("b_") else None
            if isinstance(own, Peer):
                with self.lock:
                    for b in self.roster_cache.get(own.name) or []:
                        if b.get("id") == spec:
                            return own, b
                return own, {"id": spec, "name": spec}
            return None

        hit = _scan()
        if hit:
            return hit
        self.fetch_remote_rosters(timeout=ROSTER_TIMEOUT)
        return _scan()

    def forward_dm(self, payload: dict[str, Any]) -> dict[str, Any]:
        to = str(payload.get("to") or "").strip()
        found = self.find_remote(to)
        if not found:
            raise KeyError(f"no bot named {to}")
        peer, bot = found
        body = {
            "from_id": payload.get("from_id"),
            "from_name": payload.get("from_name"),
            "from_node": payload.get("from_node") or self.node_name,
            "to": bot.get("id") or to,
            "text": payload.get("text"),
        }
        code, data = None, None
        try:
            code, data, _ms = self._json_request(peer, "POST", "/v1/cluster/dm", body=body, timeout=DM_TIMEOUT)
        except Exception:
            code = None
        if code in (401, 403):
            raise PermissionError("cluster auth failed")
        if code is not None and code < 400:
            out = data if isinstance(data, dict) else {}
            out.setdefault("ok", True)
            out.setdefault("to", bot.get("id") or to)
            out.setdefault("to_name", bot.get("name") or to)
            out.setdefault("node", peer.name)
            return out
        # Peer cannot be reached from here (common: body listens on 127.0.0.1
        # but can still dial us). Relay over /v1/cluster/events that peer already opened.
        self._emit(
            {
                "type": "cluster.dm",
                "to": body["to"],
                "from_id": body["from_id"],
                "from_name": body["from_name"],
                "from_node": body["from_node"],
                "text": body["text"],
            }
        )
        return {
            "ok": True,
            "to": bot.get("id") or to,
            "to_name": bot.get("name") or to,
            "node": peer.name,
            "via": "relay",
        }

    def create_on_peer(self, peer: Peer, body: dict[str, Any]) -> dict[str, Any]:
        if peer.status == "offline":
            raise ConnectionError("peer offline")
        try:
            code, data, _ms = self._json_request(peer, "POST", "/v1/bots", body=body, timeout=10.0)
        except Exception as e:
            raise ConnectionError(str(e)) from e
        if code in (401, 403):
            raise PermissionError("cluster auth failed")
        if code >= 400:
            err = ""
            if isinstance(data, dict):
                err = str(data.get("error") or "")
            raise ValueError(err or f"HTTP {code}")
        out = data if isinstance(data, dict) else {}
        bid = ""
        if isinstance(out.get("bot"), dict):
            bid = str(out["bot"].get("id") or "")
        bid = bid or str(out.get("bot_id") or "")
        if bid:
            with self.lock:
                self.index[bid] = peer.name
        return out

    def peer_models(self, peer: Peer) -> dict[str, Any]:
        try:
            code, data, _ms = self._json_request(peer, "GET", "/v1/models", timeout=2.0)
        except Exception as e:
            raise ConnectionError(str(e)) from e
        if code in (401, 403):
            raise PermissionError("cluster auth failed")
        if code >= 400 or not isinstance(data, dict):
            raise RuntimeError(f"HTTP {code}")
        return data

    def _refresh_remote_profile(self, peer: Peer, bot_id: str | None) -> None:
        if not bot_id:
            return
        try:
            code, data, _ms = self._json_request(peer, "GET", f"/v1/bots/{bot_id}", timeout=2.0)
        except Exception:
            return
        if code != 200 or not isinstance(data, dict):
            return
        data = dict(data)
        data["remote"] = True
        data["node"] = peer.name
        data["node_status"] = "ok"
        self._emit({"type": "bot.updated", "bot": data, "origin_node": peer.name, "proxied": True})

    def proxy(self, handler: Any, peer: Peer, timeout: float = 2.0) -> None:
        host, port = self._peer_host_port(peer)
        src = urlparse(handler.path)
        target = _strip_token_query(handler.path)
        n = int(handler.headers.get("Content-Length") or "0")
        hdrs = {
            "X-Hermes-Cluster-Token": self.token,
            "Accept": handler.headers.get("Accept") or "*/*",
            "Connection": "close",
        }
        ctype = handler.headers.get("Content-Type")
        if ctype:
            hdrs["Content-Type"] = ctype
        for h in ("If-None-Match", "X-View-Width", "X-View-Height"):
            v = handler.headers.get(h)
            if v:
                hdrs[h] = v
        if n > 0:
            hdrs["Content-Length"] = str(n)
        t0 = time.monotonic()
        conn: HTTPConnection | None = None
        try:
            conn = HTTPConnection(host, port, timeout=timeout)
            conn.putrequest(handler.command, target or src.path, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            remain = n
            while remain > 0:
                chunk = handler.rfile.read(min(65536, remain))
                if not chunk:
                    break
                conn.send(chunk)
                remain -= len(chunk)
            resp = conn.getresponse()
        except Exception as e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            handler._json(503, {"error": str(e), "node": peer.name, "peer": peer.url})
            return
        status = resp.status
        if status in (401, 403):
            try:
                resp.read()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            handler._json(502, {"error": "cluster auth failed"})
            return
        try:
            handler.send_response(status)
            sent_cc = False
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                if k.lower() == "cache-control":
                    sent_cc = True
                handler.send_header(k, v)
            if not sent_cc:
                handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            if status not in (204, 304):
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        ms = int((time.monotonic() - t0) * 1000)
        print(f"[deskd] proxy {peer.name} {handler.command} {src.path} {status} {ms}ms", file=sys.stderr)
        if handler.command == "POST" and src.path.endswith("/prompt") and status == 200:
            bid = bot_id_from_path(src.path)
            threading.Thread(target=self._refresh_remote_profile, args=(peer, bid), daemon=True).start()

    def _ingest_event(self, peer: Peer, ev: dict[str, Any]) -> None:
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "cluster.dm":
            if self._on_cluster_dm:
                try:
                    self._on_cluster_dm(ev)
                except Exception:
                    pass
            return
        tagged = {**ev, "origin_node": peer.name, "proxied": True}
        bot = tagged.get("bot") if isinstance(tagged.get("bot"), dict) else {}
        bid = str(bot.get("id") or tagged.get("bot_id") or "")
        if t in ("bot.created", "bot.updated") and bid:
            with self.lock:
                self.index[bid] = tagged.get("origin_node") or peer.name
        elif t == "bot.deleted" and bid:
            with self.lock:
                self.index.pop(bid, None)
        self._emit(tagged)

    def _fanin_once(self, peer: Peer) -> str:
        if not self.token:
            return "unconfigured"
        host, port = self._peer_host_port(peer)
        conn = HTTPConnection(host, port, timeout=30)
        with self.lock:
            self._fanin_conns.append(conn)
        try:
            conn.request(
                "GET",
                "/v1/cluster/events",
                headers={
                    "X-Hermes-Cluster-Token": self.token,
                    "Accept": "text/event-stream",
                    "Connection": "close",
                    "Cache-Control": "no-cache",
                },
            )
            resp = conn.getresponse()
            if resp.status in (401, 403):
                with self.lock:
                    peer.status = "auth"
                    peer.last_err = f"HTTP {resp.status}"
                self._emit({"type": "cluster.peer", "node": peer.name, "status": "auth", "proxied": True})
                return "auth"
            if resp.status != 200:
                with self.lock:
                    peer.status = "offline"
                    peer.last_err = f"HTTP {resp.status}"
                self._emit({"type": "cluster.peer", "node": peer.name, "status": "offline", "proxied": True})
                return "offline"
            with self.lock:
                peer.status = "ok"
                peer.last_err = ""
            self._emit({"type": "cluster.peer", "node": peer.name, "status": "ok", "proxied": True})
            while not self._fanin_stop.is_set():
                try:
                    raw = resp.readline()
                except Exception:
                    break
                if raw == b"":
                    break
                try:
                    line = raw.decode("utf-8", errors="replace").strip("\r\n")
                except Exception:
                    continue
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].lstrip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._ingest_event(peer, ev)
            with self.lock:
                if peer.status == "ok":
                    peer.status = "offline"
            self._emit({"type": "cluster.peer", "node": peer.name, "status": "offline", "proxied": True})
            return "offline"
        except Exception as e:
            with self.lock:
                peer.status = "offline"
                peer.last_err = str(e)
            self._emit({"type": "cluster.peer", "node": peer.name, "status": "offline", "proxied": True})
            return "offline"
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self.lock:
                try:
                    self._fanin_conns.remove(conn)
                except ValueError:
                    pass

    def _fanin_loop(self, peer_name: str) -> None:
        delay = 0.5
        while not self._fanin_stop.is_set():
            peer = self.peer_by_name(peer_name)
            if peer is None or not self.token:
                return
            kind = self._fanin_once(peer)
            if self._fanin_stop.is_set():
                return
            if kind == "ok":
                delay = 0.5
            else:
                time.sleep(delay)
                delay = min(8.0, max(0.5, delay * 2))

    def _announce_loop(self) -> None:
        while not self._fanin_stop.is_set():
            try:
                self.announce_to_peers()
            except Exception:
                pass
            self._fanin_stop.wait(15.0)

    def start_fanin(self) -> None:
        self.stop_fanin()
        if not self.token or not self.peers:
            return
        self._fanin_stop = threading.Event()
        for p in list(self.peers):
            t = threading.Thread(target=self._fanin_loop, args=(p.name,), daemon=True, name=f"fanin-{p.name}")
            self._fanin_threads.append(t)
            t.start()
        t = threading.Thread(target=self._announce_loop, daemon=True, name="cluster-announce")
        self._fanin_threads.append(t)
        t.start()

    def stop_fanin(self) -> None:
        self._fanin_stop.set()
        with self.lock:
            conns = list(self._fanin_conns)
            self._fanin_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        for t in self._fanin_threads:
            t.join(timeout=1.0)
        self._fanin_threads = []
