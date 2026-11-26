"""DETERMINISTIC EVAL: a drafted review waits for a maintainer's click in
Slack, and is posted as a comment only (spec 035, group 2).

Monday's PR desk run drafts a review of PR #428: "This drops the duplicate
check for the four non-SQLite stores." The call parks. Its request appears in
#dev in a thread under that morning's digest, with Approve and Reject. A
click from a person WAKU_APPROVERS maps to a GitHub login with write access
becomes the same `yes` the worker resolves for a Telegram reply, and the
stored text is posted with `gh pr review --comment`, exactly as drafted.

These tests use a real state.db in a temporary home, a fake `gh`, a fake
Slack and a fake clock. Nothing reaches GitHub or Slack.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from waku.alwayson import approvals, outbox, runs
from waku.alwayson.dispatcher import Dispatcher
from waku.alwayson.senders import github as github_sender
from waku.db import connect
from waku.tools import github
from waku.tools.registry import ToolRegistry

# Monday 30 November 2026, 09:00 in Los Angeles.
MONDAY = datetime(2026, 11, 30, 17, 0, tzinfo=UTC)
REVIEW = {"repo": "acme/app", "number": 428,
          "body": "This drops the duplicate check for the four non-SQLite stores.\n\n--approve"}
REVIEW_HASH = hashlib.sha256(json.dumps(
    {"tool": "github_review", "args": REVIEW}, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()


class Clock:
    def __init__(self, at: datetime = MONDAY) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


class ReviewGh:
    """Stands in for `gh`: records every argv and what it read on standard
    input. `pr view --json reviews` answers with `reviews`."""

    def __init__(self, reviews=()) -> None:
        self.calls, self.reviews = [], list(reviews)

    def __call__(self, argv, input_text=None):
        self.calls.append((argv, input_text))
        if argv[:3] == ["gh", "pr", "view"]:
            return True, json.dumps({"reviews": self.reviews})
        return True, ""

    @property
    def posted(self):
        return [(argv, text) for argv, text in self.calls if argv[:3] == ["gh", "pr", "review"]]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.setenv("WAKU_GITHUB_REPOS", "acme/app")
    for name in ("WAKU_APPROVE_ALLOW", "WAKU_APPROVE_ASK", "WAKU_HOSTED",
                 "WAKU_DIGEST_CHANNEL", "WAKU_APPROVERS"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def conn(home):
    connection = connect(home)
    yield connection
    connection.close()


def _outbox(conn):
    return [(r["channel"], json.loads(r["target"]), r["body"], r["idempotency_key"])
            for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


# ---- C1: only a comment ------------------------------------------------------

def test_github_review_only_comments(home, conn):
    """The approved call queues one row, and the sender posts it with the
    event COMMENT. The text goes in on standard input, so "--approve" in the
    review is a word in a sentence, never a flag."""
    tool = github.make_review_tool(home)
    with outbox.running(home, "7", None, approved_key="7:abc"):
        said = tool.fn(**REVIEW)
    assert said == "The review on acme/app#428 is on its way, as a comment."
    assert _outbox(conn) == [("github", {"repo": "acme/app", "number": 428, "review": True},
                              REVIEW["body"], "7:abc")]

    gh = ReviewGh()
    Dispatcher(conn, {"github": github_sender.make(run=gh)}, log=lambda line: None).step()

    ((argv, stdin),) = gh.posted
    assert argv == ["gh", "pr", "review", "428", "--repo", "acme/app", "--comment",
                    "--body-file", "-"]
    assert stdin == REVIEW["body"] + "\n\n<!-- waku:7:abc -->"
    every_word = {word for argv, _ in gh.calls for word in argv}
    assert not {"--approve", "-a", "--request-changes", "-r", "--body", "-b"} & every_word
    assert set(tool.input_schema["properties"]) == {"repo", "number", "body"}


@pytest.mark.parametrize("args", [
    {**REVIEW, "event": "APPROVE"},
    {**REVIEW, "approve": True},
    {**REVIEW, "repo": "acme/app --approve"},
    {**REVIEW, "number": "428 --approve"},
    {**REVIEW, "repo": "other/repo"},
    {**REVIEW, "body": "   "},
])
def test_no_model_input_reaches_a_flag_or_another_repository(home, conn, args):
    registry = ToolRegistry()
    registry.register(github.make_review_tool(home))
    with outbox.running(home, "7", None, approved_key="7:abc"):
        said = registry.execute("github_review", args)
    assert said.startswith(("Error running github_review", "Not posted")), said
    assert _outbox(conn) == []


def test_github_review_queues_nothing_without_a_yes(home, conn):
    tool = github.make_review_tool(home)
    assert tool.fn(**REVIEW).startswith("Not posted: a review is posted only after")
    with outbox.running(home, "7", None):
        assert tool.fn(**REVIEW).startswith("Not posted: a review is posted only after")
    assert _outbox(conn) == []


def test_a_review_delivered_twice_is_posted_once(home, conn):
    """A process that dies after GitHub took the review and before the row is
    marked sent finds the marker on its next attempt."""
    tool = github.make_review_tool(home)
    with outbox.running(home, "7", None, approved_key="7:abc"):
        tool.fn(**REVIEW)
    gh = ReviewGh(reviews=[{"body": REVIEW["body"] + "\n\n<!-- waku:7:abc -->"}])
    Dispatcher(conn, {"github": github_sender.make(run=gh)}, log=lambda line: None).step()
    assert gh.posted == []
    assert conn.execute("SELECT status FROM outbox").fetchone()["status"] == "sent"


def test_github_review_is_registered_only_with_repositories(home, monkeypatch):
    from evals.helpers import ScriptedClient, make_waku

    (home / "on").mkdir()
    (home / "off").mkdir()
    on = make_waku(home / "on", client=ScriptedClient([]), gh_tool=True)
    monkeypatch.delenv("WAKU_GITHUB_REPOS")
    off = make_waku(home / "off", client=ScriptedClient([]), gh_tool=True)
    try:
        assert "github_review" in on.tools._tools
        assert "github_review" not in off.tools._tools
    finally:
        for agent in (on, off):
            agent.close()
            agent.conn.close()


# ---- C1: always ask -----------------------------------------------------------

@pytest.mark.parametrize("address", [
    None,
    ("telegram", {"chat_id": 42}),
    ("slack", {"channel": "D1", "private": True}),
    ("github", {"repo": "acme/app", "number": 428}),
    ("slack", {"channel": "C0DEV", "digest": True}),
])
def test_github_review_always_asks(home, monkeypatch, address):
    """Every address, the owner's private chat included, and no setting in
    the environment changes it."""
    assert approvals.mode("github_review", REVIEW, address) == "ask"
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "github_review, reply:slack, reply:github")
    monkeypatch.setenv("WAKU_HOSTED", "1")
    assert approvals.mode("github_review", REVIEW, address) == "ask"


def test_github_review_parks_under_the_gate(home, conn):
    run = runs.start(conn, "pr-desk:1", [1], now=MONDAY)
    with outbox.running(home, str(run.id), ("telegram", {"chat_id": 42})):
        deny, _ = approvals.Gate(home, conn, now=Clock())("github_review", REVIEW)
    assert deny is not None
    row = conn.execute("SELECT pending_call, pending_hash FROM runs").fetchone()
    assert json.loads(row["pending_call"])["args"] == REVIEW
    assert row["pending_hash"] == REVIEW_HASH


# ---- E1: one request shape ----------------------------------------------------

def test_request_has_one_shape_for_every_channel():
    """Decision 5: title, why, link, body and choices, whatever channel draws
    them. A board sender can render the same five later."""
    request = approvals.request("github_review", REVIEW, REVIEW_HASH)
    assert request.fields() == {
        "title": "Review PR #428",
        "why": ("A drafted review of acme/app#428. Approve posts it on the pull request as a "
                "comment, and Reject discards it."),
        "link": "https://github.com/acme/app/pull/428",
        "body": REVIEW["body"],
        "choices": ["Approve", "Reject"],
        "code": REVIEW_HASH[:8]}
    assert request.text("telegram").endswith(
        f"{REVIEW['body']}\n\nApprove? Reply yes or no. [{REVIEW_HASH[:8]}]")
    assert request.text("slack").endswith(
        f"Approve or Reject with the buttons below. [{REVIEW_HASH[:8]}]")


def test_a_tool_without_words_of_its_own_asks_exactly_as_before():
    """The Telegram request for a message is unchanged, character for character."""
    request = approvals.request("send_message", {"to": "telegram:77", "body": "hi"}, "b" * 64)
    assert request.text("telegram") == ('Send to telegram:77: "hi"\n\n'
                                        'Approve? Reply yes or no. [bbbbbbbb]')
    assert request.fields()["title"] == 'Send to telegram:77: "hi"'


# ---- D1: who approves ---------------------------------------------------------

APPROVERS = "U0SEAN:ShenSeanChen, U0IAN:yangzhao"


class PermissionGh:
    """Stands in for `gh api repos/<repo>/collaborators/<login>/permission`.
    `roles` maps a login to its role; a login without one answers 404."""

    def __init__(self, roles) -> None:
        self.roles, self.calls = dict(roles), []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        role = self.roles.get(argv[2].split("/")[4])
        if role is None:
            return False, "HTTP 404: Not Found"
        legacy = {"maintain": "write", "triage": "read"}.get(role, role)
        return True, json.dumps({"permission": legacy, "role_name": role})


@pytest.mark.parametrize("role, may", [
    ("admin", True), ("maintain", True), ("write", True),
    ("triage", False), ("read", False), (None, False),   # None: gh answered an error
])
def test_approver_follows_github_permission(monkeypatch, role, may):
    """Decision 1: GitHub decides, at the moment of the click."""
    monkeypatch.setenv("WAKU_APPROVERS", APPROVERS)
    gh = PermissionGh({"ShenSeanChen": role} if role else {})
    assert approvals.may_approve("U0SEAN", "acme/app", run=gh) is may
    assert gh.calls == [["gh", "api", "repos/acme/app/collaborators/ShenSeanChen/permission"]]


def test_permission_is_asked_at_every_click_and_never_kept(monkeypatch):
    """Someone removed from the repository loses approval at once."""
    monkeypatch.setenv("WAKU_APPROVERS", APPROVERS)
    gh = PermissionGh({"ShenSeanChen": "write"})
    assert approvals.may_approve("U0SEAN", "acme/app", run=gh) is True
    gh.roles["ShenSeanChen"] = "read"
    assert approvals.may_approve("U0SEAN", "acme/app", run=gh) is False
    assert len(gh.calls) == 2


def test_an_unmapped_person_or_a_bad_value_never_reaches_gh(monkeypatch):
    monkeypatch.setenv("WAKU_APPROVERS",
                       "U0SEAN:ShenSeanChen, U0EVIL:-flag, notanid:someone, U0X:a/b, U0IAN:yangzhao")
    assert approvals.approvers() == {"U0SEAN": "ShenSeanChen", "U0IAN": "yangzhao"}
    gh = PermissionGh({"ShenSeanChen": "admin"})
    assert approvals.may_approve("U0EVE", "acme/app", run=gh) is False
    assert approvals.may_approve("U0SEAN", "acme/app; rm -rf ~", run=gh) is False
    assert gh.calls == []


def test_a_review_is_asked_in_the_digest_channel(home, monkeypatch):
    monkeypatch.setenv("WAKU_DIGEST_CHANNEL", "C0DEV")
    assert approvals.who_may_approve("github_review", REVIEW) == [("telegram", {"chat_id": 42})], (
        "without approvers the owner is asked, as before")
    monkeypatch.setenv("WAKU_APPROVERS", APPROVERS)
    assert approvals.who_may_approve("github_review", REVIEW) == [
        ("slack", {"channel": "C0DEV"}), ("telegram", {"chat_id": 42})]
    assert approvals.who_may_approve("send_message", {"to": "telegram:77", "body": "hi"}) == [
        ("telegram", {"chat_id": 42})], "every other call is asked as before"
    assert approvals.can_answer(("slack", {"channel": "C0DEV"})) is True
    assert approvals.can_answer(("slack", {"channel": "C1"})) is False


def test_waku_connections_names_each_approvers_role():
    from waku import integrations

    gh = PermissionGh({"ShenSeanChen": "admin", "yangzhao": "read"})
    env = {"WAKU_APPROVERS": APPROVERS, "WAKU_DIGEST_CHANNEL": "C0DEV",
           "WAKU_GITHUB_REPOS": "acme/app"}
    assert integrations.approvers_status(env, run=gh) == (
        True, "2 mapped · ShenSeanChen admin on acme/app, yangzhao read on acme/app")
    assert integrations.approvers_status({}, run=gh)[0] is None
    assert integrations.approvers_status({**env, "WAKU_DIGEST_CHANNEL": ""}, run=gh)[0] is False
    nobody = PermissionGh({"ShenSeanChen": "read", "yangzhao": "read"})
    assert integrations.approvers_status(env, run=nobody)[0] is False


# ---- D2a: the request waits in a thread under its digest ---------------------

DIGEST = "#428 check full fact table for duplicates · new · CI green · +140/-15 · memory"
NOTE = "PR desk: acme/app"


def _desk_env(monkeypatch):
    monkeypatch.setenv("WAKU_DIGEST_CHANNEL", "C0DEV")
    monkeypatch.setenv("WAKU_APPROVERS", APPROVERS)


def _park_review(home, conn, clock):
    """Monday's PR desk run: it drafts a review of PR #428, which parks, and
    replies with the digest. Returns the run's id."""
    from waku.alwayson import events
    from waku.alwayson.worker import Worker, reply_address

    event_id = events.insert(conn, source="timer", external_id="timer:1:2026-11-30",
                             session_id="pr-desk:1",
                             payload={"text": NOTE, "set_at": "2026-11-29T17:00:00+00:00",
                                      "channel": "telegram", "reply_to": {"chat_id": 42}})

    def desk_run(session_id, batch):
        run = runs.find(conn, session_id, batch[0].id)
        with outbox.running(home, str(run.id), reply_address(batch)):
            said, _ = approvals.Gate(home, conn, now=clock)("github_review", REVIEW)
        assert "under your digest" in said, "the model is told to finish the digest"
        return DIGEST

    Worker(conn, desk_run, wait=lambda s: None, now=clock, log=lambda line: None).step()
    return runs.find(conn, "pr-desk:1", event_id).id


