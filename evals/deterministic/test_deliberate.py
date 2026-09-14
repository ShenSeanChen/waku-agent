"""DETERMINISTIC EVAL — the deliberation graph: two brains in parallel, a decider.

Two claims, both of which would be embarrassing to make falsely:

  1. THE TWO BRAINS RUN AT THE SAME TIME. The engine puts them in one wave
     (they share no dependencies) and a ThreadPoolExecutor runs them together.
     test_the_two_brains_really_overlap measures it rather than trusting the
     picture.

  2. A DEAD BRAIN MUST NOT KILL THE DELIBERATION. The binder wraps each brain
     in `_safe` so it returns honest text instead of raising — a node that
     raises fires no edges, and `decide`'s dependencies would never complete.
     test_safe_returns_honest_text pins the wrapper itself.

Everything here runs offline: injected callables, no model, no network.
"""

from __future__ import annotations

import threading
import time

import pytest

from waku.graph import run_graph
from waku.graph.workflows.deliberate import (
    build_deliberation_graph,
    deliberation_topology,
)


def _graph(**overrides):
    base = {
        "brain_a_fn": lambda s: "A",
        "brain_b_fn": lambda s: "B",
        "decide_fn": lambda s: f"{s.get('position_a')}+{s.get('position_b')}",
    }
    base.update(overrides)
    return build_deliberation_graph(**base)


def test_the_two_brains_really_overlap():
    """The claim the video rests on: fan-out, not sequence. Same overlap-style
    assertion as gather — start-times within one sleep, total under two."""
    SLEEP = 0.15
    started: dict[str, float] = {}
    lock = threading.Lock()

    def slow(name: str, out: str):
        def fn(state):
            with lock:
                started[name] = time.perf_counter()
            time.sleep(SLEEP)
            return out
        return fn

    g = _graph(brain_a_fn=slow("a", "A"), brain_b_fn=slow("b", "B"))
    t0 = time.perf_counter()
    run_graph(g, {})
    elapsed = time.perf_counter() - t0

    spread = max(started.values()) - min(started.values())
    assert spread < SLEEP, f"brains started {spread:.3f}s apart — sequential, not parallel"
    assert elapsed < SLEEP * 2, f"took {elapsed:.3f}s; two sequential sleeps would be {SLEEP * 2:.3f}s"


def test_decider_waits_for_both_brains():
    order: list[str] = []
    lock = threading.Lock()

    def note(name, out):
        def fn(state):
            with lock:
                order.append(name)
            return out
        return fn

    g = _graph(brain_a_fn=note("a", "A"), brain_b_fn=note("b", "B"),
               decide_fn=note("d", "D"))
    run_graph(g, {})
    assert order[:2] == ["a", "b"], order  # engine orders a wave deterministically
    assert order[-1] == "d"


def test_decider_sees_both_positions():
    state = run_graph(_graph(), {})
    assert state["position_a"] == "A" and state["position_b"] == "B"
    assert state["decision"] == "A+B"


def test_brains_write_disjoint_keys():
    """position_a vs position_b — a collision would raise GraphStateCollision."""
    state = run_graph(_graph(), {})
    assert "position_a" in state and "position_b" in state and "decision" in state


def test_a_failed_brain_yields_no_decision_without_the_wrapper():
    """The failure mode the binder's `_safe` exists to prevent: a raising brain
    fires no edges, so decide never runs and the state has no decision. Pin it,
    so removing `_safe` cannot silently pass."""
    def boom(state):
        raise RuntimeError("lens down")

    state = run_graph(_graph(brain_a_fn=boom), {})
    assert "decision" not in state
    assert "brain_a" in state["errors"]


def test_safe_returns_honest_text_on_failure():
    from waku.ops.deliberate import _safe

    def boom(state):
        raise RuntimeError("down")

    out = _safe(boom, "brain A (architect)")({})
    assert "unavailable" in out and "down" in out


def test_review_prompt_labels_each_lens():
    from waku.ops.deliberate import _prompt

    a = _prompt("brain_a", {"task": "T", "work": "W"})
    b = _prompt("brain_b", {"task": "T", "work": "W"})
    assert "SOFTWARE ARCHITECT" in a and "T" in a and "W" in a
    assert "PRODUCT AND SYSTEMS REVIEWER" in b


def test_design_prompt_passes_task_only():
    from waku.ops.deliberate import _prompt

    assert "SOFTWARE ARCHITECT" in _prompt("brain_a", {"task": "T", "work": ""})
    assert "PRODUCT AND SYSTEMS REVIEWER" in _prompt("brain_b", {"task": "T", "work": ""})


def test_the_topology_matches_the_graph_that_runs():
    assert deliberation_topology() == _graph().describe()


def test_cli_main_requires_a_task_and_runs_nothing_without_one():
    """Regression: an empty argv used to fall through and run a REAL deliberation
    (the dispatch read sys.argv, which still held 'deliberate'). No args must
    print usage, exit 1, and never build a Waku / launch a sub-agent."""
    from waku.ops import deliberate

    with pytest.raises(SystemExit) as exc:
        deliberate.main([])
    assert exc.value.code == 1


def test_the_topology_fans_out_and_fans_in():
    topo = deliberation_topology()
    from_start = {e["dst"] for e in topo["edges"] if e["src"] == "START"}
    assert from_start == {"brain_a", "brain_b"}
    into_decide = {e["src"] for e in topo["edges"] if e["dst"] == "decide"}
    assert into_decide == {"brain_a", "brain_b"}
    assert {n["name"] for n in topo["nodes"]} == {"brain_a", "brain_b", "decide"}
