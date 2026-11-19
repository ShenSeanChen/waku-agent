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
from waku.alwayson.worker import CHAT_STYLE, OWNER_READS, AgentRun, Worker
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


def _on_clock(waku, clock):
    """The agent's approval gate on the test's clock. The gate reads the real
    time unless it is told otherwise, and a request it wrote was then due only
    while the real time was before THURSDAY. These tests passed until
    2026-11-19 09:00 UTC and failed from that minute on."""
    waku.approvals._now = clock
    return waku


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
    assert approvals.mode("send_message", {"to": "telegram:77", "body": "hi"},
                          address=("telegram", {"chat_id": 77})) == "allow"
    for to in ("github:acme/app#12", "telegram:77", "slack:C2", "Alex", "",
               "slack:C1:1700.0001"):   # a thread other people read (spec 024)
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
    run = AgentRun(lambda: _on_clock(make_waku(home, client=client, episodic_store="sqlite"),
                                     clock), log=lambda line: None)
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
    assert len(rows) == 2, "the request, and the fixed line in Ana's thread"
    assert rows[1][:3] == ("slack", THREAD[1], approvals.asked_line())
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
    assert _outbox(conn) == []
    assert runs.get(conn, run.id).pending_call is None


def test_park_only_happens_inside_a_run(home, conn):
    """The terminal and the dashboard have no worker to resume a parked call.
    The gate stays out of the way there, and send_message itself refuses."""
    from waku.tools import messages

    assert approvals.Gate(home, conn)("send_message", COMMENT) == (None, "")
    said = messages.make_tool(home).fn(**COMMENT)
    assert "approval" in said and "cannot ask" in said
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
    # a GitHub issue and a group chat are read by several people, and `waku
    # serve` hears no Slack direct message, so no yes could come back from one
    for owner in ("", "github:acme/app#1", "telegram:-1001234", "slack:D0123ABC"):
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


# ---- spec 024, decision 1: a parked run sends one message ------------------

OWNER_CHAT = 5441400551


def _parked_run(home, monkeypatch, insert, reply):
    """One run through waku.respond() whose model calls send_message for the
    GitHub comment and then answers `reply`. Returns (outbox rows, what the
    model read in place of the tool's result)."""
    monkeypatch.setenv("WAKU_OWNER", f"telegram:{OWNER_CHAT}")
    client = ScriptedClient([
        response([text_block(GATE)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block(reply)])])
    seen = []
    create = client.messages.create
    client.messages.create = lambda **kw: (seen.append(list(kw["messages"])), create(**kw))[1]
    run = AgentRun(lambda: _on_clock(make_waku(home, client=client, episodic_store="sqlite"),
                                     Clock()), log=lambda line: None)
    conn = connect(home)
    try:
        insert(conn)
        _worker(conn, run, Clock()).step()
        (row,) = _run_rows(conn)
        assert row["status"] == "parked"
        return _outbox(conn), json.dumps(seen[-1][-1]["content"], default=str)
    finally:
        run.close()
        conn.close()


def test_one_message_a_run_that_parks_in_the_owners_chat_sends_only_the_request(
        home, monkeypatch):
    """Spec 024, acceptance 1. The owner asked in his own chat. The model's
    reply after the call parked is not sent, and nothing names the chat id."""
    rows, told = _parked_run(
        home, monkeypatch,
        lambda conn: _telegram(conn, "comment on issue 12", sender=str(OWNER_CHAT),
                               chat_id=OWNER_CHAT),
        "Your comment is queued, but it needs the owner's approval first.")

    (request,) = rows
    channel, target, body, key = request
    assert (channel, target) == ("telegram", {"chat_id": OWNER_CHAT})
    assert key.endswith(":ask") and "Approve? Reply yes or no." in body
    assert str(OWNER_CHAT) not in body
    assert str(OWNER_CHAT) not in told, "the model is not given the owner's chat id"
    assert "SILENT" in told


def test_one_message_the_text_a_parked_call_returns_carries_no_address():
    assert "{" not in approvals.WAITS
    assert "SILENT" in approvals.WAITS
    assert "telegram" not in approvals.WAITS.lower()


def test_one_message_another_conversation_reads_one_fixed_line(home, monkeypatch):
    """Ana asked in Slack and Sean approves on Telegram. Her thread gets one
    line that code wrote, not the model's reply."""
    monkeypatch.setenv("WAKU_OWNER_NAME", "Sean")
    rows, _ = _parked_run(home, monkeypatch, _slack,
                          "Queued. It will post once the outbox is processed.")

    assert [(channel, key.rsplit(":", 1)[1]) for channel, _, _, key in rows] == [
        ("telegram", "ask"), ("slack", "0")]
    assert rows[1][1] == THREAD[1]
    assert rows[1][2] == "I asked Sean to approve this. I will say here what Sean decides."


