"""DETERMINISTIC EVAL: nothing leaves except through the outbox (spec 023, group D).

A run writes one row into `outbox` per outgoing message, and the dispatcher
sends it. These tests use a real state.db in a temporary home, fake senders
and a fake clock, so they need no network, no model and no real seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import outbox
from waku.db import connect

NOON = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


class Clock:
    """A clock that only moves when a test moves it."""

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


def _add(conn, key="e1:0", body="I will check issue 12 on Thursday at 09:00", now=NOON,
         channel="telegram", target=None, run_id="e1"):
    return outbox.insert(conn, run_id=run_id, channel=channel,
                         target=target or {"chat_id": 42}, body=body,
                         idempotency_key=key, now=now)


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


# ---- D1: the table ---------------------------------------------------------

def test_insert_writes_one_pending_row_that_is_due_at_once(conn):
    message_id = _add(conn)

    (row,) = _rows(conn)
    assert message_id == row["id"]
    assert (row["run_id"], row["channel"], row["body"], row["idempotency_key"]) == (
        "e1", "telegram", "I will check issue 12 on Thursday at 09:00", "e1:0")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 0, "")
    assert row["next_attempt_at"] == "2026-11-16T12:00:00+00:00"
    assert outbox.get(conn, message_id).target == {"chat_id": 42}


def test_insert_refuses_a_channel_nothing_can_send_on(conn):
    with pytest.raises(ValueError):
        _add(conn, channel="carrier-pigeon")
    assert _rows(conn) == []


def test_replay_of_the_same_key_leaves_one_row(conn):
    """Acceptance 7: a run replayed after a crash writes the same key again,
    and the person gets one reply."""
    first = _add(conn, key="e1:0", body="the first run's reply")
    second = _add(conn, key="e1:0", body="the replayed run's reply")

    assert first is not None
    assert second is None, "a replay reports that nothing was inserted"
    (row,) = _rows(conn)
    assert row["body"] == "the first run's reply"


def test_replay_is_refused_by_the_table_itself(conn):
    """The constraint is in the schema, so a writer that skips outbox.insert
    cannot create a second row either."""
    _add(conn, key="e1:0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outbox (run_id, channel, target, body, idempotency_key, "
            "next_attempt_at) VALUES ('e1', 'telegram', '{}', 'again', 'e1:0', "
            "'2026-11-16T12:00:00+00:00')")


def test_replay_after_the_row_was_sent_does_not_send_it_again(conn):
    message_id = _add(conn, key="e1:0")
    outbox.mark_sent(conn, message_id)

    assert _add(conn, key="e1:0") is None
    assert [r["status"] for r in _rows(conn)] == ["sent"]
    assert outbox.due(conn, NOON + timedelta(days=1)) == []


def test_insert_then_due_returns_only_rows_whose_time_has_passed(conn):
    now = _add(conn, key="e1:0")
    later = _add(conn, key="e2:0", now=NOON + timedelta(minutes=5))

    assert [m.id for m in outbox.due(conn, NOON)] == [now]
    assert [m.id for m in outbox.due(conn, NOON + timedelta(minutes=5))] == [now, later]


def test_insert_then_a_failure_records_the_error_and_the_next_attempt(conn):
    message_id = _add(conn)

    status = outbox.record_failure(conn, message_id, "HTTP 502", NOON)

    assert status == "pending"
    (row,) = _rows(conn)
    assert (row["attempts"], row["last_error"]) == (1, "HTTP 502")
    assert row["next_attempt_at"] == "2026-11-16T12:00:10+00:00", "the first wait is 10 seconds"
    assert outbox.due(conn, NOON + timedelta(seconds=9)) == []
    assert [m.id for m in outbox.due(conn, NOON + timedelta(seconds=10))] == [message_id]


def test_insert_migrates_a_database_from_before_the_table(tmp_path):
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, role TEXT NOT NULL, "
                "content TEXT NOT NULL, consolidated INTEGER DEFAULT 0)")
    old.commit()
    old.close()

    upgraded = connect(tmp_path)
    try:
        assert _add(upgraded) is not None
    finally:
        upgraded.close()


# ---- D2: the dispatcher ----------------------------------------------------

class FakeSender:
    """Records every delivery attempt and fails the first `failures` of them."""

    def __init__(self, failures: int = 0) -> None:
        self.failures, self.attempts, self.delivered = failures, 0, []

    def __call__(self, message) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError(f"HTTP 502 on attempt {self.attempts}")
        self.delivered.append((message.target, message.body))


def _dispatcher(conn, clock, senders, **kwargs):
    from waku.alwayson.dispatcher import Dispatcher

    kwargs.setdefault("log", lambda line: None)
    return Dispatcher(conn, senders, now=clock, **kwargs)


def test_once_a_pending_row_is_sent_and_marked_sent(conn):
    clock, sender = Clock(), FakeSender()
    _add(conn, body="hello Ana")
    dispatcher = _dispatcher(conn, clock, {"telegram": sender})

    assert dispatcher.step() == 1
    assert sender.delivered == [({"chat_id": 42}, "hello Ana")]
    assert [r["status"] for r in _rows(conn)] == ["sent"]
    assert dispatcher.step() == 0
    assert sender.attempts == 1, "a sent row is never sent again"


def test_once_a_sender_that_fails_twice_then_succeeds_delivers_once(conn):
    """Acceptance 7."""
    clock, sender = Clock(), FakeSender(failures=2)
    _add(conn, body="hello Ana")
    dispatcher = _dispatcher(conn, clock, {"telegram": sender})

    dispatcher.step()                      # fails; next attempt in 10 seconds
    clock.advance(9)
    dispatcher.step()
    assert sender.attempts == 1, "nothing is tried before the 10 seconds pass"
    clock.advance(1)
    dispatcher.step()                      # fails; next attempt in 1 minute
    clock.advance(59)
    dispatcher.step()
    assert sender.attempts == 2
    clock.advance(1)
    dispatcher.step()                      # succeeds

    assert sender.delivered == [({"chat_id": 42}, "hello Ana")]
    (row,) = _rows(conn)
    assert (row["status"], row["attempts"], row["last_error"]) == ("sent", 2, "")
    clock.advance(86400)
    dispatcher.step()
    assert sender.attempts == 3


def test_retry_waits_ten_seconds_a_minute_ten_minutes_then_an_hour(conn):
    clock, sender = Clock(), FakeSender(failures=99)
    _add(conn)
    dispatcher = _dispatcher(conn, clock, {"telegram": sender})

    waits = []
    for _ in range(4):
        dispatcher.step()
        (row,) = _rows(conn)
        due_at = datetime.fromisoformat(row["next_attempt_at"])
        waits.append((due_at - clock.at).total_seconds())
        clock.at = due_at
    assert waits == [10, 60, 600, 3600]
    assert _rows(conn)[0]["last_error"] == "RuntimeError: HTTP 502 on attempt 4"


def test_retry_does_not_hold_back_another_row(conn):
    clock = Clock()
    broken, working = FakeSender(failures=99), FakeSender()
    _add(conn, key="e1:0", channel="slack", target={"channel": "C1", "thread_ts": "1.0"})
    _add(conn, key="e2:0", body="to Sean")
    dispatcher = _dispatcher(conn, clock, {"slack": broken, "telegram": working})

    dispatcher.step()

    assert working.delivered == [({"chat_id": 42}, "to Sean")]
    assert [r["status"] for r in _rows(conn)] == ["pending", "sent"]


def test_dead_after_five_failures_and_never_tried_again(conn):
    """Acceptance 7."""
    clock, sender, logged = Clock(), FakeSender(failures=99), []
    _add(conn)
    dispatcher = _dispatcher(conn, clock, {"telegram": sender}, log=logged.append)

    for _ in range(5):
        dispatcher.step()
        clock.advance(3600)

    (row,) = _rows(conn)
    assert (row["status"], row["attempts"]) == ("dead", 5)
    clock.advance(86400)
    assert dispatcher.step() == 0
    assert sender.attempts == 5
    assert any("5 attempts" in line for line in logged)


def test_dead_row_on_a_channel_with_no_sender(conn):
    """Slack is not configured on this machine. The row is not sent somewhere
    else and it does not stay pending for ever."""
    clock, telegram = Clock(), FakeSender()
    _add(conn, channel="slack", target={"channel": "C1", "thread_ts": "1.0"})
    dispatcher = _dispatcher(conn, clock, {"telegram": telegram})

    for _ in range(5):
        dispatcher.step()
        clock.advance(3600)

    (row,) = _rows(conn)
    assert row["status"] == "dead"
    assert "slack" in row["last_error"]
    assert telegram.attempts == 0


def test_dead_row_sends_the_owner_one_notice_on_another_channel(conn):
    clock = Clock()
    slack, telegram = FakeSender(failures=99), FakeSender()
    _add(conn, channel="slack", target={"channel": "C1", "thread_ts": "1.0"},
         body="I will check issue 12 on Thursday at 09:00")
    dispatcher = _dispatcher(conn, clock, {"slack": slack, "telegram": telegram},
                             owner=[("slack", {"channel": "D9"}), ("telegram", {"chat_id": 7})])

    for _ in range(6):
        dispatcher.step()
        clock.advance(3600)

    assert len(telegram.delivered) == 1, "one notice, on the channel that did not fail"
    target, notice = telegram.delivered[0]
    assert target == {"chat_id": 7}
    assert "slack" in notice and "I will check issue 12" in notice
    assert [r["status"] for r in _rows(conn)] == ["dead", "sent"]


def test_dead_notice_that_dies_sends_no_further_notice(conn):
    clock = Clock()
    slack, telegram = FakeSender(failures=99), FakeSender(failures=99)
    _add(conn, channel="slack", target={"channel": "C1", "thread_ts": "1.0"})
    dispatcher = _dispatcher(conn, clock, {"slack": slack, "telegram": telegram},
                             owner=[("telegram", {"chat_id": 7}), ("slack", {"channel": "D9"})])

    for _ in range(12):
        dispatcher.step()
        clock.advance(3600)

    assert [r["status"] for r in _rows(conn)] == ["dead", "dead"]
    assert slack.attempts == 5 and telegram.attempts == 5


def test_dead_without_an_owner_writes_no_notice(conn):
    clock, sender = Clock(), FakeSender(failures=99)
    _add(conn)
    dispatcher = _dispatcher(conn, clock, {"telegram": sender})
    for _ in range(5):
        dispatcher.step()
        clock.advance(3600)
    assert len(_rows(conn)) == 1


# ---- D2: where a message can go --------------------------------------------

def test_retry_safe_address_forms():
    assert outbox.parse_address("telegram:42") == ("telegram", {"chat_id": 42})
    assert outbox.parse_address("telegram:-1001234") == ("telegram", {"chat_id": -1001234})
    assert outbox.parse_address("slack:C0123ABC") == ("slack", {"channel": "C0123ABC"})
    assert outbox.parse_address("slack:C0123ABC:1700000000.000100") == (
        "slack", {"channel": "C0123ABC", "thread_ts": "1700000000.000100"})
    assert outbox.parse_address("github:acme/app#12") == (
        "github", {"repo": "acme/app", "number": 12})
    for address in ("telegram:42", "slack:C0123ABC", "slack:C0123ABC:1700000000.000100",
                    "github:acme/app#12"):
        assert outbox.format_address(*outbox.parse_address(address)) == address
    for not_an_address in ("Alex", "telegram:sean", "slack:", "github:acme/app",
                           "github:-x/app#1", "github:acme/app#0", "email:a@b.c", ""):
        assert outbox.parse_address(not_an_address) is None, not_an_address


def test_retry_owner_addresses_come_from_the_setting(monkeypatch):
    monkeypatch.delenv("WAKU_OWNER", raising=False)
    assert outbox.owner_addresses() == []
    monkeypatch.setenv("WAKU_OWNER", "telegram:42, Sean ,slack:D9")
    assert outbox.owner_addresses() == [("telegram", {"chat_id": 42}),
                                        ("slack", {"channel": "D9"})]


# ---- D2: the three senders -------------------------------------------------

def _message(channel, target, body="hello", key="e1:0"):
    return outbox.Message(1, "e1", channel, target, body, key, "pending", 0,
                          "2026-11-16T12:00:00+00:00", "")


def test_once_the_telegram_sender_posts_to_the_chat():
    from waku.alwayson.senders import telegram

    calls = []

    def post(url, payload, headers):
        calls.append((url, payload, headers))
        return {"ok": True}

    telegram.make("123:abc", post=post)(_message("telegram", {"chat_id": 42}, "hello Sean"))

    assert calls == [("https://api.telegram.org/bot123:abc/sendMessage",
                      {"chat_id": 42, "text": "hello Sean"}, {})]


def test_once_the_telegram_sender_splits_a_long_message():
    from waku.alwayson.senders import telegram

    texts = []
    telegram.make("123:abc", post=lambda url, payload, headers: texts.append(
        payload["text"]) or {"ok": True})(_message("telegram", {"chat_id": 42}, "a" * 5000))

    assert [len(t) for t in texts] == [4096, 904]


def test_retry_when_telegram_refuses_and_the_error_never_carries_the_token():
    from waku.alwayson.senders import SendFailed, telegram

    def post(url, payload, headers):
        return {"ok": False, "description": "Forbidden: bot was blocked by the user"}

    with pytest.raises(SendFailed) as failed:
        telegram.make("123:abc", post=post)(_message("telegram", {"chat_id": 42}))
    assert "blocked" in str(failed.value)
    assert "123:abc" not in str(failed.value)


def test_once_the_slack_sender_replies_in_the_thread():
    from waku.alwayson.senders import slack

    calls = []

    def post(url, payload, headers):
        calls.append((url, payload, headers))
        return {"ok": True}

    slack.make("xoxb-test", post=post)(
        _message("slack", {"channel": "C1", "thread_ts": "1700.0001"}, "hello Ana"))
    slack.make("xoxb-test", post=post)(_message("slack", {"channel": "D9"}, "hello Sean"))

    assert calls == [
        ("https://slack.com/api/chat.postMessage",
         {"channel": "C1", "text": "hello Ana", "thread_ts": "1700.0001"},
         {"Authorization": "Bearer xoxb-test"}),
        ("https://slack.com/api/chat.postMessage",
         {"channel": "D9", "text": "hello Sean"}, {"Authorization": "Bearer xoxb-test"}),
    ]


def test_retry_when_slack_answers_200_with_an_error():
    from waku.alwayson.senders import SendFailed, slack

    with pytest.raises(SendFailed) as failed:
        slack.make("xoxb-test", post=lambda *a: {"ok": False, "error": "channel_not_found"})(
            _message("slack", {"channel": "C1"}))
    assert "channel_not_found" in str(failed.value)
    assert "xoxb-test" not in str(failed.value)


def test_once_the_github_sender_comments_through_gh():
    from waku.alwayson.senders import github

    calls = []

    def run(argv):
        calls.append(argv)
        return True, "https://github.com/acme/app/issues/12#issuecomment-1"

    github.make(run=run)(_message("github", {"repo": "acme/app", "number": 12},
                                  "--repo evil/other is only text", key="e7:0"))

    (argv,) = calls
    assert argv[:6] == ["gh", "issue", "comment", "12", "--repo", "acme/app"]
    assert len(argv) == 7, "the body is one argument, so it cannot become a flag"
    assert argv[6].startswith("--body=--repo evil/other is only text")
    assert argv[6].endswith("<!-- waku:e7:0 -->"), "the comment carries its key"


def test_retry_when_gh_fails_or_the_target_is_not_an_issue():
    from waku.alwayson.senders import SendFailed, github

    calls = []

    def run(argv):
        calls.append(argv)
        return False, "gh is not signed in. Run `gh auth login`."

    with pytest.raises(SendFailed) as failed:
        github.make(run=run)(_message("github", {"repo": "acme/app", "number": 12}))
    assert "gh auth login" in str(failed.value)

    calls.clear()
    for target in ({"repo": "--web", "number": 12}, {"repo": "acme/app", "number": "12; rm"},
                   {"repo": "acme/app"}):
        with pytest.raises(SendFailed):
            github.make(run=run)(_message("github", target))
    assert calls == [], "a target that is not owner/name and a number never reaches gh"


def test_once_only_configured_senders_exist(tmp_path, monkeypatch):
    from waku.alwayson import senders
    from waku.config import Settings

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert sorted(senders.configured(Settings(home=tmp_path, telegram_token=""))) == ["github"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    assert sorted(senders.configured(Settings(home=tmp_path, telegram_token="123:abc"))) == [
        "github", "slack", "telegram"]


# ---- D3: a run's reply is an outbox row ------------------------------------

def _event(conn, external_id="1", session_id="telegram:42", text="hello", source="telegram",
           reply_to=None):
    from waku.alwayson import events

    return events.insert(conn, source=source, external_id=external_id, session_id=session_id,
                         payload={"text": text, "reply_to": reply_to or {"chat_id": 42}})


def _worker(conn, reply):
    from waku.alwayson.worker import Worker

    run = reply if callable(reply) else (lambda session_id, batch: reply)
    return Worker(conn, run, wait=lambda seconds: None, now=Clock(), log=lambda line: None)


def _run_id(conn, event_id):
    """The id of the run that read this event, as the outbox writes it."""
    import json

    (row,) = [r for r in conn.execute("SELECT id, event_ids FROM runs")
              if event_id in json.loads(r["event_ids"])]
    return str(row["id"])


def _sent_rows(conn):
    import json

    return [(r["run_id"], r["channel"], json.loads(r["target"]), r["body"],
             r["idempotency_key"], r["status"]) for r in _rows(conn)]


def test_silent_is_not_needed_for_a_reply_to_reach_the_conversation(conn):
    first = _event(conn, text="are you there?")

    assert _worker(conn, "I am here.").step() is True

    run = _run_id(conn, first)
    assert _sent_rows(conn) == [
        (run, "telegram", {"chat_id": 42}, "I am here.", f"{run}:0", "pending")]


def test_silent_reply_writes_no_row(conn):
    """Acceptance 8: the Thursday run that finds issue 12 closed says nothing."""
    for number, reply in enumerate(["SILENT", "  silent.\n", "", "   ", None]):
        _event(conn, external_id=str(number))
        assert _worker(conn, reply).step() is True
    assert _rows(conn) == []
    assert [r["status"] for r in conn.execute("SELECT status FROM events")] == ["done"] * 5


def test_silent_inside_a_sentence_is_still_a_reply(conn):
    _event(conn)
    _worker(conn, "The room is silent now.").step()
    assert [row[3] for row in _sent_rows(conn)] == ["The room is silent now."]


def test_silent_or_not_a_slack_reply_goes_to_the_thread_and_github_to_the_issue(conn):
    _event(conn, external_id="Ev1", session_id="slack:C1:1700.0001", source="slack",
           reply_to={"channel": "C1", "thread_ts": "1700.0001"})
    _event(conn, external_id="label:acme/app#12", session_id="github:acme/app#12",
           source="github", reply_to={"repo": "acme/app", "number": 12})
    worker = _worker(conn, "On it.")
    while worker.step():
        pass

    assert [(row[1], row[2]) for row in _sent_rows(conn)] == [
        ("slack", {"channel": "C1", "thread_ts": "1700.0001"}),
        ("github", {"repo": "acme/app", "number": 12})]


def test_silent_failed_run_writes_no_row(conn):
    _event(conn)

    def run(session_id, batch):
        raise RuntimeError("model is down")

    _worker(conn, run).step()
    assert _rows(conn) == []


def test_replay_of_a_run_after_a_crash_leaves_one_row(tmp_path, monkeypatch):
    """Acceptance 7. The process dies after the reply is written and before
    the events are marked done, so the next start runs them again."""
    from waku.alwayson import events

    class Killed(BaseException):
        pass

    def die(conn, ids, status):
        raise Killed

    first_conn = connect(tmp_path)
    event_id = _event(first_conn, text="book a room")
    with monkeypatch.context() as patch:
        patch.setattr(events, "mark", die)
        with pytest.raises(Killed):
            _worker(first_conn, "Booked.").step()
    assert len(_rows(first_conn)) == 1
    first_conn.close()

    second_conn = connect(tmp_path)
    try:
        runs = []

        def run(session_id, batch):
            runs.append(session_id)
            return "Booked, again."

        worker = _worker(second_conn, run)
        assert worker.recover() == 1
        while worker.step():
            pass
        assert runs == ["telegram:42"], "the run did happen a second time"
        run = _run_id(second_conn, event_id)
        assert _sent_rows(second_conn) == [
            (run, "telegram", {"chat_id": 42}, "Booked.", f"{run}:0", "pending")]
    finally:
        second_conn.close()


def test_silent_reply_from_the_model_reaches_no_sender(tmp_path):
    """The whole path with waku.respond(): one conversation gets a reply and
    one gets nothing, and the dispatcher sends exactly one message."""
    from evals.helpers import ScriptedClient, make_waku, response, text_block
    from waku.alwayson.worker import AgentRun

    home = tmp_path / "home"
    home.mkdir()
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = ScriptedClient([gate, response([text_block("Issue 12 is still open.")]),
                             gate, response([text_block("SILENT")])])
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    conn = connect(home)
    sender = FakeSender()
    try:
        _event(conn, external_id="1", text="is issue 12 open?")
        _event(conn, external_id="2", session_id="telegram:77", text="timer: check issue 12",
               reply_to={"chat_id": 77})
        worker = _worker(conn, run)
        while worker.step():
            pass
        _dispatcher(conn, Clock(), {"telegram": sender}).step()
    finally:
        run.close()
        conn.close()

    assert sender.delivered == [({"chat_id": 42}, "Issue 12 is still open.")]
