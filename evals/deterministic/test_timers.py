"""DETERMINISTIC EVAL: a timer is an event that arrives later (spec 023, group B).

`schedule_task` writes one row into `timers`. A loop in `waku serve` writes
each due row into `events` with `source = timer`, and the run that follows
reads the note as the reason it woke. These tests use a real state.db in a
temporary home and a fake clock, so they need no model and no real seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import timers
from waku.db import connect

# Monday 16 November 2026, 12:00 UTC. Thursday 09:00 UTC is 69 hours later.
MONDAY_NOON = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
THURSDAY_9 = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
NOTE = "Check whether issue 12 in acme/app is still open."
THREAD = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})


class Clock:
    """A clock that only moves when a test moves it."""

    def __init__(self, at: datetime = MONDAY_NOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path)
    yield connection
    connection.close()


def _add(conn, due_at=THURSDAY_9, key="e1:t1", session_id="slack:C1:1700.0001",
         note=NOTE, address=THREAD):
    return timers.insert(conn, due_at=due_at, session_id=session_id, note=note,
                         address=address, idempotency_key=key, now=MONDAY_NOON)


# ---- B1: the table ---------------------------------------------------------

def test_insert_writes_one_pending_row_with_its_session_and_note(conn):
    timer_id = _add(conn)

    timer = timers.get(conn, timer_id)
    assert (timer.session_id, timer.note, timer.status) == (
        "slack:C1:1700.0001", NOTE, "pending")
    assert timer.address == THREAD
    assert timer.due_at == "2026-11-19T09:00:00+00:00"
    assert timer.created_at == "2026-11-16T12:00:00+00:00"


def test_insert_stores_the_due_time_in_utc_whatever_zone_it_was_given_in(conn):
    from datetime import timezone

    madrid = timezone(timedelta(hours=1))
    timer_id = _add(conn, due_at=datetime(2026, 11, 19, 10, 0, tzinfo=madrid))
    assert timers.get(conn, timer_id).due_at == "2026-11-19T09:00:00+00:00"


def test_insert_twice_with_one_key_keeps_one_row(conn):
    """A run that is replayed after a crash calls schedule_task again."""
    first = _add(conn)
    assert _add(conn) is None
    assert [row["id"] for row in conn.execute("SELECT id FROM timers")] == [first]


def test_insert_without_an_address_is_allowed(conn):
    timer_id = _add(conn, address=None, session_id="default")
    assert timers.get(conn, timer_id).address is None


def test_due_lists_only_pending_rows_whose_time_has_passed(conn):
    early = _add(conn, due_at=THURSDAY_9 - timedelta(hours=1), key="e1:t1")
    on_time = _add(conn, due_at=THURSDAY_9, key="e1:t2")
    _add(conn, due_at=THURSDAY_9 + timedelta(seconds=1), key="e1:t3")

    assert timers.due(conn, THURSDAY_9 - timedelta(hours=2)) == []
    assert [t.id for t in timers.due(conn, THURSDAY_9)] == [early, on_time]


def test_due_leaves_out_a_timer_that_is_marked_fired(conn):
    timer_id = _add(conn)
    timers.mark_fired(conn, timer_id)

    assert timers.due(conn, THURSDAY_9) == []
    assert timers.get(conn, timer_id).status == "fired"


def test_insert_into_a_database_made_before_timers_existed(tmp_path):
    """The table arrives on an existing state.db the next time it is opened."""
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, role TEXT NOT NULL, "
                "content TEXT NOT NULL, consolidated INTEGER DEFAULT 0, "
                "created_at TEXT DEFAULT (datetime('now')))")
    old.commit()
    old.close()

    connection = connect(tmp_path)
    try:
        assert _add(connection) == 1
    finally:
        connection.close()