def test_one_message_the_fixed_line_says_the_owner_without_a_name(home, monkeypatch):
    monkeypatch.delenv("WAKU_OWNER_NAME", raising=False)
    rows, _ = _parked_run(home, monkeypatch, _slack, "SILENT")

    assert rows[1][2] == ("I asked the owner to approve this. I will say here what "
                          "the owner decides.")


# ---- spec 024, decision 4: the request says when the same call ran ---------

def _ask_again(conn, home, clock, external_id, args=COMMENT):
    """The same person asks for a call once more. Returns the request."""
    event_id = _slack(conn, external_id=external_id)
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    with outbox.running(home, str(run.id), THREAD):
        approvals.Gate(home, conn, now=clock, tz=UTC)("send_message", args)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, run.id, "parked", now=clock())
    return _outbox(conn)[-1][2]


def test_ran_before_the_request_names_the_time_the_same_call_ran(home, conn):
    """Spec 024, acceptance 4. Sean approved "hello from Waku" at 09:00 and
    the same comment is asked for an hour later."""
    clock, recorder = Clock(), Recorder()
    first = _park(conn, home, clock)
    assert "The same call ran" not in _outbox(conn)[0][2]
    _telegram(conn, "yes")
    _drain(_worker(conn, recorder, clock))
    assert runs.get(conn, first).status == "done"

    clock.advance(hours=1)
    request = _ask_again(conn, home, clock, "Ev2")

    assert request == ('Comment on acme/app#12: "Sean will review this issue this week."\n\n'
                       "The same call ran at 09:00 today.\n\n"
                       f"Approve? Reply yes or no. [{COMMENT_HASH[:8]}]")


def test_ran_before_the_owner_still_decides_and_a_second_yes_posts_again(home, conn):
    """The line informs. The key stays <run id>:<hash>, so a second yes is a
    second call."""
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    _telegram(conn, "yes")
    _drain(_worker(conn, recorder, clock))
    clock.advance(hours=1)
    _ask_again(conn, home, clock, "Ev2")

    _telegram(conn, "yes", external_id="901")
    _drain(_worker(conn, recorder, clock))

    assert len(recorder.executed) == 2


def test_ran_before_says_yesterday_for_a_call_that_ran_the_day_before(home, conn):
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    _telegram(conn, "yes")
    _drain(_worker(conn, recorder, clock))

    clock.advance(hours=23)
    assert "The same call ran at 09:00 yesterday." in _ask_again(conn, home, clock, "Ev2")


def test_ran_before_is_silent_about_a_declined_an_older_and_another_call(home, conn):
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    _telegram(conn, "no")
    _drain(_worker(conn, recorder, clock))
    clock.advance(hours=1)
    assert "The same call ran" not in _ask_again(conn, home, clock, "Ev2"), "it was declined"

    _telegram(conn, "yes", external_id="901")
    _drain(_worker(conn, recorder, clock))
    other = {**COMMENT, "body": "Another comment."}
    assert "The same call ran" not in _ask_again(conn, home, clock, "Ev3", other), "another call"
    _telegram(conn, "no", external_id="902")
    _drain(_worker(conn, recorder, clock))

    clock.advance(hours=24, minutes=1)
    assert "The same call ran" not in _ask_again(conn, home, clock, "Ev4"), "over 24 hours ago"


# ---- C4: resuming ----------------------------------------------------------

def test_yes_from_the_owner_runs_only_the_stored_call(home, monkeypatch):
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
    run = AgentRun(lambda: _on_clock(make_waku(home, client=client, episodic_store="sqlite"),
                                     clock), log=lambda line: None)
    gh, told, clock = FakeGh(), [], Clock()
    # The comment the approved call writes takes the real time unless given one.
    monkeypatch.setattr(outbox, "utc_now", clock)
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
    assert first_line.startswith("[Approved and posted: Comment on acme/app#12")
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
    assert text.startswith("[Approved and posted: Comment on acme/app#12")


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
    # The word, then the line that says who reads the place it came from.
    assert all(text.split("\n\n")[0] == "yes" for _, text in recorder.runs)


def test_yes_inside_a_sentence_is_an_ordinary_message(home, conn):
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    _telegram(conn, "yes but change the wording first")

    _drain(_worker(conn, recorder, clock))

    assert recorder.executed == []
    assert runs.get(conn, run_id).status == "parked"
    assert recorder.runs == [
        ("telegram:42", f"yes but change the wording first\n\n{CHAT_STYLE}\n\n{OWNER_READS}")]


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
    assert recorder.runs[0][0] == "telegram:42"
    assert recorder.runs[0][1].startswith("yes\n\n")
    # spec 024: Ana's thread hears that the call expired
    assert [session for session, _ in recorder.runs[1:]] == ["slack:C1:1700.0001"]


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


# ---- spec 024, decision 5: a reply other people can read is a write ---------

ISSUE = ("github", {"repo": "acme/app", "number": 12})


