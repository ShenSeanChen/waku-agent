"""DETERMINISTIC EVAL: the six faults of the second live run of `waku serve`
(2026-11-17, Telegram).

The owner talked to the agent in their own Telegram chat. A timer and a
message became one run, and that run said a comment waited for approval when
no call had been made. The reply carried a "[tools used: ...]" line. The
agent spoke about "the owner" to the owner. A bare "Approve" with nothing
waiting got an invented answer. A run that raised said nothing.

These tests use a real state.db in a temporary home, a scripted model, a fake
clock, and no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.alwayson import approvals, events, outbox, runs, waiting
from waku.alwayson.worker import AgentRun, Worker, render
from waku.db import connect

TUESDAY = datetime(2026, 11, 17, 17, 43, 0, tzinfo=UTC)
OWNER = ("telegram", {"chat_id": 42})
SESSION = "telegram:42"
COMMENT = {"to": "github:acme/app#12", "body": "whatsssupp"}
GATE = '{"retrieve": false, "query": "", "reason": "test"}'
# What the model wrote in the live run in place of a call: a line in the
# form of the note that history keeps after a real call.
IMITATED = ("Time to stretch!\n\nI've queued \"whatsssupp\" as a comment on acme/app#12.\n"
            "[tools used: send_message({'to': 'github:acme/app#12', 'body': 'whatsssupp'}) "
            "-> A plugin hook stopped this call before it ran: This call waits for the "
            "owner's approval and has not run. The owner was asked on telegram:42.]")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    for name in ("WAKU_OWNER_NAME", "WAKU_APPROVE_ALLOW", "WAKU_APPROVE_ASK"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def conn(home):
    connection = connect(home)
    yield connection
    connection.close()


def _message(conn, text, external_id="900", sender="42", chat_id=42):
    return events.insert(conn, source="telegram", external_id=external_id,
                         session_id=f"telegram:{chat_id}",
                         payload={"text": text, "sender": {"id": sender, "name": "S"},
                                  "reply_to": {"chat_id": chat_id}})


def _timer(conn, note="Remind the user to stretch.", external_id="timer:2"):
    return events.insert(conn, source="timer", external_id=external_id, session_id=SESSION,
                         payload={"text": note, "set_at": "2026-11-17T17:42:27+00:00",
                                  "due_at": "2026-11-17T17:43:00+00:00",
                                  "channel": OWNER[0], "reply_to": OWNER[1]})


def _outbox(conn):
    return [(r["channel"], json.loads(r["target"]), r["body"], r["idempotency_key"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def _runs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


def _worker(conn, run):
    return Worker(conn, run, wait=lambda seconds: None, now=lambda: TUESDAY,
                  log=lambda line: None)


def _agent_run(home, script):
    client = ScriptedClient(script)
    return AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                    log=lambda line: None)


def _step(home, conn, script):
    run = _agent_run(home, script)
    try:
        _worker(conn, run).step()
    finally:
        run.close()
    return _runs(conn), _outbox(conn)


def _assert_parked(conn, rows, sent):
    (row,) = rows
    assert row["status"] == "parked"
    assert json.loads(row["pending_call"])["args"] == COMMENT
    (request,) = sent
    assert request[:2] == OWNER and request[3] == f"{row['id']}:ask"
    assert 'Comment on acme/app#12: "whatsssupp"' in request[2] and "Approve?" in request[2]
    listed = waiting.listing(conn)
    assert [item.what for item in listed.calls] == ['Comment on acme/app#12: "whatsssupp"']


# ---- 1: a call from a run a timer started -------------------------------------

def test_a_timer_and_a_message_in_one_batch_park_the_call(home, conn):
    _timer(conn)
    _message(conn, "Comment “whatsssupp” in our GitHub issue")

    rows, sent = _step(home, conn, [
        response([text_block(GATE)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block("SILENT")])])

    assert len(json.loads(rows[0]["event_ids"])) == 2, "one run read both events"
    _assert_parked(conn, rows, sent)


def test_a_run_a_timer_started_alone_parks_its_call(home, conn):
    _timer(conn, note="Comment whatsssupp on acme/app#12.")

    rows, sent = _step(home, conn, [
        response([text_block(GATE)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block("SILENT")])])

    _assert_parked(conn, rows, sent)


def test_a_reply_that_writes_the_note_of_a_call_it_never_made_makes_the_call(home, conn):
    """The live run: the model called no tool and wrote the note itself. It is
    asked once more, and the call it then makes parks."""
    _timer(conn)
    _message(conn, "Comment “whatsssupp” in our GitHub issue")

    rows, sent = _step(home, conn, [
        response([text_block(GATE)]),
        response([text_block(IMITATED)]),
        response([tool_block("send_message", COMMENT)], stop_reason="tool_use"),
        response([text_block("SILENT")])])

    _assert_parked(conn, rows, sent)
    stored = conn.execute("SELECT content FROM chat_log WHERE role = 'assistant'").fetchall()
    assert [row["content"].count("[tools used: ") for row in stored] == [1], (
        "history keeps the note of the one call that was made")
    assert "The owner was asked on telegram:42" not in stored[0]["content"]


# ---- 2: no message carries the note --------------------------------------------

NOTE = ("[tools used: list_events({'day': 'today'}) -> A plugin hook stopped this call "
        "before it ran: This call waits. The owner was asked on telegram:42.]")


def test_the_reply_of_a_run_reaches_the_person_without_a_note(home, conn):
    _message(conn, "What is on today?")

    _worker(conn, lambda session_id, batch: f"Nothing today.\n{NOTE}").step()

    assert _outbox(conn) == [(*OWNER, "Nothing today.", "1:0")]


def test_a_reply_that_is_only_a_note_sends_nothing(home, conn):
    _message(conn, "What is on today?")

    _worker(conn, lambda session_id, batch: NOTE).step()

    assert _outbox(conn) == []


def test_no_row_in_the_outbox_holds_a_note_whoever_wrote_it(home, conn):
    outbox.insert(conn, run_id="chat", channel="telegram", target=OWNER[1],
                  body=f"Hello.\n{NOTE}", idempotency_key="chat:1", now=TUESDAY)
    only_a_note = outbox.insert(conn, run_id="chat", channel="telegram", target=OWNER[1],
                                body=NOTE, idempotency_key="chat:2", now=TUESDAY)

    assert _outbox(conn) == [(*OWNER, "Hello.", "chat:1")]
    assert only_a_note is None


def test_a_turn_hands_back_its_reply_without_a_note_the_model_wrote(home):
    """The note names the call the turn made, so the model is not asked again.
    The reply loses the note, and history keeps the one Waku writes."""
    client = ScriptedClient([
        response([text_block(GATE)]),
        response([tool_block("list_events", {"day": "today"})], stop_reason="tool_use"),
        response([text_block("Nothing today.\n[tools used: list_events({'day': 'today'}) "
                             "-> A plugin hook stopped this call before it ran: no.]")])])
    waku = make_waku(home, client=client, episodic_store="sqlite")
    try:
        result = waku.respond("What is on today?")
        record = waku.session.history[-1]["content"]
    finally:
        waku.close()
        waku.conn.close()

    assert result.reply == "Nothing today."
    assert record.count("[tools used: ") == 1 and "plugin hook" not in record


# ---- 3: the agent knows when it talks to the owner ------------------------------

class Recorder:
    """A fake run for the worker: it keeps what each run read."""

    def __init__(self, reply="SILENT") -> None:
        self.reply, self.read = reply, []

    def __call__(self, session_id, batch):
        self.read.append(render(batch))
        return self.reply


def test_a_run_in_the_owners_chat_is_told_who_it_talks_to(home, conn):
    """WAKU_OWNER_NAME is not set (the fixture removes it)."""
    _message(conn, "Comment whatsssupp in our GitHub issue")
    run = Recorder()

    _worker(conn, run).step()

    (read,) = run.read
    assert read.startswith("Comment whatsssupp in our GitHub issue")
    assert "You are talking to the person who runs this assistant" in read
    assert "they answer yes or no in this chat" in read
    assert "42" not in read


def test_a_timer_in_the_owners_chat_is_told_who_reads_the_reply(home, conn):
    _timer(conn)
    run = Recorder()

    _worker(conn, run).step()

    assert "You are talking to the person who runs this assistant" in run.read[0]


@pytest.mark.parametrize("sender, chat_id", [("77", 77), ("77", 42)])
def test_a_run_for_anyone_else_is_not_told_it_talks_to_the_owner(home, conn, sender, chat_id):
    """Another person's chat, and a sender who is not the owner."""
    _message(conn, "hello", sender=sender, chat_id=chat_id)
    run = Recorder()

    _worker(conn, run).step()

    assert run.read == ["hello"]


