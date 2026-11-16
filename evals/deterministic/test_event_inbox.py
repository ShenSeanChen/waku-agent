"""DETERMINISTIC EVAL: the event inbox loses nothing (spec 023, group A).

A handler writes one row into `events` and returns. One worker claims rows,
batches them per session and starts one run. These tests use a real state.db
in a temporary home, a fake run and a fake wait, so they need no network, no
model and no real seconds.
"""

from __future__ import annotations

import sqlite3

import pytest

from waku.alwayson import events
from waku.db import connect


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path)
    yield connection
    connection.close()


def _add(conn, external_id="1", session_id="telegram:42", text="hello", source="telegram"):
    return events.insert(conn, source=source, external_id=external_id,
                         session_id=session_id, payload={"text": text})


def _statuses(conn):
    return [r["status"] for r in conn.execute("SELECT status FROM events ORDER BY id")]


# ---- A1: the table ---------------------------------------------------------

def test_insert_writes_one_pending_row_with_its_payload(conn):
    event_id = _add(conn, external_id="900", text="are you there?")

    row = conn.execute("SELECT * FROM events").fetchone()
    assert event_id == row["id"]
    assert (row["source"], row["external_id"], row["session_id"], row["status"]) == (
        "telegram", "900", "telegram:42", "pending")
    assert row["received_at"].endswith("+00:00"), "received_at is a UTC time"
    assert events.get(conn, event_id).payload == {"text": "are you there?"}


def test_insert_keeps_the_same_external_id_from_two_sources(conn):
    """UNIQUE covers the pair: Telegram's update 7 is not Slack's event 7."""
    assert _add(conn, external_id="7", source="telegram") is not None
    assert _add(conn, external_id="7", source="slack", session_id="slack:C1:1.0") is not None
    assert len(conn.execute("SELECT id FROM events").fetchall()) == 2


def test_duplicate_external_id_leaves_one_row(conn):
    first = _add(conn, external_id="900", text="first delivery")
    second = _add(conn, external_id="900", text="redelivered")

    assert first is not None
    assert second is None, "a redelivery reports that nothing was inserted"
    rows = conn.execute("SELECT payload FROM events").fetchall()
    assert len(rows) == 1
    assert "first delivery" in rows[0]["payload"]


def test_duplicate_is_refused_by_the_table_itself(conn):
    """The constraint is in the schema, so a writer that skips events.insert
    cannot create a second row either."""
    _add(conn, external_id="900")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (received_at, source, external_id, session_id, payload) "
            "VALUES ('2026-11-16T09:00:00+00:00', 'telegram', '900', 'telegram:42', '{}')")


def test_insert_then_claim_takes_the_oldest_pending_row(conn):
    _add(conn, external_id="1", session_id="telegram:42")
    _add(conn, external_id="2", session_id="telegram:77")

    claimed = events.claim_next(conn)

    assert (claimed.external_id, claimed.session_id) == ("1", "telegram:42")
    assert _statuses(conn) == ["claimed", "pending"]
    assert events.claim_next(conn).external_id == "2"
    assert events.claim_next(conn) is None


def test_insert_then_claim_session_takes_only_that_session(conn):
    _add(conn, external_id="1", session_id="telegram:42")
    _add(conn, external_id="2", session_id="telegram:77")
    _add(conn, external_id="3", session_id="telegram:42")

    batch = events.claim_session(conn, "telegram:42")

    assert [e.external_id for e in batch] == ["1", "3"]
    assert _statuses(conn) == ["claimed", "pending", "claimed"]


def test_insert_then_mark_done_and_failed(conn):
    first = _add(conn, external_id="1")
    second = _add(conn, external_id="2")

    events.mark(conn, [first], "done")
    events.mark(conn, [second], "failed")

    assert _statuses(conn) == ["done", "failed"]
    with pytest.raises(ValueError):
        events.mark(conn, [first], "finished")


def test_recover_returns_claimed_rows_to_pending(conn):
    done = _add(conn, external_id="1")
    _add(conn, external_id="2")
    _add(conn, external_id="3")
    events.mark(conn, [done], "done")
    events.claim_next(conn)

    assert events.recover(conn) == 1
    assert _statuses(conn) == ["done", "pending", "pending"]


def test_recover_survives_a_closed_connection(tmp_path):
    """A claim is on disk, so the process that made it can die."""
    first = connect(tmp_path)
    _add(first, external_id="1")
    events.claim_next(first)
    first.close()

    second = connect(tmp_path)
    try:
        assert _statuses(second) == ["claimed"]
        assert events.recover(second) == 1
        assert events.claim_next(second).external_id == "1"
    finally:
        second.close()


def test_insert_runs_in_wal_mode(conn, tmp_path):
    """`waku serve` and the dashboard are two processes on one state.db. In WAL
    mode a reader does not block the writer. SQLite's default is `delete`."""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    other = sqlite3.connect(tmp_path / "state.db")
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal", (
            "WAL is stored in the file, so every later connection gets it")
    finally:
        other.close()


def test_insert_migrates_a_database_from_before_the_table(tmp_path):
    """A state.db created before spec 023 gains the table on the next connect."""
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, role TEXT NOT NULL, "
                "content TEXT NOT NULL, consolidated INTEGER DEFAULT 0)")
    old.commit()
    old.close()

    upgraded = connect(tmp_path)
    try:
        assert _add(upgraded, external_id="1") is not None
    finally:
        upgraded.close()


# ---- A2: the worker --------------------------------------------------------

class Killed(BaseException):
    """The process dying: not an Exception, so nothing in the worker catches it."""


