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

import json
from datetime import UTC, datetime

import pytest

from waku.alwayson import events
from waku.alwayson.worker import FAILED, Worker
from waku.db import connect
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
