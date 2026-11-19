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
    assert "Thursday 09:00" in said and "always-on mode" in said
    assert "waku serve" not in said, "the model repeats what it is given (spec 024)"


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
    assert said == "Timer 1 is set for Thursday 09:00."


def test_tool_outside_a_run_sends_the_woken_runs_reply_to_the_owner(home, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    said = _tool(home).fn(when="2026-11-19T09:00", note=NOTE)

    assert _timers(home)[0].address == ("telegram", {"chat_id": 42})
    assert "telegram:42" not in said, "the owner's chat id is not handed to the model"


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


# ---- B3: the timer loop -----------------------------------------------------

def _events(conn):
    import json

    return [(r["source"], r["external_id"], r["session_id"], json.loads(r["payload"]),
             r["status"]) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def _loop(conn, clock):
    return timers.TimerLoop(conn, now=clock, log=lambda line: None)


def test_timer_fires_once_as_one_event_with_its_note(home):
    """Acceptance 4: schedule_task for Thursday 09:00, the clock moves to
    Thursday 09:00, and one event with source timer and the note appears."""
    from waku.alwayson import outbox

    clock = Clock()
    with outbox.running(home, "e7", THREAD):
        _tool(home, clock, session="slack:C1:1700.0001").fn(when="2026-11-19T09:00", note=NOTE)
    connection = connect(home)
    try:
        loop = _loop(connection, clock)
        clock.at = datetime(2026, 11, 19, 8, 59, 59, tzinfo=PACIFIC)
        assert loop.step() == 0
        assert _events(connection) == []

        clock.at = datetime(2026, 11, 19, 9, 0, tzinfo=PACIFIC)
        assert loop.step() == 1
        ((source, external_id, session_id, payload, status),) = _events(connection)
        assert (source, external_id, session_id, status) == (
            "timer", "timer:1", "slack:C1:1700.0001", "pending")
        assert payload["text"] == NOTE
        assert (payload["channel"], payload["reply_to"]) == THREAD

        clock.advance(days=30)
        assert loop.step() == 0, "a fired timer never fires again"
        assert len(_events(connection)) == 1
        assert timers.get(connection, 1).status == "fired"
    finally:
        connection.close()


def test_timer_fires_once_when_the_process_dies_between_the_event_and_the_mark(
        tmp_path, monkeypatch):
    """The event is written and the process dies before the timer is marked
    fired. The next start sees the timer as due again and writes no second
    event, because the event's external id is the timer's id."""

    class Killed(BaseException):
        pass

    def die(conn, timer_id):
        raise Killed

    clock = Clock(THURSDAY_9)
    first = connect(tmp_path)
    _add(first)
    with monkeypatch.context() as patch:
        patch.setattr(timers, "mark_fired", die)
        with pytest.raises(Killed):
            _loop(first, clock).step()
    assert timers.get(first, 1).status == "pending"
    assert len(_events(first)) == 1
    first.close()

    second = connect(tmp_path)
    try:
        clock.advance(seconds=30)
        _loop(second, clock).step()
        assert [(e[0], e[1]) for e in _events(second)] == [("timer", "timer:1")]
        assert timers.get(second, 1).status == "fired"
    finally:
        second.close()


def test_two_timers_due_together_each_fires_once_in_due_order(conn):
    _add(conn, due_at=THURSDAY_9, key="e1:t1", note="second")
    _add(conn, due_at=THURSDAY_9 - timedelta(hours=1), key="e1:t2", note="first")

    assert _loop(conn, Clock(THURSDAY_9)).step() == 2
    assert [(e[1], e[3]["text"]) for e in _events(conn)] == [
        ("timer:2", "first"), ("timer:1", "second")]


def test_the_loop_checks_every_30_seconds():
    assert timers.CHECK_SECONDS == 30


def _worker(conn, reply):
    from waku.alwayson.worker import Worker

    run = reply if callable(reply) else (lambda session_id, batch: reply)
    return Worker(conn, run, wait=lambda seconds: None, now=Clock(THURSDAY_9),
                  log=lambda line: None)


def _outbox(conn):
    import json

    return [(r["channel"], json.loads(r["target"]), r["body"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def test_a_run_a_timer_fires_once_replies_to_the_conversation_that_set_it(conn, monkeypatch):
    # Spec 024: a reply to a Slack thread or a GitHub issue waits for a yes
    # unless the owner allows it. These rows are about delivery, so it is on.
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "reply:slack")
    _add(conn)
    _loop(conn, Clock(THURSDAY_9)).step()
    seen = []

    def run(session_id, batch):
        seen.append((session_id, [e.source for e in batch]))
        return "Issue 12 is still open."

    assert _worker(conn, run).step() is True
    assert seen == [("slack:C1:1700.0001", ["timer"])]
    assert _outbox(conn) == [("slack", THREAD[1], "Issue 12 is still open.")]


def test_a_run_a_timer_without_an_address_fires_once_and_writes_no_reply(conn):
    _add(conn, address=None, session_id="default")
    _loop(conn, Clock(THURSDAY_9)).step()

    assert _worker(conn, "Issue 12 is still open.").step() is True
    assert _outbox(conn) == []
    assert [e[4] for e in _events(conn)] == ["done"]


def test_the_prompt_of_a_timer_run_carries_the_note_and_allows_silent(conn):
    """The model can only choose to say nothing when it is told that it may."""
    from waku.alwayson import events
    from waku.alwayson.worker import render

    _add(conn)
    _loop(conn, Clock(THURSDAY_9)).step()
    prompt = render([events.claim_next(conn)])

    assert NOTE in prompt
    assert "SILENT" in prompt
    assert "timer" in prompt.lower()
    assert "2026-11-16" in prompt, "the prompt says when the timer was set"


def test_the_prompt_of_a_message_run_is_the_message_alone(conn):
    from waku.alwayson import events
    from waku.alwayson.worker import render

    events.insert(conn, source="telegram", external_id="1", session_id="telegram:42",
                  payload={"text": "is issue 12 open?", "reply_to": {"chat_id": 42}})
    assert render([events.claim_next(conn)]) == "is issue 12 open?"


def test_a_timer_run_that_answers_silent_sends_nothing_and_the_model_was_told(tmp_path):
    """The whole path with waku.respond(): the Thursday run finds nothing to
    report, answers SILENT, and no outbox row is written."""
    from evals.helpers import ScriptedClient, make_waku, response, text_block
    from waku.alwayson.worker import AgentRun

    class Recording(ScriptedClient):
        def __init__(self, script):
            super().__init__(script)
            self.asked = []

        def _create(self, **kwargs):
            self.asked.append(kwargs)
            return super()._create(**kwargs)

    home = tmp_path / "home"
    home.mkdir()
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = Recording([gate, response([text_block("SILENT")])])
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    connection = connect(home)
    try:
        _add(connection)
        _loop(connection, Clock(THURSDAY_9)).step()
        assert _worker(connection, run).step() is True
        assert _outbox(connection) == []
        assert [e[4] for e in _events(connection)] == ["done"]
    finally:
        run.close()
        connection.close()

    told = [str(m["content"]) for m in client.asked[-1]["messages"] if m["role"] == "user"]
    assert any(NOTE in text and "SILENT" in text for text in told), told


def test_serve_fires_once_a_timer_that_comes_due_while_it_runs(tmp_path):
    """The timer loop runs inside `waku serve`, beside the worker."""
    import time

    from waku.alwayson import serve
    from waku.config import Settings

    settings = Settings(home=tmp_path)
    settings.ensure_home()
    runs = []
    service = serve.Service(
        settings, batch_wait=0, timer_interval=0.01, log=lambda line: None,
        run=lambda session_id, batch: runs.append(
            (session_id, [(e.source, e.payload["text"]) for e in batch])))
    service.start()
    connection = connect(tmp_path)
    try:
        timers.insert(connection, due_at=datetime.now(UTC) - timedelta(seconds=1),
                      session_id="telegram:42", note=NOTE, address=None,
                      idempotency_key="e1:t1")
        timers.insert(connection, due_at=datetime.now(UTC) + timedelta(days=1),
                      session_id="telegram:42", note="not yet", address=None,
                      idempotency_key="e1:t2")
        deadline = time.monotonic() + 5
        while not runs and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)   # several more checks of the loop
    finally:
        service.stop()
        statuses = [r["status"] for r in connection.execute(
            "SELECT status FROM timers ORDER BY id")]
        connection.close()
    assert runs == [("telegram:42", [("timer", NOTE)])]
    assert statuses == ["fired", "pending"]
    assert service.timer_thread is not None and not service.timer_thread.is_alive()


# ---- spec 024, decision 9: the loop sleeps until the next timer -------------

def _run_loop(conn, clock, rounds):
    """Run the loop with a wait that moves the fake clock by what the loop
    asked to sleep, for `rounds` waits. Returns the seconds it asked for."""
    loop = timers.TimerLoop(conn, now=clock, log=lambda line: None)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.advance(seconds=seconds)
        if len(waits) >= rounds:
            loop.stop.set()

    loop.run_forever(wait=wait)
    return waits


def test_on_time_a_timer_due_in_5_seconds_fires_within_2_seconds_of_its_time(conn):
    """Spec 024, acceptance 9. The stretch reminder set for 2:09:00 arrived
    at 2:09:29, because the loop looked every 30 seconds."""
    clock = Clock(MONDAY_NOON)
    due_at = MONDAY_NOON + timedelta(seconds=5)
    _add(conn, due_at=due_at)

    waits = _run_loop(conn, clock, rounds=2)

    assert waits[0] == 5, "the loop sleeps until the timer, not for 30 seconds"
    ((_, _, _, payload, _),) = _events(conn)
    fired_at = conn.execute("SELECT received_at FROM events").fetchone()[0]
    late = datetime.fromisoformat(fired_at) - due_at
    assert timedelta(0) <= late <= timedelta(seconds=2)
    assert payload["text"] == NOTE


def test_on_time_the_loop_sleeps_30_seconds_at_most(conn):
    """Without a timer, and with one that is far away, the loop still looks
    every 30 seconds: a timer set by another process is found then."""
    clock = Clock(MONDAY_NOON)
    assert _run_loop(conn, clock, rounds=2) == [30, 30]

    _add(conn, due_at=clock() + timedelta(seconds=45))
    assert _run_loop(conn, clock, rounds=2) == [30, 15]
    assert len(_events(conn)) == 0
    assert timers.TimerLoop(conn, now=clock, log=lambda line: None).step() == 1


def test_on_time_two_timers_each_fire_at_their_own_time(conn):
    clock = Clock(MONDAY_NOON)
    _add(conn, due_at=MONDAY_NOON + timedelta(seconds=5), key="e1:t1", note="first")
    _add(conn, due_at=MONDAY_NOON + timedelta(seconds=12), key="e1:t2", note="second")

    assert _run_loop(conn, clock, rounds=3)[:2] == [5, 7]
    stamps = [r["received_at"] for r in conn.execute("SELECT received_at FROM events ORDER BY id")]
    assert stamps == ["2026-11-16T12:00:05+00:00", "2026-11-16T12:00:12+00:00"]


def test_on_time_a_timer_set_while_the_loop_sleeps_wakes_it_at_once(tmp_path):
    """schedule_task inserts a row while the loop is in its 30-second sleep.
    The loop wakes for it and does not finish the sleep. Real seconds: the
    test fails if the timer takes two of them."""
    import threading
    import time

    connect(tmp_path).close()   # the tables exist before two threads open the file
    loop = timers.TimerLoop(None, log=lambda line: None)

    def run():
        loop.conn = connect(tmp_path)   # SQLite keeps a connection on its thread
        try:
            loop.run_forever()
        finally:
            loop.conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    other = connect(tmp_path)
    try:
        time.sleep(0.2)   # the loop found nothing and went to sleep
        started = time.monotonic()
        timers.insert(other, due_at=datetime.now(UTC), session_id="telegram:42", note=NOTE,
                      address=None, idempotency_key="e1:t1")
        while time.monotonic() - started < 5:
            if other.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
                break
            time.sleep(0.01)
        took = time.monotonic() - started
        fired = other.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        loop.stop.set()
        thread.join(5)
        other.close()

    assert fired == 1 and took < 2
    assert not thread.is_alive(), "stop wakes the loop out of its sleep too"
