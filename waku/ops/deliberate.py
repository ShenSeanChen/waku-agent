"""`python -m waku deliberate "<task>"` — the two-brain deliberation, run as a graph.

S6–S8 (design) and S9 (build + review) share ONE graph: two brains hold
different lenses in parallel, a decider reconciles them. The graph is ONE
round; this file runs it up to `WAKU_DELIBERATE_MAX_ROUNDS` times (default 3),
feeding each round's synthesis back to the brains so they converge, and the
decider only makes the FINAL call on the last round.

This file is the ONE place real callables meet the pure workflow in
waku/graph/workflows/deliberate.py — mirroring ops/gather.py — so a reviewer
only has to read one function to know what a deliberation is allowed to touch.

The brains are roles (see docs/project-roles.md), read from the bound harness's
`[[roles]]` when `WAKU_HARNESS` is set; otherwise the env-var defaults below
apply. A role is {lens, runtime, model, effort, authority, skills}; `runtime`
picks the callable: claude/codex/pi = `delegate_task` (a subprocess),
deepseek = one bare model call. Those callables are bound into the graph's
nodes; `loop` and `eval` are not deliberation brains.

With a harness bound, two more things happen — the wires that make the harness
a brain rather than a config file (waku/ops/knowledge.py):

  * every brain prompt starts with the KNOWLEDGE MAP — the index with authority
    labels, then the authoritative bodies — and the coding-agent brains run in
    the harness directory so they can read the rest;
  * the final decision is RECORDED: a file under decisions/ and a [[map]] entry
    with authority "proposal" for the owner to promote.

Failure is loud. A dead brain still costs only a voice (its honest text goes to
the decider), but the run is marked DEGRADED, the CLI says which brain and why
and exits 2; if BOTH brains are dead the decider does not run at all. A role
that names a skill file that does not exist fails before any brain is spent.

    WAKU_DELIBERATE_BRAIN_A          default architect model (claude) — "fable-5.1"
    WAKU_DELIBERATE_BRAIN_A_EFFORT   its reasoning effort — "medium"
    WAKU_DELIBERATE_BRAIN_B          default reviewer model (deepseek) — "deepseek-v4-pro"
    WAKU_DELIBERATE_DECIDER          default decider model (codex) — "astra"
    WAKU_DELIBERATE_DECIDER_EFFORT   its reasoning effort — "high"
    WAKU_DELIBERATE_MAX_ROUNDS       rounds before the final decision — 3
    WAKU_DELIBERATE_BUDGET_SECONDS   wall-clock cap for the whole run — 1800
    WAKU_HARNESS_CONTEXT_CHARS       map bodies inlined per prompt — 60000
    WAKU_BUILD_MODEL                 default engineer model (claude) — "sonnet"
"""

from __future__ import annotations

import os
import sys
import time
import tomllib
from pathlib import Path

from rich.console import Console

from waku.app import Waku
from waku.graph import run_graph
from waku.graph.workflows.deliberate import (
    BRAIN_A_PROMPT,
    BRAIN_B_PROMPT,
    BRAIN_REVISE_PROMPT,
    FINAL_PROMPT,
    REVIEW_PROMPT,
    SYNTHESIS_PROMPT,
    build_deliberation_graph,
)
from waku.ops.knowledge import knowledge_context, record_decision

BRAIN_A_LABEL = "brain A (architect)"
BRAIN_B_LABEL = "brain B (reviewer)"
DECIDER_LABEL = "decider"


class RoleUnavailable(Exception):
    """A role cannot run as a deliberation brain (wrong runtime, or a rung that
    needs the owner). Caught by `_safe` like any other dead brain — recorded,
    never silently turned into a position."""


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def load_roles(project_dir: str | Path) -> dict[str, dict]:
    """Read `[[roles]]` from a harness's project.toml into {id: role}. Empty when
    there is no harness or no roles block."""
    path = Path(project_dir) / "project.toml"
    if not path.is_file():
        return {}
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in cfg.get("roles", []) if r.get("id")}