def _mention(conn, external_id="comment:1"):
    return events.insert(conn, source="github", external_id=external_id,
                         session_id="github:acme/app#12",
                         payload={"text": "ana mentioned you: is this still open?",
                                  "sender": {"login": "ana"}, "reply_to": ISSUE[1]})


class Replier:
    """A run that answers with one sentence, and an executor that runs the
    stored call through the real send_message, as AgentRun.execute does."""

    def __init__(self, home, reply="It is still open.") -> None:
        self.home, self.reply, self.runs = home, reply, []

    def __call__(self, session_id, batch):
        self.runs.append(session_id)
        return self.reply

    def execute(self, run, address):
        from waku.tools import messages

        call = run.pending_call
        with outbox.running(self.home, str(run.id), address,
                            approved_key=f"{run.id}:{run.pending_hash}"):
            return messages.make_tool(self.home).fn(**call["args"])


def test_public_reply_to_a_github_issue_waits_for_the_owners_yes(home, conn):
    """Spec 024, item 5: a comment nobody approved appeared under the owner's
    name. The reply is now the request the owner reads."""
    clock, run = Clock(), Replier(home)
    _mention(conn)
    _drain(_worker(conn, run, clock))

    ((channel, target, body, _key),) = _outbox(conn)
    assert (channel, target) == ("telegram", {"chat_id": 42}), "nothing is queued for GitHub"
    assert 'Comment on acme/app#12: "It is still open."' in body and "Approve?" in body
    (parked,) = _run_rows(conn)
    assert parked["status"] == "parked"
    assert json.loads(parked["pending_call"])["args"] == {
        "to": "github:acme/app#12", "body": "It is still open."}


def test_public_reply_is_posted_once_after_a_yes_and_starts_no_run(home, conn):
    clock, run = Clock(), Replier(home)
    _mention(conn)
    worker = _worker(conn, run, clock)
    _drain(worker)
    _telegram(conn, "yes")
    _drain(worker)

    posted = [row for row in _outbox(conn) if row[0] == "github"]
    assert [(target, body) for _, target, body, _ in posted] == [(ISSUE[1], "It is still open.")]
    assert run.runs == ["github:acme/app#12"], (
        "the posted reply is the end: no run is started to talk about the approval")
    assert [r["status"] for r in _run_rows(conn)] == ["done"]


def test_public_reply_is_dropped_after_a_no(home, conn):
    clock, run = Clock(), Replier(home)
    _mention(conn)
    worker = _worker(conn, run, clock)
    _drain(worker)
    _telegram(conn, "no")
    _drain(worker)

    assert [row[0] for row in _outbox(conn)] == ["telegram"]
    assert run.runs == ["github:acme/app#12"]


def test_public_reply_to_a_slack_thread_and_a_group_chat_wait_too(home, conn):
    clock, run = Clock(), Replier(home)
    _slack(conn)
    _telegram(conn, "hello all", external_id="77", sender="7", chat_id=-100123)
    _drain(_worker(conn, run, clock))

    assert [row[0] for row in _outbox(conn)] == ["telegram", "telegram"]
    assert all(row[1] == {"chat_id": 42} and "Approve?" in row[2] for row in _outbox(conn))
    assert 'Send to slack:C1:1700.0001: "It is still open."' in _outbox(conn)[0][2]


def test_public_reply_rule_leaves_a_private_place_alone(home, conn, monkeypatch):
    """The owner's own chat, any address WAKU_OWNER names, and a Telegram chat
    with one person get the reply at once."""
    monkeypatch.setenv("WAKU_OWNER", "telegram:42,slack:D9")
    clock, run = Clock(), Replier(home, reply="Done.")
    _telegram(conn, "hello")
    _telegram(conn, "hello", external_id="901", sender="77", chat_id=77)
    events.insert(conn, source="slack", external_id="Ev7", session_id="slack:D9",
                  payload={"text": "hello", "reply_to": {"channel": "D9"}})
    _drain(_worker(conn, run, clock))

    assert sorted((row[0], json.dumps(row[1]), row[2]) for row in _outbox(conn)) == [
        ("slack", '{"channel": "D9"}', "Done."),
        ("telegram", '{"chat_id": 42}', "Done."),
        ("telegram", '{"chat_id": 77}', "Done.")]
    assert {r["status"] for r in _run_rows(conn)} == {"done"}


def test_public_reply_goes_out_at_once_when_the_owner_allowed_the_channel(
        home, conn, monkeypatch):
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "reply:github")
    clock, run = Clock(), Replier(home)
    _mention(conn)
    _slack(conn)
    _drain(_worker(conn, run, clock))

    rows = _outbox(conn)
    assert (rows[0][0], rows[0][1], rows[0][2]) == ("github", ISSUE[1], "It is still open.")
    assert rows[1][0] == "telegram" and "Send to slack:" in rows[1][2], "Slack still asks"
    assert approvals.mode("send_message", {"to": "github:acme/app#12", "body": "x"},
                          address=ISSUE) == "allow"