def test_a_call_from_the_owners_own_chat_says_the_person_was_asked(home, conn):
    event_id = _message(conn, "Comment whatsssupp in our GitHub issue")
    run = runs.start(conn, SESSION, [event_id], now=TUESDAY)

    with outbox.running(home, str(run.id), OWNER):
        said, _ = approvals.Gate(home, conn, now=lambda: TUESDAY)("send_message", COMMENT)

    assert "The person you are talking to has been asked" in said
    assert "they answer yes or no in this chat" in said
    assert "owner" not in said and "42" not in said
    assert runs.get(conn, run.id).pending_hash, "the call is stored all the same"


def test_a_call_from_another_conversation_says_the_owner_was_asked(home, conn):
    thread = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})
    event_id = events.insert(conn, source="slack", external_id="Ev1",
                             session_id="slack:C1:1700.0001",
                             payload={"text": "comment", "sender": {"id": "U_ANA"},
                                      "reply_to": thread[1]})
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=TUESDAY)

    with outbox.running(home, str(run.id), thread):
        said, _ = approvals.Gate(home, conn, now=lambda: TUESDAY)("send_message", COMMENT)

    assert said == approvals.WAITS and "The request is with the owner" in said
    assert "42" not in said


@pytest.mark.parametrize("wrote", [
    "It went to the approval request on Telegram (chat ID 7300000042).",
    "The owner was asked on telegram:7300000042.",
    "Your id is 7300000042, right?"])
