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


def test_role_skills_text_loads_by_id(tmp_path):
    from waku.ops.deliberate import _role_skills_text

    d = tmp_path / "skills" / "architect" / "write-adr"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: write-adr\ndescription: Record a decision.\n---\n\n"
        "## Instructions\nRecord context, options, decision.", encoding="utf-8")
    text = _role_skills_text(str(tmp_path), "architect", ["write-adr"])
    assert "Record context, options, decision" in text
    assert "description" not in text  # frontmatter is stripped


def test_role_skills_text_is_empty_without_harness_or_skills(tmp_path):
    from waku.ops.deliberate import _role_skills_text

    assert _role_skills_text("", "architect", ["write-adr"]) == ""
    assert _role_skills_text(str(tmp_path), "architect", []) == ""


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


# --- loud failure: skills, quorum, degraded state, budget ---------------------

def _fake_waku(harness: str = ""):
    """Just enough of a Waku for the binder: settings.harness. No client, no db."""
    from types import SimpleNamespace
    return SimpleNamespace(settings=SimpleNamespace(harness=harness))


def _roles(runtime: str = "eval") -> dict:
    """Roles whose runtime is NOT a deliberation brain, so `_run_role` raises
    RoleUnavailable before touching any subprocess or network."""
    return {rid: {"id": rid, "lens": rid.upper(), "runtime": runtime,
                  "authority": "recommend", "skills": []}
            for rid in ("architect", "reviewer", "decider")}


def test_a_named_skill_without_a_file_fails_before_any_brain_runs(tmp_path):
    from waku.ops.deliberate import _build_bound_graph, _role_skills_text

    with pytest.raises(FileNotFoundError, match="write-adr"):
        _role_skills_text(str(tmp_path), "architect", ["write-adr"])

    (tmp_path / "project.toml").write_text('name = "p"\n', encoding="utf-8")
    roles = _roles()
    roles["architect"]["skills"] = ["write-adr"]
    with pytest.raises(FileNotFoundError, match="write-adr"):
        _build_bound_graph(_fake_waku(str(tmp_path)), roles)


def test_safe_records_health_per_round():
    from waku.ops.deliberate import _safe

    health: dict = {}
    boom = _safe(lambda s: (_ for _ in ()).throw(RuntimeError("rate limited")), "brain A", health)
    fine = _safe(lambda s: "ok", "brain A", health)
    assert "unavailable" in boom({})
    assert health == {"brain A": "unavailable (RuntimeError: rate limited)"}
    assert fine({}) == "ok"
    assert health == {}   # a later success clears the label


def test_no_quorum_means_the_decider_does_not_run():
    """Both brains dead → the decider is skipped, health names all three, and
    the state carries no decision — never a verdict over two error strings."""
    from waku.ops.deliberate import (
        BRAIN_A_LABEL,
        BRAIN_B_LABEL,
        DECIDER_LABEL,
        _build_bound_graph,
        _run_rounds,
    )

    graph, health = _build_bound_graph(_fake_waku(), _roles("eval"))
    state = _run_rounds(graph, {"task": "t"}, max_rounds=1, health=health)
    assert set(health) == {BRAIN_A_LABEL, BRAIN_B_LABEL, DECIDER_LABEL}
    assert "no quorum" in health[DECIDER_LABEL]
    assert state["decision"].startswith("decider unavailable")


def test_one_dead_brain_costs_a_voice_and_marks_the_run_degraded(monkeypatch):
    from waku.ops import deliberate
    from waku.ops.deliberate import BRAIN_B_LABEL, _build_bound_graph, _run_rounds

    def run_role(waku, state, role, prompt, knowledge="", skills_text=""):
        if role["id"] == "reviewer":
            raise RuntimeError("429")
        return f"{role['id']} says: {prompt[:12]}"

    monkeypatch.setattr(deliberate, "_run_role", run_role)
    graph, health = _build_bound_graph(_fake_waku(), _roles("claude"))
    state = _run_rounds(graph, {"task": "t"}, max_rounds=1, health=health)
    assert state["decision"].startswith("decider says")     # the decider still ran
    assert list(health) == [BRAIN_B_LABEL]                  # and the run is marked
    assert "429" in health[BRAIN_B_LABEL]


def test_brains_see_the_knowledge_map_and_run_in_the_harness(tmp_path, monkeypatch):
    """The wire: with a harness bound, every brain prompt starts with the map,
    and a coding-agent brain is delegated with cwd = the harness."""
    from waku.ops import deliberate
    from waku.ops.deliberate import _build_bound_graph, _run_rounds

    (tmp_path / "PLAN.md").write_text("The brokerage test binds.", encoding="utf-8")
    (tmp_path / "project.toml").write_text(
        'name = "q"\n[[map]]\nid = "q.doctrine"\npath = "PLAN.md"\n'
        'authority = "source-of-truth"\nstatus = "established"\n', encoding="utf-8")
    seen: list[tuple[str, str, str]] = []

    def delegate(waku, state, prompt, agent, model, effort, sandbox="", cwd=""):
        seen.append((agent, sandbox, cwd))
        return prompt
    monkeypatch.setattr(deliberate, "_delegate", delegate)

    graph, health = _build_bound_graph(_fake_waku(str(tmp_path)), _roles("claude"))
    state = _run_rounds(graph, {"task": "decide X"}, max_rounds=1, health=health)
    assert health == {}
    assert state["position_a"].startswith("Project knowledge map — q.")
    assert "The brokerage test binds." in state["position_a"]
    assert "q.doctrine [source-of-truth / established] PLAN.md" in state["decision"]
    assert seen and all(cwd == str(tmp_path) and sb == "read-only" for _, sb, cwd in seen)


def test_budget_stops_the_rounds_and_says_so():
    from waku.ops.deliberate import _run_rounds

    g = build_deliberation_graph(brain_a_fn=lambda s: "A", brain_b_fn=lambda s: "B",
                                 decide_fn=lambda s: f"r{s['round']}")
    health: dict = {}
    state = _run_rounds(g, {}, max_rounds=3, budget_seconds=1e-9, health=health)
    assert state["round"] == 1 and state["decision"] == "r1"
    assert "budget" in health and "after round 1 of 3" in health["budget"]


def test_cli_exit_codes_carry_meaning(monkeypatch, capsys):
    """0 clean · 1 no decision · 2 degraded — the founder can script on it."""
    from waku.ops import deliberate

    class Quiet:
        def close(self): pass
    monkeypatch.setattr(deliberate, "Waku", lambda: Quiet())

    def fake(state):
        def run(waku, task="", record=True):
            return state
        return run

    monkeypatch.setattr(deliberate, "run_deliberation", fake({"decision": "go", "unavailable": {}}))
    deliberate.main(["t"])
    assert "go" in capsys.readouterr().out

    monkeypatch.setattr(deliberate, "run_deliberation",
                        fake({"decision": "go", "unavailable": {"brain B (reviewer)": "unavailable (429)"}}))
    with pytest.raises(SystemExit) as exc:
        deliberate.main(["t"])
    assert exc.value.code == 2 and "DEGRADED" in capsys.readouterr().out

    monkeypatch.setattr(deliberate, "run_deliberation", fake({"decision": "", "unavailable": {}}))
    with pytest.raises(SystemExit) as exc:
        deliberate.main(["t"])
    assert exc.value.code == 1