def test_public_reply_a_call_parked_from_an_issue_posts_no_line_on_the_issue(home, conn):
    """The fixed line of decision 1 is for a chat. On GitHub it would be one
    more comment under the owner's name."""
    clock = Clock()
    _mention(conn)
    gate = approvals.Gate(home, conn, now=clock)

    def run(session_id, batch):
        (row,) = _run_rows(conn)
        with outbox.running(home, str(row["id"]), ISSUE):
            gate("send_message", {"to": "telegram:77", "body": "Issue 12 needs you."})
        return "I asked."

    _worker(conn, run, clock).step()

    assert [row[0] for row in _outbox(conn)] == ["telegram"]
    assert [r["status"] for r in _run_rows(conn)] == ["parked"]


def test_public_reply_through_send_message_asks_as_well(home):
    """The model cannot post into a public conversation by calling the tool
    in place of replying."""
    assert approvals.mode("send_message", {"to": "github:acme/app#12", "body": "x"},
                          address=ISSUE) == "ask"
    assert approvals.mode("send_message", {"to": "slack:C1:1700.0001", "body": "x"},
                          address=THREAD) == "ask"
    assert approvals.mode("send_message", {"to": "telegram:77", "body": "x"},
                          address=("telegram", {"chat_id": 77})) == "allow"


def test_public_reply_without_an_owner_to_ask_is_not_posted(home, conn, monkeypatch):
    monkeypatch.delenv("WAKU_OWNER")
    clock, run, said = Clock(), Replier(home), []
    _mention(conn)
    Worker(conn, run, wait=lambda seconds: None, now=clock, log=said.append).step()

    assert _outbox(conn) == []
    assert [r["status"] for r in _run_rows(conn)] == ["done"]
    assert any("WAKU_OWNER" in line for line in said)


# ---- spec 024, decision 6: a tool the table does not know asks -------------

def test_unknown_tool_asks_and_one_that_declares_read_only_is_allowed(home):
    """Spec 024, acceptance 6. A tool from an MCP server ran without a yes."""
    assert approvals.mode("notion_create_page", {"title": "x"}) == "ask"
    assert approvals.mode("notion_search", {"query": "x"}, read_only=True) == "allow"


def test_unknown_tool_parks_under_the_gate_and_a_read_only_one_runs(home, conn):
    from waku.tools.registry import Tool, ToolRegistry

    tools = ToolRegistry()
    for name, read_only in (("notion_create_page", False), ("notion_search", True)):
        tools.register(Tool(name=name, description="", input_schema={}, fn=lambda **kw: "ok",
                            read_only=read_only))
    clock = Clock()
    event_id = _telegram(conn, "add a page")
    run = runs.start(conn, "telegram:42", [event_id], now=clock())
    gate = approvals.Gate(home, conn, now=clock, tools=tools)
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42})):
        assert gate("notion_search", {"query": "roadmap"}) == (None, "")
        deny, _ = gate("notion_create_page", {"title": "Roadmap"})

    assert "The person you are talking to has been asked" in deny
    assert runs.get(conn, run.id).pending_call["tool"] == "notion_create_page"
    ((_, _, body, _),) = _outbox(conn)
    assert "Run notion_create_page with:\n• title: Roadmap" in body


def test_unknown_tool_the_owner_named_in_allow_runs(home, monkeypatch):
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "notion_create_page")
    assert approvals.mode("notion_create_page", {}) == "allow"
    assert approvals.mode("notion_delete_page", {}) == "ask"


def test_unknown_tool_read_only_does_not_outrank_the_owner_or_the_table(home, monkeypatch):
    """A server's own word makes an unknown tool `allow`, and nothing more."""
    monkeypatch.setenv("WAKU_APPROVE_ASK", "notion_search")
    assert approvals.mode("notion_search", {}, read_only=True) == "ask"
    assert approvals.mode("send_message", {"to": "telegram:77", "body": "hi"},
                          read_only=True) == "ask"


def test_unknown_tool_from_an_mcp_server_carries_its_read_only_hint():
    from types import SimpleNamespace

    from waku.tools import mcp_client

    def listed(name, **annotations):
        return SimpleNamespace(name=name, description="", inputSchema={"type": "object"},
                               annotations=SimpleNamespace(**annotations) if annotations
                               else None)

    metas = [mcp_client.tool_meta(tool) for tool in (
        listed("search", readOnlyHint=True), listed("create_page", readOnlyHint=False),
        listed("delete_page"), listed("fetch", read_only_hint=True))]
    tools = [mcp_client.as_tool("notion", meta, call=lambda *a: "ok") for meta in metas]

    assert [(tool.name, tool.read_only) for tool in tools] == [
        ("notion_search", True), ("notion_create_page", False),
        ("notion_delete_page", False), ("notion_fetch", True)]


