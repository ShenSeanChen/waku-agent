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
