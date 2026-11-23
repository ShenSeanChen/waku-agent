"""DETERMINISTIC EVAL: seeing and cancelling what waits (spec 024, decision 7).

A timer, a call that waits for a yes and a queued message were rows in three
tables, and nothing showed them or could cancel one. One function lists all
three; `waku waiting`, two tools and one dashboard card read it.

  t3    a timer that has not fired
  r7    a call that waits for the owner's yes
  m12   a message that is queued, or that failed five times

These tests use a real state.db in a temporary home and a fake clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import approvals, events, outbox, runs, timers, waiting
from waku.alwayson.dispatcher import Dispatcher
from waku.alwayson.worker import Worker
from waku.db import connect

NOW = datetime(2026, 11, 17, 22, 0, 0, tzinfo=UTC)
THURSDAY_9 = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
CHAT = ("telegram", {"chat_id": 42})
THREAD = ("slack", {"channel": "C1", "thread_ts": "1700.0001"})
COMMENT = {"to": "github:acme/app#12", "body": "Sean will review this issue this week."}


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


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


def _three(conn, home, clock=None):
    """One timer, one call that waits and one queued message. Returns their
    ids as (timer, run, message)."""
    clock = clock or Clock()
    timer_id = timers.insert(conn, due_at=THURSDAY_9, session_id="telegram:42",
                             note="Check whether issue 12 is still open.", address=CHAT,
                             idempotency_key="1:t1", now=clock())
    event_id = events.insert(conn, source="slack", external_id="Ev1",
                             session_id="slack:C1:1700.0001",
                             payload={"text": "comment on issue 12", "reply_to": THREAD[1]})
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=clock())
    with outbox.running(home, str(run.id), THREAD):
        approvals.Gate(home, conn, now=clock)("send_message", COMMENT)
    events.mark(conn, [event_id], "done")
    runs.finish(conn, run.id, "parked", now=clock())
    # the request to the owner went out; what is left queued is one reply
    conn.execute("UPDATE outbox SET status = 'sent'")
    message_id = outbox.insert(conn, run_id="9", channel="telegram", target=CHAT[1],
                               body="The demo moved to Friday.", idempotency_key="9:0",
                               now=clock())
    return timer_id, run.id, message_id


# ---- C1: one function lists all three, and one cancels ----------------------

def test_list_shows_one_timer_one_waiting_call_and_one_queued_message(home, conn):
    """Acceptance 7."""
    timer_id, run_id, message_id = _three(conn, home)

    found = waiting.listing(conn)

    assert [(item.id, item.what) for item in found.timers] == [
        (f"t{timer_id}", "Check whether issue 12 is still open.")]
    assert found.timers[0].when == "2026-11-19T09:00:00+00:00"
    assert [(item.id, item.what) for item in found.calls] == [
        (f"r{run_id}", 'Comment on acme/app#12: "Sean will review this issue this week."')]
    assert found.calls[0].when == "2026-11-17T22:00:00+00:00", "when it was asked"
    assert [(item.id, item.what, item.status) for item in found.messages] == [
        (f"m{message_id}", "The demo moved to Friday.", "queued")]
    assert found.messages[0].where == "telegram:42"
    assert len(found) == 3


def test_list_leaves_out_what_no_longer_waits(home, conn):
    timer_id, run_id, message_id = _three(conn, home)
    timers.mark_fired(conn, timer_id)
    runs.resolve(conn, run_id, "done", approved=True, now=NOW)
    outbox.mark_sent(conn, message_id)

    found = waiting.listing(conn)
    assert (found.timers, found.calls, found.messages) == ([], [], [])
    assert len(found) == 0


def test_list_shows_a_message_that_failed_five_times_as_dead(home, conn):
    _, _, message_id = _three(conn, home)
    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.record_failure(conn, message_id, "chat not found", NOW)

    (message,) = waiting.listing(conn).messages
    assert (message.id, message.status) == (f"m{message_id}", "dead")


def test_cancel_a_timer_leaves_nothing_to_fire(home, conn):
    """Acceptance 7."""
    timer_id, _, _ = _three(conn, home)

    cancelled = waiting.cancel(conn, f"t{timer_id}")

    assert cancelled.what == "Check whether issue 12 is still open."
    assert timers.get(conn, timer_id).status == "cancelled", "the row keeps its record"
    assert timers.TimerLoop(conn, now=Clock(THURSDAY_9), log=lambda line: None).step() == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE source = 'timer'").fetchone()[0] == 0
    assert waiting.listing(conn).timers == []


def test_cancel_a_waiting_call_leaves_nothing_to_run(home, conn):
    """Acceptance 7: a yes after the cancel approves nothing."""
    _, run_id, _ = _three(conn, home)
    executed = []

    def run(session_id, batch):
        return "SILENT"

    run.execute = lambda parked, address: executed.append(parked.id) or "ok"
    waiting.cancel(conn, f"r{run_id}")
    events.insert(conn, source="telegram", external_id="900", session_id="telegram:42",
                  payload={"text": "yes", "sender": {"id": "42"}, "reply_to": CHAT[1]})
    worker = Worker(conn, run, wait=lambda seconds: None, now=Clock(), log=lambda line: None)
    while worker.step():
        pass

    assert executed == []
    assert runs.get(conn, run_id).status == "cancelled"
    assert runs.get(conn, run_id).pending_call["tool"] == "send_message", "the record stays"
    assert waiting.listing(conn).calls == []


def test_cancel_a_queued_message_leaves_nothing_to_send(home, conn):
    """Acceptance 7."""
    _, _, message_id = _three(conn, home)
    sent = []

    waiting.cancel(conn, f"m{message_id}")
    Dispatcher(conn, {"telegram": sent.append}, now=Clock(), log=lambda line: None).step()

    assert sent == []
    assert outbox.get(conn, message_id).status == "cancelled"
    assert waiting.listing(conn).messages == []


def test_cancel_a_dead_message_takes_it_off_the_list(home, conn):
    _, _, message_id = _three(conn, home)
    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.record_failure(conn, message_id, "chat not found", NOW)

    waiting.cancel(conn, f"m{message_id}")
    assert outbox.get(conn, message_id).status == "cancelled"


@pytest.mark.parametrize("item_id", ["t999", "r999", "m999", "x1", "", "t", "12", "t-1"])
def test_cancel_refuses_an_id_that_names_nothing_waiting(home, conn, item_id):
    _three(conn, home)
    with pytest.raises(waiting.NotWaiting):
        waiting.cancel(conn, item_id)
    assert len(waiting.listing(conn)) == 3


def test_cancel_refuses_what_already_fired_ran_or_was_sent(home, conn):
    timer_id, run_id, message_id = _three(conn, home)
    timers.mark_fired(conn, timer_id)
    runs.resolve(conn, run_id, "done", approved=True, now=NOW)
    outbox.mark_sent(conn, message_id)

    for item_id in (f"t{timer_id}", f"r{run_id}", f"m{message_id}"):
        with pytest.raises(waiting.NotWaiting):
            waiting.cancel(conn, item_id)
    assert timers.get(conn, timer_id).status == "fired"
    assert runs.get(conn, run_id).status == "done"
    assert outbox.get(conn, message_id).status == "sent"


def test_cancel_frees_the_conversation_for_another_call(home, conn):
    """One call waits per conversation. A cancelled one no longer counts."""
    _, run_id, _ = _three(conn, home)
    waiting.cancel(conn, f"r{run_id}")

    event_id = events.insert(conn, source="slack", external_id="Ev2",
                             session_id="slack:C1:1700.0001",
                             payload={"text": "again", "reply_to": THREAD[1]})
    run = runs.start(conn, "slack:C1:1700.0001", [event_id], now=NOW)
    with outbox.running(home, str(run.id), THREAD):
        deny, _ = approvals.Gate(home, conn, now=Clock())("send_message", COMMENT)
    assert "already waits" not in deny
    assert json.loads(conn.execute("SELECT pending_call FROM runs WHERE id = ?",
                                   (run.id,)).fetchone()[0])["tool"] == "send_message"


# ---- C2: `waku waiting` -----------------------------------------------------

def _cli(home, monkeypatch, capsys, *argv):
    """Run `waku waiting ...` through the real entry point. Returns (exit
    code, what it printed)."""
    import sys

    import waku.__main__ as entry

    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.setattr(waiting, "LOCAL", UTC)   # the zone the times are printed in
    monkeypatch.setattr(sys, "argv", ["waku", "waiting", *argv])
    with pytest.raises(SystemExit) as done:
        entry.main()
    return done.value.code, capsys.readouterr().out


def test_cli_lists_one_timer_one_waiting_call_and_one_queued_message(
        home, conn, monkeypatch, capsys):
    """Acceptance 7."""
    timer_id, run_id, message_id = _three(conn, home)

    code, out = _cli(home, monkeypatch, capsys)

    assert code == 0
    lines = out.splitlines()
    assert "Timers that have not fired" in lines
    assert "Calls that wait for your yes" in lines
    assert "Messages that are queued or could not be sent" in lines
    (timer,) = [line for line in lines if line.strip().startswith(f"t{timer_id} ")]
    assert "Thursday 19 November 09:00" in timer and "Check whether issue 12" in timer
    (call,) = [line for line in lines if line.strip().startswith(f"r{run_id} ")]
    assert 'Comment on acme/app#12: "Sean will review this issue this week."' in call
    (message,) = [line for line in lines if line.strip().startswith(f"m{message_id} ")]
    assert "queued" in message and "telegram:42" in message and "The demo moved" in message
    assert "waku waiting cancel <id>" in out


def test_cli_says_so_when_nothing_waits(home, conn, monkeypatch, capsys):
    code, out = _cli(home, monkeypatch, capsys)
    assert (code, out.strip()) == (0, "Nothing waits.")


def test_cli_cancel_on_each_leaves_nothing_to_fire_run_or_send(
        home, conn, monkeypatch, capsys):
    """Acceptance 7."""
    timer_id, run_id, message_id = _three(conn, home)

    for item_id in (f"t{timer_id}", f"r{run_id}", f"m{message_id}"):
        code, out = _cli(home, monkeypatch, capsys, "cancel", item_id)
        assert code == 0 and out.startswith(f"Cancelled {item_id}: ")

    assert len(waiting.listing(conn)) == 0
    assert timers.due(conn, THURSDAY_9) == []
    assert runs.parked(conn) == []
    assert outbox.due(conn, THURSDAY_9) == []
    assert _cli(home, monkeypatch, capsys)[1].strip() == "Nothing waits."


def test_cli_cancel_of_an_unknown_id_fails_and_changes_nothing(
        home, conn, monkeypatch, capsys):
    _three(conn, home)

    code, out = _cli(home, monkeypatch, capsys, "cancel", "t999")
    assert code == 1 and "'t999' is not waiting" in out
    code, out = _cli(home, monkeypatch, capsys, "cancel")
    assert code == 1 and "waku waiting cancel <id>" in out
    assert len(waiting.listing(conn)) == 3


def test_cli_is_in_the_help(monkeypatch, capsys):
    import sys

    import waku.__main__ as entry

    monkeypatch.setattr(sys, "argv", ["waku", "no-such-command"])
    with pytest.raises(SystemExit):
        entry.main()
    assert "waku waiting" in capsys.readouterr().out


def test_cli_prints_no_emoji(home, conn, monkeypatch, capsys):
    _three(conn, home)
    _, out = _cli(home, monkeypatch, capsys)
    assert out.isascii()


# ---- C3: the tools list_waiting and cancel_waiting --------------------------

def _tools(home, session="telegram:42"):
    from waku.tools import waiting as waiting_tools

    made = {tool.name: tool for tool in waiting_tools.make_tools(
        home, session_id=lambda: session, tz=UTC)}
    return made["list_waiting"].fn, made["cancel_waiting"].fn


def test_tool_list_answers_what_reminders_do_i_have(home, conn):
    timer_id, run_id, message_id = _three(conn, home)
    list_waiting, _ = _tools(home)

    said = list_waiting()

    assert f"t{timer_id}: Thursday 19 November 09:00. Check whether issue 12" in said
    assert f'r{run_id}: Comment on acme/app#12: "Sean will review' in said
    assert f"m{message_id}: to telegram:42, queued. The demo moved to Friday." in said
    assert "waku" not in said.lower().replace("label waku", ""), "no command for the model to quote"


def test_tool_list_says_so_when_nothing_waits(home, conn):
    list_waiting, _ = _tools(home)
    assert list_waiting().startswith("Nothing waits")


def test_tool_cancel_cancels_the_thursday_one(home, conn):
    timer_id, _, _ = _three(conn, home)
    _, cancel_waiting = _tools(home)

    said = cancel_waiting(id=f"t{timer_id}")

    assert said == f"Cancelled t{timer_id}: Check whether issue 12 is still open."
    assert timers.get(conn, timer_id).status == "cancelled"
    again = cancel_waiting(id=f"t{timer_id}")
    assert again.startswith("Not cancelled:") and "list_waiting" in again


def test_tool_in_another_persons_conversation_sees_only_that_conversation(home, conn):
    """Ana's thread is not the owner. A run there lists and cancels what her
    thread set, and nothing of Sean's."""
    timer_id, run_id, message_id = _three(conn, home)
    list_waiting, cancel_waiting = _tools(home, session="slack:C1:1700.0001")

    with outbox.running(home, "5", THREAD):
        said = list_waiting()
        refused = cancel_waiting(id=f"t{timer_id}")
        cancelled = cancel_waiting(id=f"r{run_id}")

    assert f"r{run_id}:" in said, "the call her thread parked"
    assert f"t{timer_id}:" not in said and f"m{message_id}:" not in said
    assert refused.startswith("Not cancelled:")
    assert timers.get(conn, timer_id).status == "pending"
    assert cancelled.startswith(f"Cancelled r{run_id}:")