# ---- spec 025, decision 4: reads run, writes ask, per connector --------------

LINEAR_URL = "https://mcp.linear.app/mcp"


def _connected(home, *servers):
    """mcp.json with these servers, and a registry holding each one's tools
    as the bridge would list them. A server is (name, address, tools), and a
    tool is (its name on the server, whether the server marks it read-only)."""
    from waku.tools import mcp_client
    from waku.tools.registry import ToolRegistry

    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": name, "url": url, "oauth": True} for name, url, _ in servers]}),
        encoding="utf-8")
    tools = ToolRegistry()
    for name, _, listed in servers:
        for tool, read_only in listed:
            tools.register(mcp_client.as_tool(
                name, {"name": tool, "description": "", "readOnly": read_only},
                call=lambda *a: "ok"))
    return tools


def _gate_says(home, conn, tools, tool, args=None):
    """What the gate answers for one call in a run in the owner's own chat:
    None when the call runs, the text the model reads when it parks."""
    clock = Clock()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = _telegram(conn, "go", external_id=f"c{count}")
    run = runs.start(conn, "telegram:42", [event_id], now=clock())
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42})):
        deny, _ = approvals.Gate(home, conn, now=clock, tools=tools)(tool, args or {})
    events.mark(conn, [event_id], "done")
    # Each call here is its own question, so the run is closed either way.
    runs.finish(conn, run.id, "done", now=clock())
    return deny


def test_connector_modes_on_a_starter_server_a_write_parks_and_a_read_runs(home, conn):
    """Spec 025, acceptance 4, first half. The list decides, not the server's
    own hints: create_issue is marked read-only here and still asks."""
    tools = _connected(home, ("linear", LINEAR_URL, [
        ("list_issues", False), ("get_issue", False), ("create_issue", True),
        ("save_comment", False)]))

    assert _gate_says(home, conn, tools, "linear_list_issues") is None
    assert _gate_says(home, conn, tools, "linear_get_issue") is None
    assert "The person you are talking to has been asked" in _gate_says(
        home, conn, tools, "linear_create_issue", {"title": "Login fails"})
    assert "The person you are talking to has been asked" in _gate_says(home, conn, tools,
                                                          "linear_save_comment")


def test_connector_modes_on_a_server_added_by_address_a_tool_without_the_hint_parks(
        home, conn):
    """Spec 025, acceptance 4, second half."""
    tools = _connected(home, ("example", "https://mcp.example.com/mcp", [
        ("search", True), ("list_things", False), ("create_thing", False)]))

    assert _gate_says(home, conn, tools, "example_search") is None
    assert "The person you are talking to has been asked" in _gate_says(home, conn, tools,
                                                          "example_list_things")
    assert "The person you are talking to has been asked" in _gate_says(home, conn, tools,
                                                          "example_create_thing")


def test_connector_modes_a_server_named_like_a_starter_one_is_not_on_the_list(home, conn):
    """A server counts as a starter one by its address. Anyone can name a
    server 'linear'."""
    tools = _connected(home, ("linear", "https://mcp.not-linear.example/mcp", [
        ("list_issues", False)]))

    assert "The person you are talking to has been asked" in _gate_says(home, conn, tools,
                                                          "linear_list_issues")


def test_connector_modes_waku_memory_saves_and_recalls_without_a_question(home, conn):
    """Spec 024 made every MCP tool without the hint ask, which stopped the
    agent saving a memory. Waku Memory's own tools run."""
    tools = _connected(home, ("waku_memory", "https://api.waku.one/mcp", [
        ("memory.remember", False), ("memory.recall", False), ("memory.forget", False)]))

    for tool in ("waku_memory_memory_remember", "waku_memory_memory_recall",
                 "waku_memory_memory_forget"):
        assert _gate_says(home, conn, tools, tool, {"text": "x"}) is None, tool


def test_connector_modes_the_environment_still_wins_over_the_list(home, conn, monkeypatch):
    tools = _connected(home, ("linear", LINEAR_URL, [
        ("list_issues", False), ("create_issue", False)]),
        ("waku_memory", "https://api.waku.one/mcp", [("memory.remember", False)]))
    monkeypatch.setenv("WAKU_APPROVE_ASK", "linear_list_issues, waku_memory_memory_remember")
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "linear_create_issue")

    assert _gate_says(home, conn, tools, "linear_list_issues") is not None
    assert _gate_says(home, conn, tools, "waku_memory_memory_remember") is not None
    assert _gate_says(home, conn, tools, "linear_create_issue") is None


