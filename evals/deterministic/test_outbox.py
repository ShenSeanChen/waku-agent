"""DETERMINISTIC EVAL: nothing leaves except through the outbox (spec 023, group D).

A run writes one row into `outbox` per outgoing message, and the dispatcher
sends it. These tests use a real state.db in a temporary home, fake senders
and a fake clock, so they need no network, no model and no real seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import outbox
from waku.db import connect

NOON = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


class Clock:
    """A clock that only moves when a test moves it."""

    def __init__(self, at: datetime = NOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path)
    yield connection
    connection.close()


def _add(conn, key="e1:0", body="I will check issue 12 on Thursday at 09:00", now=NOON,
         channel="telegram", target=None, run_id="e1"):
    return outbox.insert(conn, run_id=run_id, channel=channel,
                         target=target or {"chat_id": 42}, body=body,
                         idempotency_key=key, now=now)


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


# ---- D1: the table ---------------------------------------------------------

def test_insert_writes_one_pending_row_that_is_due_at_once(conn):
    message_id = _add(conn)

    (row,) = _rows(conn)
    assert message_id == row["id"]
    assert (row["run_id"], row["channel"], row["body"], row["idempotency_key"]) == (
        "e1", "telegram", "I will check issue 12 on Thursday at 09:00", "e1:0")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 0, "")
    assert row["next_attempt_at"] == "2026-11-16T12:00:00+00:00"
    assert outbox.get(conn, message_id).target == {"chat_id": 42}


def test_insert_refuses_a_channel_nothing_can_send_on(conn):
    with pytest.raises(ValueError):
        _add(conn, channel="carrier-pigeon")
    assert _rows(conn) == []


def test_replay_of_the_same_key_leaves_one_row(conn):
    """Acceptance 7: a run replayed after a crash writes the same key again,
    and the person gets one reply."""
    first = _add(conn, key="e1:0", body="the first run's reply")
    second = _add(conn, key="e1:0", body="the replayed run's reply")

    assert first is not None
    assert second is None, "a replay reports that nothing was inserted"
    (row,) = _rows(conn)
    assert row["body"] == "the first run's reply"


def test_replay_is_refused_by_the_table_itself(conn):
    """The constraint is in the schema, so a writer that skips outbox.insert
    cannot create a second row either."""
    _add(conn, key="e1:0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outbox (run_id, channel, target, body, idempotency_key, "
            "next_attempt_at) VALUES ('e1', 'telegram', '{}', 'again', 'e1:0', "
            "'2026-11-16T12:00:00+00:00')")


def test_replay_after_the_row_was_sent_does_not_send_it_again(conn):
    message_id = _add(conn, key="e1:0")
    outbox.mark_sent(conn, message_id)

    assert _add(conn, key="e1:0") is None
    assert [r["status"] for r in _rows(conn)] == ["sent"]
    assert outbox.due(conn, NOON + timedelta(days=1)) == []


def test_insert_then_due_returns_only_rows_whose_time_has_passed(conn):
    now = _add(conn, key="e1:0")
    later = _add(conn, key="e2:0", now=NOON + timedelta(minutes=5))

    assert [m.id for m in outbox.due(conn, NOON)] == [now]
    assert [m.id for m in outbox.due(conn, NOON + timedelta(minutes=5))] == [now, later]


def test_insert_then_a_failure_records_the_error_and_the_next_attempt(conn):
    message_id = _add(conn)

    status = outbox.record_failure(conn, message_id, "HTTP 502", NOON)

    assert status == "pending"
    (row,) = _rows(conn)
    assert (row["attempts"], row["last_error"]) == (1, "HTTP 502")
    assert row["next_attempt_at"] == "2026-11-16T12:00:10+00:00", "the first wait is 10 seconds"
    assert outbox.due(conn, NOON + timedelta(seconds=9)) == []
    assert [m.id for m in outbox.due(conn, NOON + timedelta(seconds=10))] == [message_id]


def test_insert_migrates_a_database_from_before_the_table(tmp_path):
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, role TEXT NOT NULL, "
                "content TEXT NOT NULL, consolidated INTEGER DEFAULT 0)")
    old.commit()
    old.close()

    upgraded = connect(tmp_path)
    try:
        assert _add(upgraded) is not None
    finally:
        upgraded.close()
