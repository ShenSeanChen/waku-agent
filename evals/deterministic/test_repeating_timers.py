"""DETERMINISTIC EVAL: a timer that repeats, and the daily brief set on one
(spec 033).

Ana writes "send me a brief every morning at 9" in a Slack thread. The agent
sets one timer with `repeat = daily`. Each morning the timer loop writes one
event and moves the same row to the next morning. The model is not asked to
set the next one, so a run that fails cannot end the series.

These tests use a real state.db in a temporary home and a fake clock. Ana is
in Madrid: 09:00 on her clock is 08:00 UTC in November and 07:00 UTC in July.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from waku.alwayson import events, handover, outbox, timers, waiting
from waku.alwayson.worker import BRIEF_PROMPT, TIMER_PROMPT, Worker, render
from waku.db import connect

MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")
# Thursday 19 November 2026, 22:00 in Madrid: the evening Ana asks.
THURSDAY_EVENING = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)
FRIDAY_9 = datetime(2026, 11, 20, 9, 0, tzinfo=MADRID)
NOTE = "Daily brief: also say what is due in Linear this week."
THREAD = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})
SESSION = "slack:C1:1700.0001"


class Clock:
    """A clock that only moves when a test moves it."""

    def __init__(self, at: datetime = THURSDAY_EVENING) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ASK", raising=False)
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    return tmp_path


@pytest.fixture
def conn(home):
    connection = connect(home)
    yield connection
    connection.close()


def _add(conn, due_at=FRIDAY_9, repeat="daily", zone="Europe/Madrid", key="e1:t1",
         note=NOTE, address=THREAD):
    return timers.insert(conn, due_at=due_at, session_id=SESSION, note=note, address=address,
                         idempotency_key=key, now=THURSDAY_EVENING, repeat=repeat, zone=zone)


def _loop(conn, clock):
    return timers.TimerLoop(conn, now=clock, log=lambda line: None)


def _events(conn):
    return [(r["external_id"], json.loads(r["payload"]), r["status"])
            for r in conn.execute("SELECT * FROM events ORDER BY id")]


def _days(conn):
    return [external_id for external_id, _, _ in _events(conn)]


def _at(conn, moment: datetime) -> int:
    """How many timers fire when the loop looks at `moment`."""
    return _loop(conn, Clock(moment)).step()


# ---- the loop sets the next occurrence itself -------------------------------

def test_a_daily_timer_fires_and_is_set_for_the_next_day_by_the_loop(conn):
    timer_id = _add(conn)
    assert _at(conn, FRIDAY_9 - timedelta(seconds=1)) == 0

    assert _at(conn, FRIDAY_9) == 1

    ((external_id, payload, status),) = _events(conn)
    assert external_id == f"timer:{timer_id}:2026-11-20"
    assert (payload["text"], status) == (NOTE, "pending")
    assert (payload["channel"], payload["reply_to"]) == THREAD
    assert payload["repeat"] == "every day at 09:00"
    timer = timers.get(conn, timer_id)
    assert timer.status == "pending", "a repeating timer is never marked fired"
    assert timer.due_at == "2026-11-21T08:00:00+00:00", "Saturday 09:00 in Madrid"
    assert (timer.repeat, timer.local_time, timer.zone) == ("daily", "09:00", "Europe/Madrid")


def test_a_daily_timer_fires_once_on_each_of_five_days(conn):
    _add(conn)
    for day in range(5):
        assert _at(conn, FRIDAY_9 + timedelta(days=day, seconds=2)) == 1

    assert _days(conn) == [f"timer:1:2026-11-{20 + day}" for day in range(5)]
    assert conn.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 1, "one row, moved"


def test_a_run_that_fails_does_not_end_the_series(conn):
    """The next day is set when the event is written, before any run starts,
    so what the run does cannot change it."""
    _add(conn)
    _at(conn, FRIDAY_9)

    def run(session_id, batch):
        raise RuntimeError("the model could not be reached")

    Worker(conn, run, batch_wait=0, wait=lambda seconds: None,
           log=lambda line: None).step()
    assert [status for _, _, status in _events(conn)] == ["failed"]

    assert _at(conn, FRIDAY_9 + timedelta(days=1)) == 1
    assert _days(conn)[-1] == "timer:1:2026-11-21"


def test_the_loop_sleeps_until_the_next_days_occurrence(conn):
    _add(conn)
    clock = Clock(FRIDAY_9)
    loop = _loop(conn, clock)
    loop.step()
    clock.advance(hours=23, minutes=59, seconds=50)

    assert loop.sleep_seconds() == 10.0
    assert timers.next_due(conn) == datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


def test_a_timer_without_repeat_still_fires_once_and_is_marked_fired(conn):
    timer_id = _add(conn, repeat="", zone="")
    assert _at(conn, FRIDAY_9) == 1
    assert _at(conn, FRIDAY_9 + timedelta(days=1)) == 0

    assert _days(conn) == [f"timer:{timer_id}"]
    assert timers.get(conn, timer_id).status == "fired"


# ---- one event per occurrence ----------------------------------------------

def test_two_looks_at_the_same_morning_write_one_event(conn):
    _add(conn)
    clock = Clock(FRIDAY_9)
    loop = _loop(conn, clock)

    assert [loop.step(), loop.step()] == [1, 0]
    clock.advance(hours=3)
    assert loop.step() == 0
    assert _days(conn) == ["timer:1:2026-11-20"]


def test_a_process_that_dies_after_the_event_and_before_the_move_fires_once(conn, monkeypatch):
    """The event is written, then the process dies before the timer moves to
    Saturday. The next start finds the timer still due on Friday. It writes
    the same external id, which the inbox drops, and moves the timer."""
    timer_id = _add(conn)
    real_execute = conn.execute

    class Dies:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE timers SET due_at"):
                raise KeyboardInterrupt("the process is killed here")
            return real_execute(sql, *args)

    with pytest.raises(KeyboardInterrupt):
        _at(Dies(conn), FRIDAY_9)
    assert _days(conn) == ["timer:1:2026-11-20"]
    assert timers.get(conn, timer_id).due_at == "2026-11-20T08:00:00+00:00", "not moved"

    _at(conn, FRIDAY_9 + timedelta(minutes=1))

    assert _days(conn) == ["timer:1:2026-11-20"], "no second event for Friday"
    assert timers.get(conn, timer_id).due_at == "2026-11-21T08:00:00+00:00"


def test_a_due_time_moved_back_by_hand_writes_no_second_event_for_that_day(conn):
    _add(conn)
    _at(conn, FRIDAY_9)
    conn.execute("UPDATE timers SET due_at = '2026-11-20T08:00:00+00:00'")
    conn.commit()

    _at(conn, FRIDAY_9 + timedelta(hours=1))

    assert _days(conn) == ["timer:1:2026-11-20"]


# ---- no backlog after the loop was stopped ---------------------------------

def test_after_a_week_stopped_one_late_brief_is_sent_and_then_the_next_day(conn):
    """The laptop was closed from Thursday to the Friday after. It is opened
    at 09:40: the person gets that morning's brief, 40 minutes late, and not
    the seven before it."""
    _add(conn)
    a_week_later = FRIDAY_9 + timedelta(days=7, minutes=40)

    assert _at(conn, a_week_later) == 1

    assert _days(conn) == ["timer:1:2026-11-27"]
    assert timers.get(conn, 1).due_at == "2026-11-28T08:00:00+00:00"
    assert _at(conn, a_week_later + timedelta(hours=1)) == 0
    assert _at(conn, FRIDAY_9 + timedelta(days=8)) == 1
    assert _days(conn) == ["timer:1:2026-11-27", "timer:1:2026-11-28"]


def test_a_morning_missed_by_12_hours_or_more_is_skipped_and_the_next_is_on_time(conn):
    """Opened at 22:30: a morning brief 13 hours late is not sent. Opened at
    08:00: yesterday's brief is 23 hours old, and today's follows in an hour,
    so the person gets today's and not two briefs an hour apart."""
    _add(conn)
    assert _at(conn, FRIDAY_9 + timedelta(days=7, hours=13, minutes=30)) == 0
    assert _days(conn) == []
    assert timers.get(conn, 1).due_at == "2026-11-28T08:00:00+00:00"

    assert _at(conn, FRIDAY_9 + timedelta(days=9, hours=-1)) == 0
    assert _days(conn) == []
    assert _at(conn, FRIDAY_9 + timedelta(days=9)) == 1
    assert _days(conn) == ["timer:1:2026-11-29"]