def test_connector_modes_the_four_steps_in_order(home):
    from waku.tools.connectors import Connector

    starter = Connector("linear", "starter", ("create_*", "save_*"))
    own = Connector("example", "own", ())
    memory = Connector("waku_memory", "memory", ())

    def mode(tool, **kw):
        return approvals.mode(tool, {}, **kw)

    # 2. the starter list: a write asks, any other tool runs
    assert mode("linear_create_issue", connector=starter, own_name="create_issue") == "ask"
    assert mode("linear_list_issues", connector=starter, own_name="list_issues") == "allow"
    assert mode("linear_create_issue", connector=starter, own_name="create_issue",
                read_only=True) == "ask", "a starter server's hint is not read"
    # 3. and 4. a server the owner added: the hint, or ask
    assert mode("example_search", connector=own, own_name="search", read_only=True) == "allow"
    assert mode("example_search", connector=own, own_name="search") == "ask"
    # Waku Memory
    assert mode("waku_memory_memory_remember", connector=memory,
                own_name="memory.remember") == "allow"
    # a built-in tool is not a connector's, whatever is passed with it
    assert mode("send_message", connector=memory) == "ask"


def test_connector_modes_every_starter_entry_asks_for_its_writes():
    """The verbs in connectors.toml, against tool names each vendor's server
    listed on 2026-11-17."""
    from waku.tools import connectors

    listed = connectors.starter()
    cases = {
        "linear": (["save_issue", "save_comment", "create_issue_label", "delete_comment",
                    "create_attachment", "update_diff", "merge_diff", "mark_notification"],
                   ["list_issues", "get_issue", "list_teams", "search_documentation",
                    "get_user", "list_comments"]),
        "notion": (["notion-create-pages", "notion-update-page", "notion-move-pages",
                    "notion-duplicate-page", "notion-create-comment", "notion-delete-pages",
                    "notion-calendar-create-events"],
                   ["notion-search", "notion-fetch", "notion-get-comments", "notion-get-users",
                    "notion-calendar-list-events"]),
        "github": (["create_pull_request", "merge_pull_request", "issue_write",
                    "add_issue_comment", "push_files", "create_or_update_file",
                    "delete_file", "fork_repository", "update_pull_request",
                    "pull_request_review_write", "request_copilot_review",
                    "actions_run_trigger", "label_write"],
                   ["get_file_contents", "list_issues", "search_code", "issue_read",
                    "pull_request_read", "get_me", "list_commits", "search_repositories"]),
    }
    assert set(cases) == set(listed)
    for key, (writes, reads) in cases.items():
        connector = connectors.Connector(key, "starter", tuple(listed[key]["writes"]))
        for tool in writes:
            assert approvals.mode(f"{key}_{tool}", {}, connector=connector,
                                  own_name=tool) == "ask", (key, tool)
        for tool in reads:
            assert approvals.mode(f"{key}_{tool}", {}, connector=connector,
                                  own_name=tool) == "allow", (key, tool)


# ---- spec 025, decision 4: the owner lets one connector write ----------------

def _owner_says(home, conn, tools, args, answer="yes"):
    """The agent calls connect_tool with `args` in the owner's chat, the
    owner answers, and the worker runs the stored call. Returns (the request
    the owner read, what the tool answered)."""
    from waku.tools import connectors

    for tool in connectors.make_tools(home):
        tools.register(tool)
    assert "The person you are talking to has been asked" in _gate_says_parked(home, conn, tools, args)
    request = _outbox(conn)[-1][2]
    (run,) = runs.parked(conn)
    if answer != "yes":
        runs.resolve(conn, run.id, "done", approved=False)
        return request, ""
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42}),
                        approved_key=f"{run.id}:{run.pending_hash}"):
        said = tools.execute(run.pending_call["tool"], run.pending_call["args"])
    runs.resolve(conn, run.id, "done", approved=True)
    return request, said


def _gate_says_parked(home, conn, tools, args):
    clock = Clock()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = _telegram(conn, "let it write", external_id=f"c{count}")
    run = runs.start(conn, "telegram:42", [event_id], now=clock())
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42})):
        deny, _ = approvals.Gate(home, conn, now=clock, tools=tools)("connect_tool", args)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, run.id, "parked", now=clock())
    return deny


def test_stored_rule_lets_the_next_write_run_and_survives_a_restart(home):
    """Spec 025, acceptance 5: "let Linear write without asking"."""
    tools = _connected(home, ("linear", LINEAR_URL, [
        ("list_issues", False), ("create_issue", False)]),
        ("example", "https://mcp.example.com/mcp", [("create_thing", False)]))
    conn = connect(home)
    try:
        assert _gate_says(home, conn, tools, "linear_create_issue") is not None

        request, said = _owner_says(home, conn, tools,
                                    {"name_or_url": "linear", "writes": "allow"})

        assert "Let linear's tools write without asking first." in request
        assert said.startswith("linear's tools now run without asking first.")
        assert _gate_says(home, conn, tools, "linear_create_issue") is None
        assert [tuple(r) for r in conn.execute("SELECT pattern, mode FROM approvals")] == [
            ("linear_*", "allow")]
    finally:
        conn.close()

    conn = connect(home)   # a restart: a new connection and a new gate
    try:
        assert _gate_says(home, conn, tools, "linear_create_issue") is None
        assert _gate_says(home, conn, tools, "linear_list_issues") is None
        # the rule is one connector's, and nothing else's
        assert _gate_says(home, conn, tools, "example_create_thing") is not None
        assert _gate_says(home, conn, tools, "send_message",
                          {"to": "github:acme/app#12", "body": "hi"}) is not None
        assert _gate_says(home, conn, tools, "connect_tool",
                          {"name_or_url": "https://mcp.other.example/mcp"}) is not None
    finally:
        conn.close()