def test_tool_in_the_owners_chat_sees_everything(home, conn):
    _three(conn, home)
    list_waiting, _ = _tools(home)
    with outbox.running(home, "5", CHAT):
        said = list_waiting()
    assert said.count("\n  ") == 3


def test_tool_both_are_core_tools_with_a_mode(home):
    from evals.helpers import ScriptedClient, make_waku

    waku = make_waku(home, client=ScriptedClient([]), episodic_store="sqlite")
    try:
        names = [schema["name"] for schema in waku.tools.schemas()]
    finally:
        waku.close()
        waku.conn.close()
    assert "list_waiting" in names and "cancel_waiting" in names
    assert approvals.mode("list_waiting", {}) == "allow"
    assert approvals.mode("cancel_waiting", {"id": "t1"}) == "allow"


# ---- C4: the dashboard card "Waiting" and its two routes --------------------

STATIC = __import__("pathlib").Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"


def test_dashboard_data_carries_the_same_three_lists(home, conn, monkeypatch):
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(home))
    timer_id, run_id, message_id = _three(conn, home)

    data = dashboard.waiting_data()

    assert [row["id"] for row in data["timers"]] == [f"t{timer_id}"]
    assert [row["id"] for row in data["calls"]] == [f"r{run_id}"]
    assert [row["id"] for row in data["messages"]] == [f"m{message_id}"]
    assert data["timers"][0] == {
        "id": f"t{timer_id}", "when": "2026-11-19T09:00:00+00:00",
        "what": "Check whether issue 12 is still open.", "where": "telegram:42",
        "status": "pending", "repeat": "", "zone": ""}
    json.dumps(data)   # the route sends it as JSON


