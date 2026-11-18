"""DETERMINISTIC EVAL: send_message writes an outbox row (spec 023, task D4).

The tool used to write a .txt file and say "Nothing was sent". It now writes
one row into `outbox`, which `waku serve` delivers. Until approvals exist
(spec 023, group C) it reaches two places only: the owner, and the
conversation the run came from. Offline: a temporary home and no sender.
"""

from __future__ import annotations

import json

import pytest

from waku.alwayson import outbox
from waku.db import connect
from waku.tools import messages


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    (tmp_path / "outbox").mkdir()
    return tmp_path


def _rows(home):
    conn = connect(home)
    try:
        return [(r["run_id"], r["channel"], json.loads(r["target"]), r["body"],
                 r["idempotency_key"], r["status"])
                for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
    finally:
        conn.close()


def _send(home, to, body="The demo moved to Friday."):
    return messages.make_tool(home).fn(to=to, body=body)


def test_a_message_to_the_owner_is_one_pending_row_and_no_file(home):
    said = _send(home, "owner")

    ((run_id, channel, target, body, key, status),) = _rows(home)
    assert (run_id, channel, target, body, status) == (
        "chat", "telegram", {"chat_id": 42}, "The demo moved to Friday.", "pending")
    assert key.startswith("chat:")
    assert list((home / "outbox").iterdir()) == [], "the .txt path is gone"
    assert "waku serve" in said and "telegram:42" in said
    assert "Nothing was sent" not in said


def test_the_owner_can_be_named_by_address(home):
    _send(home, "telegram:42")
    assert [row[2] for row in _rows(home)] == [{"chat_id": 42}]


def test_two_messages_outside_a_run_are_two_rows(home):
    _send(home, "owner", "first")
    _send(home, "owner", "second")
    assert [row[3] for row in _rows(home)] == ["first", "second"]


def test_a_name_without_an_address_writes_nothing_and_says_what_works(home):
    said = _send(home, "Alex")

    assert _rows(home) == []
    assert "Alex" in said and "telegram:<chat id>" in said
    assert "not sent" in said.lower()


def test_a_third_party_is_refused_until_approval_exists(home):
    """A message to someone other than the owner needs the owner's yes. Nothing
    can ask for that yes yet, so the tool writes no row."""
    for to in ("telegram:77", "slack:C0123ABC", "github:acme/app#12"):
        said = _send(home, to)
        assert "approv" in said.lower(), said
    assert _rows(home) == []


def test_owner_without_the_setting_explains_the_setting(home, monkeypatch):
    monkeypatch.delenv("WAKU_OWNER")
    said = _send(home, "owner")
    assert _rows(home) == []
    assert "WAKU_OWNER" in said


def test_an_empty_body_writes_nothing(home):
    said = _send(home, "owner", "   ")
    assert _rows(home) == []
    assert "empty" in said.lower()


def test_inside_a_run_the_message_can_go_to_the_runs_own_conversation(home):
    thread = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})
    with outbox.running(home, "e7", thread):
        _send(home, "slack:C1:1700.0001", "Started; I will report here.")
        said = _send(home, "slack:C2")

    assert _rows(home) == [("e7", "slack", {"channel": "C1", "thread_ts": "1700.0001"},
                            "Started; I will report here.", "e7:1", "pending")]
    assert "approv" in said.lower()
    assert outbox.current_run(home) is None, "the run's context ends with the run"


def test_inside_a_run_the_keys_count_up_and_a_replay_adds_no_row(home):
    """A run that is replayed after a crash calls the tool again. The same
    call in the same place has the same key, so the owner gets one message."""
    for attempt in range(2):
        with outbox.running(home, "e7", None):
            first = _send(home, "owner", "one")
            _send(home, "owner", "two")
        assert ("already" in first) == (attempt == 1)

    assert [(row[3], row[4]) for row in _rows(home)] == [("one", "e7:1"), ("two", "e7:2")]


def test_the_description_says_the_message_is_sent():
    tool = messages.make_tool(None)
    assert "local outbox for the user to review" not in tool.description
    assert "waku serve" in tool.description
    assert set(tool.input_schema["required"]) == {"to", "body"}


def test_the_worker_gives_the_tool_its_run(tmp_path, monkeypatch):
    """Under `waku serve` a send_message call and the reply are two rows of
    the same run: key 1 for the call, key 0 for the reply."""
    from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
    from waku.alwayson import events
    from waku.alwayson.worker import AgentRun, Worker

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    home = tmp_path / "home"
    home.mkdir()
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([tool_block("send_message", {"to": "owner", "body": "Ana asked for a room."})],
                 stop_reason="tool_use"),
        response([text_block("I told Sean.")]),
    ])
    run = AgentRun(lambda: make_waku(home, client=client, episodic_store="sqlite"),
                   log=lambda line: None)
    conn = connect(home)
    try:
        event_id = events.insert(
            conn, source="slack", external_id="Ev1", session_id="slack:C1:1700.0001",
            payload={"text": "tell Sean I need a room",
                     "reply_to": {"channel": "C1", "thread_ts": "1700.0001"}})
        Worker(conn, run, wait=lambda seconds: None, log=lambda line: None).step()
    finally:
        run.close()
        conn.close()

    assert [(row[0], row[1], row[3], row[4]) for row in _rows(home)] == [
        (f"e{event_id}", "telegram", "Ana asked for a room.", f"e{event_id}:1"),
        (f"e{event_id}", "slack", "I told Sean.", f"e{event_id}:0"),
    ]
