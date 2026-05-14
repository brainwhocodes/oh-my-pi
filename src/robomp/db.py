"""SQLite-backed durable event queue + bot state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

EventState = Literal["queued", "running", "done", "failed", "skipped"]
IssueState = Literal[
    "new",
    "reproducing",
    "fixing",
    "opened",
    "merged",
    "closed",
    "abandoned",
]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
  delivery_id   TEXT PRIMARY KEY,
  event_type    TEXT NOT NULL,
  repo          TEXT,
  issue_key     TEXT,
  payload_json  TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  state         TEXT NOT NULL
    CHECK (state IN ('queued','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  started_at    TEXT,
  finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS events_state_received
  ON events(state, received_at);

CREATE TABLE IF NOT EXISTS issues (
  key            TEXT PRIMARY KEY,
  repo           TEXT NOT NULL,
  number         INTEGER NOT NULL,
  branch         TEXT,
  session_dir    TEXT,
  pr_number      INTEGER,
  state          TEXT NOT NULL,
  classification TEXT,         -- bug|enhancement|question|proposal|documentation|invalid|duplicate
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key     TEXT NOT NULL,
  tool          TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  result_json   TEXT,
  error         TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_issue ON tool_calls(issue_key, ts);

CREATE TABLE IF NOT EXISTS submissions (
  delivery_id   TEXT PRIMARY KEY,
  login         TEXT NOT NULL,
  repo          TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_login_ts ON submissions(login, ts);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_seconds_ago(seconds: float) -> str:
    """ISO-UTC timestamp for `seconds` ago, matching the format `_utcnow` writes."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(slots=True, frozen=True)
class EventRow:
    delivery_id: str
    event_type: str
    repo: str | None
    issue_key: str | None
    payload: dict[str, Any]
    received_at: str
    state: EventState
    attempts: int
    last_error: str | None


@dataclass(slots=True, frozen=True)
class IssueRow:
    key: str
    repo: str
    number: int
    branch: str | None
    session_dir: str | None
    pr_number: int | None
    state: IssueState
    updated_at: str
    classification: str | None = None


def issue_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