class FakeSlack:
    """Stands in for the Slack sender: records each message and answers with
    the next ts, the way chat.postMessage does."""

    def __init__(self) -> None:
        self.sent = []

    def __call__(self, message):
        self.sent.append(message)
        return f"1800.000{len(self.sent)}"


def test_a_drafted_review_waits_in_a_thread_under_its_digest(home, conn, monkeypatch):
    _desk_env(monkeypatch)
    clock = Clock()
    run_id = _park_review(home, conn, clock)
    assert runs.get(conn, run_id).status == "parked"
    assert json.loads(conn.execute("SELECT pending_call FROM runs").fetchone()[0])["asked"] == {
        "channel": "slack", "target": {"channel": "C0DEV"}}

    slack = FakeSlack()
    dispatcher = Dispatcher(conn, {"slack": slack}, now=clock, log=lambda line: None)
    dispatcher.step()
    dispatcher.step()

    digest, asked = slack.sent
    assert (digest.target, digest.body) == ({"channel": "C0DEV", "digest": True}, DIGEST)
    assert asked.target == {
        "channel": "C0DEV", "thread_ts": "1800.0001",
        "request": approvals.request("github_review", REVIEW, REVIEW_HASH).fields()}
    assert asked.body.endswith(f"Approve or Reject with the buttons below. [{REVIEW_HASH[:8]}]")
    assert REVIEW["body"] in asked.body
    assert [r["receipt"] for r in conn.execute("SELECT receipt FROM outbox ORDER BY id")] == [
        "1800.0002", "1800.0001"], "the request row, then the digest row"


