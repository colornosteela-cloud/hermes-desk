"""Tail a bot's Hermes ``state.db`` (sqlite) and emit TUI turns.

Grok persisted sessions as ``sessions/**/updates.jsonl`` files; Hermes persists
every turn to ``<HERMES_HOME>/state.db`` (``messages`` table, one row per
message). This mirror polls the db for the newest ``source='tui'`` session and
emits only user text + assistant text (same contract as the old
``updates.jsonl`` mirror): ``on_turn(role, text)`` and ``on_usage(dict)``.

ACP chat sessions (``source='acp'``) are streamed over the ACP protocol and
must NOT be double-emitted here — the filter is by session ``source``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

EmitFn = Callable[[dict[str, Any]], None]

# Poll interval; sqlite in WAL/DELETE mode tolerates short-lived read connections.
_POLL_SECONDS = 0.35


def _ro_connect(db_path: Path) -> sqlite3.Connection | None:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        return con
    except (OSError, sqlite3.Error):
        return None


class SessionMirror:
    """Mirror TUI turns from the bot's Hermes state.db into the desk chat feed."""

    def __init__(
        self,
        state_db: Path,
        on_turn: Callable[[str, str], None],
        on_usage: Callable[[dict], None] | None = None,
        source: str = "tui",
    ) -> None:
        self.state_db = Path(state_db)
        self.on_turn = on_turn
        self.on_usage = on_usage
        self.source = source
        self._stop = threading.Event()
        # session id -> last message row id consumed
        self._seen: dict[str, int] = {}
        self._session_id: str | None = None
        self._asst: str = ""
        self._pending_usage: dict[str, Any] = {}

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- internals -----------------------------------------------------------

    def _newest_tui_session(self, con: sqlite3.Connection) -> str | None:
        try:
            row = con.execute(
                "select s.id from sessions s "
                "join messages m on m.session_id = s.id "
                "where s.source = ? "
                "group by s.id "
                "order by max(m.timestamp) desc "
                "limit 1",
                (self.source,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return str(row[0]) if row else None

    def _flush(self) -> None:
        if self._asst.strip():
            self.on_turn("assistant", self._asst.strip())
            if self.on_usage and self._pending_usage:
                try:
                    self.on_usage(self._pending_usage)
                except Exception:
                    pass
        self._asst = ""
        self._pending_usage = {}

    def _ingest_new(self, con: sqlite3.Connection, session_id: str, last_id: int) -> int:
        rows = con.execute(
            "select id, role, content, token_count, finish_reason, tool_name "
            "from messages where session_id = ? and id > ? order by id",
            (session_id, last_id),
        ).fetchall()
        new_last = last_id
        for r in rows:
            new_last = max(new_last, int(r["id"]))
            role = str(r["role"] or "")
            content = str(r["content"] or "")
            if role == "user":
                # A new user turn ends the previous assistant turn.
                self._flush()
                text = content.strip()
                if text:
                    self.on_turn("user", text)
            elif role == "assistant":
                text = content.strip()
                if text:
                    self._asst += ("" if not self._asst else "\n") + text
                finish = str(r["finish_reason"] or "")
                tokens = r["token_count"]
                if finish in {"stop", "tool_calls", "end_turn", "complete"} and tokens:
                    self._pending_usage = {
                        "input_tokens": 0,
                        "output_tokens": int(tokens or 0),
                        "source": "hermes_state",
                    }
                    # Turn boundary: emit what we have.
                    self._flush()
        return new_last

    def _loop(self) -> None:
        while not self._stop.is_set():
            con = _ro_connect(self.state_db)
            if con is not None:
                try:
                    sid = self._newest_tui_session(con)
                    if sid and sid != self._session_id:
                        # New TUI session: flush stale buffer, start fresh.
                        self._flush()
                        self._session_id = sid
                        self._seen = {sid: 0}
                        # Don't replay history of an already-running session:
                        # seed from the newest row so we only see live turns.
                        try:
                            row = con.execute(
                                "select max(id) from messages where session_id = ?", (sid,)
                            ).fetchone()
                            if row and row[0]:
                                self._seen[sid] = int(row[0])
                        except sqlite3.Error:
                            pass
                    if self._session_id:
                        last = self._seen.get(self._session_id, 0)
                        self._seen[self._session_id] = self._ingest_new(
                            con, self._session_id, last
                        )
                except Exception:
                    pass
                finally:
                    con.close()
            self._stop.wait(_POLL_SECONDS)
