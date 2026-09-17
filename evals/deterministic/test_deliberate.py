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
    from waku.ops.deliberate import _brain_prompt

    a = _brain_prompt("brain_a", {"task": "T", "work": "W"}, "SOFTWARE ARCHITECT")
    b = _brain_prompt("brain_b", {"task": "T", "work": "W"}, "PRODUCT AND SYSTEMS REVIEWER")
    assert "SOFTWARE ARCHITECT" in a and "T" in a and "W" in a
    assert "PRODUCT AND SYSTEMS REVIEWER" in b


def test_design_prompt_passes_task_only():
    from waku.ops.deliberate import _brain_prompt

    assert "SOFTWARE ARCHITECT" in _brain_prompt("brain_a", {"task": "T", "work": ""}, "SOFTWARE ARCHITECT")
    assert "PRODUCT AND SYSTEMS REVIEWER" in _brain_prompt("brain_b", {"task": "T", "work": ""}, "PRODUCT AND SYSTEMS REVIEWER")


def test_brain_prompt_uses_the_roles_lens_not_a_hardcoded_one():
    from waku.ops.deliberate import _brain_prompt

    out = _brain_prompt("brain_a", {"task": "T", "work": ""}, "CHIEF ARCHITECT")
    assert "CHIEF ARCHITECT" in out and "SOFTWARE ARCHITECT" not in out


def test_brain_prompt_revises_after_round_one():
    from waku.ops.deliberate import _brain_prompt

    state = {"task": "T", "work": "", "round": 2, "max_rounds": 3,
             "position_a": "prior A", "position_b": "prior B", "decision": "critique"}
    a = _brain_prompt("brain_a", state, "SOFTWARE ARCHITECT")
    assert "SOFTWARE ARCHITECT" in a and "round 2 of 3" in a
    assert "prior A" in a and "prior B" in a and "critique" in a
    assert "PRODUCT AND SYSTEMS REVIEWER" in _brain_prompt("brain_b", state, "PRODUCT AND SYSTEMS REVIEWER")


def test_decider_finalizes_only_on_last_round():
    from waku.ops.deliberate import _decide_prompt

    base = {"position_a": "A", "position_b": "B", "max_rounds": 3}
    mid = _decide_prompt({**base, "round": 1}, "SOFTWARE ARCHITECT", "PRODUCT AND SYSTEMS REVIEWER")
    fin = _decide_prompt({**base, "round": 3}, "SOFTWARE ARCHITECT", "PRODUCT AND SYSTEMS REVIEWER")
    assert "FINAL round" in fin
    assert "FINAL round" not in mid
    assert "Do NOT make the final" in mid
    assert "SOFTWARE ARCHITECT" in fin and "PRODUCT AND SYSTEMS REVIEWER" in fin


def test_decider_says_verdict_in_review_mode():
    from waku.ops.deliberate import _decide_prompt

    base = {"position_a": "A", "position_b": "B", "max_rounds": 3, "work": "the build"}
    fin = _decide_prompt({**base, "round": 3}, "SOFTWARE ARCHITECT", "PRODUCT AND SYSTEMS REVIEWER")
    assert "final verdict" in fin and "final decision" not in fin


def test_load_roles_reads_the_roles_block(tmp_path):
    from waku.ops.deliberate import load_roles

    (tmp_path / "project.toml").write_text(
        '[[roles]]\nid = "architect"\nlens = "CHIEF ARCHITECT"\nruntime = "claude"\n'
        'model = "fable-5.1"\nskills = ["write-adr"]\n', encoding="utf-8")
    roles = load_roles(tmp_path)
    assert roles["architect"]["lens"] == "CHIEF ARCHITECT"
    assert roles["architect"]["skills"] == ["write-adr"]


def test_load_roles_is_empty_without_a_harness(tmp_path):
    from waku.ops.deliberate import load_roles

    assert load_roles(tmp_path) == {}


def test_default_roles_preserve_the_env_fallbacks():
    from waku.ops.deliberate import _default_roles

    roles = _default_roles()
    assert roles["architect"]["runtime"] == "claude"
    assert roles["reviewer"]["runtime"] == "deepseek"
    assert roles["decider"]["runtime"] == "codex"
    assert roles["engineer"]["runtime"] == "claude"


def test_run_rounds_loops_to_the_cap_and_finalizes():
    from waku.ops.deliberate import _run_rounds

    calls: list[tuple[str, int]] = []

    def brain_a(state):
        calls.append(("a", state.get("round")))
        return f"A{state.get('round')}"

    def brain_b(state):
        calls.append(("b", state.get("round")))
        return f"B{state.get('round')}"

    def decide(state):
        calls.append(("d", state.get("round")))
        r = state.get("round")
        return "FINAL" if r >= state.get("max_rounds", 3) else "provisional"

    g = build_deliberation_graph(brain_a_fn=brain_a, brain_b_fn=brain_b, decide_fn=decide)
    state = _run_rounds(g, {}, max_rounds=3)
    assert state["decision"] == "FINAL"
    assert state["round"] == 3
    assert len(calls) == 9                      # 3 nodes × 3 rounds
    assert calls.count(("a", 1)) == 1 and calls.count(("a", 3)) == 1
    assert calls.count(("d", 3)) == 1


def test_run_rounds_brains_see_prior_positions():
    from waku.ops.deliberate import _run_rounds

    seen: dict[int, tuple] = {}

    def brain_a(state):
        seen[state.get("round")] = (state.get("position_a"), state.get("position_b"),
                                    state.get("decision"))
        return f"A{state.get('round')}"

    def brain_b(state):
        return f"B{state.get('round')}"

    def decide(state):
        return "provisional" if state.get("round") < state.get("max_rounds", 3) else "FINAL"

    g = build_deliberation_graph(brain_a_fn=brain_a, brain_b_fn=brain_b, decide_fn=decide)
    _run_rounds(g, {}, max_rounds=3)
    assert seen[2] == ("A1", "B1", "provisional")   # round 2 saw round 1's output
    assert seen[3] == ("A2", "B2", "provisional")   # round 3 saw round 2's output


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
