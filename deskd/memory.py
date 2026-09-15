"""Per-bot three-tier memory: working turns, session summary, SQLite+FTS5 facts.

Isolation: one MemoryManager per bot, rooted at that bot's workspace/.memory/.
Never import deskd (avoids cycles). Callers pass paths and the local-model URL.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import telemetry as tel

DEFAULT_COMPACT_THRESHOLD = 4000
DEFAULT_MIN_VERBATIM = 4
TOOL_RESULT_SOFT_MAX = 800
TOOL_RESULT_HARD_MAX = 8000
TIER3_BUDGET_FRACTION = 0.10
MAX_OVERFLOW_TURNS = 24
SESSION_MEMORY_MARK = "# Session memory"
DEFAULT_MAX_RETRIEVE = 3
DEFAULT_MIN_RELEVANCE = 0.22
_CORRECTION_RE = re.compile(
    r"(?i)\b(that'?s wrong|that is wrong|you didn'?t|not like that|you forgot|"
    r"you should have|next time|don'?t do that|do not do that|incorrect|"
    r"when i ask you to|turn your head|orient (?:the )?head)\b"
)
_BODY_DUMP_RE = re.compile(
    r"\[TEELA BODY NOW\]|\bRevision:\s*\d+.*\bHead:\s*yaw|\bpose=\w+.*\bwaving=",
    re.I | re.S,
)
_FRAME_DUMP_RE = re.compile(r"(?i)\bframe\s+\d{4,}\b")
_PATH_RE = re.compile(
    r"(?:(?:[A-Za-z]:)?(?:/|\\))?[\w./\\-]+\.[A-Za-z0-9]{1,8}"
)
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool"


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


def estimate_tokens(text: str) -> int:
    n = len(text or "")
    return max(1, n // 4) if n else 0


def query_tokens(text: str) -> set[str]:
    return {t.lower() for t in _FTS_TOKEN_RE.findall(text or "") if len(t) >= 3}


def looks_like_correction(text: str) -> bool:
    return bool(_CORRECTION_RE.search(text or ""))


def looks_like_body_dump(text: str) -> bool:
    raw = text or ""
    if _BODY_DUMP_RE.search(raw):
        return True
    low = raw.lower()
    # Conversational claims about current pose are not live telemetry.
    if "arm is raised" in low or "arm is currently" in low:
        return True
    if "head is currently" in low or "i am currently posing" in low:
        return True
    return False


def looks_like_frame_dump(text: str) -> bool:
    raw = text or ""
    if _FRAME_DUMP_RE.search(raw):
        return True
    if raw.lower().count("frame ") >= 4 and len(raw) > 800:
        return True
    return False


def score_memory_record(
    rec: "MemoryRecord",
    query: str,
    *,
    task_goal: str | None = None,
) -> tuple[float, list[str]]:
    """Intent-aware score in [0, 1] plus reason codes. Manager-emitted, not model prose."""
    qtoks = query_tokens(query)
    ftoks = query_tokens(rec.text) | {t.lower() for t in (rec.tags or [])}
    overlap = 0.0
    if qtoks:
        overlap = len(qtoks & ftoks) / float(len(qtoks))
    base = _bm25_score(rec.rank)
    if base is None:
        base = 0.0
    score = (0.35 * base) + (0.65 * overlap)
    codes: list[str] = []
    if overlap > 0:
        codes.append("SEMANTIC_MATCH")
    tags_l = {t.lower() for t in (rec.tags or [])}
    if "correction" in tags_l or "corrections" in tags_l or looks_like_correction(rec.text):
        codes.append("USER_CORRECTION")
        if overlap > 0:
            score += 0.2
    if "skill" in tags_l or "procedural" in tags_l:
        codes.append("SKILL_MATCH")
        if overlap > 0:
            score += 0.12
    if task_goal:
        gtoks = query_tokens(task_goal)
        if gtoks and (gtoks & ftoks):
            codes.append("ACTIVE_TASK")
            score += 0.1
    if overlap >= 0.5:
        codes.append("ENTITY_MATCH")
    score = max(0.0, min(1.0, score))
    if not codes and score >= 0.01:
        codes.append("SEMANTIC_MATCH")
    return score, codes


def extract_tool_result(content: str) -> str:
    """Keep tool output out of working memory when it is a dump or image bytes."""
    raw = content or ""
    stripped = raw.strip()
    if not stripped:
        return ""
    if stripped.startswith(("data:image", "iVBOR", "/9j/", "\x89PNG")):
        return "[image omitted from working memory; store via memory_write if needed]"
    if len(raw) > TOOL_RESULT_HARD_MAX:
        return (
            raw[:TOOL_RESULT_SOFT_MAX].rstrip()
            + f"\n...[{len(raw)} chars truncated; not stored in working memory]"
        )
    if len(raw) > TOOL_RESULT_SOFT_MAX * 4:
        return raw[:TOOL_RESULT_SOFT_MAX].rstrip() + "\n...[truncated]"
    if looks_like_frame_dump(raw):
        return (
            raw[:240].rstrip()
            + "\n...[perception frames omitted; store semantic events only]"
        )
    return raw


@dataclass
class Turn:
    role: Role
    content: str
    token_count: int
    timestamp: int


@dataclass
class TokenBudget:
    total_context: int
    completion_reserve: int
    available_for_memory: int

    @classmethod
    def for_model(cls, model_id: str) -> TokenBudget:
        mid = (model_id or "").lower()
        # Qwen 3.8 27B Q4/Q5 llama.cpp: native 262k; give memory more than the old 32k/4k cap.
        if "27b" in mid and ("q4" in mid or "q5" in mid or "qwen38-27b" in mid or "qwen3.8-27b" in mid):
            return cls(total_context=262144, completion_reserve=2048, available_for_memory=16000)
        local = (
            mid.startswith("qwen")
            or "muse" in mid
            or mid in {"qwen38", "qwen38-27b", "muse-glimmer"}
        )
        if local:
            return cls(total_context=32768, completion_reserve=1024, available_for_memory=4000)
        return cls(total_context=500000, completion_reserve=8192, available_for_memory=24000)


@dataclass
class MemoryRecord:
    id: int
    text: str
    tags: list[str]
    media_path: str | None
    media_type: MediaType | None
    created_at: int
    rank: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "media_path": self.media_path,
            "media_type": self.media_type.value if self.media_type else None,
            "created_at": self.created_at,
        }


@dataclass
class AssembledContext:
    text: str
    token_count: int
    summary_tokens: int = 0
    retrieval_tokens: int = 0
    tail_tokens: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    retrieval: dict[str, Any] | None = None
    retrieved_ids: list[str] = field(default_factory=list)


@dataclass
class SessionSummary:
    goal: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    dead_ends: list[dict[str, Any]] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "decisions": list(self.decisions),
            "dead_ends": list(self.dead_ends),
            "files_touched": list(self.files_touched),
            "open_questions": list(self.open_questions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SessionSummary:
        if not isinstance(data, dict):
            return cls()
        files = data.get("files_touched") or []
        return cls(
            goal=data.get("goal") if isinstance(data.get("goal"), str) else None,
            decisions=_stamp_list(data.get("decisions")),
            dead_ends=_stamp_list(data.get("dead_ends")),
            files_touched=[str(p) for p in files if str(p).strip()],
            open_questions=_stamp_list(data.get("open_questions")),
        )

    def render(self) -> str:
        lines = [SESSION_MEMORY_MARK]
        if self.goal:
            lines.append(f"Goal: {self.goal}")
        if self.decisions:
            lines.append("Decisions:")
            for item in self.decisions:
                lines.append(f"- {item.get('text', '')}")
        if self.dead_ends:
            lines.append("Dead ends (do not retry):")
            for item in self.dead_ends:
                lines.append(f"- {item.get('text', '')}")
        if self.files_touched:
            lines.append("Files touched: " + ", ".join(self.files_touched))
        if self.open_questions:
            lines.append("Open questions:")
            for item in self.open_questions:
                lines.append(f"- {item.get('text', '')}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> SessionSummary:
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return cls()


def _stamp_list(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append({"text": item.strip(), "timestamp": 0})
        elif isinstance(item, dict) and str(item.get("text") or "").strip():
            try:
                ts = int(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0
            out.append({"text": str(item["text"]).strip(), "timestamp": ts})
    return out


def _now() -> int:
    return int(time.time())


class WorkingMemory:
    def __init__(self, min_verbatim_turns: int = DEFAULT_MIN_VERBATIM) -> None:
        self.min_verbatim_turns = max(1, int(min_verbatim_turns))
        self._turns: deque[Turn] = deque()
        self.total_tokens = 0

    def __len__(self) -> int:
        return len(self._turns)

    def turns(self) -> list[Turn]:
        return list(self._turns)

    def append(self, turn: Turn) -> None:
        self._turns.append(turn)
        self.total_tokens += turn.token_count

    def overflow(self) -> list[Turn]:
        keep = min(self.min_verbatim_turns, len(self._turns))
        if keep >= len(self._turns):
            return []
        n = len(self._turns) - keep
        return [self._turns[i] for i in range(n)]

    def drain_overflow(self) -> list[Turn]:
        drained = self.overflow()
        for _ in drained:
            old = self._turns.popleft()
            self.total_tokens -= old.token_count
        if self.total_tokens < 0:
            self.total_tokens = 0
        return drained

    def tail_newest_first(self) -> list[Turn]:
        return list(reversed(self._turns))


class LongTermStore(Protocol):
    def write(
        self,
        text: str,
        tags: list[str],
        media: tuple[MediaType, Path] | None = None,
    ) -> int: ...

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryRecord]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    tags        TEXT,
    media_path  TEXT,
    media_type  TEXT,
    created_at  INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, tags, content='facts', content_rowid='id'
);
"""

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, tags)
        VALUES('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, tags)
        VALUES('delete', old.id, old.text, old.tags);
    INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


def _fts_match_query(query: str) -> str:
    toks = [t for t in _FTS_TOKEN_RE.findall(query or "") if len(t) >= 3]
    if not toks:
        return '""'
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks[:24])