def test_a_request_without_its_own_digest_goes_under_the_newest_one(home, conn, monkeypatch):
    """A review asked for in a mention, not by the morning run, waits under
    the newest digest in the channel, or in the channel when there is none."""
    _desk_env(monkeypatch)
    lone = outbox.insert(conn, run_id="9", channel="slack",
                         target={"channel": "C0DEV", "under_digest": True}, body="asked",
                         idempotency_key="9:ask")
    message = outbox.get(conn, lone)
    assert outbox.resolve_target(conn, message) == {"channel": "C0DEV"}

    old = outbox.insert(conn, run_id="3", channel="slack",
                        target={"channel": "C0DEV", "digest": True}, body="Friday",
                        idempotency_key="3:0")
    outbox.mark_sent(conn, old, "1799.0001")
    assert outbox.resolve_target(conn, message) == {"channel": "C0DEV", "thread_ts": "1799.0001"}
    assert outbox.digest_thread(conn, "C0DEV") == "1799.0001"
    assert outbox.digest_thread(conn, "C1") == ""


def test_the_slack_sender_draws_a_request_with_two_buttons():
    from waku.alwayson.senders import slack

    calls = []

    def post(url, payload, headers):
        calls.append(payload)
        return {"ok": True, "ts": "1800.0002"}

    long_review = {**REVIEW, "body": "x" * 3500}
    fields = approvals.request("github_review", long_review, REVIEW_HASH).fields()
    message = outbox.Message(1, "1", "slack", {"channel": "C0DEV", "thread_ts": "1800.0001",
                                               "request": fields},
                             "fallback text", "1:ask", "pending", 0,
                             "2026-11-30T17:00:00+00:00", "")

    assert slack.make("xoxb-test", post=post)(message) == "1800.0002"

    (payload,) = calls
    assert (payload["channel"], payload["thread_ts"], payload["text"]) == (
        "C0DEV", "1800.0001", "fallback text")
    blocks = payload["blocks"]
    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": (
        "*Review PR #428*\nA drafted review of acme/app#428. Approve posts it on the pull "
        "request as a comment, and Reject discards it.\n<https://github.com/acme/app/pull/428>")}}
    assert "".join(block["text"]["text"] for block in blocks[1:-1]) == "x" * 3500
    assert all(block["text"]["type"] == "plain_text" and len(block["text"]["text"]) <= 3000
               for block in blocks[1:-1])
    assert blocks[-1] == {"type": "actions", "block_id": f"waku_approval_{REVIEW_HASH[:8]}",
                          "elements": [
        {"type": "button", "action_id": "waku_approve", "style": "primary",
         "text": {"type": "plain_text", "text": "Approve"}, "value": REVIEW_HASH[:8]},
        {"type": "button", "action_id": "waku_reject", "style": "danger",
         "text": {"type": "plain_text", "text": "Reject"}, "value": REVIEW_HASH[:8]}]}