def _default_roles() -> dict[str, dict]:
    """The env-var defaults, expressed as roles — what a no-harness run uses."""
    return {
        "architect": {"id": "architect", "lens": "SOFTWARE ARCHITECT",
                      "runtime": "claude",
                      "model": _env("WAKU_DELIBERATE_BRAIN_A", "fable-5.1"),
                      "effort": _env("WAKU_DELIBERATE_BRAIN_A_EFFORT", "medium"),
                      "authority": "recommend", "skills": []},
        "reviewer": {"id": "reviewer", "lens": "PRODUCT AND SYSTEMS REVIEWER",
                     "runtime": "deepseek",
                     "model": _env("WAKU_DELIBERATE_BRAIN_B", "deepseek-v4-pro"),
                     "authority": "recommend", "skills": []},
        "decider": {"id": "decider", "lens": "DECIDER", "runtime": "codex",
                    "model": _env("WAKU_DELIBERATE_DECIDER", "astra"),
                    "effort": _env("WAKU_DELIBERATE_DECIDER_EFFORT", "high"),
                    "authority": "recommend", "skills": []},
        "engineer": {"id": "engineer", "lens": "ENGINEER", "runtime": "claude",
                     "model": _env("WAKU_BUILD_MODEL", "sonnet"),
                     "authority": "sandbox", "skills": []},
    }


def _harness(waku: Waku) -> str:
    return getattr(waku.settings, "harness", "") or ""


def _resolve_roles(waku: Waku) -> dict[str, dict]:
    """Roles from the bound harness, else the env-var defaults."""
    harness = _harness(waku)
    if harness:
        loaded = load_roles(harness)
        if loaded:
            return loaded
    return _default_roles()


def _delegate(waku: Waku, state: dict, prompt: str, agent: str, model: str,
              effort: str, sandbox: str = "", cwd: str = "") -> str:
    """One coding-agent brain = one delegate_task call on the configured CLI.
    With a harness bound, `cwd` is the harness directory so the agent can read
    the files the map points at (and never lands in a scratch workspace)."""
    from waku.tools import experimental

    tool = experimental.make_delegate_tool(waku.settings)
    return tool.fn(task=prompt, agent=agent, model=model, effort=effort,
                   sandbox=sandbox, cwd=cwd, _notify=state.get("_notify"))