def test_a_brief_under_12_hours_late_is_sent(conn):
    _add(conn)
    assert _at(conn, FRIDAY_9 + timedelta(hours=11, minutes=59)) == 1
    assert _days(conn) == ["timer:1:2026-11-20"]


# ---- the time zone and daylight saving -------------------------------------

def test_09_00_stays_09_00_when_the_clocks_change_in_the_persons_zone(conn):
    """New York is UTC-5 until 8 March 2026 and UTC-4 after it, and goes back
    to UTC-5 on 1 November. A timer that added 24 hours would fire at 10:00
    from 8 March on."""
    _add(conn, due_at=datetime(2026, 3, 6, 9, 0, tzinfo=NEW_YORK), zone="America/New_York")
    due = []
    for day in (6, 7, 8, 9):
        due.append(timers.get(conn, 1).due_at)
        _at(conn, datetime(2026, 3, day, 9, 0, tzinfo=NEW_YORK))
    assert due == ["2026-03-06T14:00:00+00:00", "2026-03-07T14:00:00+00:00",
                   "2026-03-08T13:00:00+00:00", "2026-03-09T13:00:00+00:00"]
    assert _days(conn) == [f"timer:1:2026-03-0{day}" for day in (6, 7, 8, 9)]

    autumn = datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK)
    assert timers.next_occurrence(autumn, "daily", "09:00", "America/New_York") == datetime(
        2026, 11, 1, 14, 0, tzinfo=UTC)


