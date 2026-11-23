"""DETERMINISTIC EVAL: every run is one row in `runs` (spec 023, task C1).

The worker writes the row before the run starts and closes it when the run
ends: which events it read, how it ended, and the id of its turn, so a run
joins its receipt and its trace. These tests use a real state.db in a
temporary home, a fake run and a fake clock.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import events, runs
from waku.alwayson.worker import Worker
from waku.db import connect

NOON = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


class Clock:
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


def _event(conn, external_id="1", session_id="telegram:42", text="hello"):
    return events.insert(conn, source="telegram", external_id=external_id,
                         session_id=session_id,
                         payload={"text": text, "reply_to": {"chat_id": 42}})


def _worker(conn, run, clock=None):
    reply = run if callable(run) else (lambda session_id, batch: run)
    return Worker(conn, reply, wait=lambda seconds: None, now=clock or Clock(),
                  log=lambda line: None)


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


def test_start_writes_one_running_row_with_its_events(conn):
    run = runs.start(conn, "telegram:42", [3, 4], now=NOON)

    (row,) = _rows(conn)
    assert row["id"] == run.id
    assert (row["session_id"], row["status"], row["started_at"], row["ended_at"]) == (
        "telegram:42", "running", "2026-11-16T12:00:00+00:00", "")
    assert json.loads(row["event_ids"]) == [3, 4]
    assert (row["pending_call"], row["pending_hash"], row["turn_id"]) == ("", "", "")
    assert runs.get(conn, run.id).event_ids == [3, 4]


def test_finish_refuses_a_status_that_is_not_one(conn):
    run = runs.start(conn, "telegram:42", [1], now=NOON)
    with pytest.raises(ValueError):
        runs.finish(conn, run.id, "paused", now=NOON)
    assert _rows(conn)[0]["status"] == "running"


def test_the_worker_records_a_run_that_ended(conn):
    clock = Clock()
    first = _event(conn, external_id="1", text="one")
    second = _event(conn, external_id="2", text="two")

    def run(session_id, batch):
        assert _rows(conn)[0]["status"] == "running", "the row exists while the run is busy"
        clock.advance(7)
        return runs.Outcome("Both read.", turn_id="t-123")

    assert _worker(conn, run, clock).step() is True

    (row,) = _rows(conn)
    assert json.loads(row["event_ids"]) == [first, second]
    assert (row["status"], row["turn_id"]) == ("done", "t-123")
    assert (row["started_at"], row["ended_at"]) == (
        "2026-11-16T12:00:00+00:00", "2026-11-16T12:00:07+00:00")


def test_the_worker_records_a_run_that_raised(conn):
    _event(conn)

    def run(session_id, batch):
        raise RuntimeError("model is down")

    _worker(conn, run).step()

    (row,) = _rows(conn)
    assert row["status"] == "failed"
    assert row["ended_at"] != ""


def test_the_reply_is_keyed_to_the_run_row(conn):
    """The interim id `e<first event id>` is gone: the outbox names the row."""
    _event(conn, external_id="1", session_id="telegram:42")
    _event(conn, external_id="2", session_id="telegram:77")
    worker = _worker(conn, "Done.")
    while worker.step():
        pass

    ids = [row["id"] for row in _rows(conn)]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert [(r["run_id"], r["idempotency_key"]) for r in conn.execute(
        "SELECT * FROM outbox ORDER BY id")] == [(str(ids[0]), f"{ids[0]}:0"),
                                                  (str(ids[1]), f"{ids[1]}:0")]


def test_a_replayed_run_reuses_its_row_so_its_keys_repeat(tmp_path, monkeypatch):
    """The process dies after the reply is written. The next start runs the
    same events again, in the same row, so the reply's key is the same."""

    class Killed(BaseException):
        pass

    def die(conn, ids, status):
        raise Killed

    first_conn = connect(tmp_path)
    _event(first_conn, text="book a room")
    with monkeypatch.context() as patch:
        patch.setattr(events, "mark", die)
        with pytest.raises(Killed):
            _worker(first_conn, "Booked.").step()
    first_conn.close()

    second_conn = connect(tmp_path)
    try:
        worker = _worker(second_conn, "Booked, again.")
        assert worker.recover() == 1
        while worker.step():
            pass
        (row,) = _rows(second_conn)
        assert row["status"] == "done"
        assert [(r["idempotency_key"], r["body"]) for r in second_conn.execute(
            "SELECT * FROM outbox")] == [(f"{row['id']}:0", "Booked.")]
    finally:
        second_conn.close()


def test_a_run_through_waku_carries_the_turn_id_of_its_receipt(tmp_path):
    from evals.helpers import ScriptedClient, make_waku, response, text_block
    from waku.alwayson.worker import AgentRun

    home = tmp_path / "home"
    home.mkdir()
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([text_block("I am here.")])])
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    conn = connect(home)
    try:
        _event(conn, text="are you there?")
        _worker(conn, run).step()
        (row,) = _rows(conn)
        meta = json.loads(conn.execute(
            "SELECT meta FROM chat_log WHERE role = 'assistant'").fetchone()["meta"])
    finally:
        run.close()
        conn.close()

    assert row["status"] == "done"
    assert row["turn_id"] and row["turn_id"] == meta["turn_id"]
    assert row["started_at"] == "2026-11-16T12:00:00+00:00", (
        "the run reads the worker's row; it does not start the row again")


def test_start_migrates_a_database_from_before_the_table(tmp_path):
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, role TEXT NOT NULL, "
                "content TEXT NOT NULL, consolidated INTEGER DEFAULT 0)")
    old.commit()
    old.close()

    upgraded = connect(tmp_path)
    try:
        assert runs.start(upgraded, "telegram:42", [1], now=NOON).status == "running"
    finally:
        upgraded.close()