def _direct_brain(waku: Waku, prompt: str, model: str = "") -> str:
    """One reasoning brain = one direct DeepSeek call. No shell, no tools."""
    from waku.config import Settings
    from waku.loop.models import get_client

    model = model or _env("WAKU_DELIBERATE_BRAIN_B", "deepseek-v4-pro")
    client = get_client(Settings(provider="deepseek", model=model))
    resp = client.messages.create(model=model, max_tokens=3000,
                                  messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _role_skills_text(harness: str, role_id: str, skill_ids) -> str:
    """The bodies of a role's skills, loaded BY ID from
    `<harness>/skills/<role_id>/<skill-id>/SKILL.md` and joined. Reuses the
    procedural-memory parser (the SKILL.md format + `_parse`), not
    `SkillLoader.match` — the role names its skills; nothing is keyword-matched.

    A named skill whose file is missing is an ERROR, not a skip: a role that
    silently runs without the procedure it was configured with is the kind of
    failure nobody notices."""
    if not harness or not skill_ids:
        return ""
    from waku.memory.procedural.loader import _parse

    parts = []
    for sid in skill_ids:
        path = Path(harness) / "skills" / role_id / sid / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(
                f"role '{role_id}' names skill '{sid}' but {path} does not exist — "
                "write the SKILL.md or drop it from the role's skills list")
        skill = _parse(path)
        if skill and skill.body:
            parts.append(skill.body)
    return "\n\n".join(parts)


def _run_role(waku: Waku, state: dict, role: dict, prompt: str,
              knowledge: str = "", skills_text: str = "") -> str:
    """Run one role's brain, dispatching on its `runtime`. The prompt is
    assembled in authority order: the knowledge map, then the role's skills,
    then the round's prompt."""
    if skills_text:
        prompt = f"Relevant skill instructions:\n{skills_text}\n\n{prompt}"
    if knowledge:
        prompt = f"{knowledge}\n\n{prompt}"
    runtime = (role.get("runtime") or "loop").strip().lower()
    model = role.get("model") or ""
    effort = role.get("effort") or ""
    if runtime in ("claude", "codex", "pi"):
        from waku.tools.authority import authority_to_sandbox

        sandbox = authority_to_sandbox(role.get("authority", "recommend"))
        if sandbox is None:
            raise RoleUnavailable(f"role '{role.get('id', '?')}' needs owner approval "
                                  f"(authority '{role.get('authority')}')")
        return _delegate(waku, state, prompt, agent=runtime, model=model,
                         effort=effort, sandbox=sandbox, cwd=_harness(waku))
    if runtime == "deepseek":
        return _direct_brain(waku, prompt, model=model)
    raise RoleUnavailable(f"role '{role.get('id', '?')}' has runtime '{runtime}', which "
                          "is not a deliberation brain (claude/codex/pi/deepseek)")


def _safe(fn, label: str, health: dict | None = None):
    """Run a brain, or return honest text saying why it could not — a dead lens
    must never kill the deliberation (the gather rule). `health` records the
    outcome per label for THIS round: cleared on success, the reason on
    failure — so the run can be marked degraded instead of looking fine."""
    health = health if health is not None else {}

    def run(state: dict) -> str:
        try:
            out = fn(state)
        except SystemExit as exc:  # get_client's "no API key" is a SystemExit
            health[label] = f"unavailable ({exc})"
            return f"{label} unavailable ({exc})"
        except Exception as exc:  # noqa: BLE001 — the whole point is to not propagate
            health[label] = f"unavailable ({type(exc).__name__}: {exc})"
            return f"{label} unavailable ({type(exc).__name__}: {exc})"
        health.pop(label, None)
        return out

    return run


def _context(state: dict) -> str:
    """The task-and-work block shared by every brain prompt."""
    task = state.get("task", "")
    work = state.get("work", "")
    return f"Task:\n{task}" + (f"\n\nWork to review:\n{work}" if work else "")


def _brain_prompt(kind: str, state: dict, lens: str) -> str:
    """The right prompt for one brain, given the round and mode.

    Round 1: independent positions (design) or first-pass review (review).
    Round 2+: revise against the prior positions and the last synthesis."""
    if int(state.get("round", 1)) <= 1:
        if state.get("work"):
            return REVIEW_PROMPT.format(lens=lens, task=state.get("task", ""),
                                        work=state.get("work", ""))
        template = BRAIN_A_PROMPT if kind == "brain_a" else BRAIN_B_PROMPT
        return template.format(lens=lens, task=state.get("task", ""))

    own_key = "position_a" if kind == "brain_a" else "position_b"
    other_key = "position_b" if kind == "brain_a" else "position_a"
    return BRAIN_REVISE_PROMPT.format(
        lens=lens, round=state.get("round", 1), max_rounds=state.get("max_rounds", 3),
        context=_context(state),
        own=state.get(own_key, ""), other=state.get(other_key, ""),
        critique=state.get("decision", ""))


def _decide_prompt(state: dict, lens_a: str, lens_b: str) -> str:
    """The decider's prompt: intermediate rounds synthesize + critique; the last
    round issues the final call. Design mode calls it a "decision"; review mode
    calls it a "verdict"."""
    positions = {"position_a": state.get("position_a", ""),
                 "position_b": state.get("position_b", ""),
                 "lens_a": lens_a, "lens_b": lens_b}
    call = "verdict" if state.get("work") else "decision"
    if int(state.get("round", 1)) >= int(state.get("max_rounds", 3)):
        return FINAL_PROMPT.format(rounds=state.get("max_rounds", 3),
                                   call=call, **positions)
    return SYNTHESIS_PROMPT.format(call=call, **positions)


def _build_bound_graph(waku: Waku, roles: dict | None = None):
    """The pure workflow, wired to this machine and these roles. Returns
    (graph, health): `health` is the per-round liveness of the three brains,
    written by `_safe` and read by the decider (no quorum → it does not run).

    Skills and the knowledge map are resolved HERE, before any brain is spent:
    a role that names a missing skill fails now, not after two model calls."""
    roles = dict(roles) if roles else _resolve_roles(waku)
    architect = roles.get("architect") or {}
    reviewer = roles.get("reviewer") or {}
    decider = roles.get("decider") or {}
    harness = _harness(waku)
    knowledge = knowledge_context(harness)
    skills = {rid: _role_skills_text(harness, rid, roles.get(rid, {}).get("skills", []))
              for rid in ("architect", "reviewer", "decider")}
    health: dict[str, str] = {}

    def brain_a(state: dict) -> str:
        return _run_role(waku, state, architect,
                         _brain_prompt("brain_a", state, architect.get("lens", "")),
                         knowledge=knowledge, skills_text=skills["architect"])

    def brain_b(state: dict) -> str:
        return _run_role(waku, state, reviewer,
                         _brain_prompt("brain_b", state, reviewer.get("lens", "")),
                         knowledge=knowledge, skills_text=skills["reviewer"])

    def decide(state: dict) -> str:
        if BRAIN_A_LABEL in health and BRAIN_B_LABEL in health:
            raise RoleUnavailable("no quorum — both brains unavailable this round, "
                                  "so there is nothing to decide")
        return _run_role(waku, state, decider,
                         _decide_prompt(state, architect.get("lens", ""),
                                        reviewer.get("lens", "")),
                         knowledge=knowledge, skills_text=skills["decider"])

    graph = build_deliberation_graph(
        brain_a_fn=_safe(brain_a, BRAIN_A_LABEL, health),
        brain_b_fn=_safe(brain_b, BRAIN_B_LABEL, health),
        decide_fn=_safe(decide, DECIDER_LABEL, health),
    )
    return graph, health


def _run_rounds(graph, state: dict, max_rounds: int, observer=None,
                budget_seconds: float = 0, health: dict | None = None) -> dict:
    """Run one round of the graph up to `max_rounds` times, carrying the state
    forward so each round's brains see the prior positions + synthesis. The
    decider finalizes on the last round (its prompt changes).

    `budget_seconds` (0 = none) is a wall-clock cap checked BEFORE each round
    after the first: when it is spent the run stops and says so in `health`,
    because the decision left behind is a synthesis, not the final call."""
    t0 = time.monotonic()
    for r in range(1, max_rounds + 1):
        if r > 1 and budget_seconds and time.monotonic() - t0 > budget_seconds:
            if health is not None:
                health["budget"] = (f"wall-clock budget of {int(budget_seconds)}s spent "
                                    f"after round {r - 1} of {max_rounds}; the decision "
                                    "is that round's synthesis, not the final call")
            break
        state["round"] = r
        state["max_rounds"] = max_rounds
        state = run_graph(graph, state, observer=observer)
    return state


def run_deliberation(waku: Waku | None = None, task: str = "", work: str = "",
                     observer=None, max_rounds: int | None = None,
                     roles: dict | None = None, record: bool = True) -> dict:
    """Run one deliberation to completion and return the final state.

    `work` present → review mode (S9); absent → design mode (S6–S8). The state
    carries `decision` (the final call, from the last round), `unavailable`
    ({label: why} for anything that failed in the last round — empty means a
    clean run), `recorded` (the decision file, when a harness is bound and
    `record` is on) and per-round `errors`. `max_rounds` defaults to
    WAKU_DELIBERATE_MAX_ROUNDS (3). `roles` override the bound harness (or the
    env-var defaults).

    Never raises for a dead brain. DOES raise before any brain runs when the
    harness itself is broken (a role names a skill file that does not exist)."""
    own = waku is None
    waku = waku or Waku()
    try:
        def notify(kind: str, ev: dict) -> None:
            waku.tracer.event(kind, ev)
            if observer:
                observer(kind, ev)

        rounds = max_rounds if max_rounds is not None else int(
            _env("WAKU_DELIBERATE_MAX_ROUNDS", "3"))
        budget = float(_env("WAKU_DELIBERATE_BUDGET_SECONDS", "1800"))
        graph, health = _build_bound_graph(waku, roles)
        state = _run_rounds(graph, {"task": task, "work": work}, rounds,
                            observer=notify, budget_seconds=budget, health=health)
        state["unavailable"] = dict(health)
        if DECIDER_LABEL in health:
            state["decision"] = ""   # a dead decider is no decision, not a string
        harness = _harness(waku)
        if record and harness and state.get("decision"):
            state["recorded"] = str(record_decision(
                harness, task, state["decision"],
                mode="review" if work else "design", unavailable=state["unavailable"]))
        return state
    finally:
        if own:
            waku.close()


def run_build_review(waku: Waku | None = None, task: str = "", cwd: str = "",
                     observer=None, max_rounds: int | None = None,
                     roles: dict | None = None, record: bool = True) -> dict:
    """S9: the engineer role builds, then the deliberation graph reviews it.

    The build is one delegate_task call on the engineer role in `cwd`; its
    summary becomes `work` for the review-mode deliberation (same bounded loop).
    The engineer sees the knowledge map's index too — it is building against
    the project the map describes."""
    own = waku is None
    waku = waku or Waku()
    try:
        from waku.tools import experimental
        from waku.tools.authority import authority_to_sandbox

        roles = dict(roles) if roles else _resolve_roles(waku)
        engineer = roles.get("engineer") or _default_roles()["engineer"]
        sandbox = authority_to_sandbox(engineer.get("authority", "sandbox"))
        harness = _harness(waku)
        skills_text = _role_skills_text(harness, "engineer", engineer.get("skills", []))
        knowledge = knowledge_context(harness, budget_chars=0)  # the index, not the bodies
        build_task = "\n\n".join(p for p in (knowledge, skills_text, task) if p)
        tool = experimental.make_delegate_tool(waku.settings)
        summary = tool.fn(task=build_task, agent=engineer.get("runtime", "claude"),
                          model=engineer.get("model", ""), sandbox=sandbox or "", cwd=cwd,
                          _notify=(observer or (lambda k, e: None)))
        return run_deliberation(waku, task=task, work=summary, observer=observer,
                                max_rounds=max_rounds, roles=roles, record=record)
    finally:
        if own:
            waku.close()


def main(argv: list[str] | None = None) -> None:
    """Exit codes carry meaning: 0 a clean decision · 1 no decision ·
    2 a decision from a DEGRADED run (a brain died, or the budget ran out)."""
    console = Console()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('usage: python -m waku deliberate "<task>" [--build] [--cwd <path>] [--no-record]')
        sys.exit(1)

    cwd, build, record = "", False, True
    task_parts: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--build":
            build = True
        elif args[i] == "--no-record":
            record = False
        elif args[i] == "--cwd" and i + 1 < len(args):
            i += 1
            cwd = args[i]
        else:
            task_parts.append(args[i])
        i += 1
    task = " ".join(task_parts)

    waku = Waku()
    try:
        if build:
            console.print("[dim]building with the engineer, then reviewing…[/dim]")
            state = run_build_review(waku, task=task, cwd=cwd, record=record)
        else:
            console.print("[dim]two brains deliberate, the decider decides…[/dim]")
            state = run_deliberation(waku, task=task, record=record)
    except FileNotFoundError as exc:   # a broken harness, caught before any spend
        console.print(f"[red]harness error:[/red] {exc}")
        sys.exit(1)
    finally:
        waku.close()

    decision = state.get("decision") or ""
    unavailable = state.get("unavailable") or {}
    console.print(decision or "(no decision — the decider did not run)")
    if state.get("recorded"):
        console.print(f"[dim]recorded as a proposal: {state['recorded']}[/dim]")
    for label, why in unavailable.items():
        console.print(f"[red]DEGRADED[/red] {label}: {why}")
    for node, err in (state.get("errors") or {}).items():
        console.print(f"[dim]{node}: {err}[/dim]")
    if not decision:
        sys.exit(1)
    if unavailable:
        sys.exit(2)


if __name__ == "__main__":
    main()
