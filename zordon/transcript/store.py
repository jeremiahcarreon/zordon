"""Transcript persistence: SQLite, one file under ``zordon_home()``.

Two tables and a link table:

* ``raw``     every line Claude Code produced (pane or jsonl), redacted
* ``spoken``  every sentence that reached the TTS stage (spoken flag updated on barge-in)
* ``links``   which raw lines a spoken sentence was derived from
* ``events``  user utterances and notices, so the client feed can be rebuilt

``user`` is ``"local"`` everywhere for now (design: multi-user later without a
migration). Thread-safe through a lock; SQLite is opened with
``check_same_thread=False`` because several threads write.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from zordon.bus import TranscriptRow
from zordon.transcript.redaction import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'local',
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS raw_session_ts ON raw(session_id, ts);

CREATE TABLE IF NOT EXISTS spoken (
    id INTEGER PRIMARY KEY,
    sentence_id INTEGER NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'local',
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    kind TEXT NOT NULL,
    spoken INTEGER
);
CREATE INDEX IF NOT EXISTS spoken_session_ts ON spoken(session_id, ts);

CREATE TABLE IF NOT EXISTS links (
    spoken_id INTEGER NOT NULL,
    raw_id INTEGER NOT NULL,
    PRIMARY KEY (spoken_id, raw_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'local',
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_ts ON events(session_id, ts);
"""


class TranscriptStore:
    def __init__(self, db_path: Path | str, user: str = "local") -> None:
        self.path = Path(db_path)
        self.user = user
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._row_ids = _Counter(self._max_row_id())

    # ---- writes ----------------------------------------------------------------

    def add_raw(self, session_id: str, text: str, ts: float | None = None, source: str = "pane") -> int:
        masked, hit = redact(text)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO raw(session_id, user, ts, source, text, redacted) VALUES (?,?,?,?,?,?)",
                (session_id, self.user, ts or time.time(), source, masked, int(hit)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_spoken(
        self,
        sentence_id: int,
        session_id: str,
        text: str,
        raw_text: str,
        kind: str,
        raw_ids: Iterable[int] = (),
        ts: float | None = None,
    ) -> int:
        text, _ = redact(text)
        raw_text, _ = redact(raw_text)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR REPLACE INTO spoken(sentence_id, session_id, user, ts, text, raw_text, kind, spoken)"
                " VALUES (?,?,?,?,?,?,?,NULL)",
                (sentence_id, session_id, self.user, ts or time.time(), text, raw_text, kind),
            )
            spoken_id = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT OR IGNORE INTO links(spoken_id, raw_id) VALUES (?,?)",
                [(spoken_id, int(r)) for r in raw_ids],
            )
            self._conn.commit()
            return spoken_id

    def mark_spoken(self, sentence_id: int, spoken: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE spoken SET spoken=? WHERE sentence_id=?", (int(spoken), sentence_id)
            )
            self._conn.commit()

    def mark_unspoken(self, sentence_id: int) -> None:
        self.mark_spoken(sentence_id, False)

    def add_event(self, session_id: str, kind: str, text: str, ts: float | None = None) -> int:
        text, _ = redact(text)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(session_id, user, ts, kind, text) VALUES (?,?,?,?,?)",
                (session_id, self.user, ts or time.time(), kind, text),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # ---- reads -----------------------------------------------------------------

    def raw_for(self, sentence_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.text FROM raw r JOIN links l ON l.raw_id = r.id"
                " JOIN spoken s ON s.id = l.spoken_id WHERE s.sentence_id=? ORDER BY r.id",
                (sentence_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def tail(self, session_id: str, n: int = 20) -> list[TranscriptRow]:
        """Last ``n`` spoken sentences and events, oldest first, as client rows."""
        with self._lock:
            spoken = self._conn.execute(
                "SELECT id, sentence_id, ts, text, kind, spoken FROM spoken WHERE session_id=?"
                " ORDER BY ts DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
            events = self._conn.execute(
                "SELECT id, ts, kind, text FROM events WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        rows: list[TranscriptRow] = []
        for sid, sentence_id, ts, text, kind, spoken_flag in spoken:
            rows.append(
                TranscriptRow(
                    row_id=sid,
                    session_id=session_id,
                    kind="spoken",
                    text=text,
                    raw_lines=self.raw_for(sentence_id),
                    ts=ts,
                    sentence_id=sentence_id,
                    spoken=None if spoken_flag is None else bool(spoken_flag),
                )
            )
        for eid, ts, kind, text in events:
            rows.append(
                TranscriptRow(
                    row_id=-eid,
                    session_id=session_id,
                    kind=kind if kind in ("user", "notice", "raw") else "notice",
                    text=text,
                    raw_lines=[],
                    ts=ts,
                )
            )
        rows.sort(key=lambda r: r.ts)
        return rows[-n:]

    def spoken_tail_text(self, session_id: str, n: int = 12) -> list[str]:
        """Just the spoken sentences, oldest first: the router/transcript-query context."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT text FROM spoken WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def raw_tail(self, session_id: str, n: int = 50) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT text FROM raw WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, n)
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def last_spoken(self, session_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM spoken WHERE session_id=? ORDER BY ts DESC LIMIT 1", (session_id,)
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def next_row_id(self) -> int:
        return self._row_ids.next()

    def _max_row_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(id),0) FROM spoken").fetchone()
        return int(row[0]) if row else 0


class _Counter:
    def __init__(self, start: int) -> None:
        self._n = start
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n
