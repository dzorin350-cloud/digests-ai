"""SQLite memory: items, stories (what was reported and when), issues, feedback, source health.

One file (state/digest.db) for all digests. Vectors are stored as float32 blobs; at digest scale
(thousands of stories) a numpy dot product beats any vector index, so there is no extension to install.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from digestkit import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  digest TEXT NOT NULL, block TEXT NOT NULL, source_id TEXT NOT NULL,
  url TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', publisher TEXT,
  published_at TEXT, fetched_at TEXT NOT NULL,
  watch TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'new',      -- new|pending|done|dup|old|filtered|baseline|overflow
  reason TEXT, story_id INTEGER, pack_id INTEGER,
  UNIQUE(digest, block, url)
);
CREATE INDEX IF NOT EXISTS items_state ON items(digest, block, state);
CREATE INDEX IF NOT EXISTS items_story ON items(story_id);

CREATE TABLE IF NOT EXISTS stories(
  id INTEGER PRIMARY KEY,
  digest TEXT NOT NULL, block TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  facts TEXT NOT NULL DEFAULT '[]', watch TEXT NOT NULL DEFAULT '[]',
  event_date TEXT,
  status TEXT NOT NULL,                   -- reported|skipped
  reason TEXT,                            -- skipped: stale|irrelevant|repeat|not_selected
  created_at TEXT NOT NULL, reported_at TEXT, updated_at TEXT, reminded_at TEXT,
  thread_id INTEGER, rating INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS stories_block ON stories(digest, block, created_at);

CREATE TABLE IF NOT EXISTS story_events(
  id INTEGER PRIMARY KEY, story_id INTEGER NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- new|update|reminder
  text TEXT NOT NULL, facts TEXT NOT NULL DEFAULT '[]', issue_id INTEGER
);

CREATE TABLE IF NOT EXISTS vectors(
  kind TEXT NOT NULL, ref INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
  PRIMARY KEY(kind, ref, model)
);

CREATE TABLE IF NOT EXISTS packs(
  id INTEGER PRIMARY KEY, digest TEXT NOT NULL, cadence TEXT NOT NULL,
  created_at TEXT NOT NULL, path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',    -- open|published|abandoned
  candidates TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY, digest TEXT NOT NULL, cadence TEXT NOT NULL,
  created_at TEXT NOT NULL, period_start TEXT, period_end TEXT,
  path TEXT, refs TEXT NOT NULL DEFAULT '{}', body TEXT NOT NULL DEFAULT '', pack_id INTEGER,
  delivered TEXT
);

CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY, digest TEXT NOT NULL, issue_id INTEGER, ref TEXT, story_id INTEGER,
  block TEXT, value INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT 'cli', created_at TEXT NOT NULL, ext_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS feedback_ext ON feedback(digest, ext_id) WHERE ext_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sources(
  digest TEXT NOT NULL, block TEXT NOT NULL, source_id TEXT NOT NULL,
  spec TEXT NOT NULL,                     -- Source as JSON
  origin TEXT NOT NULL DEFAULT 'config',  -- config|discovered
  status TEXT NOT NULL DEFAULT 'active',  -- active|trial|muted|broken|disabled|removed
  reason TEXT, added_at TEXT NOT NULL, status_at TEXT,
  last_run TEXT, last_ok TEXT, last_error TEXT, fails INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(digest, block, source_id)
);

CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY, digest TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL
);
"""


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Schema changes after the first release: append (version, SQL) and never edit old entries.
MIGRATIONS: list[tuple[int, str]] = [
    (1, ""),  # initial schema (SCHEMA above)
    (2, "ALTER TABLE issues ADD COLUMN data TEXT NOT NULL DEFAULT '{}';"
        "ALTER TABLE issues ADD COLUMN subject TEXT;"),  # structured issue (HTML cards) and the hook subject
]
RETENTION_TEXT_DAYS = 120  # article texts older than this are blanked (rows stay: URLs keep deduplicating)
RETENTION_JUNK_DAYS = 60  # old / filtered / baseline / overflow items are deleted entirely


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = str(path or paths.db_path())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # timeout: a scheduled run and a manual run may overlap; wait for the lock instead of failing
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        ver = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        for v, sql in MIGRATIONS:
            if v > ver:
                if sql:
                    self.db.executescript(sql)
                self.db.execute(f"PRAGMA user_version={v}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type: object, *a: object) -> None:
        # never keep a half-written run: on an error everything since the last commit is dropped
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        self.close()

    def prune(self, now: datetime) -> dict[str, int]:
        """Keep the file small over years of daily runs. Memory of stories is never pruned."""
        from datetime import timedelta

        junk = self.db.execute(
            "DELETE FROM items WHERE state IN ('old','filtered','baseline','overflow') AND fetched_at < ?",
            (iso(now - timedelta(days=RETENTION_JUNK_DAYS)),)).rowcount
        blank = self.db.execute(
            "UPDATE items SET text='' WHERE text!='' AND fetched_at < ?",
            (iso(now - timedelta(days=RETENTION_TEXT_DAYS)),)).rowcount
        vec = self.db.execute(
            "DELETE FROM vectors WHERE kind IN ('item','itemh') AND ref NOT IN (SELECT id FROM items WHERE text!='')").rowcount
        self.db.execute("DELETE FROM packs WHERE status!='open' AND created_at < ?",
                        (iso(now - timedelta(days=RETENTION_TEXT_DAYS)),))
        return {"items_deleted": junk, "texts_blanked": blank, "vectors_deleted": vec}

    # ---- generic ----
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self.db.execute(sql, tuple(args))
        return int(cur.lastrowid or 0)

    def commit(self) -> None:
        self.db.commit()

    # ---- vectors ----
    def put_vec(self, kind: str, ref: int, model: str, vec: np.ndarray, dtype: type = np.float32) -> None:
        self.x("INSERT OR REPLACE INTO vectors(kind, ref, model, vec) VALUES (?,?,?,?)",
               (kind, ref, model, np.asarray(vec, dtype=dtype).tobytes()))

    def get_vecs(self, kind: str, refs: list[int], model: str, dtype: type = np.float32) -> dict[int, np.ndarray]:
        out: dict[int, np.ndarray] = {}
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.q(f"SELECT ref, vec FROM vectors WHERE kind=? AND model=? AND ref IN ({marks})",
                            [kind, model, *chunk]):
                out[int(r["ref"])] = np.frombuffer(r["vec"], dtype=dtype).astype(np.float32)
        return out

    # ---- changes log (what the system did on its own: sources muted/added, config tuned) ----
    def log_change(self, digest: str, text: str, at: datetime | None = None) -> None:
        self.x("INSERT INTO changes(digest, at, text) VALUES (?,?,?)", (digest, iso(at or now_utc()), text))


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)
