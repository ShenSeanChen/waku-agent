"""DETERMINISTIC EVAL: a call that needs a yes waits for it (spec 023, group C).

Each tool has a mode, `allow` or `ask`. Under `waku serve` a call to an `ask`
tool does not run: the run parks with the exact call stored, and one outbox
row asks the owner. The owner's "yes" arrives as an event and code runs the
stored call; "no" discards it; a "yes" from anyone else runs nothing.

These tests use a real state.db in a temporary home, a scripted model, a fake
`gh`, fake senders and a fake clock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.alwayson import approvals, events, outbox, runs
from waku.alwayson.dispatcher import Dispatcher
from waku.alwayson.senders import github as github_sender
from waku.alwayson.worker import AgentRun, Worker
from waku.db import connect

THURSDAY = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
COMMENT = {"to": "github:acme/app#12", "body": "Sean will review this issue this week."}
COMMENT_HASH = hashlib.sha256(json.dumps(
    {"tool": "send_message", "args": COMMENT}, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()
THREAD = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})
GATE = '{"retrieve": false, "query": "", "reason": "test"}'


class Clock:
    def __init__(self, at: datetime = THURSDAY) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


class FakeGh:
    """Stands in for the `gh` CLI. It records every argv it is given."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "issue", "view"]:
            return True, json.dumps({"comments": []})
        return True, "https://github.com/acme/app/issues/12#issuecomment-1"

    @property
    def comments(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[:3] == ["gh", "issue", "comment"]]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ASK", raising=False)
    return tmp_path


@pytest.fixture
def conn(home):
    connection = connect(home)
    yield connection
    connection.close()


def _slack(conn, text="comment on issue 12", external_id="Ev1"):
    return events.insert(conn, source="slack", external_id=external_id,
                         session_id="slack:C1:1700.0001",
                         payload={"text": text, "sender": {"id": "U_ANA"},
                                  "reply_to": THREAD[1]})


def _telegram(conn, text, external_id="900", sender="42", chat_id=42):
    return events.insert(conn, source="telegram", external_id=external_id,
                         session_id=f"telegram:{chat_id}",
                         payload={"text": text, "sender": {"id": sender, "name": "Sean"},
                                  "reply_to": {"chat_id": chat_id}})