# ---- D2b: the click is the answer ---------------------------------------------

def _click(home, user="U0SEAN", action="waku_approve", code=None, action_ts="1800.0100"):
    """Sean clicks a button on the request, which sits in the digest's thread."""
    from waku.alwayson.events import Inbox
    from waku.alwayson.sources import slack

    return slack.record_click(Inbox(home), {
        "type": "block_actions", "team": {"id": "T1"},
        "user": {"id": user, "team_id": "T1"}, "channel": {"id": "C0DEV"},
        "message": {"ts": "1800.0002", "thread_ts": "1800.0001"},
        "actions": [{"action_id": action, "value": code or REVIEW_HASH[:8],
                     "action_ts": action_ts}]},
        team_id="T1", channels={"C0DEV"})


def _executor(home):
    """What AgentRun.execute does for an approved call, without a model: the
    stored call runs through the real tool under the approved key."""
    tool = github.make_review_tool(home)

    def execute(run, address):
        with outbox.running(home, str(run.id), address,
                            approved_key=f"{run.id}:{run.pending_hash}"):
            return tool.fn(**run.pending_call["args"])

    return execute


class NoModel:
    """A run that must never be called: a click starts no model run."""

    def __init__(self) -> None:
        self.runs = []

    def __call__(self, session_id, batch):
        self.runs.append(session_id)
        return "SILENT"