_TYPE_TAGS = {
    "episodic": ("episodic", "episode", "experience", "event"),
    "semantic": ("semantic", "knowledge", "fact"),
    "procedural": ("procedural", "procedure", "skill", "how-to", "correction", "corrections"),
    "relational": ("relational", "person", "relationship", "people"),
    "self": ("self", "identity", "self-model"),
    "task": ("task", "goal", "checkpoint", "horizon"),
}


def _classify_tags(tags: list[str] | None) -> str:
    tset = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
    if not tset:
        return "unknown"
    for kind, keys in _TYPE_TAGS.items():
        if tset & set(keys):
            return kind
    return "unknown"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bm25_score(rank: float | None) -> float | None:
    if rank is None:
        return None
    try:
        x = float(rank)
    except (TypeError, ValueError):
        return None
    try:
        s = 1.0 / (1.0 + math.exp(x))
    except OverflowError:
        s = 0.0 if x > 0 else 1.0
    return max(0.0, min(1.0, float(s)))


def _memory_source(tags: list[str] | None) -> str:
    kind = _classify_tags(tags)
    return {
        "episodic": "episodic_memory",
        "semantic": "semantic_memory",
        "procedural": "procedural_memory",
        "self": "self_model",
        "task": "active_task",
    }.get(kind, "unknown")


