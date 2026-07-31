"""DETERMINISTIC EVAL — the gather workflow: really parallel, and unable to act.

Two claims this workflow makes, both of which would be embarrassing to make
falsely on camera:

  1. THE FOUR SCANS RUN AT THE SAME TIME. Not "are drawn side by side" — the
     engine puts them in one wave and a ThreadPoolExecutor runs them together.
     test_the_four_scans_really_overlap measures it rather than trusting the
     picture.

  2. IT PROPOSES, IT NEVER ACTS. No agent_node, no run_loop, no ToolRegistry,
     and the single model call passes no tools. A model that is never handed a
     tool schema cannot call a tool. test_the_workflow_cannot_act reads the
     source, because that is the only way to catch someone adding a tool-using
     node later "just to make the digest smarter".

Everything here runs offline: injected callables, no model, no network, no gh.
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from waku.graph import END, run_graph
from waku.graph.workflows.gather import (
    SCAN_KEYS,
    build_gather_graph,
    gather_topology,
    needs_action,
)


def _graph(**overrides):
    """A gather with harmless stubs; override one callable per test."""
    base = {
        "github_fn": lambda: {"gh_text": "prs", "gh_open_prs": 2, "gh_open_issues": 1},
        "web_fn": lambda: "web",
        "calendar_fn": lambda: {"cal_text": "cal", "cal_event_count": 1},
        "memory_fn": lambda s=None: "mem",
        "synth_fn": lambda s: "DIGEST",
        "draft_fn": lambda s: "/tmp/gather.md",
    }
    base.update(overrides)
    return build_gather_graph(**base)


# --- claim 1: it is really parallel -----------------------------------------

def test_the_four_scans_really_overlap():
    """The claim the whole video rests on. Each scan records when it started and
    sleeps; if the engine ran them one after another the last would start ~3
    sleeps after the first. Asserting on OVERLAP rather than on total duration
    keeps this stable on a loaded CI box."""
    SLEEP = 0.15
    started: dict[str, float] = {}
    lock = threading.Lock()

    def slow(name: str, out: dict):
        def fn():
            with lock:
                started[name] = time.perf_counter()
            time.sleep(SLEEP)
            return out
        return fn

    # All four use the same helper so every one records its start BEFORE
    # sleeping — timing the sleep itself would just measure the sleep.
    g = _graph(
        github_fn=slow("gh", {"gh_text": "", "gh_open_prs": 0, "gh_open_issues": 0}),
        web_fn=lambda: slow("web", {"web_text": ""})()["web_text"],
        calendar_fn=slow("cal", {"cal_text": "", "cal_event_count": 0}),
        memory_fn=lambda: slow("mem", {"mem_text": ""})()["mem_text"],
    )
    t0 = time.perf_counter()
    run_graph(g, {})
    elapsed = time.perf_counter() - t0

    spread = max(started.values()) - min(started.values())
    assert spread < SLEEP, (
        f"the scans started {spread:.3f}s apart — more than one sleep, so they "
        "ran in sequence, not together"
    )
    assert elapsed < SLEEP * 3, (
        f"the whole gather took {elapsed:.3f}s; three sequential sleeps would be "
        f"{SLEEP * 3:.3f}s, so the wave is not running in parallel"
    )


def test_synthesize_waits_for_every_scan():
    """Fan-in: the digest must not be written from three of four sources. The
    engine enforces this through static deps; this pins it against someone
    'simplifying' an edge away."""
    order: list[str] = []
    lock = threading.Lock()

    def note(name, out):
        def fn():
            with lock:
                order.append(name)
            return out
        return fn

    g = _graph(
        github_fn=note("scan", {"gh_text": "", "gh_open_prs": 0, "gh_open_issues": 0}),
        web_fn=lambda: (order.append("scan"), "")[-1],
        calendar_fn=note("scan", {"cal_text": "", "cal_event_count": 0}),
        memory_fn=lambda: (order.append("scan"), "")[-1],
        synth_fn=lambda s: (order.append("synth"), "D")[-1],
    )
    run_graph(g, {})
    assert order.count("scan") == 4
    assert order.index("synth") == 4, f"synthesize ran before all four scans: {order}"


def test_scan_nodes_write_disjoint_keys():
    """Parallel nodes writing the same key raise GraphStateCollision. The prefix
    convention is what stops that happening; check it holds."""
    seen: set[str] = set()
    for node, keys in SCAN_KEYS.items():
        clash = seen & set(keys)
        assert not clash, f"{node} reuses {clash} — parallel branches must be disjoint"
        seen |= set(keys)


# --- claim 2: one dead branch must not kill the briefing ---------------------

def test_one_dead_branch_still_yields_a_digest():
    """A node that raises fires no edges, so synthesize's deps would never
    complete and the run would produce NOTHING — no error, no digest, an empty
    outbox. Silence is the worst failure for a morning routine. _safe exists to
    prevent exactly this; removing it must fail here."""
    def boom():
        raise RuntimeError("github is down")

    state = run_graph(_graph(github_fn=boom), {})
    assert state.get("digest") == "DIGEST", "a dead scan silently killed the whole gather"
    assert "unavailable" in state["gh_text"] and "github is down" in state["gh_text"]
    assert state["gh_open_prs"] == 0


def test_every_branch_can_die_and_the_briefing_survives():
    for killed in ("github_fn", "web_fn", "calendar_fn", "memory_fn"):
        def boom():
            raise RuntimeError("nope")

        state = run_graph(_graph(**{killed: boom}), {})
        assert state.get("digest") == "DIGEST", f"{killed} failing killed the gather"


# --- the router --------------------------------------------------------------

@pytest.mark.parametrize("state,expected", [
    ({"gh_open_prs": 2}, "propose"),
    ({"gh_open_issues": 1}, "propose"),
    ({"cal_event_count": 3}, "propose"),
    ({"gh_open_prs": 0, "gh_open_issues": 0, "cal_event_count": 0}, "quiet"),
    ({}, "quiet"),
])
def test_the_router_is_code_over_counts(state, expected):
    """Routing on counts, never on the digest's wording — the engine's rule is
    'routers are code, never models'. It also means this table exists at all."""
    assert needs_action(state) == expected


def test_a_quiet_morning_writes_nothing():
    """Nothing open, nothing on: no file. A briefing that writes an empty digest
    every morning trains you to ignore the folder."""
    wrote: list[str] = []
    g = _graph(
        github_fn=lambda: {"gh_text": "", "gh_open_prs": 0, "gh_open_issues": 0},
        calendar_fn=lambda: {"cal_text": "", "cal_event_count": 0},
        draft_fn=lambda s: (wrote.append("x"), "/tmp/x")[-1],
    )
    state = run_graph(g, {})
    assert wrote == [], "drafted a digest on a morning with nothing to report"
    assert "draft_path" not in state


def test_a_busy_morning_drafts():
    state = run_graph(_graph(), {})
    assert state["draft_path"] == "/tmp/gather.md"


# --- the propose-never-act guarantee ----------------------------------------

def test_the_workflow_cannot_act():
    """THE structural guarantee, checked at the source level because there is no
    runtime signal for 'someone added a tool-using node'.

    The day an agent_node appears here to make the digest smarter, this graph
    gains the ability to send messages and create events, and nothing else in
    the system would notice. The model call in the binder passes NO tools
    parameter — a model handed no tool schemas cannot call a tool, which is a
    guarantee about capability rather than about instructions.
    """
    from waku.graph.workflows import gather as workflow
    from waku.ops import gather as binder

    for module in (workflow, binder):
        code = _code_lines(module)
        for banned in ("agent_node", "run_loop", "ToolRegistry", "send_message",
                       "create_event", "subprocess"):
            offenders = [ln.strip() for ln in code if banned in ln]
            assert offenders == [], (
                f"{module.__name__} now references {banned!r}: {offenders}. The gather "
                "PROPOSES and never ACTS — it drafts into the outbox for a human. "
                "Adding a tool-using node here silently removes that guarantee."
            )
    # Assert on the CALL, via the AST — not on a substring, because the function
    # docstring explains the rule and would match itself. This also states the
    # property exactly: whatever else the call grows, it must never gain a
    # `tools=` keyword.
    import ast

    calls = [n for n in ast.walk(ast.parse(inspect.getsource(binder._synthesize)))
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "create"]
    assert calls, "expected a messages.create call in _synthesize"
    for call in calls:
        passed = {kw.arg for kw in call.keywords}
        assert "tools" not in passed, (
            "the synthesis call now passes tools — the model could act from "
            "inside a workflow whose entire contract is that it cannot"
        )


def _code_lines(module) -> list[str]:
    """Source minus docstrings and comments — these modules DISCUSS the banned
    names at length in prose explaining why they are banned, and a test that
    trips over its own explanation is a bad test (the lesson from
    test_apple_tools.py's `whose` rule)."""
    import ast

    src = inspect.getsource(module)
    doc: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [ln for i, ln in enumerate(src.splitlines(), 1)
            if i not in doc and not ln.strip().startswith("#")]


# --- the topology the dashboard draws ---------------------------------------

def test_the_topology_matches_the_graph_that_runs():
    """The chart is rendered from describe(), so it cannot drift — as long as
    the stub build and the real build stay the same shape."""
    assert gather_topology() == _graph().describe()


def test_the_chart_shows_a_four_way_fan_out():
    topo = gather_topology()
    from_start = {e["dst"] for e in topo["edges"] if e["src"] == "START"}
    assert from_start == set(SCAN_KEYS), from_start
    into_synth = {e["src"] for e in topo["edges"] if e["dst"] == "synthesize"}
    assert into_synth == set(SCAN_KEYS), "every scan must feed the fan-in"
    conditional = {(e["src"], e["dst"]) for e in topo["edges"] if e["conditional"]}
    assert conditional == {("synthesize", "draft_digest"), ("synthesize", END)}
    assert {n["name"] for n in topo["nodes"]} == set(SCAN_KEYS) | {"synthesize", "draft_digest"}
