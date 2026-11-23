"""AppleScript cannot cold-start an app, so waku has to (issue: both calendars
failed on 2026-08-29).

`launch application "Calendar"` was in the codebase specifically to stop -600
"Application isn't running". It was the line RAISING it: with Calendar closed,

    osascript -e 'launch application "Calendar"'
    -> execution error: Calendar got an error: Application isn't running. (-600)

The character offset in the real failure ("15:44") pointed straight at that
line. Mail, Reminders and Notes never attempted a launch at all, so each one
failed the same way whenever its app happened to be closed.

Only a shell-level `open` starts the app. These tests are offline — no
osascript runs, no app is launched — they check that waku still asks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from waku.tools import apple

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "waku" / "tools"


@pytest.fixture
def spy(monkeypatch):
    """Record what waku shells out, and pretend the app starts on demand."""
    calls: list[list[str]] = []
    started = {"yes": False}

    class Result:
        def __init__(self, rc): self.returncode = rc

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["pgrep", "-x"]:
            return Result(0 if started["yes"] else 1)
        if argv[0] == "open":
            started["yes"] = True          # the shell launch is what works
            return Result(0)
        return Result(0)

    monkeypatch.setattr(apple.subprocess, "run", fake_run)
    monkeypatch.setattr(apple.sys, "platform", "darwin")
    return calls


def test_a_closed_app_is_started_before_we_talk_to_it(spy):
    apple.ensure_running("Calendar")
    assert ["open", "-gj", "-a", "Calendar"] in spy, (
        "a closed app must be started by the shell — AppleScript answers -600"
    )


def test_it_starts_hidden_and_without_stealing_focus(spy):
    """-g keeps it behind the user's windows, -j starts it hidden. Reading a
    calendar mid-sentence must not pull the user out of what they were doing."""
    apple.ensure_running("Calendar")
    launch = next(c for c in spy if c[0] == "open")
    assert "-g" in launch[1] and "j" in launch[1], launch


def test_an_already_running_app_is_left_alone(monkeypatch):
    """No relaunch, and no 12-second wait, on the common path."""
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(argv, **kw):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(apple.subprocess, "run", fake_run)
    monkeypatch.setattr(apple.sys, "platform", "darwin")
    apple.ensure_running("Calendar")
    assert not [c for c in calls if c[0] == "open"], "should not relaunch a live app"


def test_no_module_still_asks_applescript_to_launch():
    """The idiom that does not work must not come back."""
    for path in TOOLS.glob("*.py"):
        # Only real string literals count — the explanation of why this idiom
        # fails necessarily contains the idiom.
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value.strip().startswith(("AppleScript cannot", "Start `app`")):
                continue  # the docstrings that explain this very rule
            for bad in ('launch application "', ' to launch'):
                if bad in node.value:
                    pytest.fail(f"{path.name}:{node.lineno} AppleScript cannot launch an app")


def test_every_osa_call_names_the_app_it_needs():
    """_osa launches whatever `app=` names, so a call that omits it is a call
    that will fail the moment that app is closed — exactly the Mail, Reminders
    and Notes bug."""
    tree = ast.parse((TOOLS / "apple.py").read_text())
    missing = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_osa"
        and "app" not in {kw.arg for kw in node.keywords}
    ]
    assert not missing, f"_osa call(s) at line(s) {missing} do not name their app"