def test_the_same_request_in_two_zones_fires_at_two_moments(conn):
    madrid = _add(conn, key="a")
    new_york = _add(conn, due_at=datetime(2026, 11, 20, 9, 0, tzinfo=NEW_YORK),
                    zone="America/New_York", key="b")

    assert timers.get(conn, madrid).due_at == "2026-11-20T08:00:00+00:00"
    assert timers.get(conn, new_york).due_at == "2026-11-20T14:00:00+00:00"
    assert _at(conn, FRIDAY_9) == 1, "09:00 in Madrid is 03:00 in New York"
    assert _days(conn) == [f"timer:{madrid}:2026-11-20"]


def test_the_day_of_an_occurrence_is_the_day_in_the_persons_zone(conn):
    """00:30 on Saturday in Madrid is still Friday in UTC. The brief belongs
    to Saturday."""
    _add(conn, due_at=datetime(2026, 11, 21, 0, 30, tzinfo=MADRID))
    _at(conn, datetime(2026, 11, 21, 0, 30, tzinfo=MADRID))
    assert _days(conn) == ["timer:1:2026-11-21"]


def test_a_time_in_the_hour_the_clocks_skip_fires_once_that_day():
    """02:30 does not exist in New York on 8 March 2026. The timer fires when
    the clock says 03:30, and at 02:30 again the day after."""
    saturday = datetime(2026, 3, 7, 2, 30, tzinfo=NEW_YORK)
    sunday = timers.next_occurrence(saturday, "daily", "02:30", "America/New_York")
    monday = timers.next_occurrence(sunday, "daily", "02:30", "America/New_York")

    assert sunday == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert f"{sunday.astimezone(NEW_YORK):%H:%M}" == "03:30"
    assert monday == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_a_time_that_comes_twice_when_the_clocks_go_back_fires_once(conn):
    """01:30 comes twice in New York on 1 November 2026."""
    _add(conn, due_at=datetime(2026, 10, 31, 1, 30, tzinfo=NEW_YORK), zone="America/New_York")
    clock = Clock(datetime(2026, 10, 31, 5, 30, tzinfo=UTC))
    loop = _loop(conn, clock)
    fired = 0
    for _ in range(48 * 4):   # every 15 minutes for two days
        fired += loop.step()
        clock.advance(minutes=15)

    assert fired == 2
    assert _days(conn) == ["timer:1:2026-10-31", "timer:1:2026-11-01"]


