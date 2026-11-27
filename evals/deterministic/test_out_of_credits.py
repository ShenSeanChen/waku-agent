"""DETERMINISTIC EVAL -- a person out of Waku credits reads where to pay (spec 042).

The hosted proxy refuses a call with a 403 whose body carries the code
`credits_used_up` and a sentence for the person (hosted/proxy/credits.py).
These evals pin what each channel does with that refusal: it shows the
sentence, and never the generic "something went wrong".

Measured 2026-11-26: a Free person who hit zero read, in Slack, "Something
went wrong on my side ... restart Waku on the computer it runs on." They have
not come back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from waku.alwayson import events
from waku.alwayson.worker import FAILED, Worker
from waku.db import connect
from waku.gateway import runner as runner_module
from waku.gateway.runner import GATEWAY_ERROR_REPLY
from waku.loop.agent import credits_notice, error_text

NOTICE = ("You've used this month's Waku credits, so Waku Agent is paused. "
          "Add 250,000 credits for $10 to carry on: https://www.waku.one/settings/plan")
NOON = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)


class OutOfCredits(Exception):
    """Shaped like the SDK's PermissionDeniedError for the proxy's refusal:
    the SDK's own message is "Error code: 403 - {...}", and the JSON is on
    `body`."""

    def __init__(self, code="credits_used_up", message=NOTICE):
        body = {"type": "error",
                "error": {"type": "permission_error", "code": code, "message": message}}
        super().__init__(f"Error code: 403 - {body}")
        self.status_code = 403
        self.message = f"Error code: 403 - {body}"
        self.body = body


# ---- the refusal is read by its code, not its English -----------------------

def test_the_notice_is_read_from_the_refusals_code():
    assert credits_notice(OutOfCredits()) == NOTICE
    assert error_text(OutOfCredits()) == NOTICE


@pytest.mark.parametrize("exc", [
    OutOfCredits(code="rate_limited"),
    OutOfCredits(message="  "),
    RuntimeError("You've used this month's Waku credits"),
])
def test_anything_else_is_not_a_credits_refusal(exc):
    assert credits_notice(exc) is None


# ---- the always-on worker: Slack and Telegram on hosted Waku ----------------

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    for name in ("WAKU_OWNER_NAME", "WAKU_APPROVE_ALLOW", "WAKU_APPROVE_ASK", "WAKU_HOSTED"):
        monkeypatch.delenv(name, raising=False)
    connection = connect(tmp_path)
    yield connection
    connection.close()


def _refused(session_id, batch):
    raise OutOfCredits()


def _step(conn, *, chat_id, source="telegram", reply_to=None):
    events.insert(conn, source=source, external_id="1", session_id=f"{source}:{chat_id}",
                  payload={"text": "What is on my calendar today?",
                           "sender": {"id": "42", "name": "S"},
                           "reply_to": reply_to or {"chat_id": chat_id}})
    worker = Worker(conn, _refused, wait=lambda seconds: None, now=lambda: NOON,
                    log=lambda line: None)
    assert worker.step() is True
    return [(r["channel"], json.loads(r["target"]), r["body"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def test_a_refusal_in_a_direct_chat_sends_the_notice_not_failed(conn):
    assert _step(conn, chat_id=42) == [("telegram", {"chat_id": 42}, NOTICE)]


def test_a_refusal_in_a_shared_place_posts_nothing_there_and_tells_the_owner(conn):
    rows = _step(conn, chat_id=-1001, reply_to={"chat_id": -1001})
    assert rows == [("telegram", {"chat_id": 42},
                     "I could not answer in Telegram. Nothing was posted there. " + NOTICE)]


def test_a_refusal_on_hosted_slack_answers_in_the_thread(conn, monkeypatch):
    """A hosted container replies in the Slack thread it was mentioned in
    (approvals._hosted_slack), so the person who asked reads the notice there."""
    monkeypatch.setenv("WAKU_HOSTED", "1")
    thread = {"channel": "C1", "thread_ts": "1700.0001"}
    assert _step(conn, chat_id="C1", source="slack", reply_to=thread) == [
        ("slack", thread, NOTICE)]


def test_any_other_failure_still_sends_failed(conn):
    def broken(session_id, batch):
        raise RuntimeError("model is down")

    events.insert(conn, source="telegram", external_id="1", session_id="telegram:42",
                  payload={"text": "hi", "reply_to": {"chat_id": 42}})
    Worker(conn, broken, wait=lambda s: None, now=lambda: NOON, log=lambda line: None).step()
    assert [r["body"] for r in conn.execute("SELECT body FROM outbox")] == [FAILED]


# ---- web chat: the dashboard's chat and the hosted embedded chat -------------

def test_web_chat_gets_the_notice_with_its_address_as_a_link():
    from waku.ops.dashboard import failure_event

    link = "https://www.waku.one/settings/plan"
    assert failure_event(OutOfCredits()) == {"notice": NOTICE.replace(
        link, f"[{link}]({link})")}
    # A sentence that ends with its address and a full stop keeps the stop outside.
    assert failure_event(OutOfCredits(message=f"Pay at {link}.")) == {
        "notice": f"Pay at [{link}]({link})."}


def test_web_chat_still_shows_any_other_failure_as_an_error():
    from waku.ops.dashboard import failure_event

    assert failure_event(RuntimeError("model is down")) == {
        "error": "RuntimeError: model is down"}


def test_a_streamed_turn_that_runs_out_of_credits_ends_with_the_notice(monkeypatch):
    """chat_stream lets the refusal reach /api/chat/stream's own catch, which
    sends failure_event's answer as the "done" event."""
    from waku.ops import dashboard

    class Refusing:
        def respond(self, *args, **kwargs):
            raise OutOfCredits()

    monkeypatch.setattr(dashboard, "get_agent", lambda: Refusing())
    monkeypatch.setattr(dashboard, "maybe_rotate_session", lambda agent: None)
    monkeypatch.setattr(dashboard.browser_agent, "follow_connectors", lambda: None)
    done = {}
    try:
        dashboard.chat_stream("hello", lambda kind, ev: None)
    except Exception as exc:
        done = dashboard.failure_event(exc)
    assert "notice" in done and "error" not in done


