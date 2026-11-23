"""DETERMINISTIC EVAL: the model is never handed words about how Waku runs
(spec 024, decision 3).

A person read "Just make sure `waku serve` is running so it can wake me up"
and "It'll post as soon as `waku serve` processes the outbox". The model took
those words from what it was given: a tool's result, a tool's description,
and the prompt of a run. This eval reads every such string and fails on the
words a person should not meet.

What is read:

  every string in waku/tools/ that is not a docstring: a tool's description,
    its schema and each sentence it can return
  every named text in waku/alwayson/approvals.py and worker.py: what the model
    reads for a call that waits, for the owner's answer and for a timer
  DEFAULT_SOUL
  what the tools answer when they are called
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import waku
from waku.alwayson import approvals, outbox, runs, worker
from waku.db import connect
from waku.runtime.session import DEFAULT_SOUL
from waku.tools import messages, schedule
from waku.tools import waiting as waiting_tool

WORDS = re.compile(r"waku serve|outbox|inbox|dispatcher|idempoten", re.IGNORECASE)
ROOT = Path(waku.__file__).parent
# Apple Mail calls its own folder the inbox, and a person who uses Mail does
# too. That word is about Mail, not about Waku.
MAIL = "apple.py"
NOW = datetime(2026, 11, 17, 22, 0, tzinfo=UTC)


def _strings(path: Path) -> list[tuple[int, str]]:
    """(line, text) of every string in a file that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def _found(text: str) -> list[str]:
    return [word.lower() for word in WORDS.findall(text)]


def test_no_string_in_a_tool_names_how_waku_runs():
    """Acceptance 3, the tools."""
    files = sorted((ROOT / "tools").rglob("*.py"))
    assert len(files) > 10
    bad = [f"{path.name}:{line}: {text[:80]!r}" for path in files
           for line, text in _strings(path)
           if _found(text) and not (path.name == MAIL and _found(text) == ["inbox"])
           # a folder on disk that the person can open, named in a path
           and text != "outbox"]
    assert bad == []


@pytest.mark.parametrize("module", [approvals, worker])
def test_no_text_the_model_reads_about_a_run_names_how_waku_runs(module):
    """Acceptance 3, the prompts and the outcomes of a call that waited."""
    texts = {name: value for name, value in vars(module).items()
             if name.isupper() and isinstance(value, str)}
    assert len(texts) >= 3
    assert {name: _found(text) for name, text in texts.items() if _found(text)} == {}


def test_the_soul_does_not_name_how_waku_runs_and_tells_the_model_not_to():
    assert _found(DEFAULT_SOUL) == []
    assert "how you run" in DEFAULT_SOUL