def test_a_fixed_offset_is_kept_when_the_machine_does_not_name_its_zone(conn):
    pacific = timezone(timedelta(hours=-8))
    assert timers.zone_name(pacific) == "-08:00"
    assert timers.zone_name(timezone(timedelta(hours=5, minutes=30))) == "+05:30"
    assert timers.zone_of("-08:00").utcoffset(None) == timedelta(hours=-8)
    assert timers.zone_name("Europe/Madrid") == "Europe/Madrid"
    assert timers.zone_name("Mars/Olympus") == ""

    _add(conn, due_at=datetime(2026, 11, 20, 9, 0, tzinfo=pacific), zone="-08:00")
    _at(conn, datetime(2026, 11, 20, 17, 0, tzinfo=UTC))
    assert timers.get(conn, 1).due_at == "2026-11-21T17:00:00+00:00"


def test_the_machines_zone_is_read_from_tz(monkeypatch):
    """A hosted container is started with TZ set to its person's zone."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert timers.local_zone_name() == "Asia/Tokyo"


def test_insert_refuses_a_repeat_or_a_zone_it_does_not_know(conn):
    with pytest.raises(ValueError, match="hourly"):
        _add(conn, repeat="hourly")
    with pytest.raises(ValueError, match="Mars/Olympus"):
        _add(conn, zone="Mars/Olympus")
    with pytest.raises(ValueError, match="needs a time zone"):
        _add(conn, zone="")
    assert conn.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0


# ---- weekdays ---------------------------------------------------------------

def test_a_weekday_timer_skips_saturday_and_sunday(conn):
    _add(conn, repeat="weekdays")
    _at(conn, FRIDAY_9)
    assert timers.get(conn, 1).due_at == "2026-11-23T08:00:00+00:00", "Monday"

    assert _at(conn, FRIDAY_9 + timedelta(days=1)) == 0
    assert _at(conn, FRIDAY_9 + timedelta(days=2)) == 0
    assert _at(conn, FRIDAY_9 + timedelta(days=3)) == 1
    assert _days(conn) == ["timer:1:2026-11-20", "timer:1:2026-11-23"]


def test_a_weekday_timer_found_late_on_saturday_sends_nothing(conn):
    """Friday's brief was missed and the laptop opens on Saturday afternoon,
    29 hours later. Nothing is sent until Monday."""
    _add(conn, repeat="weekdays")
    assert _at(conn, FRIDAY_9 + timedelta(days=1, hours=5)) == 0
    assert _days(conn) == []
    assert timers.get(conn, 1).due_at == "2026-11-23T08:00:00+00:00"


# ---- cancel ends the series ------------------------------------------------

def test_cancel_ends_every_later_day(conn):
    timer_id = _add(conn)
    _at(conn, FRIDAY_9)

    item = waiting.cancel(conn, f"t{timer_id}")

    assert item.repeat == "every day at 09:00"
    assert timers.get(conn, timer_id).status == "cancelled"
    for day in (1, 2, 30):
        assert _at(conn, FRIDAY_9 + timedelta(days=day)) == 0
    assert _days(conn) == ["timer:1:2026-11-20"]
    assert waiting.listing(conn).timers == []
    assert timers.next_due(conn) is None


def test_a_timer_cancelled_while_it_fires_stays_cancelled(conn):
    """The loop read the timer as due, and the person cancelled it before the
    loop moved it. The move must not bring it back."""
    timer_id = _add(conn)
    (read_as_due,) = timers.due(conn, FRIDAY_9)
    waiting.cancel(conn, f"t{timer_id}")

    timers.fire(conn, read_as_due, FRIDAY_9)

    timer = timers.get(conn, timer_id)
    assert timer.status == "cancelled"
    assert timer.due_at == "2026-11-20T08:00:00+00:00", "a cancelled row is not moved"
    assert _at(conn, FRIDAY_9 + timedelta(days=1)) == 0


# ---- a restart --------------------------------------------------------------

def test_a_restart_between_two_days_keeps_the_series(home):
    first = connect(home)
    _add(first)
    _at(first, FRIDAY_9)
    first.close()

    second = connect(home)   # a new process: the schema and the migration run again
    try:
        assert timers.get(second, 1).due_at == "2026-11-21T08:00:00+00:00"
        assert _at(second, FRIDAY_9 + timedelta(hours=2)) == 0
        assert _at(second, FRIDAY_9 + timedelta(days=1)) == 1
        assert _days(second) == ["timer:1:2026-11-20", "timer:1:2026-11-21"]
    finally:
        second.close()


def test_a_database_made_before_timers_could_repeat_keeps_its_timers(tmp_path):
    """The three columns arrive on an existing state.db, and the timer that
    was already waiting in it fires once, as it was set."""
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute(
        "CREATE TABLE timers (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, "
        "due_at TEXT NOT NULL, session_id TEXT NOT NULL, note TEXT NOT NULL, "
        "reply_to TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL DEFAULT 'pending')")
    old.execute("INSERT INTO timers (created_at, due_at, session_id, note, idempotency_key) "
                "VALUES ('2026-11-16T12:00:00+00:00', '2026-11-20T08:00:00+00:00', "
                "'telegram:42', 'Check issue 12.', 'old:t1')")
    old.commit()
    old.close()

    connection = connect(tmp_path)
    connection.close()
    connection = connect(tmp_path)   # a second open adds nothing twice
    try:
        timer = timers.get(connection, 1)
        assert (timer.note, timer.status, timer.repeat) == ("Check issue 12.", "pending", "")
        assert _add(connection, key="new") == 2
        assert _at(connection, FRIDAY_9) == 2
        assert timers.get(connection, 1).status == "fired"
        assert timers.get(connection, 2).status == "pending"
    finally:
        connection.close()


# ---- what a hosted container reports before it stops ------------------------

def test_a_hosted_container_reports_tomorrows_occurrence_after_todays_fired(conn):
    """The gateway starts a stopped container at `next_timer_at`. After
    Friday's brief the report must name Saturday's, or the container sleeps
    through it."""
    _add(conn)
    assert handover.next_work(conn)["next_timer_at"] == "2026-11-20T08:00:00+00:00"

    _at(conn, FRIDAY_9)

    report = handover.next_work(conn)
    assert report["next_timer_at"] == "2026-11-21T08:00:00+00:00"
    assert report["events"] == 1, "Friday's event still waits for its run"


def test_a_hosted_container_reports_the_earlier_of_a_repeating_and_a_single_timer(conn):
    _add(conn)
    _at(conn, FRIDAY_9)
    timers.insert(conn, due_at=FRIDAY_9 + timedelta(hours=5), session_id=SESSION,
                  note="Check issue 12.", address=None, idempotency_key="once")

    assert handover.next_work(conn)["next_timer_at"] == "2026-11-20T13:00:00+00:00"


def test_a_cancelled_repeating_timer_is_not_reported(conn):
    _add(conn)
    waiting.cancel(conn, "t1")
    assert handover.next_work(conn)["next_timer_at"] is None


# ---- the schedule_task tool -------------------------------------------------

def _tool(home, clock=None, tz=MADRID):
    from waku.tools import schedule

    return schedule.make_tool(home, session_id=lambda: SESSION, now=clock or Clock(), tz=tz)


def _timers(home):
    connection = connect(home)
    try:
        return [timers.get(connection, row["id"])
                for row in connection.execute("SELECT id FROM timers ORDER BY id")]
    finally:
        connection.close()


def test_tool_sets_one_daily_timer_in_the_persons_zone(home):
    with outbox.running(home, "e7", THREAD):
        said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat="daily")

    (timer,) = _timers(home)
    assert (timer.repeat, timer.local_time, timer.zone) == ("daily", "09:00", "Europe/Madrid")
    assert timer.due_at == "2026-11-20T08:00:00+00:00"
    assert timer.address == THREAD, "the brief goes to the thread it was asked for in"
    assert said.startswith("Timer 1 repeats every day at 09:00, first on Friday 09:00.")
    assert "do not set it again" in said


def test_tool_says_a_timer_set_in_a_shared_place_answers_there_and_not_privately(home):
    """On 2026-11-20 the hosted agent set a brief in a Slack channel and told
    its person it would arrive privately. Nothing can send that: the woken run
    answers in the thread the timer was set in."""
    with outbox.running(home, "e8", THREAD):
        said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat="daily")

    assert "answers here, in this same conversation" in said
    assert "cannot send a private message" in said


def test_tool_says_nothing_about_a_shared_place_for_a_private_chat(home):
    one_person = ("telegram", {"chat_id": 4242})
    with outbox.running(home, "e9", one_person):
        said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat="daily")

    assert "several people" not in said and "private message" not in said


def test_tool_without_repeat_sets_a_timer_that_fires_once(home):
    said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE)

    (timer,) = _timers(home)
    assert (timer.repeat, timer.local_time, timer.zone) == ("", "", "")
    assert "repeats" not in said and "Friday 09:00" in said


@pytest.mark.parametrize("repeat", ["hourly", "weekly", "every day", "0 9 * * *", "yes"])
def test_tool_refuses_a_repeat_that_is_not_daily_or_weekdays(home, repeat):
    said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat=repeat)

    assert _timers(home) == []
    assert said.startswith("Not set:") and "'daily' or 'weekdays'" in said


def test_tool_reads_the_repeat_whatever_its_case(home):
    _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat=" Weekdays ")
    assert _timers(home)[0].repeat == "weekdays"


def test_tool_still_refuses_a_past_time_and_an_empty_note_with_repeat(home):
    tool = _tool(home)
    assert "past" in tool.fn(when="2026-11-19T09:00", note=NOTE, repeat="daily")
    assert "note" in tool.fn(when="2026-11-20T09:00", note=" ", repeat="daily")
    assert "cannot read" in tool.fn(when="every morning", note=NOTE, repeat="daily")
    assert _timers(home) == []


def test_tool_starts_a_weekday_timer_on_monday_when_the_first_time_is_a_saturday(home):
    said = _tool(home).fn(when="2026-11-21T09:00", note=NOTE, repeat="weekdays")

    (timer,) = _timers(home)
    assert timer.due_at == "2026-11-23T08:00:00+00:00"
    assert "every weekday at 09:00, first on Monday 09:00" in said


def test_tool_takes_the_time_of_day_in_the_persons_zone_when_the_time_carries_an_offset(home):
    """`when` says 08:00 UTC. On Ana's clock that is 09:00."""
    _tool(home).fn(when="2026-11-20T08:00:00+00:00", note=NOTE, repeat="daily")
    assert _timers(home)[0].local_time == "09:00"


