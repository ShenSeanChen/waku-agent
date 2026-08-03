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


def test_triage_is_a_command_and_takes_a_message():
    """The router got a front door too. It still runs itself on every message
    when the flag is on — calling it by name just lets you watch ONE message
    choose a branch, with the flag off and nothing else changed.

    It is the one runner that needs an argument: a router with nothing to route
    has nothing to decide."""
    import importlib
    import inspect

    assert "triage" in commands.discover()
    fn = importlib.import_module("waku.ops.triage").run_triage
    assert "message" in inspect.signature(fn).parameters


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
    assert "triage" in text, "should explain how the router differs"


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


def test_a_runner_without_a_message_parameter_is_not_given_one():
    """gather takes no input — always the same four sources, which is what
    makes it a fixed shape. Passing it an argument would be a TypeError, so the
    dispatcher inspects rather than assumes."""
    import importlib
    import inspect

    fn = importlib.import_module("waku.ops.gather").run_gather
    assert "message" not in inspect.signature(fn).parameters


def test_triage_without_a_message_explains_itself_instead_of_running():
    """A bare /triage has nothing to route. Say so, and show the two commands
    worth comparing, rather than routing an empty string."""
    state = commands.run("triage", lambda *a: None, "")
    assert state is not None
    assert "/triage thanks!" in state["digest"]


def test_the_listing_no_longer_says_triage_has_no_command():
    assert "no command" not in commands.describe()