# ---- local gateways (Telegram, Discord) and the terminal chat ---------------

class _RefusingAgent:
    def __init__(self) -> None:
        self.session = SimpleNamespace(session_id="")
        self.closed = False

    def respond(self, text, **kwargs):
        raise OutOfCredits()

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("exc,expected", [
    (OutOfCredits(), NOTICE),
    (RuntimeError("model is down"), GATEWAY_ERROR_REPLY),
])
def test_a_gateway_sends_the_notice_and_only_the_notice(exc, expected):
    class Agent(_RefusingAgent):
        def respond(self, text, **kwargs):
            raise exc

    runner = runner_module.GatewayAgentRunner(Agent, session_id="telegram", source="telegram")
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    try:
        asyncio.run(runner_module.run_gateway_turn(runner, "hi", send, logger=lambda line: None))
    finally:
        runner.close()
    assert sent == [expected]


def test_the_terminal_prints_the_notice_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    from rich.console import Console

    from waku import app, config, memory_choice
    from waku.gateway import cli

    agent = _RefusingAgent()
    agent.settings = SimpleNamespace(home=tmp_path, model="m")
    typed = iter(["What is on my calendar today?", "this is never read"])
    monkeypatch.setattr(app, "Waku", lambda: agent)
    monkeypatch.setattr(cli, "Waku", lambda: agent)
    monkeypatch.setattr(config, "load_settings", lambda: None)
    monkeypatch.setattr(config, "home_notice", lambda: "")
    monkeypatch.setattr(memory_choice, "maybe_ask", lambda settings: None)
    monkeypatch.setattr(cli, "console", Console(width=400, force_terminal=False))
    monkeypatch.setattr(cli.console, "input", lambda prompt: next(typed))

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    out = capsys.readouterr().out
    assert stopped.value.code == 1
    assert NOTICE in out and "Traceback" not in out and "Error code" not in out
    assert agent.closed


# ---- the 80% line, once a calendar month ------------------------------------

