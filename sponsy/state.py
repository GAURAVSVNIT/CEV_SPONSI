"""SQLite state + audit log.

The audit log *is* the product: every run, every tool call, every human verdict
lands here. `actions.human_verdict` is the column that gives you an accuracy
number instead of a vibe.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  job         TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  status      TEXT,                      -- running | success | escalated | failed
  steps       INTEGER DEFAULT 0,
  cost_usd    REAL DEFAULT 0,
  summary     TEXT,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS actions (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER REFERENCES runs(id),
  created_at    TEXT,
  kind          TEXT,                    -- draft_email | tool:fetch_page | classify_reply | ...
  target        TEXT,                    -- entity id
  payload       TEXT,                    -- JSON
  confidence    REAL,
  escalated     INTEGER DEFAULT 0,
  human_verdict TEXT                     -- approved | rejected | corrected | NULL
);

CREATE TABLE IF NOT EXISTS memory (
  job        TEXT,
  key        TEXT,
  value      TEXT,
  updated_at TEXT,
  PRIMARY KEY (job, key)
);

CREATE INDEX IF NOT EXISTS ix_actions_run    ON actions(run_id);
CREATE INDEX IF NOT EXISTS ix_actions_target ON actions(target);
CREATE INDEX IF NOT EXISTS ix_runs_job       ON runs(job, started_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around sqlite3. One process, one connection, no ORM."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(CORE_SCHEMA)

    # -- plumbing ----------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        self.conn.executescript(sql)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.query("PRAGMA table_info({})".format(table))}

    # -- runs --------------------------------------------------------------
    def start_run(self, job: str) -> int:
        cur = self.execute(
            "INSERT INTO runs (job, started_at, status) VALUES (?, ?, 'running')",
            (job, utcnow()))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, status: str, summary: str,
                   steps: int = 0, cost_usd: float = 0.0) -> None:
        self.execute(
            "UPDATE runs SET finished_at=?, status=?, summary=?, steps=?, cost_usd=? "
            "WHERE id=?",
            (utcnow(), status, summary, steps, round(cost_usd, 6), run_id))

    def fail_run(self, run_id: int, error: str) -> None:
        self.execute(
            "UPDATE runs SET finished_at=?, status='failed', error=? WHERE id=?",
            (utcnow(), error[:2000], run_id))

    def last_run(self, job: str, status: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM runs WHERE job=?"
        params: list[Any] = [job]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 1"
        return self.one(sql, tuple(params))

    def ran_on(self, job: str, day: str) -> bool:
        """Did this job already finish successfully on `day` (YYYY-MM-DD, UTC)?"""
        row = self.one(
            "SELECT 1 FROM runs WHERE job=? AND status IN ('success','escalated') "
            "AND substr(started_at,1,10)=? LIMIT 1", (job, day))
        return row is not None

    # -- actions -----------------------------------------------------------
    def log_action(self, run_id: int | None, kind: str, target: str = "",
                   payload: Any = None, confidence: float | None = None,
                   escalated: bool = False) -> int:
        cur = self.execute(
            "INSERT INTO actions (run_id, created_at, kind, target, payload, "
            "confidence, escalated) VALUES (?,?,?,?,?,?,?)",
            (run_id, utcnow(), kind, target,
             json.dumps(payload, default=str, ensure_ascii=False)
             if payload is not None else None,
             confidence, int(escalated)))
        return int(cur.lastrowid)

    def set_verdict(self, action_id: int, verdict: str) -> None:
        self.execute("UPDATE actions SET human_verdict=? WHERE id=?",
                     (verdict, action_id))

    # -- memory ------------------------------------------------------------
    def memory_get(self, job: str, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM memory WHERE job=? AND key=?", (job, key))
        return row["value"] if row else default

    def memory_set(self, job: str, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO memory (job, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(job, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (job, key, value, utcnow()))

    def memory_delete(self, job: str, key: str) -> None:
        self.execute("DELETE FROM memory WHERE job=? AND key=?", (job, key))