def _answer(home, conn, clock, monkeypatch, roles, **click):
    """Park the review, post the digest and the request, click, and let the
    worker and the dispatcher act. Returns (run id, gh, Slack, the model,
    the permission check)."""
    from waku.alwayson.worker import Worker

    _desk_env(monkeypatch)
    permissions = PermissionGh(roles)
    monkeypatch.setattr(github, "_run", permissions)
    run_id = _park_review(home, conn, clock)
    gh, slack = ReviewGh(), FakeSlack()
    dispatcher = Dispatcher(conn, {"github": github_sender.make(run=gh), "slack": slack},
                            now=clock, log=lambda line: None)
    dispatcher.step()
    dispatcher.step()
    assert _click(home, **click) is True
    model = NoModel()
    worker = Worker(conn, model, execute=_executor(home), wait=lambda s: None, now=clock,
                    log=lambda line: None)
    while worker.step():
        pass
    dispatcher.step()
    return run_id, gh, slack, model, permissions


def test_slack_button_answers_the_parked_call(home, conn, monkeypatch):
    """A mapped approver with write access clicks Approve: the stored body is
    posted unchanged, and the thread says who approved."""
    clock = Clock()
    run_id, gh, slack, model, permissions = _answer(home, conn, clock, monkeypatch,
                                                    {"ShenSeanChen": "admin"})

    ((argv, stdin),) = gh.posted
    assert argv == ["gh", "pr", "review", "428", "--repo", "acme/app", "--comment",
                    "--body-file", "-"]
    assert stdin == f"{REVIEW['body']}\n\n<!-- waku:{run_id}:{REVIEW_HASH} -->"
    assert runs.get(conn, run_id).status == "done"
    assert model.runs == [], "the click starts no model run"
    assert permissions.calls == [
        ["gh", "api", "repos/acme/app/collaborators/ShenSeanChen/permission"]]
    (said,) = [m for m in slack.sent if m.idempotency_key == f"answered:{run_id}"]
    assert said.target == {"channel": "C0DEV", "thread_ts": "1800.0001"}
    assert said.body.startswith(
        "ShenSeanChen chose Approve. Approved and done: Review on acme/app#428, as a comment:")