def test_tool_called_again_by_a_replayed_run_keeps_one_repeating_timer(home):
    for attempt in range(2):
        with outbox.running(home, "e7", THREAD):
            said = _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat="daily")
        assert ("already" in said) == (attempt == 1)
    assert len(_timers(home)) == 1


def test_tool_offers_repeat_as_an_optional_argument_with_two_values(home):
    tool = _tool(home)
    assert tool.input_schema["properties"]["repeat"]["enum"] == ["daily", "weekdays"]
    assert set(tool.input_schema["required"]) == {"when", "note"}
    assert "Daily brief" in tool.description


def test_tool_on_a_machine_that_names_its_zone_stores_the_name(home, monkeypatch):
    """Without the evals' `tz`, the tool reads the machine: a hosted container
    is started with TZ set to its person's zone."""
    from waku.tools import schedule

    monkeypatch.setenv("TZ", "Europe/Madrid")
    schedule.make_tool(home, session_id=lambda: SESSION, now=Clock()).fn(
        when="2026-11-20T09:00:00+01:00", note=NOTE, repeat="daily")

    (timer,) = _timers(home)
    assert (timer.zone, timer.local_time) == ("Europe/Madrid", "09:00")


# ---- `waku waiting`, list_waiting and the dashboard card ---------------------

