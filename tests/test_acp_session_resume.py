"""Hermes Agent bots keep their conversation across ACP restarts via session resume."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deskd"))

import deskd as d  # noqa: E402


def _bare_bot(root: Path) -> d.Bot:
    bot = d.Bot.__new__(d.Bot)
    bot.root = root
    bot.agent_home = root / "hermes-home"
    bot.workspace = root / "workspace"
    bot.agent_home.mkdir(parents=True, exist_ok=True)
    bot.workspace.mkdir(parents=True, exist_ok=True)
    bot.messages = []
    bot.chat_id = ""
    return bot


class SessionRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bot = _bare_bot(Path(tmp.name))
        self.bot.chat_id = "c_active"

    def test_record_and_resume_roundtrip(self) -> None:
        self.bot.record_acp_session("01a0-sid-one")
        self.assertEqual(self.bot.last_acp_session_id(), "01a0-sid-one")

    def test_resume_refuses_session_from_other_chat(self) -> None:
        self.bot.record_acp_session("01a0-sid-one")
        self.bot.chat_id = "c_other"
        self.assertIsNone(self.bot.last_acp_session_id())

    def test_resume_without_chat_recorded(self) -> None:
        self.bot.record_acp_session("01a0-sid-one")
        self.bot.chat_id = ""
        self.assertEqual(self.bot.last_acp_session_id(), "01a0-sid-one")

    def test_missing_file_without_sessions_yields_none(self) -> None:
        self.assertIsNone(self.bot.last_acp_session_id())

    @staticmethod
    def _write_state_db(home, rows) -> None:
        import sqlite3

        db = home / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "create table sessions (id text primary key, last_activity_at real, "
            "message_count integer default 0)"
        )
        con.execute(
            "create table messages (session_id text, role text, content text, timestamp real)"
        )
        for sid, ts, count in rows:
            con.execute(
                "insert into sessions (id, last_activity_at, message_count) values (?,?,?)",
                (sid, ts, count),
            )
            for k in range(count):
                con.execute(
                    "insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                    (sid, "user", "x", ts + k),
                )
        con.commit()
        con.close()

    def test_missing_file_bootstraps_from_newest_on_disk_session(self) -> None:
        self._write_state_db(
            self.bot.agent_home,
            [("01a0-old-sid", 1000.0, 1), ("01a0-new-sid", 2000.0, 1)],
        )
        self.assertEqual(self.bot.last_acp_session_id(), "01a0-new-sid")

    def test_chat_mismatch_does_not_fall_back_to_disk(self) -> None:
        self.bot.record_acp_session("01a0-sid-one")
        self._write_state_db(self.bot.agent_home, [("01a0-disk-sid", 3000.0, 1)])
        self.bot.chat_id = "c_other"
        self.assertIsNone(self.bot.last_acp_session_id())

    def test_attach_acp_session_binds_active_chat(self) -> None:
        data = self.bot.ensure_timeline()
        cid = str(data.get("activeId") or "")
        self.bot.attach_acp_session("01a0-sid-bound")
        saved = json.loads(self.bot.timeline_path().read_text(encoding="utf-8"))
        row = next(c for c in saved["chats"] if c.get("id") == cid)
        self.assertEqual(row.get("agentSession"), "01a0-sid-bound")

    def test_attach_survives_chat_touch(self) -> None:
        data = self.bot.ensure_timeline()
        cid = str(data.get("activeId") or "")
        self.bot.attach_acp_session("01a0-sid-bound")
        self.bot.messages = [{"role": "user", "text": "hello", "ts": time.time()}]
        self.bot._touch_active_chat()
        saved = json.loads(self.bot.timeline_path().read_text(encoding="utf-8"))
        row = next(c for c in saved["chats"] if c.get("id") == cid)
        self.assertEqual(row.get("agentSession"), "01a0-sid-bound")
        self.assertEqual(row.get("messageCount"), 1)


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def poll(self) -> int | None:
        return None


def _fake_bot(root: Path) -> "unittest.mock.MagicMock":
    bot = unittest.mock.MagicMock()
    bot.id = "b_resume"
    bot.kind = "hermes"
    bot.model = "grok-4.6"
    bot.effort = ""
    bot.status = ""
    bot.surface = "chat"
    bot.agent_home = root / "hermes-home"
    bot.workspace = root / "workspace"
    bot.chat_id = "c_active"
    bot.agent_home.mkdir(parents=True, exist_ok=True)
    bot.workspace.mkdir(parents=True, exist_ok=True)
    return bot


def _bare_acp(bot) -> d.AcpClient:
    acp = d.AcpClient.__new__(d.AcpClient)
    acp.bot = bot
    acp.proc = None
    acp._id = 0
    acp._pending = {}
    acp._wlock = threading.Lock()
    acp._life = threading.Lock()
    acp.session_id = None
    acp.alive = False
    acp._stopping = False
    acp._generation = 0
    acp._respawn_at = []
    acp._last_acp_event = time.time()
    acp._prompt_rid = None
    acp._turn_cancel = threading.Event()
    return acp


class AcpStartResumeTests(unittest.TestCase):
    def _run_start(self, load_session_id, load_ok: bool = True, load_null_result: bool = False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bot = _fake_bot(root)
        acp = _bare_acp(bot)
        calls: list[tuple[str, dict]] = []

        def fake_request(method: str, params: dict | None = None, timeout: float | None = None):
            calls.append((method, dict(params or {})))
            if method == "session/load" and not load_ok:
                raise RuntimeError("session not found")
            if method in ("session/new", "session/load"):
                sid = (params or {}).get("sessionId") or "01a0-new-sid"
                if method == "session/load" and load_null_result:
                    return {}  # ACP agents may answer a successful load with null
                return {"sessionId": sid, "models": {}}
            return {}

        acp.request = fake_request  # type: ignore[method-assign]
        caps = {
            "acp": True,
            "available": True,
            "acp_stdio": True,
            "cwd_flag": True,
            "permission_mode": True,
            "leader_socket": False,
            "no_leader": True,
            "always_approve": True,
            "model_flag": True,
            "agent_profile": False,
            "plugin_dir": False,
            "sandbox_flag": False,
            "version": "test",
        }
        with patch.object(d, "agent_capabilities", return_value=caps):
            with patch.object(d.subprocess, "Popen", return_value=_FakeProc()):
                with patch.object(d, "apply_shared_agent_auth"):
                    with patch.object(d, "copy_auth"):
                        with patch.object(d, "acp_mcp_specs", return_value=[]):
                            with patch.object(d, "acp_session_meta", return_value={}):
                                with patch.object(d, "desk_token", return_value="t"):
                                    with patch.object(d, "bot_kind_has_host_coding", return_value=False):
                                        acp._start_locked(load_session_id=load_session_id)
        return acp, calls, bot

    def test_fresh_start_uses_session_new_only(self) -> None:
        acp, calls, bot = self._run_start(None)
        methods = [m for m, _ in calls]
        self.assertNotIn("session/load", methods)
        self.assertIn("session/new", methods)
        self.assertEqual(acp.session_id, "01a0-new-sid")
        bot.record_acp_session.assert_called_once_with("01a0-new-sid")
        bot.attach_acp_session.assert_called_once_with("01a0-new-sid")

    def test_resume_start_loads_existing_session(self) -> None:
        acp, calls, bot = self._run_start("01a0-old-sid")
        session_calls = [(m, p) for m, p in calls if m in ("session/new", "session/load")]
        self.assertEqual(session_calls[0][0], "session/load")
        self.assertEqual(session_calls[0][1].get("sessionId"), "01a0-old-sid")
        self.assertEqual(len(session_calls), 1, "must not create a second session on success")
        self.assertEqual(acp.session_id, "01a0-old-sid")
        bot.record_acp_session.assert_called_once_with("01a0-old-sid")

    def test_failed_load_falls_back_to_session_new(self) -> None:
        acp, calls, _bot = self._run_start("01a0-gone-sid", load_ok=False)
        methods = [m for m, _ in calls if m in ("session/new", "session/load")]
        self.assertEqual(methods, ["session/load", "session/new"])
        self.assertEqual(acp.session_id, "01a0-new-sid")

    def test_null_load_result_still_resumes(self) -> None:
        acp, calls, _bot = self._run_start("01a0-null-sid", load_null_result=True)
        methods = [m for m, _ in calls if m in ("session/new", "session/load")]
        self.assertEqual(methods, ["session/load"], "a clean null response must not spawn session/new")
        self.assertEqual(acp.session_id, "01a0-null-sid")


class RestartWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bot = _bare_bot(Path(tmp.name))

    def test_restart_session_forwards_load_id(self) -> None:
        started: list[dict] = []

        class _Acp:
            def __init__(self, bot) -> None:
                self.bot = bot

            def stop(self) -> None:
                pass

            def start(self, load_session_id: str | None = None) -> None:
                started.append({"load_session_id": load_session_id})

        self.bot.acp = _Acp(self.bot)
        with patch.object(d, "AcpClient", _Acp):
            self.bot._restart_session(load_session_id="01a0-x")
            self.bot._restart_session()
        self.assertEqual(started, [{"load_session_id": "01a0-x"}, {"load_session_id": None}])

    def test_open_chat_resumes_that_chats_session(self) -> None:
        data = self.bot.ensure_timeline()
        active = str(data["activeId"])
        old_id = "c_old"
        data["chats"].append({
            "id": old_id,
            "title": "old chat",
            "createdAt": 1.0,
            "updatedAt": 2.0,
            "preview": "",
            "messageCount": 1,
            "agentSession": "01a0-old-sid",
        })
        self.bot._write_timeline(data)
        started: list[dict] = []
        self.bot._snapshot_active = lambda: None  # type: ignore[method-assign]
        self.bot.persist_messages = lambda: None  # type: ignore[method-assign]
        self.bot.reset_telemetry = lambda: None  # type: ignore[method-assign]
        self.bot.append_log = lambda *a, **k: None  # type: ignore[method-assign]
        self.bot._emit_chats = lambda *a, **k: {}  # type: ignore[method-assign]
        self.bot._emit_usage = lambda: None  # type: ignore[method-assign]

        def restart(load_session_id: str | None = None) -> None:
            started.append({"load_session_id": load_session_id})

        self.bot._restart_session = restart  # type: ignore[method-assign]
        self.bot.open_chat(old_id)
        self.assertEqual(started, [{"load_session_id": "01a0-old-sid"}])
        self.assertEqual(self.bot.chat_id, old_id)

    def test_open_chat_without_saved_session_starts_fresh(self) -> None:
        data = self.bot.ensure_timeline()
        old_id = "c_old"
        data["chats"].append({
            "id": old_id,
            "title": "old chat",
            "createdAt": 1.0,
            "updatedAt": 2.0,
            "preview": "",
            "messageCount": 1,
        })
        self.bot._write_timeline(data)
        started: list[dict] = []
        self.bot._snapshot_active = lambda: None  # type: ignore[method-assign]
        self.bot.persist_messages = lambda: None  # type: ignore[method-assign]
        self.bot.reset_telemetry = lambda: None  # type: ignore[method-assign]
        self.bot.append_log = lambda *a, **k: None  # type: ignore[method-assign]
        self.bot._emit_chats = lambda *a, **k: {}  # type: ignore[method-assign]
        self.bot._emit_usage = lambda: None  # type: ignore[method-assign]
        self.bot._restart_session = lambda load_session_id=None: started.append(
            {"load_session_id": load_session_id}
        )  # type: ignore[method-assign]
        self.bot.open_chat(old_id)
        self.assertEqual(started, [{"load_session_id": None}])


if __name__ == "__main__":
    unittest.main()