def test_slack_reject_discards_the_call(home, conn, monkeypatch):
    run_id, gh, slack, model, _ = _answer(home, conn, Clock(), monkeypatch,
                                          {"ShenSeanChen": "write"}, action="waku_reject")
    assert gh.posted == []
    assert runs.get(conn, run_id).status == "done"
    (said,) = [m for m in slack.sent if m.idempotency_key == f"answered:{run_id}"]
    assert said.body.startswith("ShenSeanChen chose Reject. Declined and discarded:")
    assert model.runs == []


@pytest.mark.parametrize("roles, user, asks_github, why", [
    ({"ShenSeanChen": "admin"}, "U0EVE", False, "nobody mapped U0EVE"),
    ({"ShenSeanChen": "read"}, "U0SEAN", True, "GitHub says read"),
    ({"ShenSeanChen": "triage"}, "U0SEAN", True, "GitHub says triage"),
])
def test_a_click_from_anyone_else_changes_nothing(home, conn, monkeypatch, roles, user,
                                                  asks_github, why):
    run_id, gh, _slack, model, permissions = _answer(home, conn, Clock(), monkeypatch, roles,
                                                    user=user)
    assert gh.posted == [], why
    assert runs.get(conn, run_id).status == "parked", why
    assert model.runs == [], "an ignored click starts no run either"
    assert {r["status"] for r in conn.execute("SELECT status FROM events")} == {"done"}
    assert bool(permissions.calls) is asks_github


