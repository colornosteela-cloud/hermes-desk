#!/usr/bin/env python3
"""Merged roster, slim lists, cluster GET does not recurse."""

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


class PeerRoster(BaseHTTPRequestHandler):
    token = "mesh-secret"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):  # noqa: N802
        if (self.headers.get("X-Hermes-Cluster-Token") or "") != self.token:
            self.send_response(401)
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path == "/v1/cluster/bots":
            body = {
                "node": "teela-body",
                "bots": [
                    {
                        "id": "b_peerbot00001",
                        "name": "wrench",
                        "description": "body bot",
                        "messages": [{"role": "user", "text": "should not leak"}],
                        "workspace": "/secret/path",
                        "model": "grok-4.6",
                        "status": "Ready",
                        "avatar": {"kind": "emoji", "value": "◉", "color": "#ff7817", "shape": "blob"},
                        "avatar_color": "#ff7817",
                        "avatar_shape": "blob",
                    }
                ],
            }
        elif path == "/v1/cluster/hello":
            body = {"node": "teela-body", "peer_names": ["teela-brain"], "bots": 1, "version": "0.1.0"}
        elif path == "/v1/bots":
            body = {"bots": [{"id": "b_should_not_fetch", "name": "leak"}]}
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


class RosterMergeTests(unittest.TestCase):
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
        cls.peer = ThreadingHTTPServer(("127.0.0.1", 0), PeerRoster)
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

    def _json(self, method, path, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        blob = resp.read()
        conn.close()
        return resp.status, json.loads(blob.decode() or "{}")

    def test_ui_merge_includes_peer_and_omits_messages(self) -> None:
        st, data = self._json("GET", "/v1/bots", headers={"Authorization": "Bearer ui-secret"})
        self.assertEqual(st, 200)
        ids = [b["id"] for b in data.get("bots") or []]
        self.assertIn("b_peerbot00001", ids)
        peer = next(b for b in data["bots"] if b["id"] == "b_peerbot00001")
        self.assertTrue(peer.get("remote"))
        self.assertEqual(peer.get("node"), "teela-body")
        self.assertNotIn("messages", peer)
        self.assertNotIn("workspace", peer)
        self.assertEqual(peer.get("avatar_color"), "#ff7817")
        self.assertEqual(peer.get("avatar_shape"), "blob")
        self.assertEqual((peer.get("avatar") or {}).get("color"), "#ff7817")
        self.assertEqual(data.get("node"), "teela-brain")

    def test_cluster_get_bots_is_local_only(self) -> None:
        st, data = self._json("GET", "/v1/bots", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
        self.assertEqual(st, 200)
        ids = [b["id"] for b in data.get("bots") or []]
        self.assertNotIn("b_peerbot00001", ids)
        for b in data.get("bots") or []:
            self.assertNotIn("messages", b)

    def test_cluster_bots_route_local(self) -> None:
        st, data = self._json("GET", "/v1/cluster/bots", headers={"X-Hermes-Cluster-Token": "mesh-secret"})
        self.assertEqual(st, 200)
        self.assertEqual(data.get("node"), "teela-brain")
        ids = [b["id"] for b in data.get("bots") or []]
        self.assertNotIn("b_peerbot00001", ids)

    def test_offline_cache(self) -> None:
        d.cluster.fetch_remote_rosters(timeout=0.5)
        with d.cluster.lock:
            self.assertIn("b_peerbot00001", d.cluster.index)
        d.cluster.peers[0].url = "http://127.0.0.1:1"
        remote, diag = d.cluster.fetch_remote_rosters(timeout=0.2)
        d.cluster.peers[0].url = f"http://127.0.0.1:{self.peer_port}"
        ids = [b["id"] for b in remote]
        self.assertIn("b_peerbot00001", ids)
        bot = next(b for b in remote if b["id"] == "b_peerbot00001")
        self.assertEqual(bot.get("node_status"), "offline")
        self.assertEqual(diag[0]["status"], "offline")

    def test_id_collision_local_wins(self) -> None:
        class FakeBot:
            def profile(self):
                return {
                    "id": "b_peerbot00001",
                    "name": "local-twin",
                    "messages": [{"role": "user", "text": "local"}],
                    "workspace": "/local",
                }

        d.bots["b_peerbot00001"] = FakeBot()  # type: ignore[assignment]
        try:
            local = d.cluster.local_roster()
            remote, diag = d.cluster.fetch_remote_rosters(timeout=0.5)
            merged = d.cluster.merge(local, remote, diag)
            hit = [b for b in merged if b["id"] == "b_peerbot00001"]
            self.assertEqual(len(hit), 1)
            self.assertEqual(hit[0]["name"], "local-twin")
            self.assertFalse(hit[0].get("remote"))
            self.assertTrue(any(x.get("warning") for x in diag))
        finally:
            d.bots.pop("b_peerbot00001", None)


if __name__ == "__main__":
    unittest.main()