def test_what_schedule_task_answers_is_the_time_and_nothing_about_the_process(
        tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:5441400551")
    tool = schedule.make_tool(tmp_path, now=lambda: NOW, tz=UTC)

    with outbox.running(tmp_path, "7", ("telegram", {"chat_id": 5441400551})):
        said = tool.fn(when="2026-11-17T22:09", note="Remind Sean to stretch.")
        later = tool.fn(when="2026-11-24T09:00", note="Check issue 12.")
    alone = tool.fn(when="2026-11-18T09:00", note="Check issue 12.")

    assert said == "Timer 1 is set for Tuesday 22:09."
    assert later == "Timer 2 is set for Tuesday 24 November 09:00."
    assert alone == ("Timer 3 is set for Wednesday 09:00. It waits until Waku's always-on "
                     "mode is running, and fires then."), "the terminal fires no timer"
    assert _found(tool.description) == []


def test_what_send_message_answers_names_no_process(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    tool = messages.make_tool(tmp_path)

    answers = [tool.fn(to="owner", body="hello"),
               tool.fn(to="github:acme/app#12", body="hello"),
               tool.fn(to="Alex", body="hello"),
               tool.description]
    conn = connect(tmp_path)
    try:
        run = runs.start(conn, "telegram:42", [1], now=NOW)
    finally:
        conn.close()
    with outbox.running(tmp_path, str(run.id), ("telegram", {"chat_id": 42})):
        answers.append(tool.fn(to="owner", body="hello again"))

    assert [_found(answer) for answer in answers] == [[], [], [], [], []]
    assert "not sent yet" in answers[0], "in the terminal nothing sends the message"
    assert answers[4] == "The message to this chat is on its way."


def test_an_approved_message_is_reported_as_posted(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    call = {"tool": "send_message",
            "args": {"to": "github:acme/app#12", "body": "hello from Waku"}}
    run = runs.Run(7, "telegram:42", "", "", "parked", [1], call,
                   approvals.call_hash(call["tool"], call["args"]), "")

    said = approvals.outcome(run, True, "The message to github:acme/app#12 is on its way.")

    assert said == 'Approved and posted: Comment on acme/app#12: "hello from Waku"'
    refused = approvals.outcome(run, True, "Not sent: the message is empty.")
    assert refused.startswith("Approved, and not posted:") and "the message is empty" in refused


def test_the_words_a_person_reads_from_a_timer_are_about_the_time(tmp_path):
    """The run a timer starts is told about the note, not about a loop."""
    prompt = worker.TIMER_PROMPT.format(set_at=str(NOW - timedelta(hours=1)), note="Stretch.")
    assert _found(prompt) == [] and "Stretch." in prompt


# ---- tool names, and "the owner" said to the owner ---------------------------
#
# The second live run (2026-11-17): a person read "You can connect these via
# `connect_tool`", and the owner read "the owner approves it first" in their
# own chat.

# The names of the tools Waku ships. A person has no use for one.
TOOL_NAMES = re.compile(r"\b(" + "|".join(sorted(approvals.MODES)) + r")\b")
OWNER = ("telegram", {"chat_id": 42})


def _owner_run(conn, text="Comment hello on acme/app#12", external_id="900"):
    """A run in the owner's own chat."""
    from waku.alwayson import events

    event_id = events.insert(conn, source="telegram", external_id=external_id,
                             session_id="telegram:42",
                             payload={"text": text, "sender": {"id": "42"},
                                      "reply_to": OWNER[1]})
    return runs.start(conn, "telegram:42", [event_id], now=NOW), events.get(conn, event_id)


def test_the_shipped_tool_names_are_the_ones_the_rule_looks_for():
    assert {"send_message", "connect_tool", "schedule_task", "list_waiting"} <= set(
        approvals.MODES)
    assert TOOL_NAMES.findall("use `connect_tool`, then send_message") == [
        "connect_tool", "send_message"]


@pytest.mark.parametrize("module", [approvals, worker])
def test_no_text_the_model_reads_about_a_run_names_a_tool(module):
    texts = {name: value for name, value in vars(module).items()
             if name.isupper() and isinstance(value, str)}
    assert {name: TOOL_NAMES.findall(text) for name, text in texts.items()
            if TOOL_NAMES.findall(text)} == {}


def test_every_turn_is_told_not_to_name_a_tool_to_the_person(tmp_path):
    """A harness rule, so an assistant with an older SOUL.md has it too."""
    from evals.helpers import ScriptedClient, make_waku

    (tmp_path / "SOUL.md").write_text("You are Waku. An older soul.", encoding="utf-8")
    agent = make_waku(tmp_path, client=ScriptedClient([]), episodic_store="sqlite")
    try:
        agent.memory = None   # no retrieval: the rule is read from the prompt alone
        agent.session.memory = None
        system = agent.session.build_system("What connectors can I add?")
    finally:
        agent.close()
        agent.conn.close()

    assert "Never say the name of a tool to the user" in system
    assert "I can connect Notion" in system


def test_what_a_run_in_the_owners_chat_reads_never_says_the_owner(tmp_path, monkeypatch):
    """Every text a run in the owner's own chat can read about a call that
    waits: the prompt, the gate's two answers, each outcome, and the tools."""
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_OWNER_NAME", raising=False)
    args = {"to": "github:acme/app#12", "body": "hello"}
    conn = connect(tmp_path)
    try:
        run, event = _owner_run(conn)
        gate = approvals.Gate(tmp_path, conn, now=lambda: NOW)
        with outbox.running(tmp_path, str(run.id), OWNER):
            waits, _ = gate("send_message", args)
            second, _ = gate("send_message", {**args, "body": "another"})
            to_self = messages.make_tool(tmp_path).fn(to="owner", body="note to self")
            listed = waiting_tool.make_tools(tmp_path)[0].fn()
        parked = runs.get(conn, run.id)
    finally:
        conn.close()
    outcomes = [approvals.outcome(parked, True, "The message is on its way."),
                approvals.outcome(parked, True, "Not sent: the message is empty."),
                approvals.outcome(parked, False),
                approvals.said(parked, approvals.EXPIRED)]
    read = {"prompt": worker.render([event]),
            "waits": waits, "a second call": second, "send to self": to_self,
            "what waits": listed,
            **{f"outcome {n}": worker.APPROVAL_PROMPT.format(outcome=text)
               for n, text in enumerate(outcomes)}}

    assert "Comment on acme/app#12" in listed, "the call was listed"
    assert "already waits" in second
    assert {name: text for name, text in read.items() if "owner" in text.lower()} == {}