class Database:
    """Thread-safe sqlite wrapper. One connection per thread via locks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        # SQLite-friendly forward migrations. Each is idempotent.
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "classification" not in cols:
            self._conn.execute("ALTER TABLE issues ADD COLUMN classification TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- events ----
    def record_event(
        self,
        *,
        delivery_id: str,
        event_type: str,
        repo: str | None,
        issue_key: str | None,
        payload: Mapping[str, Any],
        state: EventState = "queued",
        last_error: str | None = None,
    ) -> bool:
        """Insert a webhook event. Returns False if duplicate (by delivery id).

        `last_error` is the reason text surfaced on the dashboard for non-queued
        states (skipped, failed). Ignored when state == 'queued'.
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO events
                  (delivery_id, event_type, repo, issue_key, payload_json, received_at, state, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    event_type,
                    repo,
                    issue_key,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    state,
                    last_error,
                ),
            )
            return cur.rowcount > 0

    def claim_next_event(self) -> EventRow | None:
        """Atomically dequeue one queued event into running state."""
        with self._txn() as conn:
            row = conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error
                FROM events
                WHERE state = 'queued'
                ORDER BY received_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            now = _utcnow()
            conn.execute(
                "UPDATE events SET state='running', attempts=attempts+1, started_at=? WHERE delivery_id=?",
                (now, row["delivery_id"]),
            )
            return EventRow(
                delivery_id=row["delivery_id"],
                event_type=row["event_type"],
                repo=row["repo"],
                issue_key=row["issue_key"],
                payload=json.loads(row["payload_json"]),
                received_at=row["received_at"],
                state="running",
                attempts=int(row["attempts"]) + 1,
                last_error=row["last_error"],
            )

    def mark_event(self, delivery_id: str, state: EventState, *, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET state=?, last_error=?, finished_at=? WHERE delivery_id=?",
                (state, error, _utcnow(), delivery_id),
            )

    def reset_stuck_running(self) -> int:
        """Recover events that were running at shutdown."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET state='queued' WHERE state='running'",
            )
            return cur.rowcount

    def list_events(self, *, limit: int = 50) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error
                FROM events
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            EventRow(
                delivery_id=row["delivery_id"],
                event_type=row["event_type"],
                repo=row["repo"],
                issue_key=row["issue_key"],
                payload=json.loads(row["payload_json"]),
                received_at=row["received_at"],
                state=row["state"],
                attempts=int(row["attempts"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def remove_event(self, delivery_id: str) -> None:
        """Hard-delete an event row. Used to clear stale state before a manual re-trigger."""
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE delivery_id=?", (delivery_id,))

    def latest_event_for_issue(self, key: str) -> EventRow | None:
        """Return the most recent event whose issue_key matches, or None."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error
                FROM events
                WHERE issue_key = ?
                ORDER BY received_at DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return EventRow(
            delivery_id=row["delivery_id"],
            event_type=row["event_type"],
            repo=row["repo"],
            issue_key=row["issue_key"],
            payload=json.loads(row["payload_json"]),
            received_at=row["received_at"],
            state=row["state"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    def event_state_counts(self) -> dict[str, int]:
        """Return current row counts per event state, including states with zero rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM events GROUP BY state"
            ).fetchall()
        counts: dict[str, int] = {s: 0 for s in ("queued", "running", "done", "failed", "skipped")}
        for row in rows:
            counts[row["state"]] = int(row["n"])
        return counts

    def list_running_events(self) -> list[dict[str, Any]]:
        """Snapshot of currently-running events. Includes started_at for elapsed-time UI."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, received_at,
                       started_at, attempts
                FROM events
                WHERE state = 'running'
                ORDER BY COALESCE(started_at, received_at)
                """
            ).fetchall()
        return [
            {
                "delivery_id": r["delivery_id"],
                "event_type": r["event_type"],
                "repo": r["repo"],
                "issue_key": r["issue_key"],
                "received_at": r["received_at"],
                "started_at": r["started_at"],
                "attempts": int(r["attempts"]),
            }
            for r in rows
        ]

    def get_event(self, delivery_id: str) -> EventRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT delivery_id, event_type, repo, issue_key, payload_json, received_at,
                       state, attempts, last_error
                FROM events WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            return None
        return EventRow(
            delivery_id=row["delivery_id"],
            event_type=row["event_type"],
            repo=row["repo"],
            issue_key=row["issue_key"],
            payload=json.loads(row["payload_json"]),
            received_at=row["received_at"],
            state=row["state"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    def requeue_event(self, delivery_id: str) -> None:
        """Move an event back to queued without clobbering last_error.

        The prior failure text stays visible until a new attempt overwrites it.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE events SET state='queued' WHERE delivery_id=?",
                (delivery_id,),
            )

    # ---- issues ----
    def upsert_issue(
        self,
        *,
        key: str,
        repo: str,
        number: int,
        state: IssueState,
        branch: str | None = None,
        session_dir: str | None = None,
        pr_number: int | None = None,
    ) -> IssueRow:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO issues (key, repo, number, branch, session_dir, pr_number, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  branch = COALESCE(excluded.branch, issues.branch),
                  session_dir = COALESCE(excluded.session_dir, issues.session_dir),
                  pr_number = COALESCE(excluded.pr_number, issues.pr_number),
                  state = excluded.state,
                  updated_at = excluded.updated_at
                """,
                (key, repo, number, branch, session_dir, pr_number, state, now),
            )
        got = self.get_issue(key)
        assert got is not None
        return got

    def set_issue_state(self, key: str, state: IssueState) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET state=?, updated_at=? WHERE key=?",
                (state, _utcnow(), key),
            )

    def set_issue_pr(self, key: str, pr_number: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET pr_number=?, updated_at=? WHERE key=?",
                (pr_number, _utcnow(), key),
            )

    def set_issue_classification(self, key: str, classification: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET classification=?, updated_at=? WHERE key=?",
                (classification, _utcnow(), key),
            )

    def get_issue(self, key: str) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def find_issue_by_pr(self, repo: str, pr_number: int) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE repo=? AND pr_number=?",
                (repo, pr_number),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]),
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def list_issues(self, limit: int = 100) -> list[IssueRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            IssueRow(
                key=r["key"],
                repo=r["repo"],
                number=int(r["number"]),
                branch=r["branch"],
                session_dir=r["session_dir"],
                pr_number=int(r["pr_number"]) if r["pr_number"] is not None else None,
                state=r["state"],
                updated_at=r["updated_at"],
                classification=r["classification"],
            )
            for r in rows
        ]

    # ---- tool_calls ----
    def log_tool_call(
        self,
        *,
        issue_key: str,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tool_calls (issue_key, tool, args_json, result_json, error, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    issue_key,
                    tool,
                    json.dumps(args, separators=(",", ":"), default=str),
                    json.dumps(result, separators=(",", ":"), default=str) if result is not None else None,
                    error,
                    _utcnow(),
                ),
            )
            return int(cur.lastrowid or 0)

    # ---- submissions (per-user rate limiting) ----
    def record_submission(
        self,
        *,
        delivery_id: str,
        login: str,
        repo: str | None,
    ) -> bool:
        """Idempotently log a queue-worthy submission by `login`.

        Returns False if the delivery_id was already recorded (webhook retry).
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO submissions (delivery_id, login, repo, ts) VALUES (?, ?, ?, ?)",
                (delivery_id, login.lower(), repo, now),
            )
            return cur.rowcount > 0

    def count_submissions_since(self, login: str, since: str) -> int:
        """Count submissions by `login` (case-insensitive) with ts >= `since`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                (login.lower(), since),
            ).fetchone()
        return int(row["n"]) if row is not None else 0


_DB_SINGLETON: Database | None = None
_DB_LOCK = threading.Lock()


def get_database(path: Path) -> Database:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is None or _DB_SINGLETON.path != path:
            if _DB_SINGLETON is not None:
                _DB_SINGLETON.close()
            _DB_SINGLETON = Database(path)
        return _DB_SINGLETON


def close_database() -> None:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is not None:
            _DB_SINGLETON.close()
            _DB_SINGLETON = None
