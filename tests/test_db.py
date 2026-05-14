from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from robomp.db import Database, iso_seconds_ago, issue_key


def test_record_event_dedupes_by_delivery(db: Database) -> None:
    payload = {"action": "opened", "issue": {"number": 1}}
    assert db.record_event(
        delivery_id="abc",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 1),
        payload=payload,
    )
    assert not db.record_event(
        delivery_id="abc",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 1),
        payload=payload,
    )


def test_claim_next_event_singleton_under_contention(db: Database) -> None:
    for i in range(5):
        db.record_event(
            delivery_id=f"d-{i}",
            event_type="issues",
            repo="octo/widget",
            issue_key=issue_key("octo/widget", i),
            payload={"i": i},
        )

    winners: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        row = db.claim_next_event()
        if row is not None:
            with lock:
                winners.append(row.delivery_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(5):
            futures = [pool.submit(claim) for _ in range(8)]
            for f in futures:
                f.result()

    # Each delivery id should appear exactly once.
    assert sorted(winners) == [f"d-{i}" for i in range(5)]
    assert all(db.get_event(f"d-{i}").state == "running" for i in range(5))


def test_reset_stuck_running_recovers(db: Database) -> None:
    db.record_event(
        delivery_id="d1",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={},
    )
    row = db.claim_next_event()
    assert row is not None
    # Simulate crash: row still running.
    recovered = db.reset_stuck_running()
    assert recovered == 1
    assert db.get_event("d1").state == "queued"


def test_upsert_issue_round_trip(db: Database) -> None:
    key = issue_key("octo/widget", 7)
    row = db.upsert_issue(
        key=key, repo="octo/widget", number=7, state="new",
    )
    assert row.state == "new"
    row = db.upsert_issue(
        key=key, repo="octo/widget", number=7, state="opened",
        branch="farm/abcd1234/some-issue", session_dir="/tmp/s",
        pr_number=42,
    )
    assert row.state == "opened"
    assert row.branch == "farm/abcd1234/some-issue"
    assert row.pr_number == 42
    fetched = db.get_issue(key)
    assert fetched and fetched.pr_number == 42

    found = db.find_issue_by_pr("octo/widget", 42)
    assert found and found.key == key


def test_log_tool_call(db: Database) -> None:
    db.upsert_issue(key="octo/widget#1", repo="octo/widget", number=1, state="new")
    row_id = db.log_tool_call(
        issue_key="octo/widget#1",
        tool="gh_post_comment",
        args={"body": "hi"},
        result={"comment_id": 9},
    )
    assert row_id > 0


def test_classification_roundtrip(db: Database) -> None:
    key = issue_key("octo/widget", 7)
    db.upsert_issue(key=key, repo="octo/widget", number=7, state="new")
    row = db.get_issue(key)
    assert row is not None and row.classification is None
    db.set_issue_classification(key, "question")
    row = db.get_issue(key)
    assert row is not None and row.classification == "question"
    # Round-trip via list_issues too.
    items = db.list_issues()
    assert any(r.key == key and r.classification == "question" for r in items)


def test_migration_adds_classification_to_existing_db(tmp_path: Path) -> None:
    """Open a DB without the classification column and verify the migration."""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE events (delivery_id TEXT PRIMARY KEY, event_type TEXT, payload_json TEXT,
          received_at TEXT, state TEXT CHECK(state IN ('queued','running','done','failed','skipped')),
          attempts INTEGER DEFAULT 0, last_error TEXT, repo TEXT, issue_key TEXT,
          started_at TEXT, finished_at TEXT);
        CREATE TABLE issues (key TEXT PRIMARY KEY, repo TEXT, number INTEGER, branch TEXT,
          session_dir TEXT, pr_number INTEGER, state TEXT, updated_at TEXT);
        CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT,
          tool TEXT, args_json TEXT, result_json TEXT, error TEXT, ts TEXT);
        INSERT INTO issues VALUES ('octo/widget#1', 'octo/widget', 1, 'farm/x', '/tmp/s', NULL,
          'reproducing', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit(); conn.close()
    # Opening through our Database class should auto-migrate.
    database = Database(path)
    row = database.get_issue("octo/widget#1")
    assert row is not None
    assert row.classification is None  # column exists, default NULL
    database.set_issue_classification("octo/widget#1", "bug")
    assert database.get_issue("octo/widget#1").classification == "bug"
    database.close()

def test_record_submission_dedupes_by_delivery(db: Database) -> None:
    assert db.record_submission(delivery_id="d-1", login="Alice", repo="octo/widget")
    # Retry of the same delivery id is a no-op (idempotent webhook delivery).
    assert not db.record_submission(delivery_id="d-1", login="alice", repo="octo/widget")


def test_count_submissions_since_is_case_insensitive(db: Database) -> None:
    db.record_submission(delivery_id="d-1", login="Alice", repo="octo/widget")
    db.record_submission(delivery_id="d-2", login="ALICE", repo="octo/widget")
    db.record_submission(delivery_id="d-3", login="bob", repo="octo/widget")
    # Window covering the whole test run.
    since = iso_seconds_ago(60)
    assert db.count_submissions_since("alice", since) == 2
    assert db.count_submissions_since("ALICE", since) == 2
    assert db.count_submissions_since("bob", since) == 1
    assert db.count_submissions_since("nobody", since) == 0


def test_count_submissions_since_respects_window(db: Database) -> None:
    db.record_submission(delivery_id="d-1", login="alice", repo="octo/widget")
    # Future cutoff means the just-inserted row is *before* the window.
    future = iso_seconds_ago(-60)
    assert db.count_submissions_since("alice", future) == 0
