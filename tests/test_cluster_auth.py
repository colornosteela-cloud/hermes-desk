#!/usr/bin/env python3
"""Cluster auth deny matrix, loopback mesh writes, links SSRF."""

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
from cluster import Cluster  # noqa: E402


def _http(host: str, port: int, method: str, path: str, *, headers=None, body=None, timeout=3):
    conn = HTTPConnection(host, port, timeout=timeout)
    raw = None if body is None else (body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode())
    hdrs = dict(headers or {})
    if raw is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(raw))
    conn.request(method, path, body=raw, headers=hdrs)
    resp = conn.getresponse()
    blob = resp.read()
    conn.close()
    try:
        data = json.loads(blob.decode() or "null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = blob
    return resp.status, data, dict(resp.getheaders()) if False else blob


class FakePeer(BaseHTTPRequestHandler):
    token = "mesh-secret"
    hits: list[str] = []

    def log_message(self, fmt, *args):
        return

    def _ok(self) -> bool:
        got = self.headers.get("X-Hermes-Cluster-Token") or ""
        return got == self.token

    def do_GET(self):  # noqa: N802
        FakePeer.hits.append(self.path)
        if not self._ok():
            self.send_response(401)
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path == "/v1/cluster/hello":
            body = {
                "node": "teela-body",
                "listen_host": "127.0.0.1",
                "listen_port": self.server.server_address[1],
                "version": "0.1.0",
                "bots": 1,
                "peer_names": ["teela-brain"],
            }
        elif path == "/v1/cluster/bots":
            body = {
                "node": "teela-body",
                "bots": [{"id": "b_remote000001", "name": "wrench", "remote": False, "node": "teela-body"}],
            }
        elif path == "/v1/cluster/links":
            body = {"node": "teela-body", "links": [{"name": "teela-brain", "status": "ok", "latency_ms": 1, "hello_node": "teela-brain"}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TrapHandler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, fmt, *args):
        return

    def do_GET(self):  # noqa: N802
        TrapHandler.hits += 1
        self.send_response(200)
        self.end_headers()


class LanHandler(d.Handler):
    def _is_loopback(self) -> bool:
        return False


class ClusterAuthTests(unittest.TestCase):
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
        cls._orig_listen_h = d.LISTEN_HOST
        cls._orig_listen_p = d.LISTEN_PORT
        d.desk_config_path = lambda: cls.desk
        d.TOKEN_PATH = cls.token_path
        cls.peer_httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakePeer)
        cls.peer_port = cls.peer_httpd.server_address[1]
        threading.Thread(target=cls.peer_httpd.serve_forever, daemon=True).start()
        cls.trap = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
        cls.trap_port = cls.trap.server_address[1]
        threading.Thread(target=cls.trap.serve_forever, daemon=True).start()
        cfg = {
            "listen_host": "127.0.0.1",
            "listen_port": 8742,
            "node_name": "teela-brain",
            "cluster_token": "mesh-secret",
            "peers": [{"name": "teela-body", "url": f"http://127.0.0.1:{cls.peer_port}"}],
        }
        cls.desk.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        d.cluster = Cluster(
            emit=d.emit,
            lookup_local_bot=lambda s: d.bots.get(s),
            local_profiles=lambda: [b.profile() for b in d.bots.values()],
            is_local_id=lambda bid: bid in d.bots,
            load_config=d.load_desk_config,
            listen_info=lambda: (d.LISTEN_HOST, int(d.LISTEN_PORT), "127.0.0.1"),
        )
        cls.httpd = d.DeskHTTPServer(("127.0.0.1", 0), d.Handler)
        cls.port = cls.httpd.server_address[1]
        d.LISTEN_HOST = "127.0.0.1"
        d.LISTEN_PORT = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.lan = d.DeskHTTPServer(("127.0.0.1", 0), LanHandler)
        cls.lan_port = cls.lan.server_address[1]
        threading.Thread(target=cls.lan.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        for s in (cls.httpd, cls.lan, cls.peer_httpd, cls.trap):
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        d.desk_config_path = cls._orig_path
        d.TOKEN_PATH = cls._orig_token
        d.cluster = cls._orig_cluster
        d.LISTEN_HOST = cls._orig_listen_h
        d.LISTEN_PORT = cls._orig_listen_p
        cls.tmp.cleanup()

    def _req(self, method, path, headers=None, body=None, port=None):
        st, data, raw = _http("127.0.0.1", port or self.port, method, path, headers=headers, body=body)
        return st, data

    def test_cluster_hello_ok(self) -> None:
        st, data = self._req("GET", "/v1/cluster/hello", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
        self.assertEqual(st, 200)
        self.assertEqual(data["node"], "teela-brain")
        self.assertIn("teela-body", data["peer_names"])
        self.assertNotIn("url", json.dumps(data["peer_names"]))

    def test_cluster_hello_wrong_token(self) -> None:
        st, data = self._req("GET", "/v1/cluster/hello", headers={"X-Hermes-Cluster-Token": "nope"})
        self.assertEqual(st, 401)

    def test_ui_bearer_on_cluster_hello(self) -> None:
        st, _ = self._req("GET", "/v1/cluster/hello", headers={"Authorization": "Bearer ui-secret"})
        self.assertEqual(st, 401)

    def test_ui_bearer_on_cluster_links(self) -> None:
        st, _ = self._req("GET", "/v1/cluster/links", headers={"Authorization": "Bearer ui-secret"})
        self.assertEqual(st, 401)

    def test_cluster_header_on_settings(self) -> None:
        st, _ = self._req("GET", "/v1/settings", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
        self.assertIn(st, (401, 403))

    def test_cluster_header_on_bootstrap(self) -> None:
        st, data = self._req("GET", "/v1/bootstrap", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
        self.assertIn(st, (401, 403))
        if isinstance(data, dict):
            self.assertNotIn("token", data)

    def test_cluster_header_on_llm_non_loopback(self) -> None:
        st, _ = self._req(
            "GET",
            "/v1/llm/models",
            headers={"X-Hermes-Cluster-Token": "mesh-secret"},
            port=self.lan_port,
        )
        self.assertIn(st, (401, 403))

    def test_settings_get_never_returns_token(self) -> None:
        st, data = self._req("GET", "/v1/settings", headers={"Authorization": "Bearer ui-secret"})
        self.assertEqual(st, 200)
        self.assertNotIn("cluster_token", data)
        self.assertTrue(data.get("cluster_token_set"))
        self.assertEqual(data.get("node_name"), "teela-brain")

    def test_bootstrap_has_node_not_cluster(self) -> None:
        st, data = self._req("GET", "/v1/bootstrap")
        self.assertEqual(st, 200)
        self.assertEqual(data.get("node_name"), "teela-brain")
        self.assertNotIn("cluster_token", data)
        self.assertNotIn("cluster_token_set", data)
        self.assertNotIn("peers", data)
        self.assertEqual(data.get("token"), "ui-secret")

    def test_empty_token_cluster_403(self) -> None:
        old = d.cluster.token
        d.cluster.token = ""
        try:
            st, data = self._req("GET", "/v1/cluster/hello", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
            self.assertEqual(st, 403)
            self.assertEqual(data.get("error"), "cluster not configured")
        finally:
            d.cluster.token = old

    def test_non_loopback_cannot_set_peers(self) -> None:
        before = self.desk.read_bytes()
        st, data = self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={
                "listen_host": "10.0.0.10",
                "listen_port": 9999,
                "peers": [{"name": "evil", "url": "http://10.0.0.99:8742"}],
                "peers_loaded": True,
            },
            port=self.lan_port,
        )
        self.assertEqual(st, 403)
        self.assertEqual(data.get("error"), "cluster config is local-only")
        self.assertEqual(self.desk.read_bytes(), before)

    def test_non_loopback_cannot_set_token(self) -> None:
        before = self.desk.read_bytes()
        st, _ = self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={"cluster_token": "x"},
            port=self.lan_port,
        )
        self.assertEqual(st, 403)
        self.assertEqual(self.desk.read_bytes(), before)

    def test_token_new_non_loopback_403(self) -> None:
        st, _ = self._req(
            "POST",
            "/v1/cluster/token-new",
            headers={"Authorization": "Bearer ui-secret"},
            body={},
            port=self.lan_port,
        )
        self.assertEqual(st, 403)

    def test_token_new_loopback(self) -> None:
        st, data = self._req(
            "POST",
            "/v1/cluster/token-new",
            headers={"Authorization": "Bearer ui-secret"},
            body={},
        )
        self.assertEqual(st, 200)
        self.assertTrue(data.get("cluster_token"))
        disk = json.loads(self.desk.read_text())
        self.assertEqual(disk.get("cluster_token"), "mesh-secret")

    def test_loopback_rotate_without_confirm(self) -> None:
        st, _ = self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={"cluster_token": "brand-new-token-value"},
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(self.desk.read_text())["cluster_token"], "brand-new-token-value")
        self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={"cluster_token": "mesh-secret"},
        )
        if d.cluster is not None:
            d.cluster.token = "mesh-secret"

    def test_listen_only_empty_token_does_not_clear(self) -> None:
        st, _ = self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={"listen_host": "127.0.0.1", "listen_port": self.port, "cluster_token": ""},
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(self.desk.read_text())["cluster_token"], "mesh-secret")

    def test_phone_listen_only_ok(self) -> None:
        st, data = self._req(
            "POST",
            "/v1/settings",
            headers={"Authorization": "Bearer ui-secret"},
            body={"listen_host": "127.0.0.1", "listen_port": self.lan_port},
            port=self.lan_port,
        )
        self.assertEqual(st, 200)
        # restore listen globals for other tests
        d.LISTEN_HOST = "127.0.0.1"
        d.LISTEN_PORT = self.port
        d.patch_desk_config({"listen_host": "127.0.0.1", "listen_port": self.port})

    def test_links_ignores_url_query(self) -> None:
        FakePeer.hits = []
        TrapHandler.hits = 0
        st, data = self._req(
            "GET",
            f"/v1/cluster/links?url=http://127.0.0.1:{self.trap_port}/",
            headers={"X-Hermes-Cluster-Token": "mesh-secret"},
        )
        self.assertEqual(st, 200)
        self.assertEqual(TrapHandler.hits, 0)
        self.assertTrue(any(h.startswith("/v1/cluster/hello") for h in FakePeer.hits))
        names = [r["name"] for r in data.get("links") or []]
        self.assertEqual(names, ["teela-body"])

    def test_authorization_cluster_scheme(self) -> None:
        st, data = self._req("GET", "/v1/cluster/hello", headers={"Authorization": "Cluster mesh-secret"})
        self.assertEqual(st, 200)
        self.assertEqual(data["node"], "teela-brain")


class EmptyTokenTests(unittest.TestCase):
    def test_unconfigured_peer_url_rejected(self) -> None:
        from cluster import validate_peer_url

        with self.assertRaises(ValueError):
            validate_peer_url("http://169.254.169.254/")
        with self.assertRaises(ValueError):
            validate_peer_url("https://10.0.0.20:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://teela-body:8742")
        with self.assertRaises(ValueError):
            validate_peer_url("http://user:pass@10.0.0.20:8742")
        self.assertEqual(validate_peer_url("http://10.0.0.20/"), "http://10.0.0.20:8742")


if __name__ == "__main__":
    unittest.main()