def _worker(conn, runs, **kwargs):
    from waku.alwayson.worker import Worker

    def run(session_id, batch):
        runs.append((session_id, [e.payload["text"] for e in batch]))

    kwargs.setdefault("wait", lambda seconds: None)
    return Worker(conn, kwargs.pop("run", run), **kwargs)


def test_batch_two_events_for_one_session_make_one_run_that_sees_both(conn):
    """Acceptance 3. The second message arrives during the 3-second wait."""
    runs, waits = [], []
    _add(conn, external_id="1", text="book a room")

    def wait(seconds):
        waits.append(seconds)
        _add(conn, external_id="2", text="for six people")

    worker = _worker(conn, runs, wait=wait)

    assert worker.step() is True
    assert runs == [("telegram:42", ["book a room", "for six people"])]
    assert waits == [3.0], "the worker waits 3 seconds after the first row"
    assert _statuses(conn) == ["done", "done"]
    assert worker.step() is False, "nothing is left to run"


def test_batch_keeps_other_sessions_for_their_own_run(conn):
    runs = []
    _add(conn, external_id="1", session_id="telegram:42", text="from Sean")
    _add(conn, external_id="2", session_id="slack:C1:1.0", text="from Ana", source="slack")
    _add(conn, external_id="3", session_id="telegram:42", text="Sean again")
    worker = _worker(conn, runs)

    while worker.step():
        pass

    assert runs == [("telegram:42", ["from Sean", "Sean again"]),
                    ("slack:C1:1.0", ["from Ana"])]


def test_batch_marks_the_rows_failed_when_the_run_raises(conn):
    _add(conn, external_id="1")
    _add(conn, external_id="2", session_id="telegram:77", text="next")
    runs, logged = [], []

    def run(session_id, batch):
        if session_id == "telegram:42":
            raise RuntimeError("model is down")
        runs.append(session_id)

    worker = _worker(conn, runs, run=run, log=logged.append)
    while worker.step():
        pass

    assert _statuses(conn) == ["failed", "done"]
    assert runs == ["telegram:77"], "one failed run does not stop the worker"
    assert any("RuntimeError" in line for line in logged)


def test_batch_of_a_redelivered_message_is_one_run(conn):
    """Acceptance 2: the same external_id twice leaves one row and one run."""
    runs = []
    _add(conn, external_id="900", text="hello")
    _add(conn, external_id="900", text="hello")
    worker = _worker(conn, runs)

    while worker.step():
        pass

    assert runs == [("telegram:42", ["hello"])]


def test_kill_after_claiming_then_a_new_worker_runs_it_once(tmp_path):
    """Acceptance 1. The first worker dies between the claim and the run."""
    runs = []
    first_conn = connect(tmp_path)
    _add(first_conn, external_id="1", text="do not lose me")

    def die(seconds):
        raise Killed

    with pytest.raises(Killed):
        _worker(first_conn, runs, wait=die).step()
    assert _statuses(first_conn) == ["claimed"]
    assert runs == []
    first_conn.close()

    second_conn = connect(tmp_path)
    try:
        worker = _worker(second_conn, runs)
        assert worker.recover() == 1
        while worker.step():
            pass
        assert runs == [("telegram:42", ["do not lose me"])]
        assert _statuses(second_conn) == ["done"]
    finally:
        second_conn.close()


def test_kill_by_stop_during_the_wait_returns_the_rows_to_pending(conn):
    """SIGTERM during the 3-second wait: no run starts, and nothing is claimed
    when the process exits."""
    runs = []
    _add(conn, external_id="1")
    worker = _worker(conn, runs)
    worker._wait = lambda seconds: worker.stop.set()

    assert worker.step() is False
    assert runs == []
    assert _statuses(conn) == ["pending"]


def test_second_worker_on_the_same_home_is_refused(tmp_path):
    from waku.alwayson.worker import AlreadyRunning, ServeLock

    first = ServeLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as refused:
            ServeLock(tmp_path).acquire()
        assert "already running" in str(refused.value)
    finally:
        first.release()

    again = ServeLock(tmp_path)
    again.acquire()   # the lock goes with its holder
    again.release()


def test_second_worker_in_another_home_starts(tmp_path):
    from waku.alwayson.worker import ServeLock

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = ServeLock(tmp_path / "a"), ServeLock(tmp_path / "b")
    first.acquire()
    second.acquire()
    first.release()
    second.release()


def test_batch_runs_through_respond_in_the_events_own_session(tmp_path):
    """The run is waku.respond(): one turn, in the session the events name,
    with both messages in the one user message."""
    from evals.helpers import ScriptedClient, make_waku, response, text_block
    from waku.alwayson.worker import AgentRun

    home = tmp_path / "home"
    home.mkdir()
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([text_block("Booked for six.")]),
    ])
    printed = []
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=printed.append)
    conn = connect(home)
    try:
        _add(conn, external_id="1", text="book a room")
        _add(conn, external_id="2", text="for six people")
        worker = _worker(conn, [], run=run)
        while worker.step():
            pass

        rows = [(r["session_id"], r["source"], r["role"], r["content"]) for r in conn.execute(
            "SELECT session_id, source, role, content FROM chat_log ORDER BY id")]
    finally:
        run.close()
        conn.close()

    assert rows == [
        ("telegram:42", "telegram", "user", "book a room\n\nfor six people"),
        ("telegram:42", "telegram", "assistant", "Booked for six."),
    ]
    assert any("Booked for six." in line for line in printed)
    assert _statuses_at(home) == ["done", "done"]


def _statuses_at(home):
    connection = connect(home)
    try:
        return _statuses(connection)
    finally:
        connection.close()