def test_dashboard_cancel_takes_a_row_off_the_card(home, conn, monkeypatch):
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(home))
    timer_id, _, _ = _three(conn, home)

    out = dashboard.waiting_cancel({"id": f"t{timer_id}"})

    assert out["ok"] is True and out["cancelled"]["id"] == f"t{timer_id}"
    assert dashboard.waiting_data()["timers"] == []
    assert timers.get(conn, timer_id).status == "cancelled"
    again = dashboard.waiting_cancel({"id": f"t{timer_id}"})
    assert "error" in again and "not waiting" in again["error"]
    assert "error" in dashboard.waiting_cancel({})


def test_dashboard_serves_the_two_routes_and_each_has_a_hosted_policy_entry():
    import inspect

    from hosted.core import policy
    from waku.ops import dashboard

    source = inspect.getsource(dashboard)
    assert '"/api/waiting"' in source and '"/api/waiting/cancel"' in source
    assert policy.DECISIONS["/api/waiting"] == policy.PASS
    assert policy.DECISIONS["/api/waiting/cancel"] == policy.PASS
    assert policy.match("/api/waiting/cancel") == "/api/waiting/cancel"


def test_dashboard_card_is_on_overview_with_a_cancel_button_on_each_row():
    script = (STATIC / "js" / "waiting.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    views = (STATIC / "js" / "views.js").read_text(encoding="utf-8")

    assert "function waitingCard(" in script and "function cancelWaiting(" in script
    assert 'title: "Waiting"' in script
    assert '"/api/waiting/cancel"' in script
    assert "cancelWaiting(" in script and "Cancel" in script
    assert "waitingCard(d)" in views, "Overview shows the card"
    assert index.index("/static/js/waiting.js") < index.index("/static/js/main.js")
    assert script.isascii(), "no emoji in the dashboard"
    assert script.count("esc(") >= 3, "a note and a message are text someone else wrote"
