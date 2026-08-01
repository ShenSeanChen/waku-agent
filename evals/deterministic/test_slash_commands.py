"""DETERMINISTIC EVAL — calling a graph workflow by name.

Two kinds of graph ship here and conflating them made the UI state something
false. `triage` is a ROUTER — it runs itself on every message and you should
never think about it. `gather` is a PROCEDURE — a named shape you ask for. The
dashboard claimed "you never pick a mode" in two places, which was true right
up until the second kind existed.

A graph is a pre-determined workflow, so naming it is the natural interface.
These tests pin the discovery rule that makes that automatic, and the parser
that decides what counts as a command — because the failure mode of a loose
parser is that an ordinary message mentioning a path silently runs something.
"""

from __future__ import annotations

from waku.ops import commands


def test_gather_is_discovered():
    assert "gather" in commands.discover()


def test_triage_is_not_a_command():
    """The rule with teeth. triage is bound inside app.py as the per-message
    door and has no waku/ops/triage.py, so it cannot be called by name — which
    is correct: a router you invoke manually is just a slower loop."""
    assert "triage" not in commands.discover()


def test_discovery_requires_both_halves():
    """A workflow module is the PURE graph — injected callables, nothing bound
    to this machine, deliberately unrunnable on its own. The binder in
    waku/ops/ is what makes it runnable, so that is what earns the command."""
    import pkgutil

    import waku.graph.workflows as pkg

    modules = {m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_")}
    assert modules >= {"triage", "gather"}
    for name, target in commands.discover().items():
        assert name in modules, f"/{name} has no workflow module"
        assert target == f"waku.ops.{name}:run_{name}"


def test_the_runner_table_and_the_commands_cannot_drift():
    """/api/graph/stream and the slash commands are two doors to one set of
    workflows. Two hand-maintained lists of the same fact drift; one function
    cannot."""
    from waku.ops import dashboard

    assert dashboard.WORKFLOW_RUNNERS() == commands.discover()


# --- the parser --------------------------------------------------------------

def test_a_leading_slash_is_a_command():
    assert commands.parse("/gather") == ("gather", "")
    assert commands.parse("  /graphs  ") == ("graphs", "")
    assert commands.parse("/gather since friday") == ("gather", "since friday")


def test_ordinary_messages_are_not_commands():
    """The expensive failure is the other direction: a message that merely
    mentions a path must never fire a workflow."""
    for text in ("hello", "look in /tmp/x", "what is 3/4", "", "   ",
                 "/ gather", "the file is at /etc/hosts"):
        assert commands.parse(text) is None, text


def test_an_unknown_command_explains_itself():
    msg = commands.unknown_reply("nope")
    assert "/nope" in msg
    assert "/gather" in msg, "must list what DOES exist"
    assert "/graphs" in msg


def test_the_listing_names_every_runnable_workflow():
    text = commands.describe()
    for name in commands.discover():
        assert f"/{name}" in text
    assert "triage" in text, "should say why the router has no command"


def test_running_an_unknown_name_returns_none_rather_than_raising():
    assert commands.run("definitely-not-a-workflow", lambda *a: None) is None


# --- the UI must stop claiming you cannot choose ------------------------------

def test_the_dashboard_no_longer_says_you_never_pick_a_mode():
    """That sentence was true when triage was the only workflow. It became
    false the moment a workflow you must invoke yourself shipped alongside it,
    and a UI that contradicts the feature is worse than no copy at all."""
    import pathlib

    js = pathlib.Path(__file__).resolve().parents[2] / "waku/ops/static/js/views.js"
    assert "never pick a mode" not in js.read_text()
