"""DETERMINISTIC EVAL: the example of spec 023, end to end (acceptance 9).

Monday, in Slack, Ana writes: "@waku if issue 12 in acme/app is still open on
Thursday, comment on it that Sean will review it this week."

  Monday      the agent sets a timer and answers Ana in her thread
  Thursday    the timer wakes it; issue 12 is open; the comment needs Sean's
              yes, so the agent asks Sean on Telegram and posts nothing
  two hours   Sean answers "yes"; the stored comment is posted once, and the
  later       agent says nothing more

Everything real runs: the Slack and Telegram handlers, the inbox, the worker,
waku.respond(), the tools, the approval gate, the timer loop, the outbox and
the dispatcher. Four things are fake: the clock, the model (a script), the
`gh` CLI and the two chat senders. No network, no model, no real seconds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.alwayson import timers
from waku.alwayson.dispatcher import Dispatcher
from waku.alwayson.events import Inbox
from waku.alwayson.senders import github as github_sender
from waku.alwayson.sources import slack as slack_source
from waku.alwayson.sources import telegram as telegram_source
from waku.alwayson.worker import AgentRun, Worker
from waku.db import connect
from waku.tools import schedule

MONDAY_NOON = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
THURSDAY_9 = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
ANA = ("<@U_WAKU> if issue 12 in acme/app is still open on Thursday, comment on it "
       "that Sean will review it this week.")
PROMISE = "I will check issue 12 on Thursday at 09:00."
NOTE = ("If issue 12 in acme/app is still open, comment on it that Sean will review it "
        "this week.")
COMMENT = "Sean will review this issue this week."
GATE = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class FakeGh:
    """Stands in for the `gh` CLI. Issue 12 is open and has no comments."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=20):
        self.calls.append(argv)
        if argv[:3] == ["gh", "issue", "view"] and "--json" in argv:
            return True, json.dumps({"comments": []})
        if argv[:3] == ["gh", "issue", "view"]:
            return True, "title: Login fails on Safari\nstate: OPEN\nnumber: 12"
        return True, "https://github.com/acme/app/issues/12#issuecomment-1"

    @property
    def comments(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[:3] == ["gh", "issue", "comment"]]


def test_the_example_runs_from_mondays_message_to_thursdays_comment(tmp_path, monkeypatch):
    import waku.tools.github as github_tool

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ASK", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    clock, gh, sent = Clock(MONDAY_NOON), FakeGh(), []
    monkeypatch.setattr(github_tool, "_run", gh)   # what the agent's github_read calls

    client = ScriptedClient([
        # Monday: set the timer, answer Ana
        GATE,
        response([tool_block("schedule_task", {"when": "2026-11-19T09:00", "note": NOTE})],
                 stop_reason="tool_use"),
        response([text_block(PROMISE)]),
        # Thursday: look at the issue, try to comment, say nothing
        GATE,
        response([tool_block("github_read", {"command": "issue view", "number": 12,
                                             "repo": "acme/app"})], stop_reason="tool_use"),
        response([tool_block("send_message", {"to": "github:acme/app#12", "body": COMMENT})],
                 stop_reason="tool_use"),
        response([text_block("SILENT")]),
        # after Sean's yes: the outcome arrives in Ana's thread; nothing to add
        GATE,
        response([text_block("SILENT")]),
    ])

    def agent():
        waku = make_waku(home, client=client, episodic_store="sqlite", gh_tool=True)
        # the agent's own schedule_task, on the test's clock and in UTC
        waku.tools.register(schedule.make_tool(
            home, session_id=lambda: waku.session.session_id, now=clock, tz=UTC))
        return waku

    def record(channel):
        return lambda message: sent.append((channel, message.target, message.body))

    run = AgentRun(agent, log=lambda line: None)
    inbox = Inbox(home)
    conn = connect(home)
    worker = Worker(conn, run, wait=lambda seconds: None, now=clock, log=lambda line: None)
    timer_loop = timers.TimerLoop(conn, now=clock, log=lambda line: None)
    dispatcher = Dispatcher(conn, {"slack": record("slack"), "telegram": record("telegram"),
                                   "github": github_sender.make(run=gh)},
                            now=clock, log=lambda line: None)

    def serve():
        """One pass of everything `waku serve` loops over, until it is idle."""
        timer_loop.step()
        while worker.step():
            pass
        dispatcher.step()

    try:
        # ---- Monday 12:00: Ana mentions the agent in Slack
        assert slack_source.record(inbox, {
            "team_id": "T1", "event_id": "Ev1",
            "event": {"type": "app_mention", "channel": "C1", "ts": "1700.0001",
                      "user": "U_ANA", "text": ANA}},
            team_id="T1", channels={"C1"})
        serve()
        assert sent == [("slack", {"channel": "C1", "thread_ts": "1700.0001"}, PROMISE)]

        # ---- Wednesday: nothing is due, so nothing happens
        clock.at = THURSDAY_9 - timedelta(hours=10)
        serve()
        assert len(sent) == 1

        # ---- Thursday 09:00: the timer wakes the agent
        clock.at = THURSDAY_9
        serve()
        assert len(sent) == 2
        channel, target, request = sent[1]
        assert (channel, target) == ("telegram", {"chat_id": 42})
        assert f'Comment on acme/app#12: "{COMMENT}"' in request
        assert "Approve?" in request
        assert gh.comments == [], "nothing is posted before Sean answers"
        assert gh.calls == [["gh", "issue", "view", "12", "--repo", "acme/app"]], (
            "the agent read the issue, and that is all gh was asked")
        parked = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status = 'parked'")]
        assert [r["session_id"] for r in parked] == ["slack:C1:1700.0001"]
        assert parked[0]["pending_hash"][:8] in request

        # ---- Thursday 11:00: Sean answers yes on Telegram
        clock.at = THURSDAY_9 + timedelta(hours=2)
        assert telegram_source.record(inbox, SimpleNamespace(
            update_id=900, effective_chat=SimpleNamespace(id=42),
            effective_user=SimpleNamespace(id=42, full_name="Sean Chen"),
            message=SimpleNamespace(text="yes")))
        serve()

        (posted,) = gh.comments
        assert posted[:6] == ["gh", "issue", "comment", "12", "--repo", "acme/app"]
        assert posted[6].startswith(f"--body={COMMENT}\n")
        assert len(sent) == 2, "the agent says nothing more to Ana or to Sean"

        # ---- later: every table is settled, and another pass sends nothing
        clock.at += timedelta(days=1)
        serve()
        assert len(gh.comments) == 1 and len(sent) == 2
        assert client._script == [], "the model ran three times and was not asked to approve"
        assert {r["status"] for r in conn.execute("SELECT status FROM events")} == {"done"}
        assert {r["status"] for r in conn.execute("SELECT status FROM runs")} == {"done"}
        assert {r["status"] for r in conn.execute("SELECT status FROM timers")} == {"fired"}
        outgoing = [(r["channel"], r["status"]) for r in conn.execute(
            "SELECT channel, status FROM outbox ORDER BY id")]
        assert outgoing == [("slack", "sent"), ("telegram", "sent"), ("github", "sent")], (
            "the three outgoing messages, in order")
    finally:
        run.close()
        conn.close()
