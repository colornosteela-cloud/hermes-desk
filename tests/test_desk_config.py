#!/usr/bin/env python3
"""Merge-safe desk.json + empty cluster_token does not clear."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402


class DeskConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.path = self.home / "desk.json"
        self._orig = d.desk_config_path
        d.desk_config_path = lambda: self.path

    def tearDown(self) -> None:
        d.desk_config_path = self._orig
        self.tmp.cleanup()

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_read_missing_is_empty(self) -> None:
        self.assertEqual(d.read_desk_file(), {})

    def test_voice_defaults_muted(self) -> None:
        self.assertFalse(d.voice_enabled())
        self.path.write_text(json.dumps({"voice": True}), encoding="utf-8")
        self.assertTrue(d.voice_enabled())
        self.path.write_text(json.dumps({"voice": False}), encoding="utf-8")
        self.assertFalse(d.voice_enabled())

    def test_read_corrupt_is_empty(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(d.read_desk_file(), {})

    def test_save_desk_config_preserves_extra_keys(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "listen_host": "127.0.0.1",
                    "listen_port": 8742,
                    "node_name": "teela-brain",
                    "cluster_token": "keep-me",
                    "peers": [{"name": "teela-body", "url": "http://10.0.0.20:8742"}],
                    "future_key": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        d.save_desk_config("10.0.0.10", 8742)
        data = self._read()
        self.assertEqual(data["listen_host"], "10.0.0.10")
        self.assertEqual(data["listen_port"], 8742)
        self.assertEqual(data["node_name"], "teela-brain")
        self.assertEqual(data["cluster_token"], "keep-me")
        self.assertEqual(data["peers"][0]["name"], "teela-body")
        self.assertEqual(data["future_key"], 1)

    def test_empty_cluster_token_is_ignored(self) -> None:
        d.write_desk_file({"listen_host": "127.0.0.1", "listen_port": 8742, "cluster_token": "secret"})
        d.patch_desk_config({"listen_host": "10.0.0.10", "listen_port": 8742, "cluster_token": ""})
        data = self._read()
        self.assertEqual(data["cluster_token"], "secret")
        self.assertEqual(data["listen_host"], "10.0.0.10")

    def test_cluster_token_clear(self) -> None:
        d.write_desk_file({"cluster_token": "secret", "listen_host": "127.0.0.1"})
        d.patch_desk_config({"cluster_token_clear": True, "cluster_token": ""})
        self.assertEqual(self._read().get("cluster_token"), "")

    def test_omitted_keys_unchanged(self) -> None:
        d.write_desk_file({"node_name": "a", "cluster_token": "t", "peers": []})
        d.patch_desk_config({"listen_port": 9000})
        data = self._read()
        self.assertEqual(data["node_name"], "a")
        self.assertEqual(data["cluster_token"], "t")
        self.assertEqual(data["peers"], [])
        self.assertEqual(data["listen_port"], 9000)

    def test_load_desk_config_includes_cluster_fields(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "10.0.0.10",
                "listen_port": 8742,
                "node_name": "teela-brain",
                "cluster_token": "secret",
                "peers": [{"name": "body", "url": "http://10.0.0.20:8742"}],
            }
        )
        cfg = d.load_desk_config()
        self.assertEqual(cfg["node_name"], "teela-brain")
        self.assertEqual(cfg["cluster_token"], "secret")
        self.assertEqual(len(cfg["peers"]), 1)
        self.assertEqual(cfg["listen_host"], "10.0.0.10")

    def test_apply_listen_does_not_drop_cluster(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 8742,
                "node_name": "n1",
                "cluster_token": "secret",
                "extra": True,
            }
        )
        with patch.object(d, "LISTEN_HOST", "127.0.0.1"), patch.object(d, "LISTEN_PORT", 8742):
            d.apply_listen("127.0.0.1", 8742)
        data = self._read()
        self.assertEqual(data["cluster_token"], "secret")
        self.assertEqual(data["node_name"], "n1")
        self.assertTrue(data["extra"])

    def test_atomic_write_replaces(self) -> None:
        d.write_desk_file({"a": 1})
        d.write_desk_file({"a": 2, "b": 3})
        self.assertEqual(self._read(), {"a": 2, "b": 3})
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())


class LanAutoListenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.path = self.home / "desk.json"
        self._orig = d.desk_config_path
        d.desk_config_path = lambda: self.path

    def tearDown(self) -> None:
        d.desk_config_path = self._orig
        d._rebind_http.clear()
        self.tmp.cleanup()

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_bind_stays_loopback_without_lan_peers(self) -> None:
        d.write_desk_file({"listen_host": "127.0.0.1", "peers": []})
        self.assertFalse(d.lan_peers_configured())
        self.assertEqual(d.bind_address("127.0.0.1"), "127.0.0.1")

    def test_bind_all_interfaces_when_lan_peers(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "peers": [{"name": "teela-body", "url": "http://10.0.0.118:8742"}],
            }
        )
        self.assertTrue(d.lan_peers_configured())
        self.assertEqual(d.bind_address("127.0.0.1"), "0.0.0.0")

    def test_loopback_peer_does_not_promote(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "peers": [{"name": "local", "url": "http://127.0.0.1:18742"}],
            }
        )
        self.assertFalse(d.lan_peers_configured())
        self.assertEqual(d.bind_address("127.0.0.1"), "127.0.0.1")
        self.assertEqual(d.desired_listen_host("127.0.0.1"), "127.0.0.1")

    def test_force_loopback_env_keeps_loopback(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "peers": [{"name": "teela-body", "url": "http://10.0.0.118:8742"}],
            }
        )
        with patch.dict(os.environ, {"HERMES_DESK_LOOPBACK": "1"}):
            self.assertEqual(d.bind_address("127.0.0.1"), "127.0.0.1")
            self.assertEqual(d.desired_listen_host("127.0.0.1"), "127.0.0.1")
            self.assertFalse(d.lan_mode())

    def test_apply_listen_promotes_loopback_when_lan_peer(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 8742,
                "cluster_token": "secret",
                "peers": [{"name": "teela-body", "url": "http://10.0.0.118:8742"}],
            }
        )
        with (
            patch.object(d, "LISTEN_HOST", "127.0.0.1"),
            patch.object(d, "LISTEN_PORT", 8742),
            patch.object(d, "detect_lan_ipv4", return_value="10.0.0.10"),
        ):
            out = d.apply_listen("127.0.0.1", 8742)
        self.assertEqual(out["listen_host"], "10.0.0.10")
        self.assertTrue(out["lan"])
        self.assertEqual(out["bind"], "0.0.0.0")
        self.assertTrue(out["rebind"])
        self.assertEqual(self._read()["listen_host"], "10.0.0.10")
        self.assertEqual(self._read()["cluster_token"], "secret")

    def test_desired_listen_falls_back_to_all_interfaces(self) -> None:
        d.write_desk_file({"peers": [{"name": "body", "url": "http://192.168.1.20:8742"}]})
        with patch.object(d, "detect_lan_ipv4", return_value=""):
            self.assertEqual(d.desired_listen_host("127.0.0.1"), "0.0.0.0")

    def test_ensure_lan_listen_promotes_then_skips(self) -> None:
        d.write_desk_file(
            {
                "listen_host": "127.0.0.1",
                "listen_port": 8742,
                "peers": [{"name": "teela-body", "url": "http://10.0.0.118:8742"}],
            }
        )
        with (
            patch.object(d, "LISTEN_HOST", "127.0.0.1"),
            patch.object(d, "LISTEN_PORT", 8742),
            patch.object(d, "detect_lan_ipv4", return_value="10.0.0.10"),
        ):
            out = d.ensure_lan_listen_for_cluster()
            self.assertIsNotNone(out)
            self.assertEqual(out["listen_host"], "10.0.0.10")
            self.assertTrue(out["rebind"])
            self.assertIsNone(d.ensure_lan_listen_for_cluster())


class HasLanPeersTests(unittest.TestCase):
    def test_rfc1918_yes_loopback_no(self) -> None:
        from cluster import has_lan_peers, is_rfc1918_ipv4

        self.assertTrue(is_rfc1918_ipv4("10.0.0.10"))
        self.assertTrue(is_rfc1918_ipv4("192.168.1.1"))
        self.assertFalse(is_rfc1918_ipv4("127.0.0.1"))
        self.assertFalse(is_rfc1918_ipv4("8.8.8.8"))
        self.assertTrue(has_lan_peers([{"name": "body", "url": "http://10.0.0.118:8742"}]))
        self.assertFalse(has_lan_peers([{"name": "local", "url": "http://127.0.0.1:8742"}]))
        self.assertFalse(has_lan_peers([]))
        self.assertFalse(has_lan_peers(None))


class AnnounceTests(unittest.TestCase):
    def test_announce_indexes_remote_bot(self) -> None:
        from cluster import Cluster, Peer

        c = Cluster(
            emit=lambda e: None,
            lookup_local_bot=lambda s: None,
            local_profiles=lambda: [],
            is_local_id=lambda i: False,
            load_config=lambda: {"node_name": "teela-brain", "cluster_token": "t", "peers": []},
        )
        c.token = "t"
        c.peers = [Peer(name="teela-body", url="http://10.0.0.118:8742")]
        out = c.accept_announce("teela-body", [{"id": "b_7cfd7be30e2c", "name": "Body"}])
        self.assertEqual(out["accepted"], 1)
        own = c.owner("b_7cfd7be30e2c")
        self.assertEqual(own.name, "teela-body")
        hit = c.find_remote("b_7cfd7be30e2c")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1]["name"], "Body")


class ClusterTokenNormTests(unittest.TestCase):
    def test_strip_paste_noise(self) -> None:
        from cluster import cluster_token_fp, normalize_cluster_token

        raw = "  abcdef\r\n"
        self.assertEqual(normalize_cluster_token(raw), "abcdef")
        self.assertEqual(cluster_token_fp("abcdef"), cluster_token_fp("abcdef\n"))
        self.assertNotEqual(cluster_token_fp("abcdef"), cluster_token_fp("abcdeg"))


class StreamMergeTests(unittest.TestCase):
    def _fold(self, parts: list[str]) -> str:
        acc = ""
        for p in parts:
            acc = d.merge_assistant_stream(acc, p)
        return acc

    def test_teela_token_split_keeps_double_e(self) -> None:
        self.assertEqual(self._fold(["Te", "ela"]), "Teela")
        self.assertEqual(self._fold(["Te", "e", "la"]), "Teela")
        self.assertEqual(self._fold(["T", "e", "e", "l", "a"]), "Teela")
        self.assertEqual(self._fold(["I'm **Te", "ela**"]), "I'm **Teela**")
        self.assertEqual(self._fold(["T-E-", "E-L-A"]), "T-E-E-L-A")

    def test_spaces_and_newlines_are_not_dropped(self) -> None:
        self.assertEqual(self._fold(["Hello", " ", "world"]), "Hello world")
        self.assertEqual(self._fold(["line", "\n", "next"]), "line\nnext")

    def test_strip_after_each_chunk_keeps_blank_lines(self) -> None:
        acc = ""
        for p in ["Done:", "\n\n", "- **Host**: x"]:
            acc = d.strip_assistant_padding(d.merge_assistant_stream(acc, p))
        self.assertEqual(acc, "Done:\n\n- **Host**: x")

    def test_cumulative_snapshot_replaces(self) -> None:
        self.assertEqual(d.merge_assistant_stream("Hello", "Hello world"), "Hello world")

    def test_long_tail_window_still_merges(self) -> None:
        cur = "the name is Teela and"
        incoming = "Teela and more"
        self.assertEqual(d.merge_assistant_stream(cur, incoming), "the name is Teela and more")

    def test_leading_newline_snapshot_does_not_duplicate(self) -> None:
        head = (
            "Here's the honest, system-specific breakdown — what each one actually buys you "
            "on hermes-desk, and where the real value (and risk) is.\n"
        )
        truncated = head + "Chrome DevTools — adopt now.\nthen copy the good ones into the"
        full = truncated + " skills directory and watch. Telescope first."
        out = d.merge_assistant_stream(truncated, "\n" + full)
        self.assertEqual(out.count("Here's the honest"), 1)
        self.assertIn("Telescope first.", out)
        self.assertTrue(out.endswith("Telescope first."))

    def test_restarted_essay_collapses_to_the_complete_copy(self) -> None:
        head = (
            "Here's the honest, system-specific breakdown — what each one actually buys you "
            "on hermes-desk, and where the real value (and risk) is.\n"
        )
        first = head + "Chrome DevTools — adopt now.\nthen copy the good ones into the"
        second = head + (
            "Chrome DevTools — adopt now.\nthen copy the good ones into the "
            "skills directory and watch. Telescope first."
        )
        glued = first + "\n" + second
        out = d.collapse_restarted_assistant(glued)
        self.assertEqual(out.count("Here's the honest"), 1)
        self.assertEqual(out, second)


class AvatarPersistTests(unittest.TestCase):
    def test_body_payload_color_and_shape(self) -> None:
        color, shape = d._avatar_from_body({"avatar_color": "#ff7817", "avatar_shape": "blob"})
        self.assertEqual(color, "#ff7817")
        self.assertEqual(shape, "blob")

    def test_nested_avatar_object(self) -> None:
        color, shape = d._avatar_from_body({"avatar": {"color": "#2f91f2", "shape": "drop"}})
        self.assertEqual(color, "#2f91f2")
        self.assertEqual(shape, "drop")

    def test_rejects_non_hex_and_unknown_shape(self) -> None:
        color, shape = d._avatar_from_body({"avatar_color": "orange", "avatar_shape": "star"})
        self.assertEqual(color, "")
        self.assertEqual(shape, "")

    def test_profile_text_reads_inline_table(self) -> None:
        raw = 'avatar = { kind = "emoji", value = "◉", color = "#21b96b", shape = "square" }\n'
        fields = {"avatar": '{ kind = "emoji", value = "◉", color = "#21b96b", shape = "square" }'}
        color, shape = d._avatar_from_profile_text(fields, raw)
        self.assertEqual(color, "#21b96b")
        self.assertEqual(shape, "square")

    def test_top_level_keys_win(self) -> None:
        fields = {
            "avatar_color": "#f03f55",
            "avatar_shape": "triangle",
            "avatar": '{ kind = "emoji", value = "◉", color = "#21b96b", shape = "square" }',
        }
        color, shape = d._avatar_from_profile_text(fields, "")
        self.assertEqual(color, "#f03f55")
        self.assertEqual(shape, "triangle")

    def test_slim_keeps_avatar_fields(self) -> None:
        from cluster import Cluster

        c = Cluster(
            emit=lambda e: None,
            lookup_local_bot=lambda s: None,
            local_profiles=lambda: [],
            is_local_id=lambda i: False,
            load_config=lambda: {"node_name": "teela-body", "cluster_token": "t", "peers": []},
        )
        row = c.slim(
            {
                "id": "b_7cfd7be30e2c",
                "name": "Teela-body",
                "avatar": {"kind": "emoji", "value": "◉", "color": "#8b5cf6", "shape": "drop"},
                "avatar_color": "#8b5cf6",
                "avatar_shape": "drop",
                "messages": [{"role": "user", "text": "nope"}],
                "workspace": "/secret",
            }
        )
        self.assertEqual(row["avatar_color"], "#8b5cf6")
        self.assertEqual(row["avatar_shape"], "drop")
        self.assertEqual(row["avatar"]["color"], "#8b5cf6")
        self.assertNotIn("messages", row)
        self.assertNotIn("workspace", row)

    def test_slim_keeps_kind_and_model(self) -> None:
        from cluster import Cluster

        c = Cluster(
            emit=lambda e: None,
            lookup_local_bot=lambda s: None,
            local_profiles=lambda: [],
            is_local_id=lambda i: False,
            load_config=lambda: {"node_name": "teela-brain", "cluster_token": "t", "peers": []},
        )
        row = c.slim(
            {
                "id": "b_build",
                "name": "Coder",
                "kind": "hermes",
                "model": "grok-4.6",
                "models": [{"id": "grok-4.6"}, {"id": "qwen38-flash-next"}],
                "permission_mode": "always-approve",
            }
        )
        self.assertEqual(row["kind"], "hermes")
        self.assertEqual(row["model"], "grok-4.6")
        self.assertEqual(row["permission_mode"], "always-approve")
        self.assertEqual(len(row["models"]), 2)


class AuthShareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        self._orig = d.USER_AGENT_HOME
        d.USER_AGENT_HOME = self.home

    def tearDown(self) -> None:
        d.USER_AGENT_HOME = self._orig
        self.tmp.cleanup()

    def test_copy_auth_symlinks_host_file(self) -> None:
        host = self.home / "auth.json"
        host.write_text('{"k": 1}', encoding="utf-8")
        dst = Path(self.tmp.name) / "bot" / "auth.json"
        d.copy_auth(dst)
        self.assertTrue(dst.is_symlink())
        self.assertEqual(dst.resolve(), host.resolve())
        host.write_text('{"k": 2}', encoding="utf-8")
        self.assertEqual(dst.read_text(encoding="utf-8"), '{"k": 2}')

    def test_copy_auth_replaces_stale_byte_copy(self) -> None:
        host = self.home / "auth.json"
        host.write_text('{"host": true}', encoding="utf-8")
        dst = Path(self.tmp.name) / "bot" / "auth.json"
        dst.parent.mkdir()
        dst.write_text('{"forked": true}', encoding="utf-8")
        d.copy_auth(dst)
        self.assertTrue(dst.is_symlink())
        self.assertEqual(dst.read_text(encoding="utf-8"), '{"host": true}')

    def test_copy_auth_shares_lockfile(self) -> None:
        dst = Path(self.tmp.name) / "bot" / "auth.json"
        d.copy_auth(dst)
        lock = dst.with_name("auth.json.lock")
        self.assertTrue(lock.is_symlink())
        self.assertEqual(lock.resolve(), (self.home / "auth.json.lock").resolve())

    def test_apply_shared_agent_auth_is_noop(self) -> None:
        # Auth is shared by symlinking the host auth.json into the bot's
        # HERMES_HOME (see copy_auth); no env override is needed anymore.
        env: dict[str, str] = {"PATH": "keep"}
        d.apply_shared_agent_auth(env)
        self.assertEqual(env, {"PATH": "keep"})

    def test_acp_and_tui_use_shared_auth(self) -> None:
        src = Path(d.__file__).read_text(encoding="utf-8")
        start = src.split("def _start_locked", 1)[1].split("def ", 1)[0]
        tui = src.split("def ensure_tui", 1)[1].split("def ", 1)[0]
        self.assertIn('env["HERMES_HOME"] = str(self.bot.agent_home)', start)
        self.assertIn('env["HERMES_HOME"] = str(self.agent_home)', tui)
        self.assertNotIn("copy2", tui)


class GrokBinTests(unittest.TestCase):
    def test_resolve_prefers_existing_path(self) -> None:
        from surfaces import resolve_agent_bin

        found = resolve_agent_bin()
        self.assertTrue(found)
        if Path(found).is_file():
            self.assertTrue(os.access(found, os.X_OK))


if __name__ == "__main__":
    unittest.main()