def test_a_reply_never_shows_the_owners_chat_id(tmp_path, monkeypatch, wrote):
    monkeypatch.setenv("WAKU_OWNER", "telegram:7300000042")
    conn = connect(tmp_path)
    try:
        _message(conn, "Who is the owner?", sender="7300000042", chat_id=7300000042)
        _worker(conn, lambda session_id, batch: wrote).step()
        ((_, _, body, _),) = _outbox(conn)
    finally:
        conn.close()

    assert "7300000042" not in body and "this chat" in body


# ---- 4: a yes with nothing waiting -----------------------------------------------

@pytest.mark.parametrize("answer", ["Approve", "yes", "No.", "approved"])
def test_a_bare_answer_with_nothing_waiting_tells_the_model_so(home, conn, answer):
    _message(conn, answer)
    run = Recorder()

    _worker(conn, run).step()

    (read,) = run.read
    assert read.startswith(f"{answer}\n\n")
    assert "Nothing is waiting for approval" in read


def test_a_sentence_with_a_yes_in_it_is_not_called_an_answer(home, conn):
    _message(conn, "yes but change the wording first")
    run = Recorder()

    _worker(conn, run).step()

    assert "Nothing is waiting" not in run.read[0]


def test_a_yes_from_someone_else_is_an_ordinary_message(home, conn):
    _message(conn, "yes", sender="77", chat_id=77)
    run = Recorder()

    _worker(conn, run).step()

    assert run.read == ["yes"]


def test_a_yes_to_a_call_that_waits_is_still_the_answer(home, conn):
    event_id = _message(conn, "Comment whatsssupp in our GitHub issue")
    parked = runs.start(conn, SESSION, [event_id], now=TUESDAY)
    with outbox.running(home, str(parked.id), OWNER):
        approvals.Gate(home, conn, now=lambda: TUESDAY)("send_message", COMMENT)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, parked.id, "parked", now=TUESDAY)
    _message(conn, "Approve", external_id="901")
    run, executed = Recorder(), []
    worker = Worker(conn, run, execute=lambda r, a: executed.append(r.id) or "on its way",
                    wait=lambda seconds: None, now=lambda: TUESDAY, log=lambda line: None)

    while worker.step():
        pass

    assert executed == [parked.id]
    assert all("Nothing is waiting" not in read for read in run.read)
