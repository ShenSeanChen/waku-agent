"""DETERMINISTIC EVAL: a timer is an event that arrives later (spec 023, group B).

`schedule_task` writes one row into `timers`. A loop in `waku serve` writes
each due row into `events` with `source = timer`, and the run that follows
reads the note as the reason it woke. These tests use a real state.db in a
temporary home and a fake clock, so they need no model and no real seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

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


# ---- B2: the schedule_task tool ---------------------------------------------

# The person's time zone in these tests: UTC-8, so Monday 12:00 UTC is Monday
# 04:00 for them, and their Thursday 09:00 is Thursday 17:00 UTC.
PACIFIC = timezone(timedelta(hours=-8))


def _tool(home, clock=None, session="default"):
    from waku.tools import schedule

    return schedule.make_tool(home, session_id=lambda: session, now=clock or Clock(),
                              tz=PACIFIC)


def _timers(home):
    connection = connect(home)
    try:
        return [timers.get(connection, row["id"])
                for row in connection.execute("SELECT id FROM timers ORDER BY id")]
    finally:
        connection.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_OWNER", raising=False)
    return tmp_path


def test_tool_sets_a_timer_at_an_absolute_time_in_the_persons_time_zone(home):
    said = _tool(home, session="telegram:42").fn(when="2026-11-19T09:00", note=NOTE)

    (timer,) = _timers(home)
    assert timer.due_at == "2026-11-19T17:00:00+00:00"
    assert (timer.session_id, timer.note, timer.status) == ("telegram:42", NOTE, "pending")
    assert "Thursday" in said and "09:00" in said and "waku serve" in said


def test_tool_keeps_the_offset_a_time_carries(home):
    _tool(home).fn(when="2026-11-19T09:00:00+01:00", note=NOTE)
    assert _timers(home)[0].due_at == "2026-11-19T08:00:00+00:00"


def test_tool_inside_a_run_sends_the_woken_runs_reply_to_that_conversation(home):
    from waku.alwayson import outbox

    with outbox.running(home, "e7", THREAD):
        said = _tool(home, session="slack:C1:1700.0001").fn(when="2026-11-19T09:00", note=NOTE)

    (timer,) = _timers(home)
    assert timer.address == THREAD
    assert timer.session_id == "slack:C1:1700.0001"
    assert "slack:C1:1700.0001" in said


def test_tool_outside_a_run_sends_the_woken_runs_reply_to_the_owner(home, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    said = _tool(home).fn(when="2026-11-19T09:00", note=NOTE)

    assert _timers(home)[0].address == ("telegram", {"chat_id": 42})
    assert "telegram:42" in said


def test_tool_outside_a_run_without_an_owner_says_the_reply_has_nowhere_to_go(home):
    said = _tool(home).fn(when="2026-11-19T09:00", note=NOTE)

    assert _timers(home)[0].address is None
    assert "WAKU_OWNER" in said


def test_tool_called_again_by_a_replayed_run_keeps_one_timer(home):
    from waku.alwayson import outbox

    for attempt in range(2):
        with outbox.running(home, "e7", THREAD):
            first = _tool(home).fn(when="2026-11-19T09:00", note=NOTE)
            _tool(home).fn(when="2026-11-20T09:00", note="Check again.")
        assert ("already" in first) == (attempt == 1)

    assert [t.note for t in _timers(home)] == [NOTE, "Check again."]


def test_tool_refuses_a_time_in_the_past(home):
    # 03:59 for the person, one minute before the clock's 04:00.
    said = _tool(home).fn(when="2026-11-16T03:59", note=NOTE)

    assert _timers(home) == []
    assert "past" in said and "not set" in said.lower()


def test_tool_refuses_a_time_more_than_one_year_away(home):
    tool = _tool(home)
    said = tool.fn(when="2027-11-16T04:01", note=NOTE)
    assert _timers(home) == []
    assert "one year" in said and "not set" in said.lower()

    tool.fn(when="2027-11-16T04:00", note=NOTE)
    assert len(_timers(home)) == 1, "exactly one year away is allowed"


def test_tool_refuses_a_time_it_cannot_read_and_an_empty_note(home):
    tool = _tool(home)
    for when in ("next Thursday", "", "09:00"):
        said = tool.fn(when=when, note=NOTE)
        assert "not set" in said.lower() and "2026-11-19T09:00" in said, said
    assert "note" in tool.fn(when="2026-11-19T09:00", note="  ").lower()
    assert _timers(home) == []


def test_tool_is_a_core_tool_and_no_longer_a_coming_soon_entry(home):
    from waku.config import Settings
    from waku.tools import build_registry, experimental

    settings = Settings(home=home, experimental=False)
    connection = connect(home)
    try:
        registry = build_registry(connection, settings)
    finally:
        connection.close()

    tool = registry.get("schedule_task")
    assert tool is not None, "schedule_task is offered without WAKU_EXPERIMENTAL"
    assert "coming soon" not in tool.description.lower()
    assert set(tool.input_schema["required"]) == {"when", "note"}
    assert "schedule_task" not in [entry["name"] for entry in experimental.PLANNED]


def test_tool_in_a_real_agent_uses_the_session_the_agent_is_in(home):
    """The agent's own tool reads the real clock and the session of the turn."""
    from evals.helpers import make_waku

    tomorrow = (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes")
    agent = make_waku(home, client=object(), episodic_store="sqlite")
    try:
        agent.session.switch("telegram:42")
        said = agent.tools.execute("schedule_task", {"when": tomorrow, "note": NOTE})
    finally:
        agent.close()
        agent.conn.close()

    assert [t.session_id for t in _timers(home)] == ["telegram:42"], said