class SqliteStore:
    def __init__(self, db_path: Path, media_dir: Path) -> None:
        self.db_path = db_path
        self.media_dir = media_dir
        self._local = threading.local()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.executescript(_TRIGGERS)
        conn.commit()
        self._local.conn = conn
        return conn

    def write(
        self,
        text: str,
        tags: list[str],
        media: tuple[MediaType, Path] | None = None,
    ) -> int:
        body = (text or "").strip()
        if not body:
            raise ValueError("fact text is required")
        tag_s = " ".join(t.strip() for t in tags if str(t).strip())
        media_path: str | None = None
        media_type: str | None = None
        if media is not None:
            kind, src = media
            media_type = kind.value
            if kind is MediaType.IMAGE:
                media_path = self._store_image(src)
            else:
                media_path = str(src)
        conn = self._connect()
        with conn:
            cur = conn.execute(
                "INSERT INTO facts(text, tags, media_path, media_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (body, tag_s, media_path, media_type, _now()),
            )
            return int(cur.lastrowid)

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryRecord]:
        limit = max(1, min(int(limit or 8), 50))
        match = _fts_match_query(query)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT f.id, f.text, f.tags, f.media_path, f.media_type, f.created_at, "
                "bm25(facts_fts) AS rank "
                "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ? "
                "ORDER BY rank ASC, f.created_at DESC "
                "LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[MemoryRecord] = []
        for row in rows:
            mt = row["media_type"]
            try:
                media_type = MediaType(mt) if mt else None
            except ValueError:
                media_type = None
            tags = [t for t in str(row["tags"] or "").split() if t]
            rank = None
            try:
                if row["rank"] is not None:
                    rank = float(row["rank"])
            except (KeyError, TypeError, ValueError, IndexError):
                rank = None
            out.append(
                MemoryRecord(
                    id=int(row["id"]),
                    text=str(row["text"]),
                    tags=tags,
                    media_path=row["media_path"],
                    media_type=media_type,
                    created_at=int(row["created_at"] or 0),
                    rank=rank,
                )
            )
        return out

    def count(self) -> int:
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) FROM facts").fetchone()
        return int(row[0] if row else 0)

    def get(self, fact_id: int) -> MemoryRecord | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT id, text, tags, media_path, media_type, created_at FROM facts WHERE id=?",
            (int(fact_id),),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row, rank=None)

    def type_counts(self) -> dict[str, int]:
        conn = self._connect()
        rows = conn.execute("SELECT tags FROM facts").fetchall()
        counts = {
            "episodic": 0,
            "semantic": 0,
            "procedural": 0,
            "relational": 0,
            "self": 0,
            "task": 0,
            "unknown": 0,
            "corrections": 0,
            "total": 0,
        }
        for (tags,) in rows:
            tag_list = [t for t in str(tags or "").split() if t]
            kind = _classify_tags(tag_list)
            counts[kind] = counts.get(kind, 0) + 1
            counts["total"] += 1
            if any(t.lower() == "correction" or t.lower() == "corrections" for t in tag_list):
                counts["corrections"] += 1
        return counts

    def list_facts(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        query: str = "",
        type_filter: str = "",
        corrections: bool = False,
    ) -> tuple[list[MemoryRecord], int]:
        limit = max(1, min(int(limit or 50), 100))
        offset = max(0, int(offset or 0))
        conn = self._connect()
        q = (query or "").strip()
        kind = (type_filter or "").strip().lower()
        if kind in {"all", "*"}:
            kind = ""
        if q:
            match = _fts_match_query(q)
            try:
                rows = conn.execute(
                    "SELECT f.id, f.text, f.tags, f.media_path, f.media_type, f.created_at, "
                    "bm25(facts_fts) AS rank "
                    "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? "
                    "ORDER BY rank ASC, f.created_at DESC",
                    (match,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            rows = conn.execute(
                "SELECT id, text, tags, media_path, media_type, created_at FROM facts "
                "ORDER BY created_at DESC, id DESC"
            ).fetchall()
        recs: list[MemoryRecord] = []
        for row in rows:
            rec = self._row_to_record(row, rank=row["rank"] if "rank" in row.keys() else None)
            tags_l = {t.lower() for t in rec.tags}
            if corrections and "correction" not in tags_l and "corrections" not in tags_l:
                continue
            if kind:
                if kind in {"correction", "corrections"}:
                    if "correction" not in tags_l and "corrections" not in tags_l:
                        continue
                elif _classify_tags(rec.tags) != kind:
                    continue
            recs.append(rec)
        total = len(recs)
        return recs[offset : offset + limit], total

    def _row_to_record(self, row: sqlite3.Row, rank: Any = None) -> MemoryRecord:
        mt = row["media_type"]
        try:
            media_type = MediaType(mt) if mt else None
        except ValueError:
            media_type = None
        tags = [t for t in str(row["tags"] or "").split() if t]
        rk = None
        try:
            if rank is not None:
                rk = float(rank)
        except (TypeError, ValueError):
            rk = None
        return MemoryRecord(
            id=int(row["id"]),
            text=str(row["text"]),
            tags=tags,
            media_path=row["media_path"],
            media_type=media_type,
            created_at=int(row["created_at"] or 0),
            rank=rk,
        )

    def _store_image(self, src: Path) -> str:
        src = Path(src)
        data = b""
        if src.is_file():
            data = src.read_bytes()
        digest = hashlib.sha256(data or str(src).encode()).hexdigest()[:12]
        suffix = src.suffix.lower() if src.suffix else ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = ".jpg"
        name = f"{_now()}{digest}{suffix}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        dest = self.media_dir / name
        if src.is_file():
            shutil.copy2(src, dest)
        else:
            dest.write_bytes(data)
        return f"media/{name}"

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None


class Summarizer(Protocol):
    def compact(self, overflow: list[Turn], existing: SessionSummary) -> SessionSummary: ...


class RuleBasedSummarizer:
    """Deterministic fallback used in tests and when the local model is down."""

    def compact(self, overflow: list[Turn], existing: SessionSummary) -> SessionSummary:
        now = _now()
        blob = "\n".join(t.content for t in overflow)
        low = blob.lower()
        summary = SessionSummary.from_dict(existing.to_dict())
        if summary.goal is None:
            for t in overflow:
                if t.role is Role.USER and t.content.strip():
                    summary.goal = t.content.strip().split("\n", 1)[0][:400]
                    break
        files = list(summary.files_touched)
        for m in _PATH_RE.findall(blob):
            p = m.replace("\\", "/")
            if p not in files and not p.startswith("http"):
                files.append(p)
        summary.files_touched = files[:80]
        for t in overflow:
            text = t.content.strip()
            if not text:
                continue
            low_t = text.lower()
            if t.role is Role.ASSISTANT and any(
                k in low_t for k in ("decided", "switching to", "because", "we'll use", "using ")
            ):
                _append_unique(summary.decisions, text[:500], now)
            if any(
                k in low_t
                for k in ("doesn't work", "does not work", "dead end", "failed because", "ruled out")
            ):
                _append_unique(summary.dead_ends, text[:500], now)
            if t.role is Role.USER and "?" in text:
                _append_unique(summary.open_questions, text[:400], now)
        kept_q: list[dict[str, Any]] = []
        for q in summary.open_questions:
            qt = str(q.get("text") or "")
            if qt and qt.lower() in low and any(
                k in low for k in ("answered", "resolved", "done:", "fixed")
            ):
                continue
            kept_q.append(q)
        summary.open_questions = kept_q
        kept_de: list[dict[str, Any]] = []
        for de in summary.dead_ends:
            dt = str(de.get("text") or "")
            if dt and dt.lower() in low and any(
                k in low for k in ("supersede", "no longer a dead end", "actually works now")
            ):
                continue
            kept_de.append(de)
        summary.dead_ends = kept_de
        return summary


def _append_unique(items: list[dict[str, Any]], text: str, ts: int) -> None:
    key = text.lower()
    for it in items:
        if str(it.get("text") or "").lower() == key:
            return
    items.append({"text": text, "timestamp": ts})


class LocalModelSummarizer:
    """Structured extraction via the local VL-8B. Never goes through deskd /v1/llm."""

    def __init__(
        self,
        upstream: str | None = None,
        model: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        # Renamed from HERMES_DESK_VLLM_*; legacy names still honored.
        self.upstream = (
            upstream
            or os.environ.get("HERMES_DESK_FAST")
            or os.environ.get("HERMES_DESK_VLLM_FAST")
            or "http://127.0.0.1:8001"
        ).rstrip("/")
        self.model = (
            model
            or os.environ.get("HERMES_DESK_FAST_MODEL")
            or os.environ.get("HERMES_DESK_VLLM_FAST_MODEL")
            or "qwen3-vl-8b"
        )
        self.timeout = timeout

    def compact(self, overflow: list[Turn], existing: SessionSummary) -> SessionSummary:
        turns = overflow[:MAX_OVERFLOW_TURNS]
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract a session summary. Reply with ONLY JSON, no markdown, keys: "
                        'goal (string or null), decisions (array of {text, timestamp}), '
                        "dead_ends (array of {text, timestamp}), files_touched (array of strings), "
                        "open_questions (array of {text, timestamp}). "
                        "Keep existing dead_ends unless a later turn explicitly supersedes them. "
                        "Drop open_questions that were clearly resolved."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "existing": existing.to_dict(),
                            "overflow": [
                                {"role": t.role.value, "content": t.content, "timestamp": t.timestamp}
                                for t in turns
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        url = self.upstream + "/v1/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode() or "{}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"local summarizer unavailable: {e}") from e
        content = ""
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("local summarizer returned no content")
        parsed = _parse_json_object(content)
        if parsed is None:
            raise RuntimeError("local summarizer did not return JSON")
        return SessionSummary.from_dict(parsed)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


class FallbackSummarizer:
    def __init__(self, primary: Summarizer, fallback: Summarizer) -> None:
        self.primary = primary
        self.fallback = fallback

    def compact(self, overflow: list[Turn], existing: SessionSummary) -> SessionSummary:
        try:
            return self.primary.compact(overflow, existing)
        except Exception:
            return self.fallback.compact(overflow, existing)


class MemoryManager:
    def __init__(
        self,
        root: Path,
        *,
        summarizer: Summarizer | None = None,
        compact_threshold_tokens: int = DEFAULT_COMPACT_THRESHOLD,
        min_verbatim_turns: int = DEFAULT_MIN_VERBATIM,
        model_id: str = "qwen38-27b",
    ) -> None:
        self.root = Path(root)
        self.summary_path = self.root / "session_summary.json"
        self.db_path = self.root / "facts.sqlite"
        self.media_dir = self.root / "media"
        self.compact_threshold_tokens = int(compact_threshold_tokens)
        self.model_id = model_id
        self._lock = threading.RLock()
        self.working = WorkingMemory(min_verbatim_turns=min_verbatim_turns)
        self.summary = SessionSummary.load(self.summary_path)
        self.store = SqliteStore(self.db_path, self.media_dir)
        if summarizer is None:
            summarizer = FallbackSummarizer(LocalModelSummarizer(), RuleBasedSummarizer())
        self.summarizer = summarizer
        self.context_events: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_assembly: AssembledContext | None = None

    @classmethod
    def for_workspace(cls, workspace: Path, model_id: str = "qwen38-27b") -> MemoryManager:
        return cls(Path(workspace) / ".memory", model_id=model_id)

    def ingest_chat_messages(self, messages: list[Any]) -> None:
        with self._lock:
            incoming = [t for t in (_turn_from_message(m) for m in messages or []) if t is not None]
            if not incoming:
                return
            existing = self.working.turns()
            start = _new_suffix_start(existing, incoming)
            for t in incoming[start:]:
                self.working.append(t)
            self._compact_if_needed()

    def append_turn(self, role: Role, content: str) -> None:
        text = extract_tool_result(content) if role is Role.TOOL_RESULT else (content or "")
        turn = Turn(role=role, content=text, token_count=estimate_tokens(text), timestamp=_now())
        with self._lock:
            self.working.append(turn)
            self._compact_if_needed()

    def _compact_if_needed(self) -> None:
        if self.working.total_tokens <= self.compact_threshold_tokens:
            return
        if len(self.working) <= self.working.min_verbatim_turns:
            return
        overflow = self.working.overflow()
        if not overflow:
            return
        before = int(self.working.total_tokens)
        self._persist_durable_overflow(overflow)
        updated = self.summarizer.compact(overflow[:MAX_OVERFLOW_TURNS], self.summary)
        self.summary = updated
        self.summary.save(self.summary_path)
        self.working.drain_overflow()
        after = int(self.working.total_tokens)
        self.context_events.append(
            {
                "type": "CONTEXT_COMPACTED",
                "ts": _iso_now(),
                "before": before,
                "after": after,
                "preserved": ["recent conversation", "session summary", "durable corrections"],
                "removed": ["older turns"],
            }
        )
        self.context_events.append(
            {
                "type": "CONVERSATION_SUMMARIZED",
                "ts": _iso_now(),
                "overflow_turns": len(overflow),
            }
        )
        self.context_events.append(
            {
                "type": "CONTEXT_ITEM_EVICTED",
                "ts": _iso_now(),
                "count": len(overflow),
            }
        )

    def _persist_durable_overflow(self, overflow: list[Turn]) -> int:
        """Store durable corrections/procedures before they leave working memory."""
        wrote = 0
        for t in overflow:
            if t.role is not Role.USER:
                continue
            text = (t.content or "").strip()
            if not text or looks_like_frame_dump(text) or looks_like_body_dump(text):
                continue
            if not looks_like_correction(text):
                continue
            try:
                self.store.write(text[:800], ["correction", "procedural", "user-taught"])
                wrote += 1
            except ValueError:
                continue
        if wrote:
            self.context_events.append(
                {
                    "type": "CORRECTION_LOADED",
                    "ts": _iso_now(),
                    "persisted": wrote,
                    "note": "persisted before eviction",
                }
            )
        return wrote

    def force_compact(self) -> bool:
        """Run compaction if any overflow exists, even below the usual threshold."""
        with self._lock:
            overflow = self.working.overflow()
            if not overflow:
                return False
            prev = self.compact_threshold_tokens
            self.compact_threshold_tokens = 0
            try:
                self._compact_if_needed()
            finally:
                self.compact_threshold_tokens = prev
            return True

    def write(
        self,
        text: str,
        tags: list[str] | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
        workspace: Path | None = None,
    ) -> int:
        media: tuple[MediaType, Path] | None = None
        if media_path:
            kind = MediaType.IMAGE
            if media_type:
                kind = MediaType(media_type)
            src = Path(media_path)
            if not src.is_absolute() and workspace is not None:
                src = Path(workspace) / media_path
            media = (kind, src)
        with self._lock:
            return self.store.write(text, list(tags or []), media)

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryRecord]:
        with self._lock:
            return self.store.retrieve(query, limit=limit)

    def assemble_context(
        self,
        budget: TokenBudget,
        retrieval_query: str,
        *,
        max_retrieve: int | None = None,
        min_relevance: float | None = None,
        max_tail_turns: int | None = None,
        task_goal: str | None = None,
        extra_blocks: list[tuple[str, dict[str, Any]]] | None = None,
        exclude_body_observations: bool = True,
    ) -> AssembledContext:
        cap = max(0, int(budget.available_for_memory))
        max_n = DEFAULT_MAX_RETRIEVE if max_retrieve is None else max(0, int(max_retrieve))
        min_rel = DEFAULT_MIN_RELEVANCE if min_relevance is None else float(min_relevance)
        with self._lock:
            summary_text = self.summary.render()
            candidates = (
                self.store.retrieve(retrieval_query, limit=24) if retrieval_query.strip() else []
            )
            tail = self.working.tail_newest_first()
            if max_tail_turns is not None:
                tail = tail[: max(0, int(max_tail_turns))]
        parts: list[str] = []
        used = 0
        summary_tokens = 0
        retrieval_tokens = 0
        tail_tokens = 0
        items: list[dict[str, Any]] = []
        loaded_at = _iso_now()
        if summary_text:
            n = estimate_tokens(summary_text)
            if used + n <= cap:
                parts.append(summary_text)
                used += n
                summary_tokens = n
                dash_n, est = tel.count_tokens_local(summary_text), True
                items.append(
                    {
                        "id": "session_summary",
                        "section": "active_task" if self.summary.goal else "conversation",
                        "type": "task" if self.summary.goal else "conversation",
                        "title": self.summary.goal or "Session memory",
                        "token_count": dash_n,
                        "estimated": est,
                        "source": "conversation_summary",
                        "reason_codes": ["ACTIVE_TASK", "TASK_CHECKPOINT"]
                        if self.summary.goal
                        else ["RECENT_CONVERSATION"],
                        "pinned": bool(self.summary.goal),
                        "loaded_at": loaded_at,
                        "store": "session_summary.json",
                    }
                )
        retrieval_cap = max(0, int(cap * TIER3_BUDGET_FRACTION))
        fact_lines: list[str] = []
        fact_used = 0
        selected_recs: list[MemoryRecord] = []
        selected_meta: dict[int, tuple[float, list[str]]] = {}
        rejected_recs: list[tuple[MemoryRecord, str]] = []
        ranked: list[tuple[float, MemoryRecord, list[str]]] = []
        seen_text: set[str] = set()
        for rec in candidates:
            if exclude_body_observations and looks_like_body_dump(rec.text):
                rejected_recs.append((rec, "stale_body_observation"))
                continue
            if looks_like_frame_dump(rec.text):
                rejected_recs.append((rec, "perception_flood"))
                continue
            key = re.sub(r"\s+", " ", (rec.text or "").strip().lower())[:240]
            if key in seen_text:
                rejected_recs.append((rec, "duplicate"))
                continue
            seen_text.add(key)
            sc, codes = score_memory_record(rec, retrieval_query, task_goal=task_goal)
            if sc < min_rel and "USER_CORRECTION" not in codes:
                rejected_recs.append((rec, "below_relevance_threshold"))
                continue
            ranked.append((sc, rec, codes))
        ranked.sort(key=lambda x: x[0], reverse=True)
        for sc, rec, codes in ranked:
            if len(selected_recs) >= max_n:
                rejected_recs.append((rec, "over_retrieve_cap"))
                continue
            line = rec.text
            if rec.media_path:
                line += f" [path: {rec.media_path}]"
            n = estimate_tokens(line)
            if fact_used + n > retrieval_cap:
                rejected_recs.append((rec, "over_retrieval_budget"))
                continue
            if used + n > cap:
                rejected_recs.append((rec, "over_memory_budget"))
                continue
            fact_lines.append("- " + line)
            fact_used += n
            used += n
            selected_recs.append(rec)
            selected_meta[rec.id] = (sc, codes)
        if fact_lines:
            block = "# Recalled facts\n" + "\n".join(fact_lines)
            extra = estimate_tokens("# Recalled facts\n") 
            if used + extra <= cap:
                parts.append(block)
                used += extra
                retrieval_tokens = fact_used + extra
            else:
                used -= fact_used
                retrieval_tokens = 0
                rejected_recs.extend((rec, "block_did_not_fit") for rec in selected_recs)
                selected_recs = []
        for rec in selected_recs:
            line = rec.text
            if rec.media_path:
                line += f" [path: {rec.media_path}]"
            dash_n = tel.count_tokens_local(line)
            score, codes = selected_meta.get(rec.id, (_bm25_score(rec.rank), ["SEMANTIC_MATCH"]))
            items.append(
                {
                    "id": f"mem_{rec.id}",
                    "section": "retrieved_memory",
                    "type": _classify_tags(rec.tags),
                    "title": (rec.text or "").strip().split("\n", 1)[0][:80],
                    "token_count": dash_n,
                    "estimated": True,
                    "source": _memory_source(rec.tags),
                    "store": "facts.sqlite",
                    "record": f"mem_{rec.id}",
                    "reason_codes": codes,
                    "relevance_score": score,
                    "confidence": score,
                    "pinned": False,
                    "loaded_at": loaded_at,
                    "last_used": loaded_at,
                    "tags": list(rec.tags),
                    "priority": "high" if score is not None and score >= 0.9 else (
                        "medium" if score is not None and score >= 0.7 else ("low" if score is not None else "unknown")
                    ),
                }
            )
        tail_lines: list[str] = []
        for t in tail:
            line = f"{t.role.value}: {t.content}"
            n = estimate_tokens(line)
            if used + n > cap:
                break
            tail_lines.append(line)
            used += n
            tail_tokens += n
        if tail_lines:
            parts.append("# Recent turns\n" + "\n".join(tail_lines))
            dash_n = tel.count_tokens_local("# Recent turns\n" + "\n".join(tail_lines))
            items.append(
                {
                    "id": "recent_turns",
                    "section": "conversation",
                    "type": "conversation",
                    "title": "Recent conversation",
                    "token_count": dash_n,
                    "estimated": True,
                    "source": "conversation",
                    "reason_codes": ["RECENT_CONVERSATION"],
                    "pinned": False,
                    "loaded_at": loaded_at,
                }
            )
        for block, meta in extra_blocks or []:
            text_b = (block or "").strip()
            if not text_b:
                continue
            n = estimate_tokens(text_b)
            if used + n > cap:
                continue
            parts.append(text_b)
            used += n
            dash_n = tel.count_tokens_local(text_b)
            item = {
                "id": str(meta.get("id") or "workspace_block"),
                "section": str(meta.get("section") or "workspace"),
                "type": str(meta.get("type") or "task"),
                "title": str(meta.get("title") or "Task workspace"),
                "token_count": dash_n,
                "estimated": True,
                "source": str(meta.get("source") or "active_task"),
                "reason_codes": list(meta.get("reason_codes") or ["ACTIVE_TASK", "TASK_CHECKPOINT"]),
                "pinned": bool(meta.get("pinned", True)),
                "loaded_at": loaded_at,
            }
            items.append(item)
        text = "\n\n".join(p for p in parts if p).strip()
        total = estimate_tokens(text) if text else 0
        if total > cap:
            text = text[: max(0, cap * 4)]
            total = estimate_tokens(text)
        injected_dash = 0
        for rec in selected_recs:
            injected_dash += tel.count_tokens_local(rec.text or "")
        retrieval = {
            "query": retrieval_query,
            "query_intent": retrieval_query,
            "candidates_examined": len(candidates),
            "selected": [
                {
                    "id": f"mem_{rec.id}",
                    "title": (rec.text or "").strip().split("\n", 1)[0][:80],
                    "score": selected_meta.get(rec.id, (_bm25_score(rec.rank), []))[0],
                    "reason_codes": selected_meta.get(rec.id, (None, ["SEMANTIC_MATCH"]))[1],
                }
                for rec in selected_recs
            ],
            "rejected": [
                {
                    "id": f"mem_{rec.id}",
                    "title": (rec.text or "").strip().split("\n", 1)[0][:80],
                    "score": _bm25_score(rec.rank),
                    "reason": why,
                }
                for rec, why in rejected_recs[:8]
            ],
            "injected_tokens": injected_dash,
            "injected_tokens_estimated": True,
            "timestamp": loaded_at,
        }
        assembled = AssembledContext(
            text=text,
            token_count=total,
            summary_tokens=summary_tokens,
            retrieval_tokens=retrieval_tokens,
            tail_tokens=tail_tokens,
            items=items,
            retrieval=retrieval,
            retrieved_ids=[f"mem_{rec.id}" for rec in selected_recs],
        )
        self.last_assembly = assembled
        if selected_recs:
            self.context_events.append(
                {
                    "type": "MEMORY_RETRIEVED",
                    "ts": loaded_at,
                    "query_intent": retrieval_query,
                    "selected": len(selected_recs),
                    "candidates_examined": len(candidates),
                    "injected_tokens": injected_dash,
                }
            )
            self.context_events.append(
                {
                    "type": "CONTEXT_ITEM_LOADED",
                    "ts": loaded_at,
                    "count": len(selected_recs),
                    "ids": [f"mem_{rec.id}" for rec in selected_recs],
                }
            )
            if any("USER_CORRECTION" in selected_meta.get(rec.id, (0, []))[1] for rec in selected_recs):
                self.context_events.append(
                    {
                        "type": "CORRECTION_LOADED",
                        "ts": loaded_at,
                        "ids": [
                            f"mem_{rec.id}"
                            for rec in selected_recs
                            if "USER_CORRECTION" in selected_meta.get(rec.id, (0, []))[1]
                        ],
                    }
                )
            if any("SKILL_MATCH" in selected_meta.get(rec.id, (0, []))[1] for rec in selected_recs):
                self.context_events.append(
                    {
                        "type": "SKILL_LOADED",
                        "ts": loaded_at,
                        "ids": [
                            f"mem_{rec.id}"
                            for rec in selected_recs
                            if "SKILL_MATCH" in selected_meta.get(rec.id, (0, []))[1]
                        ],
                    }
                )
        return assembled

    def observe_and_assemble(
        self,
        messages: list[Any],
        retrieval_query: str,
        budget: TokenBudget | None = None,
        **assemble_kw: Any,
    ) -> AssembledContext:
        self.ingest_chat_messages(messages)
        if not retrieval_query.strip():
            retrieval_query = _last_user_text(messages)
        return self.assemble_context(
            budget or TokenBudget.for_model(self.model_id),
            retrieval_query,
            **assemble_kw,
        )


def _last_user_text(messages: list[Any]) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "user":
            continue
        t = _message_text(msg)
        if t:
            return t
    return ""


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                bits.append(str(part.get("text") or ""))
            elif isinstance(part, dict) and part.get("type") in {"image_url", "image"}:
                bits.append("[image omitted]")
        return "\n".join(bits)
    if content is None:
        return str(msg.get("text") or "")
    return str(content)


def _turn_from_message(msg: Any) -> Turn | None:
    if not isinstance(msg, dict):
        return None
    role_s = str(msg.get("role") or "").lower()
    text = _message_text(msg)
    if role_s == "system" and text.startswith(SESSION_MEMORY_MARK):
        return None
    if role_s == "system":
        return None
    if role_s in {"tool", "function"}:
        role = Role.TOOL_RESULT
        text = extract_tool_result(text)
    elif role_s == "assistant":
        role = Role.ASSISTANT
    elif role_s == "user":
        role = Role.USER
    else:
        return None
    if not text:
        return None
    ts = msg.get("timestamp") or msg.get("ts")
    try:
        timestamp = int(ts) if ts else _now()
    except (TypeError, ValueError):
        timestamp = _now()
    return Turn(role=role, content=text, token_count=estimate_tokens(text), timestamp=timestamp)


def _new_suffix_start(existing: list[Turn], incoming: list[Turn]) -> int:
    if not existing:
        return 0
    last = existing[-1]
    for i in range(len(incoming) - 1, -1, -1):
        t = incoming[i]
        if t.role == last.role and t.content == last.content:
            return i + 1
    return 0


def parse_llm_proxy_path(path: str) -> tuple[str | None, str]:
    """Split /v1/llm[/b_<id>]/chat/completions → (bot_id or None, /chat/completions)."""
    raw = path or ""
    if raw.startswith("/v1/llm"):
        raw = raw[len("/v1/llm") :]
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = [p for p in raw.split("/") if p]
    bid: str | None = None
    if parts and parts[0].startswith("b_") and len(parts[0]) > 2:
        bid = parts[0]
        parts = parts[1:]
    rest = "/" + "/".join(parts) if parts else "/"
    return bid, rest


def _message_content_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                bits.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                bits.append(part)
        return "\n".join(bits)
    if content is None:
        return ""
    return str(content)


def inject_assembled_messages(
    payload: dict[str, Any],
    assembled: AssembledContext,
) -> dict[str, Any]:
    """Merge memory into the first system message. Qwen3.8 forbids a second system turn."""
    if not assembled.text:
        return payload
    messages = list(payload.get("messages") or [])
    messages = [
        m
        for m in messages
        if not (
            isinstance(m, dict)
            and str(m.get("role")) == "system"
            and _message_content_str(m.get("content")).lstrip().startswith(SESSION_MEMORY_MARK)
        )
    ]
    block = assembled.text.strip()
    if messages and isinstance(messages[0], dict) and str(messages[0].get("role")) == "system":
        prev = _message_content_str(messages[0].get("content")).rstrip()
        merged = f"{prev}\n\n{block}" if prev else block
        messages[0] = {**messages[0], "content": merged}
    else:
        messages = [{"role": "system", "content": block}] + messages
    payload["messages"] = messages
    return payload