def test_stored_rule_is_not_stored_when_the_owner_says_no(home, conn):
    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False)]))

    _owner_says(home, conn, tools, {"name_or_url": "linear", "writes": "allow"}, answer="no")

    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert _gate_says(home, conn, tools, "linear_create_issue") is not None


def test_stored_rule_the_owner_takes_it_back(home, conn):
    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False)]))
    _owner_says(home, conn, tools, {"name_or_url": "linear", "writes": "allow"})

    request, said = _owner_says(home, conn, tools,
                                {"name_or_url": "linear", "writes": "ask"})

    assert "ask first again" in request and "ask first again" in said
    assert _gate_says(home, conn, tools, "linear_create_issue") is not None


def test_stored_rule_the_environment_still_wins(home, conn, monkeypatch):
    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False),
                                                     ("delete_issue", False)]))
    _owner_says(home, conn, tools, {"name_or_url": "linear", "writes": "allow"})
    monkeypatch.setenv("WAKU_APPROVE_ASK", "linear_delete_issue")

    assert _gate_says(home, conn, tools, "linear_create_issue") is None
    assert _gate_says(home, conn, tools, "linear_delete_issue") is not None


def test_stored_rule_works_for_a_server_the_owner_added_by_address(home, conn):
    tools = _connected(home, ("example", "https://mcp.example.com/mcp",
                              [("create_thing", False)]))

    _owner_says(home, conn, tools, {"name_or_url": "example", "writes": "allow"})

    assert _gate_says(home, conn, tools, "example_create_thing") is None


def test_stored_rule_for_a_connector_that_is_not_there_changes_nothing(home, conn):
    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False)]))

    _, said = _owner_says(home, conn, tools, {"name_or_url": "jira", "writes": "allow"})

    assert said.startswith("Not changed:") and "jira" in said
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_stored_rule_goes_when_the_connector_is_removed(home, conn):
    """A server added later under the same name starts by asking."""
    from waku.tools import connectors

    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False)]))
    _owner_says(home, conn, tools, {"name_or_url": "linear", "writes": "allow"})

    connectors.remove(home, "linear")
    connectors.add(home, "linear", url="https://mcp.not-linear.example/mcp")

    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert _gate_says(home, conn, tools, "linear_create_issue") is not None


def test_stored_rule_from_the_card_is_one_switch(home, conn, monkeypatch):
    from hosted.core import policy
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    tools = _connected(home, ("linear", LINEAR_URL, [("create_issue", False)]))
    row = dashboard.connectors_data()["servers"][0]
    assert (row["name"], row["writes"]) == ("linear", "ask")

    assert dashboard.connectors_approve({"name": "linear", "allow": True}) == {
        "ok": True, "name": "linear", "writes": "allow"}
    assert dashboard.connectors_data()["servers"][0]["writes"] == "allow"
    assert _gate_says(home, conn, tools, "linear_create_issue") is None

    assert dashboard.connectors_approve({"name": "linear", "allow": False})["writes"] == "ask"
    assert _gate_says(home, conn, tools, "linear_create_issue") is not None
    assert "error" in dashboard.connectors_approve({"name": "jira", "allow": True})
    assert "error" in dashboard.connectors_approve({"name": "linear", "allow": "yes"})
    assert policy.decide("POST", "/api/connectors/approve", {}).verdict == "block"


# ---- spec 024, decision 8: an expiry is said once --------------------------

def test_expiry_said_one_reminder_at_48_hours_and_one_message_at_72(home, conn):
    """Spec 024, acceptance 8. A waiting call expired and nobody was told."""
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    worker = _worker(conn, recorder, clock)
    request, line = _outbox(conn)[0], 'Comment on acme/app#12: "Sean will review'

    clock.advance(hours=47, minutes=59)
    worker.step()
    assert len(_outbox(conn)) == 1, "nothing before 48 hours"

    clock.advance(minutes=1)
    worker.step()
    worker.step()
    (reminder,) = _outbox(conn)[1:]
    assert reminder[:2] == request[:2], "it goes where the request went"
    assert line in reminder[2] and "24 hours" in reminder[2]
    assert f"[{COMMENT_HASH[:8]}]" in reminder[2], "the same code"

    clock.advance(hours=23, minutes=59)
    worker.step()
    assert len(_outbox(conn)) == 2 and runs.get(conn, run_id).status == "parked"

    clock.advance(minutes=1)
    _drain(worker)
    _drain(worker)
    (expired,) = _outbox(conn)[2:]
    assert expired[:2] == request[:2]
    assert expired[2].startswith("I stopped waiting for your answer on: " + line)
    assert expired[2].endswith("Nothing was posted.")
    assert runs.get(conn, run_id).status == "expired"