def test_listing_says_that_a_timer_repeats_and_when_it_fires_next(conn):
    _add(conn)
    timers.insert(conn, due_at=FRIDAY_9 + timedelta(hours=5), session_id=SESSION,
                  note="Check issue 12.", address=None, idempotency_key="once")

    daily, once = waiting.listing(conn).timers

    assert (daily.repeat, daily.when) == ("every day at 09:00", "2026-11-20T08:00:00+00:00")
    assert once.repeat == ""
    out = waiting.render(waiting.listing(conn), UTC)
    assert "every day at 09:00, next Friday 20 November 09:00   Daily brief" in out
    assert "Friday 20 November 13:00   Check issue 12." in out


def test_listing_shows_the_next_day_after_a_repeating_timer_fired(conn):
    _add(conn, repeat="weekdays")
    _at(conn, FRIDAY_9)

    (item,) = waiting.listing(conn).timers
    assert item.status == "pending"
    assert waiting.timer_when(item) == "every weekday at 09:00, next Monday 23 November 09:00"


def test_list_waiting_tool_says_that_a_timer_repeats(home, conn):
    from waku.tools import waiting as waiting_tool

    _add(conn)
    listed, cancel = waiting_tool.make_tools(home, session_id=lambda: SESSION, tz=UTC)

    assert "t1: every day at 09:00, next Friday 20 November 09:00. Daily brief" in listed.fn()
    said = cancel.fn(id="t1")
    assert said.startswith("Cancelled t1: Daily brief") and "does not repeat any more" in said
    assert timers.get(conn, 1).status == "cancelled"