def _outbox(conn):
    return [(r["channel"], json.loads(r["target"]), r["body"], r["idempotency_key"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def _run_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


def _park(conn, home, clock, args=COMMENT, external_id="Ev1"):
    """A run in Ana's Slack thread that called send_message with `args` and
    parked. Returns the run's id."""
    event_id = _slack(conn, external_id=external_id)
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    with outbox.running(home, str(run.id), THREAD):
        deny, _ = approvals.Gate(home, conn, now=clock)("send_message", args)
    assert deny is not None
    events.mark(conn, [event_id], "done")
    runs.finish(conn, run.id, "parked", now=clock())
    return run.id


class Recorder:
    """A fake run and a fake executor for the worker."""

    def __init__(self, reply="SILENT") -> None:
        self.reply, self.runs, self.executed = reply, [], []

    def __call__(self, session_id, batch):
        from waku.alwayson.worker import render

        self.runs.append((session_id, render(batch)))
        return self.reply

    def execute(self, run, address):
        self.executed.append((run.pending_call["tool"], run.pending_call["args"], address))
        return "Queued for github:acme/app#12."


def _worker(conn, run, clock):
    return Worker(conn, run, wait=lambda seconds: None, now=clock, log=lambda line: None)


def _drain(worker):
    while worker.step():
        pass


# ---- C2: modes -------------------------------------------------------------

def test_modes_a_tool_that_reads_or_writes_for_the_person_is_allow():
    for tool in ("list_events", "create_event", "save_note", "search_web", "schedule_task",
                 "github_read", "manage_memory"):
        assert approvals.mode(tool, {}) == "allow", tool


def test_modes_send_message_asks_unless_it_goes_to_the_owner_or_the_conversation(home):
    assert approvals.mode("send_message", {"to": "owner", "body": "hi"}) == "allow"
    assert approvals.mode("send_message", {"to": "telegram:42", "body": "hi"}) == "allow"
    assert approvals.mode("send_message", {"to": "slack:C1:1700.0001", "body": "hi"},
                          address=THREAD) == "allow"
    for to in ("github:acme/app#12", "telegram:77", "slack:C2", "Alex", ""):
        assert approvals.mode("send_message", {"to": to, "body": "hi"},
                              address=THREAD) == "ask", to


def test_modes_every_built_in_tool_has_a_row_in_the_table(home):
    """A new tool does not get a mode by accident: its author names one."""
    waku = make_waku(home, client=ScriptedClient([]), episodic_store="sqlite",
                     experimental=True, gh_tool=True)
    try:
        names = [schema["name"] for schema in waku.tools.schemas()]
    finally:
        waku.close()
        waku.conn.close()
    assert "send_message" in names and "create_skill" in names
    assert [name for name in names if name not in approvals.MODES] == []


def test_modes_env_overrides_one_tool_at_a_time(home, monkeypatch):
    monkeypatch.setenv("WAKU_APPROVE_ASK", "create_event, save_note")
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "send_message")

    assert approvals.mode("create_event", {}) == "ask"
    assert approvals.mode("save_note", {}) == "ask"
    assert approvals.mode("list_events", {}) == "allow"
    assert approvals.mode("send_message", {"to": "telegram:77", "body": "hi"}) == "allow"


def test_modes_ask_wins_when_a_tool_is_in_both_lists(home, monkeypatch):
    monkeypatch.setenv("WAKU_APPROVE_ASK", "search_web")
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "search_web")
    assert approvals.mode("search_web", {}) == "ask"


def test_modes_who_may_approve_is_the_owner(home, monkeypatch):
    """The one function a team layer replaces. v1 answers with the owner."""
    assert approvals.who_may_approve("send_message", COMMENT) == [
        ("telegram", {"chat_id": 42})]
    monkeypatch.delenv("WAKU_OWNER")
    assert approvals.who_may_approve("send_message", COMMENT) == []


# ---- C3: parking -----------------------------------------------------------

def test_park_an_ask_call_stores_it_asks_the_owner_and_runs_nothing(home):
    """Acceptance 5, through waku.respond()."""
    client = ScriptedClient([
        response([text_block(GATE)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block("SILENT")])])
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    gh, told, clock = FakeGh(), [], Clock()
    conn = connect(home)
    try:
        _slack(conn)
        _worker(conn, run, clock).step()
        Dispatcher(conn, {"github": github_sender.make(run=gh),
                          "telegram": lambda m: told.append((m.target, m.body))},
                   now=clock, log=lambda line: None).step()
        (row,) = _run_rows(conn)
        rows = _outbox(conn)
        tool_result = conn.execute(
            "SELECT content FROM chat_log WHERE content LIKE '%send_message%'").fetchall()
    finally:
        run.close()
        conn.close()

    assert gh.calls == [], "nothing reached gh"
    assert row["status"] == "parked"
    assert json.loads(row["pending_call"])["tool"] == "send_message"
    assert json.loads(row["pending_call"])["args"] == COMMENT
    assert row["pending_hash"] == COMMENT_HASH
    assert len(rows) == 1, "one row: the request. The run's SILENT reply writes none"
    channel, target, body, key = rows[0]
    assert (channel, target) == ("telegram", {"chat_id": 42})
    assert key == f"{row['id']}:ask"
    assert 'Comment on acme/app#12: "Sean will review this issue this week."' in body
    assert COMMENT_HASH[:8] in body and "yes" in body and "no" in body
    assert told == [({"chat_id": 42}, body)]
    assert tool_result, "the turn is in the chat log"
    assert "approval" in tool_result[0]["content"]


def test_park_tells_the_model_the_call_waits(home, conn):
    clock = Clock()
    event_id = _slack(conn)
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    with outbox.running(home, str(run.id), THREAD):
        deny, extra = approvals.Gate(home, conn, now=clock)("send_message", COMMENT)

    assert "waits for the owner's approval" in deny and "has not run" in deny
    assert extra == ""
    assert runs.get(conn, run.id).pending_hash == COMMENT_HASH


def test_park_lets_an_allow_call_through(home, conn):
    clock = Clock()
    event_id = _slack(conn)
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    gate = approvals.Gate(home, conn, now=clock)
    with outbox.running(home, str(run.id), THREAD):
        assert gate("list_events", {}) == (None, "")
        assert gate("send_message", {"to": "owner", "body": "hi"}) == (None, "")
        assert gate("send_message", {"to": "slack:C1:1700.0001", "body": "hi"}) == (None, "")
    assert _outbox(conn) == []
    assert runs.get(conn, run.id).pending_call is None


def test_park_only_happens_inside_a_run(home, conn):
    """The terminal and the dashboard have no worker to resume a parked call.
    The gate stays out of the way there, and send_message itself refuses."""
    from waku.tools import messages

    assert approvals.Gate(home, conn)("send_message", COMMENT) == (None, "")
    said = messages.make_tool(home).fn(**COMMENT)
    assert "approval" in said and "waku serve" in said
    assert _outbox(conn) == []


def test_park_a_second_ask_call_in_the_session_is_refused(home, conn):
    clock = Clock()
    _park(conn, home, clock)
    other = {"to": "telegram:77", "body": "a second message"}

    event_id = _slack(conn, external_id="Ev2")
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    with outbox.running(home, str(run.id), THREAD):
        deny, _ = approvals.Gate(home, conn, now=clock)("send_message", other)

    assert "already waits" in deny and COMMENT_HASH[:8] in deny
    assert runs.get(conn, run.id).pending_call is None
    assert len(_outbox(conn)) == 1, "the owner was asked once, about the first call"


def test_park_a_second_ask_call_in_the_same_run_is_refused(home, conn):
    clock = Clock()
    event_id = _slack(conn)
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    gate = approvals.Gate(home, conn, now=clock)
    with outbox.running(home, str(run.id), THREAD):
        gate("send_message", COMMENT)
        deny, _ = gate("send_message", {"to": "telegram:77", "body": "a second message"})

    assert "already waits" in deny
    assert runs.get(conn, run.id).pending_hash == COMMENT_HASH
    assert len(_outbox(conn)) == 1


def test_park_a_replayed_run_asks_the_owner_once(home, conn):
    """The run died after it parked. Its second attempt makes the same call."""
    clock = Clock()
    event_id = _slack(conn)
    gate = approvals.Gate(home, conn, now=clock)
    for _attempt in range(2):
        run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
        with outbox.running(home, str(run.id), THREAD):
            deny, _ = gate("send_message", COMMENT)
        assert "waits for the owner's approval" in deny

    assert len(_run_rows(conn)) == 1
    assert len(_outbox(conn)) == 1


def test_park_without_an_owner_who_can_answer_stores_nothing(home, conn, monkeypatch):
    clock = Clock()
    for owner in ("", "github:acme/app#1"):
        monkeypatch.setenv("WAKU_OWNER", owner)
        event_id = _slack(conn, external_id=f"Ev-{owner}")
        run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
        with outbox.running(home, str(run.id), THREAD):
            deny, _ = approvals.Gate(home, conn, now=clock)("send_message", COMMENT)
        assert "WAKU_OWNER" in deny
        assert runs.get(conn, run.id).pending_call is None
    assert _outbox(conn) == []


def test_park_the_worker_ends_the_run_as_parked(home, conn):
    clock = Clock()
    _slack(conn)
    gate = approvals.Gate(home, conn, now=clock)

    def run(session_id, batch):
        (row,) = _run_rows(conn)
        with outbox.running(home, str(row["id"]), THREAD):
            gate("send_message", COMMENT)
        return "SILENT"

    _worker(conn, run, clock).step()

    assert [row["status"] for row in _run_rows(conn)] == ["parked"]


# ---- C4: resuming ----------------------------------------------------------

def test_yes_from_the_owner_runs_only_the_stored_call(home):
    """Acceptance 6, through waku.respond() and the fake gh."""
    client = ScriptedClient([
        response([text_block(GATE)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block("SILENT")]),
        # the run that follows the yes, in Ana's thread
        response([text_block(GATE)]),
        response([text_block("SILENT")])])
    seen = []
    create = client.messages.create
    client.messages.create = lambda **kw: (seen.append(list(kw["messages"])), create(**kw))[1]
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    gh, told, clock = FakeGh(), [], Clock()
    conn = connect(home)
    try:
        worker = _worker(conn, run, clock)
        dispatcher = Dispatcher(conn, {"github": github_sender.make(run=gh),
                                       "telegram": lambda m: told.append(m.body)},
                                now=clock, log=lambda line: None)
        _slack(conn)
        _drain(worker)
        dispatcher.step()
        assert gh.comments == [] and len(told) == 1

        clock.advance(hours=2)
        _telegram(conn, "yes")
        _drain(worker)
        dispatcher.step()
        (row,) = [r for r in _run_rows(conn) if r["pending_hash"]]
        sessions = [r["session_id"] for r in _run_rows(conn)]
        statuses = [r["status"] for r in conn.execute("SELECT status FROM events")]
    finally:
        run.close()
        conn.close()

    (argv,) = gh.comments
    assert argv[:6] == ["gh", "issue", "comment", "12", "--repo", "acme/app"]
    assert argv[6].startswith("--body=Sean will review this issue this week.\n")
    assert row["status"] == "done"
    assert sessions == ["slack:C1:1700.0001", "slack:C1:1700.0001"], (
        "the yes starts no run in Sean's chat; the next run is in Ana's thread")
    assert set(statuses) == {"done"}
    assert len(told) == 1, "Sean hears nothing more after his yes"
    first_line = seen[-1][-1]["content"].splitlines()[0]
    assert first_line.startswith("[Approved and done: Comment on acme/app#12")
    assert client._script == [], "the model was called for two runs, not to approve"


def test_yes_executes_the_stored_arguments_and_starts_the_next_run(home, conn):
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    clock.advance(hours=2)
    _telegram(conn, " Yes. ")

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == [("send_message", COMMENT, THREAD)]
    assert runs.get(conn, run_id).status == "done"
    ((session, text),) = recorder.runs
    assert session == "slack:C1:1700.0001"
    assert text.startswith("[Approved and done: Comment on acme/app#12")


def test_yes_spelled_approve_counts(home, conn):
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    _telegram(conn, "approve")
    _drain(_worker(conn, recorder, clock))
    assert len(recorder.executed) == 1


def test_yes_runs_the_call_once_when_the_answer_is_replayed(home, conn):
    """The process dies after the call ran and before the answer was marked
    done. The answer is read again and finds nothing parked."""
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    answer = _telegram(conn, "yes")
    worker = _worker(conn, recorder, clock)
    _drain(worker)
    events.mark(conn, [answer], "pending")
    _drain(worker)

    assert len(recorder.executed) == 1
    assert [session for session, _ in recorder.runs] == [
        "slack:C1:1700.0001", "telegram:42"], "the second yes is an ordinary message"


def test_yes_with_a_changed_stored_call_runs_nothing(home, conn):
    """The hash binds the yes to one exact call."""
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    changed = {"tool": "send_message", "args": {**COMMENT, "body": "Ship it today."},
               "asked": {"channel": "telegram", "target": {"chat_id": 42}}}
    conn.execute("UPDATE runs SET pending_call = ? WHERE id = ?",
                 (json.dumps(changed), run_id))
    conn.commit()
    _telegram(conn, "yes")

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "failed"


def test_no_from_the_owner_discards_the_call(home, conn):
    """Acceptance 6."""
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    _telegram(conn, "no")

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "done"
    ((session, text),) = recorder.runs
    assert session == "slack:C1:1700.0001"
    assert text.startswith("[Declined and discarded: Comment on acme/app#12")
    _telegram(conn, "yes", external_id="901")
    _drain(_worker(conn, recorder, clock))
    assert recorder.executed == [], "a yes after the no finds nothing to approve"


def test_stranger_yes_runs_nothing(home, conn):
    """Acceptance 6: Ana's yes in her Slack thread, a yes from another
    Telegram chat, and a yes from another person in the owner's chat."""
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    _slack(conn, text="yes", external_id="Ev2")
    _telegram(conn, "yes", external_id="901", sender="77", chat_id=77)
    _telegram(conn, "yes", external_id="902", sender="77", chat_id=42)

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "parked"
    assert sorted(session for session, _ in recorder.runs) == [
        "slack:C1:1700.0001", "telegram:42", "telegram:77"], (
        "each yes went to the model as an ordinary message")
    assert all(text == "yes" for _, text in recorder.runs)


def test_yes_inside_a_sentence_is_an_ordinary_message(home, conn):
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    _telegram(conn, "yes but change the wording first")

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "parked"
    assert recorder.runs == [("telegram:42", "yes but change the wording first")]


def test_expire_after_72_hours_a_yes_runs_nothing(home, conn):
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    clock.advance(hours=71, minutes=59)
    _worker(conn, recorder, clock).step()
    assert runs.get(conn, run_id).status == "parked"

    clock.advance(minutes=1)
    _telegram(conn, "yes")
    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "expired"
    assert recorder.runs == [("telegram:42", "yes")]


def test_yes_with_two_calls_waiting_needs_the_code(home, conn):
    """Two conversations each parked a call. A bare yes names neither."""
    clock, recorder = Clock(), Recorder()
    first = _park(conn, home, clock)
    event_id = events.insert(conn, source="slack", external_id="Ev9",
                             session_id="slack:C1:1800.0002",
                             payload={"text": "tell 77", "reply_to": {"channel": "C1"}})
    second = runs.start(conn, "slack:C1:1800.0002", [event_id], now=clock())
    other = {"to": "telegram:77", "body": "The demo moved to Friday."}
    with outbox.running(home, str(second.id), ("slack", {"channel": "C1"})):
        approvals.Gate(home, conn, now=clock)("send_message", other)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, second.id, "parked", now=clock())
    code = runs.get(conn, second.id).pending_hash[:8]

    _telegram(conn, "yes")
    _drain(_worker(conn, recorder, clock))
    assert recorder.executed == [] and recorder.runs == []
    which = _outbox(conn)[-1]
    assert which[0] == "telegram" and code in which[2] and COMMENT_HASH[:8] in which[2]

    _telegram(conn, f"yes {code}", external_id="901")
    _drain(_worker(conn, recorder, clock))
    assert [args for _, args, _ in recorder.executed] == [other]
    assert runs.get(conn, first).status == "parked"
    assert runs.get(conn, second.id).status == "done"
