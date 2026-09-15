#!/usr/bin/env python3
"""bot_id_from_path, timeouts, URL validation, reverse proxy."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
from cluster import (  # noqa: E402
    Cluster,
    bot_id_from_path,
    timeout_for,
    validate_peer_url,
)


class LlmProbeTests(unittest.TestCase):
    def test_probe_uses_urllib_not_httpconnection(self) -> None:
        import inspect

        src = inspect.getsource(d._probe_one_llm)
        self.assertIn("urllib.request", src)
        self.assertIn("urlopen", src)
        self.assertNotIn("http.client", src)


class PathTests(unittest.TestCase):
    def test_nested_bot_paths(self) -> None:
        bid = "b_abc123def456"
        cases = [
            f"/v1/bots/{bid}",
            f"/v1/bots/{bid}/chats/c_xxx/open",
            f"/v1/bots/{bid}/browser/frame",
            f"/v1/agent/{bid}/prompt",
            f"/v1/workspaces/ws_{bid[2:]}/raw",
            f"/v1/control/ws_{bid[2:]}",
        ]
        for p in cases:
            self.assertEqual(bot_id_from_path(p), bid, p)
        self.assertIsNone(bot_id_from_path("/v1/bots"))
        self.assertIsNone(bot_id_from_path("/v1/settings"))
        self.assertNotEqual(bot_id_from_path(f"/v1/bots/{bid}/chats/c_xxx/open"), "open")
        self.assertNotEqual(bot_id_from_path(f"/v1/bots/{bid}/browser/frame"), "frame")
        self.assertNotEqual(bot_id_from_path(f"/v1/workspaces/ws_{bid[2:]}/raw"), "raw")

    def test_timeouts(self) -> None:
        self.assertEqual(timeout_for("/v1/agent/b_x/prompt", "POST"), 10.0)
        self.assertEqual(timeout_for("/v1/bots/b_x/import", "POST"), 120.0)
        self.assertEqual(timeout_for("/v1/bots/b_x/export", "GET"), 60.0)
        self.assertEqual(timeout_for("/v1/local-llm/control", "POST"), 45.0)
        self.assertEqual(timeout_for("/v1/bots/b_x/browser/frame", "GET"), 10.0)
        self.assertEqual(timeout_for("/v1/bots/b_x/shell/pull", "GET"), 15.0)
        self.assertEqual(timeout_for("/v1/bots/b_x/tui/pull", "GET"), 15.0)
        self.assertEqual(timeout_for("/v1/bots/b_x", "GET"), 2.0)

    def test_url_validation(self) -> None:
        self.assertEqual(validate_peer_url("http://10.0.0.20:8742/foo"), "http://10.0.0.20:8742")
        self.assertEqual(validate_peer_url("http://192.168.1.5"), "http://192.168.1.5:8742")
        self.assertEqual(validate_peer_url("http://172.16.0.8:9"), "http://172.16.0.8:9")
        self.assertEqual(validate_peer_url("http://127.0.0.1:18742"), "http://127.0.0.1:18742")
        with self.assertRaises(ValueError):
            validate_peer_url("https://10.0.0.20:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://teela-body.local:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://user:pass@10.0.0.20:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://169.254.169.254/")
        with self.assertRaises(ValueError):
            validate_peer_url("http://8.8.8.8:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://[::1]:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://10.0.0.20:8742", self_host="10.0.0.20", self_port=8742)
        with self.assertRaises(ValueError):
            validate_peer_url("http://127.0.0.1:8742", self_host="10.0.0.10", self_port=8742)


class FakeOwner(BaseHTTPRequestHandler):
    token = "mesh-secret"
    last_import: bytes | None = None
    last_path = ""
    last_query = ""

    def log_message(self, fmt, *args):
        return

    def _auth(self) -> bool:
        return (self.headers.get("X-Hermes-Cluster-Token") or "") == self.token

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        FakeOwner.last_path = urlparse(self.path).path
        FakeOwner.last_query = urlparse(self.path).query
        if not self._auth():
            self.send_response(401)
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path.endswith("/browser/frame"):
            etag = '"7"'
            extra = {"ETag": etag, "X-Frame-Seq": "7", "X-View-Width": "1280", "X-View-Height": "800"}
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            body = b"\xff\xd8fakejpeg"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return
        if path.endswith("/export"):
            body = b"PK\x03\x04zip-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="chat.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/v1/bots/") and path.count("/") == 3:
            self._json(200, {"id": "b_remote000001", "name": "wrench", "messages": [{"role": "user", "text": "hi"}]})
            return
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(n)
        if not self._auth():
            self.send_response(401)
            self.end_headers()
            return
        self._json(200, {"ok": True, "revision": 1, "bot": {"id": "b_remote000001", "soul": "ok"}})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(n) if n else b""
        FakeOwner.last_import = raw
        if not self._auth():
            self.send_response(401)
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path.endswith("/import"):
            self._json(200, {"ok": True, "imported": 0, "bytes": len(raw)})
            return
        if path.endswith("/dm") or path == "/v1/cluster/dm":
            self._json(200, {"ok": True, "to": "b_remote000001", "to_name": "wrench", "node": "teela-body"})
            return
        self.send_response(404)
        self.end_headers()


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name)
        cls.desk = cls.home / "desk.json"
        cls.token_path = cls.home / "token"
        cls.token_path.write_text("ui-secret\n", encoding="utf-8")
        cls._orig_path = d.desk_config_path
        cls._orig_token = d.TOKEN_PATH
        cls._orig_cluster = d.cluster
        cls._orig_h = d.LISTEN_HOST
        cls._orig_p = d.LISTEN_PORT
        d.desk_config_path = lambda: cls.desk
        d.TOKEN_PATH = cls.token_path
        cls.peer = ThreadingHTTPServer(("127.0.0.1", 0), FakeOwner)
        cls.peer_port = cls.peer.server_address[1]
        threading.Thread(target=cls.peer.serve_forever, daemon=True).start()
        cls.desk.write_text(
            json.dumps(
                {
                    "listen_host": "127.0.0.1",
                    "listen_port": 8742,
                    "node_name": "teela-brain",
                    "cluster_token": "mesh-secret",
                    "peers": [{"name": "teela-body", "url": f"http://127.0.0.1:{cls.peer_port}"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        d.cluster = Cluster(
            emit=d.emit,
            lookup_local_bot=lambda s: d.bots.get(s),
            local_profiles=lambda: [b.profile() for b in d.bots.values()],
            is_local_id=lambda bid: bid in d.bots,
            load_config=d.load_desk_config,
            listen_info=lambda: (d.LISTEN_HOST, int(d.LISTEN_PORT), "127.0.0.1"),
        )
        d.cluster.index["b_remote000001"] = "teela-body"
        cls.httpd = d.DeskHTTPServer(("127.0.0.1", 0), d.Handler)
        cls.port = cls.httpd.server_address[1]
        d.LISTEN_HOST = "127.0.0.1"
        d.LISTEN_PORT = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        for s in (cls.httpd, cls.peer):
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        d.desk_config_path = cls._orig_path
        d.TOKEN_PATH = cls._orig_token
        d.cluster = cls._orig_cluster
        d.LISTEN_HOST = cls._orig_h
        d.LISTEN_PORT = cls._orig_p
        cls.tmp.cleanup()

    def _raw(self, method, path, headers=None, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = None if body is None else (body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode())
        hdrs = {"Authorization": "Bearer ui-secret", **(headers or {})}
        if raw is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(raw))
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        blob = resp.read()
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, blob, headers_out

    def test_frame_304(self) -> None:
        st, body, hdrs = self._raw("GET", "/v1/bots/b_remote000001/browser/frame")
        self.assertEqual(st, 200)
        self.assertTrue(body.startswith(b"\xff\xd8") or body)
        self.assertEqual(hdrs.get("etag"), '"7"')
        st, body, hdrs = self._raw(
            "GET",
            "/v1/bots/b_remote000001/browser/frame",
            headers={"If-None-Match": '"7"'},
        )
        self.assertEqual(st, 304)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs.get("etag"), '"7"')
        self.assertEqual(hdrs.get("x-frame-seq"), "7")

    def test_export_zip_disposition(self) -> None:
        st, body, hdrs = self._raw("GET", "/v1/bots/b_remote000001/export?format=zip")
        self.assertEqual(st, 200)
        self.assertTrue(body.startswith(b"PK"))
        self.assertIn("chat.zip", hdrs.get("content-disposition", ""))

    def test_import_not_parsed_on_origin(self) -> None:
        payload = b"{" + (b"x" * 64_000)
        FakeOwner.last_import = None
        st, body, _ = self._raw(
            "POST",
            "/v1/bots/b_remote000001/import",
            headers={"Content-Type": "application/octet-stream"},
            body=payload,
        )
        self.assertEqual(st, 200)
        self.assertEqual(FakeOwner.last_import, payload)
        data = json.loads(body.decode())
        self.assertEqual(data.get("bytes"), len(payload))

    def test_put_soul_proxied(self) -> None:
        st, body, _ = self._raw("PUT", "/v1/bots/b_remote000001/soul", body={"body": "be kind"})
        self.assertEqual(st, 200)
        data = json.loads(body.decode())
        self.assertTrue(data.get("ok"))

    def test_strips_token_query(self) -> None:
        FakeOwner.last_query = "sentinel"
        self._raw("GET", "/v1/bots/b_remote000001?token=ui-secret")
        self.assertNotIn("token=", FakeOwner.last_query)

    def test_offline_503(self) -> None:
        d.cluster.peers[0].status = "offline"
        try:
            st, body, _ = self._raw("GET", "/v1/bots/b_remote000001")
            self.assertEqual(st, 503)
            data = json.loads(body.decode())
            self.assertEqual(data.get("error"), "peer offline")
        finally:
            d.cluster.peers[0].status = "ok"


class SelfTestHints(unittest.TestCase):
    def test_empty_token_and_peers(self) -> None:
        from cluster import Cluster

        c = Cluster(
            emit=lambda e: None,
            lookup_local_bot=lambda s: None,
            local_profiles=lambda: [],
            is_local_id=lambda i: False,
            load_config=lambda: {"node_name": "teela-body"},
        )
        j = c.self_test()
        self.assertFalse(j["token_set"])
        self.assertEqual(j["peer_count"], 0)
        self.assertEqual(j["results"], [])
        self.assertIn("token", (j.get("hint") or "").lower())


if __name__ == "__main__":
    unittest.main()
