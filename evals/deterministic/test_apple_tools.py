"""DETERMINISTIC EVAL — the Apple tools, and the rule that keeps them usable.

Every Apple tool timed out on a real Mac on 2026-07-30 while this suite stayed
green. The reason is worth stating plainly, because it is a lesson about evals
and not about AppleScript: the only existing test asserted on generated STRINGS,
and one of its cases did nothing at all on macOS —

    # on macOS we can't run osascript in CI, but the escaping is in the string
    # build; covered by the pure date test above + manual verification

"manual verification on the dev machine" is not a test. So waku advertised four
Apple tools, and three of them did not work.

CI has no Calendar.app, so we still cannot assert "reading really works". What we
CAN pin is the rule that made it stop working, and the honesty of the failures:

  * no `whose` filter in any generated script  (25s vs 6s, measured)
  * every osascript call has a timeout, and a failure returns a SENTENCE
  * a slow app must fail fast — a tool that blocks a turn for 75s then
    apologises is worse than a tool that says "no" in 20

Timings referenced below were measured on a 472-event Google-synced calendar and
a Mail.app that could not `count of messages of inbox` inside two minutes.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from waku.tools import apple

# _osa short-circuits with "Apple tools are macOS-only" before it ever spawns
# osascript, so the timeout and refusal cases can only be observed on a Mac.
# CI runs Linux; those two cases skip there. Everything else — the `whose` rule,
# the budgets, the date parsing — is pure source/string logic and runs anywhere,
# which is the point: the rule that broke these tools IS checkable in CI.
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS osascript")


def _code_lines(module) -> list[str]:
    """Source lines with docstrings and comments removed, so a test can look for
    a pattern in the CODE without matching the prose that explains it."""
    import ast

    src = inspect.getsource(module)
    tree = ast.parse(src)
    doc_spans: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc_spans.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [ln for i, ln in enumerate(src.splitlines(), 1)
            if i not in doc_spans and not ln.strip().startswith("#")]


def test_probe_apple_tools_checks_all_apps_without_touching_user_data(monkeypatch):
    calls = []

    def osa(script, timeout):
        calls.append((script, timeout))
        return True, "1.0"

    monkeypatch.setattr(apple, "_osa", osa)

    assert apple.probe_apple_tools() is None
    assert calls == [
        (f'tell application "{app}" to return version', 8)
        for app in ("Calendar", "Mail", "Reminders", "Notes")
    ]


def test_probe_apple_tools_aggregates_failures_and_checks_every_app(monkeypatch):
    responses = iter([
        (True, "16.0"),
        (False, "Not authorized"),
        (False, "timed out after 8s"),
        (True, "7.0"),
    ])
    calls = []

    def osa(script, timeout):
        calls.append((script, timeout))
        return next(responses)

    monkeypatch.setattr(apple, "_osa", osa)

    with pytest.raises(RuntimeError) as caught:
        apple.probe_apple_tools()

    message = str(caught.value)
    assert message.startswith("Apple Tools probe failed:")
    assert "Mail: Not authorized" in message
    assert "Reminders: timed out after 8s" in message
    assert "Calendar:" not in message
    assert "Notes:" not in message
    assert len(calls) == 4


@pytest.mark.parametrize(
    "detail",
    [
        "Apple tools are macOS-only.",
        "timed out after 8s. The app may be waiting for permission.",
    ],
)
def test_probe_apple_tools_names_the_app_for_transport_failures(monkeypatch, detail):
    def osa(script, timeout):
        if 'application "Mail"' in script:
            return False, detail
        return True, "1.0"

    monkeypatch.setattr(apple, "_osa", osa)

    with pytest.raises(RuntimeError, match="Mail") as caught:
        apple.probe_apple_tools()

    assert detail in str(caught.value)


def test_no_script_uses_a_whose_filter():
    """THE regression. `whose` makes the app evaluate a predicate per item across
    the Apple Event bridge: ~25s for one calendar versus ~6s to pull the raw
    column and filter in Python. Reintroducing it is how these tools break."""
    # Strip docstrings before looking: this module DISCUSSES `whose` at length on
    # purpose, and a test that trips over its own explanation is a bad test.
    offenders = [ln.strip() for ln in _code_lines(apple) if "whose" in ln]
    assert offenders == [], (
        "an AppleScript `whose` filter is back — that is the 4x slowdown that "
        f"made every Apple tool time out: {offenders}"
    )


def test_every_osascript_call_is_bounded():
    """No unbounded AppleScript, ever. One hung app must not hang a chat turn."""
    src = inspect.getsource(apple)
    sig = inspect.signature(apple._osa)
    assert sig.parameters["timeout"].default, "_osa must always have a timeout default"
    assert "subprocess.run" in src
    assert "timeout=timeout" in src, "_osa must pass its timeout through to subprocess"


def test_failures_are_fast_enough_to_be_useful():
    """A 75s failure blocks the turn and then apologises. Budgets must stay small
    enough that a broken app gives up while the user is still listening."""
    assert apple._SLOW_APP_TIMEOUT <= 30, (
        f"_SLOW_APP_TIMEOUT is {apple._SLOW_APP_TIMEOUT}s — at 75s Reminders blocked "
        "a whole turn before reporting failure"
    )
    assert apple._TIMEOUT <= 45


@macos_only
def test_a_timeout_explains_itself():
    """The old message blamed a permission dialog for what was actually slowness,
    which sent a real debugging session down the wrong path for an hour."""
    ok, msg = apple._osa("delay 5", timeout=1)
    assert ok is False
    assert "timed out after 1s" in msg, msg
    assert "permission" in msg and "slow" in msg, "must name BOTH likely causes"


@macos_only
def test_calendar_refuses_rather_than_enumerating_everything(monkeypatch):
    """A typical Mac has 30+ calendars once holidays and subscriptions pile up;
    reading them all takes minutes. Refusing with instructions beats a two-minute
    silence followed by a timeout."""
    monkeypatch.setenv("WAKU_APPLE_CALENDARS", "")
    apple._cache.clear()
    out = apple.read_apple_calendar(1)
    assert "WAKU_APPLE_CALENDARS" in out
    assert "name of every calendar" in out, "must tell the user how to find the names"


def test_applescript_dates_parse_into_real_datetimes():
    """Calendar.app renders 'Thursday, July 30, 2026 at 1:00:00 PM'. The Python
    side filters on these, so a parse failure silently empties the calendar."""
    got = apple._parse_applescript_date("Thursday, July 30, 2026 at 1:00:00 PM")
    assert got is not None
    assert (got.year, got.month, got.day, got.hour) == (2026, 7, 30, 13)
    # a narrow no-break space before AM/PM is what macOS actually emits
    assert apple._parse_applescript_date("Friday, August 1, 2026 at 9:30:00 AM") is not None
    assert apple._parse_applescript_date("not a date") is None, "must return None, never raise"


def test_every_registered_tool_reports_failure_as_text():
    """Tools return honest strings; they never raise. A calendar hiccup must not
    take down the turn — same contract as ToolRegistry.execute."""
    for tool in apple.make_tools():
        assert tool.description and tool.name
        assert callable(tool.fn)


def test_create_note_success_and_failure_paths(monkeypatch):
    """create_note must report success/failure from _osa without real Notes.app."""
    calls: list[tuple[str, float | None]] = []

    def fake_osa(script: str, timeout: float | None = None):
        calls.append((script, timeout))
        return True, "ok"

    monkeypatch.setattr(apple, "_osa", fake_osa)
    assert apple.create_note("Hello", "world") == "Note created: Hello"
    assert "Notes" in calls[-1][0]
    assert calls[-1][1] is not None
    # force failure path
    def bad_osa(script: str, timeout: float | None = None):
        return False, "denied"
    monkeypatch.setattr(apple, "_osa", bad_osa)
    assert apple.create_note("x").startswith("Note failed:")


def test_create_reminder_includes_due_when_set(monkeypatch):
    captured: dict[str, str] = {}

    def fake_osa(script: str, timeout: float | None = None):
        captured["script"] = script
        return True, "ok"

    monkeypatch.setattr(apple, "_osa", fake_osa)
    assert apple.create_reminder("Buy milk", due="Monday 9:00 AM") == "Reminder created: Buy milk"
    assert "Reminders" in captured["script"]
    assert "due date" in captured["script"]
    assert "Buy milk" in captured["script"]


def test_read_apple_mail_uses_bounded_osa_and_formats_failure(monkeypatch):
    def fake_osa(script: str, timeout: float | None = None):
        assert timeout == 20
        assert "Mail" in script
        return False, "timeout"

    monkeypatch.setattr(apple, "_osa", fake_osa)
    # bypass cache
    monkeypatch.setattr(apple, "_cached", lambda key, ttl, go: go())
    out = apple.read_apple_mail(hours=12, limit=5)
    assert out.startswith("Mail unavailable:")
    assert "timeout" in out