def test_expiry_said_to_the_conversation_that_asked_as_one_outcome(home, conn):
    """Ana's thread parked the call. Its next run starts with the outcome."""
    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    worker = _worker(conn, recorder, clock)

    clock.advance(hours=72)
    _drain(worker)
    _drain(worker)

    ((session, text),) = recorder.runs
    assert session == "slack:C1:1700.0001"
    assert text.startswith("[Expired and discarded: Comment on acme/app#12")
    assert "72 hours" in text.splitlines()[0]


def test_expiry_said_once_when_the_owner_asked_in_their_own_chat(home, conn):
    """The owner reads the expiry message. No run starts to say it again."""
    clock, recorder = Clock(), Recorder()
    event_id = _telegram(conn, "comment on issue 12")
    run = runs.start(conn, "telegram:42", [event_id], now=clock())
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42})):
        approvals.Gate(home, conn, now=clock)("send_message", COMMENT)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, run.id, "parked", now=clock())
    worker = _worker(conn, recorder, clock)

    clock.advance(hours=72)
    _drain(worker)

    assert recorder.runs == []
    assert [row[2].split(":")[0] for row in _outbox(conn)] == [
        "Comment on acme/app#12", "I stopped waiting for your answer on"], (
        "the worker was down at 48 hours, so the expiry is the one thing said")


def test_expiry_said_nothing_about_a_call_that_was_answered_or_cancelled(home, conn):
    from waku.alwayson import waiting

    clock, recorder = Clock(), Recorder()
    _park(conn, home, clock)
    _telegram(conn, "no")
    worker = _worker(conn, recorder, clock)
    _drain(worker)
    cancelled = _park(conn, home, clock, external_id="Ev2")
    waiting.cancel(conn, f"r{cancelled}")
    before = len(_outbox(conn))

    clock.advance(hours=80)
    _drain(worker)

    assert len(_outbox(conn)) == before
    assert runs.get(conn, cancelled).status == "cancelled"


# ---- the licence line ------------------------------------------------------

def test_who_may_approve_is_the_one_place_that_decides(home, conn, monkeypatch):
    """A team layer replaces one function. With another answer, another
    person is asked, and that person's yes counts while the owner's does not."""
    monkeypatch.setattr(approvals, "who_may_approve",
                        lambda tool, args: [("telegram", {"chat_id": 77})])
    clock, recorder = Clock(), Recorder()
    run_id = _park(conn, home, clock)
    assert [(row[0], row[1]) for row in _outbox(conn)] == [("telegram", {"chat_id": 77})]

    _telegram(conn, "yes", external_id="900", sender="42", chat_id=42)
    _drain(_worker(conn, recorder, clock))
    assert recorder.executed == [] and runs.get(conn, run_id).status == "parked"

    _telegram(conn, "yes", external_id="901", sender="77", chat_id=77)
    _drain(_worker(conn, recorder, clock))
    assert len(recorder.executed) == 1 and runs.get(conn, run_id).status == "done"


def test_the_always_on_package_imports_nothing_from_team_or_hosted():
    """Everything in waku/alwayson/ is MIT. The team layer and the hosted
    platform are under another licence, and neither is imported here."""
    import ast
    from pathlib import Path

    import waku.alwayson

    files = sorted(Path(waku.alwayson.__file__).parent.rglob("*.py"))
    assert len(files) >= 14
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert name.split(".")[0] not in ("team", "hosted"), f"{path.name}: {name}"


def test_a_slack_reply_on_hosted_waku_needs_no_yes(home, monkeypatch):
    """The first mention on hosted Waku, 2026-11-18: the agent answered and
    the answer waited for an approval that a hosted container cannot get."""
    thread = ("slack", {"channel": "C09", "thread_ts": "1794952040.892829"})
    assert approvals.reply_is_free(thread) is False, "your own Slack app still asks"

    monkeypatch.setenv("WAKU_HOSTED", "1")

    assert approvals.reply_is_free(thread) is True
    assert approvals.reply_is_free(("github", {"repo": "a/b", "issue": 1})) is False
    assert approvals.mode("send_message", {"to": "slack:C09:1794952040.892829", "body": "hi"},
                          thread) == "allow", "a message back into the same thread"
    assert approvals.mode("send_message", {"to": "slack:C77", "body": "hi"},
                          thread) == "ask", "another channel is still a write"
    assert approvals.mode("send_message", {"to": "slack:C09", "body": "stretch"},
                          thread) == "allow", "the channel this thread is in"