def test_dashboard_card_data_says_that_a_timer_repeats(home, conn, monkeypatch):
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(home))
    _add(conn)

    (row,) = dashboard.waiting_data()["timers"]
    assert (row["id"], row["repeat"], row["status"]) == ("t1", "every day at 09:00", "pending")
    json.dumps(row)


# ---- the daily brief ---------------------------------------------------------

def _prompt(conn, moment=FRIDAY_9) -> str:
    _at(conn, moment)
    return render([events.claim_next(conn)])


def test_a_brief_timer_wakes_a_run_with_the_steps_of_the_brief(conn):
    _add(conn)
    prompt = _prompt(conn)

    assert NOTE in prompt, "what the person asked the brief to cover"
    assert "which services are connected right now" in prompt
    assert "six short lines at most" in prompt
    assert "never mention that you skipped it" in prompt
    assert "Do not apologise" in prompt
    assert "team board" in prompt
    assert "do not set another timer" in prompt and "every day at 09:00" in prompt
    assert "2026-11-19" in prompt, "the prompt says when the timer was set"


@pytest.mark.parametrize("note", ["Daily brief", "daily brief: Linear first", "  DAILY BRIEF."])
def test_a_note_that_starts_with_daily_brief_is_a_brief(conn, note):
    _add(conn, note=note)
    assert "which services are connected right now" in _prompt(conn)


def test_a_repeating_timer_that_is_not_a_brief_gets_the_note_and_no_brief_steps(conn):
    _add(conn, note="Remind Ana to stretch. The daily brief is another timer.")
    prompt = _prompt(conn)

    assert "which services are connected" not in prompt
    assert "SILENT" in prompt
    assert "do not set another timer" in prompt, "a woken run must not set the next one"


def test_a_timer_that_fires_once_is_not_told_that_it_repeats(conn):
    _add(conn, repeat="", zone="", note="Check issue 12.")
    assert "repeats" not in _prompt(conn)


def test_the_brief_goes_to_the_thread_it_was_asked_for_in_each_morning(home, conn, monkeypatch):
    """The whole path without a model: the tool call in Ana's thread, two
    mornings, and one reply in that thread for each. On hosted Waku a Slack
    reply needs no yes."""
    monkeypatch.setenv("WAKU_HOSTED", "1")
    with outbox.running(home, "e7", THREAD):
        _tool(home).fn(when="2026-11-20T09:00", note=NOTE, repeat="daily")
    seen = []

    def run(session_id, batch):
        seen.append((session_id, render(batch)))
        return f"Brief {len(seen)}: two asks wait for you."

    worker = Worker(conn, run, batch_wait=0, wait=lambda seconds: None,
                    log=lambda line: None)
    for day in (0, 1):
        _at(conn, FRIDAY_9 + timedelta(days=day))
        assert worker.step() is True

    assert [session for session, _ in seen] == [SESSION, SESSION]
    assert all("which services are connected right now" in prompt for _, prompt in seen)
    sent = [(r["channel"], json.loads(r["target"]), r["body"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
    assert sent == [("slack", THREAD[1], "Brief 1: two asks wait for you."),
                    ("slack", THREAD[1], "Brief 2: two asks wait for you.")]


def test_the_two_prompts_take_the_same_two_fields():
    """Either prompt is filled with the time the timer was set and its note."""
    for prompt in (TIMER_PROMPT, BRIEF_PROMPT):
        said = prompt.format(set_at="2026-11-19", note="the note")
        assert "2026-11-19" in said and "the note" in said