def test_a_typed_yes_does_not_answer_a_review_asked_in_slack(home, conn, monkeypatch):
    """The owner's Telegram yes answers the owner's calls. A review asked in
    the channel is answered with a button there."""
    from waku.alwayson import events
    from waku.alwayson.worker import Worker

    _desk_env(monkeypatch)
    monkeypatch.setattr(github, "_run", PermissionGh({"ShenSeanChen": "admin"}))
    clock = Clock()
    run_id = _park_review(home, conn, clock)
    events.insert(conn, source="telegram", external_id="900", session_id="telegram:42",
                  payload={"text": f"yes {REVIEW_HASH[:8]}", "sender": {"id": "42"},
                           "reply_to": {"chat_id": 42}})
    worker = Worker(conn, NoModel(), execute=_executor(home), wait=lambda s: None, now=clock,
                    log=lambda line: None)
    while worker.step():
        pass
    assert runs.get(conn, run_id).status == "parked"


def test_a_redelivered_click_is_one_event(home):
    assert _click(home) is True
    assert _click(home) is False


@pytest.mark.parametrize("change, why", [
    ({"type": "view_submission"}, "not a button"),
    ({"team": {"id": "T2"}}, "another workspace"),
    ({"channel": {"id": "C9"}}, "a channel nobody allowed"),
    ({"actions": [{"action_id": "other", "value": "abcd1234"}]}, "another button"),
    ({"actions": [{"action_id": "waku_approve", "value": "--approve"}]}, "not a code"),
])
def test_a_click_that_is_not_an_answer_is_not_stored(home, change, why):
    from waku.alwayson.events import Inbox
    from waku.alwayson.sources import slack

    payload = {"type": "block_actions", "team": {"id": "T1"},
               "user": {"id": "U0SEAN", "team_id": "T1"}, "channel": {"id": "C0DEV"},
               "message": {"ts": "1800.0002", "thread_ts": "1800.0001"},
               "actions": [{"action_id": "waku_approve", "value": "abcd1234",
                            "action_ts": "1800.0100"}], **change}
    assert slack.record_click(Inbox(home), payload, team_id="T1", channels={"C0DEV"}) is False, why


def test_a_review_that_waits_is_reminded_and_expires_in_its_thread(home, conn, monkeypatch):
    from waku.alwayson.worker import Worker

    _desk_env(monkeypatch)
    clock = Clock()
    run_id = _park_review(home, conn, clock)
    worker = Worker(conn, NoModel(), wait=lambda s: None, now=clock, log=lambda line: None)
    clock.advance(hours=49)
    worker.step()
    clock.advance(hours=24)
    worker.step()

    said = {r["idempotency_key"]: (json.loads(r["target"]), r["body"])
            for r in conn.execute("SELECT * FROM outbox")}
    target, body = said[f"remind:{run_id}"]
    assert target == {"channel": "C0DEV", "under_digest": True}
    assert body.endswith(f"Approve or Reject with the buttons above. [{REVIEW_HASH[:8]}]")
    assert said[f"expired:{run_id}"][1].startswith(
        "I stopped waiting for your answer on: Review on acme/app#428")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE source = 'approval'").fetchone()[0] == 0