LOW = "You have used 80% of this month's Waku credits. https://www.waku.one/settings/plan"
OWNER = ("telegram", {"chat_id": 42})


def test_the_first_low_turn_of_a_month_gets_the_line_and_the_second_does_not(conn, tmp_path):
    from waku.alwayson.worker import with_low_credits_line

    first = with_low_credits_line(tmp_path, "Lunch is at 1.", "low", OWNER, NOON)
    second = with_low_credits_line(tmp_path, "Dinner is at 7.", "low", OWNER, NOON)
    next_month = with_low_credits_line(tmp_path, "Breakfast is at 8.", "low", OWNER,
                                       datetime(2026, 12, 1, 0, 5, tzinfo=UTC))
    assert first == "Lunch is at 1.\n\n" + LOW
    assert second == "Dinner is at 7."
    assert next_month == "Breakfast is at 8.\n\n" + LOW


@pytest.mark.parametrize("state,reply,address", [
    ("ok", "Lunch is at 1.", OWNER),
    (None, "Lunch is at 1.", OWNER),
    ("low", "SILENT", OWNER),
    ("low", "Lunch is at 1.", ("telegram", {"chat_id": -1001})),
])
def test_the_line_waits_when_it_is_not_low_or_the_reply_is_not_private(
        conn, tmp_path, state, reply, address):
    """Each case leaves the line for the next private low turn."""
    from waku.alwayson.worker import with_low_credits_line

    assert with_low_credits_line(tmp_path, reply, state, address, NOON) == reply
    assert with_low_credits_line(tmp_path, "Later.", "low", OWNER, NOON) == "Later.\n\n" + LOW


def test_a_low_turn_reaches_the_person_with_the_line(conn, tmp_path):
    """AgentRun, the worker's real run: the turn's credits_state comes from
    the proxy's charges answer (Waku._receipt) and the reply carries the line."""
    from waku.alwayson.worker import AgentRun

    class Session:
        session_id = "telegram:42"

    class Agent:
        settings = SimpleNamespace(home=tmp_path)
        session, conn = Session(), None

        def __init__(self, connection):
            self.conn = connection

        def respond(self, text, **kwargs):
            return SimpleNamespace(reply="Lunch is at 1.", credits_state="low",
                                   receipt=None, sign_in_notices=[])

    run = AgentRun(lambda: Agent(conn), log=lambda line: None)
    events.insert(conn, source="telegram", external_id="1", session_id="telegram:42",
                  payload={"text": "When is lunch?", "reply_to": {"chat_id": 42}})
    Worker(conn, run, wait=lambda s: None, now=lambda: NOON, log=lambda line: None).step()
    assert [r["body"] for r in conn.execute("SELECT body FROM outbox")] == [
        "Lunch is at 1.\n\n" + LOW]


@pytest.mark.parametrize("answer,state", [
    ({"model_usd": 0.01, "calls": 1, "credits": 250, "credits_state": "low"}, "low"),
    ({"model_usd": 0.01, "calls": 1, "credits": 250, "credits_state": "full"}, None),
    ({"model_usd": 0.01, "calls": 1, "credits": 250}, None),
    (None, None),
])
def test_a_turn_reads_its_credits_state_from_the_charges_answer(tmp_path, answer, state):
    from evals.helpers import ScriptedClient, make_waku, response, text_block

    client = ScriptedClient([response([text_block("Lunch is at 1.")])] * 3)
    client.charges = lambda turn_id: answer
    waku = make_waku(tmp_path, client=client)
    try:
        result = waku.respond("When is lunch?")
    finally:
        waku.close()
    assert result.reply == "Lunch is at 1." and result.credits_state == state


def test_an_unwritable_home_never_costs_the_person_their_reply(tmp_path, monkeypatch):
    """Review, 2026-11-26: the 80% line must not turn a good reply into a failure."""
    from pathlib import Path

    from waku.alwayson.worker import with_low_credits_line

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only home")

    monkeypatch.setattr(Path, "write_text", refuse)
    assert with_low_credits_line(tmp_path, "Lunch is at 1.", "low", OWNER, NOON) == "Lunch is at 1."
