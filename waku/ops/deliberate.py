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
maps to a graph node kind — claude/codex/pi = a tool node (`delegate_task`),
deepseek = an `llm` node (a bare call), loop = an agent node.

    WAKU_DELIBERATE_BRAIN_A          default architect model (claude) — "fable-5.1"
    WAKU_DELIBERATE_BRAIN_A_EFFORT   its reasoning effort — "medium"
    WAKU_DELIBERATE_BRAIN_B          default reviewer model (deepseek) — "deepseek-v4-pro"
    WAKU_DELIBERATE_DECIDER          default decider model (codex) — "astra"
    WAKU_DELIBERATE_DECIDER_EFFORT   its reasoning effort — "high"
    WAKU_DELIBERATE_MAX_ROUNDS       rounds before the final decision — 3
    WAKU_BUILD_MODEL                 default engineer model (claude) — "sonnet"

A dead brain is wrapped so it returns honest text and the decider still runs —
a broken lens costs a voice, never the whole answer.
"""

from __future__ import annotations

import os
import sys
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


def _resolve_roles(waku: Waku) -> dict[str, dict]:
    """Roles from the bound harness, else the env-var defaults."""
    harness = getattr(waku.settings, "harness", "") or ""
    if harness:
        loaded = load_roles(harness)
        if loaded:
            return loaded
    return _default_roles()


def _delegate(waku: Waku, state: dict, prompt: str, agent: str, model: str,
              effort: str) -> str:
    """One coding-agent brain = one delegate_task call on the configured CLI."""
    from waku.tools import experimental

    tool = experimental.make_delegate_tool(waku.settings)
    return tool.fn(task=prompt, agent=agent, model=model, effort=effort,
                   _notify=state.get("_notify"))


def _direct_brain(waku: Waku, prompt: str, model: str = "") -> str:
    """One reasoning brain = one direct DeepSeek call. No shell, no tools."""
    from waku.config import Settings
    from waku.loop.models import get_client

    model = model or _env("WAKU_DELIBERATE_BRAIN_B", "deepseek-v4-pro")
    client = get_client(Settings(provider="deepseek", model=model))
    resp = client.messages.create(model=model, max_tokens=3000,
                                  messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _run_role(waku: Waku, state: dict, role: dict, prompt: str) -> str:
    """Run one role's brain, dispatching on its `runtime` (the node kind)."""
    runtime = (role.get("runtime") or "loop").strip().lower()
    model = role.get("model") or ""
    effort = role.get("effort") or ""
    if runtime in ("claude", "codex", "pi"):
        return _delegate(waku, state, prompt, agent=runtime, model=model, effort=effort)
    if runtime == "deepseek":
        return _direct_brain(waku, prompt, model=model)
    # loop and eval are not deliberation brains
    return (f"role '{role.get('id', '?')}' has runtime '{runtime}', which is not a "
            "deliberation brain (claude/codex/pi/deepseek)")


def _safe(fn, label: str):
    """Run a brain, or return honest text saying why it could not — a dead lens
    must never kill the deliberation (the gather rule)."""

    def run(state: dict) -> str:
        try:
            return fn(state)
        except SystemExit as exc:  # get_client's "no API key" is a SystemExit
            return f"{label} unavailable ({exc})"
        except Exception as exc:  # noqa: BLE001 — the whole point is to not propagate
            return f"{label} unavailable ({type(exc).__name__}: {exc})"

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
    """The pure workflow, wired to this machine and these roles."""
    roles = dict(roles) if roles else _resolve_roles(waku)
    architect = roles.get("architect") or {}
    reviewer = roles.get("reviewer") or {}
    decider = roles.get("decider") or {}

    def brain_a(state: dict) -> str:
        return _run_role(waku, state, architect,
                         _brain_prompt("brain_a", state, architect.get("lens", "")))

    def brain_b(state: dict) -> str:
        return _run_role(waku, state, reviewer,
                         _brain_prompt("brain_b", state, reviewer.get("lens", "")))

    def decide(state: dict) -> str:
        return _run_role(waku, state, decider,
                         _decide_prompt(state, architect.get("lens", ""),
                                        reviewer.get("lens", "")))

    return build_deliberation_graph(
        brain_a_fn=_safe(brain_a, "brain A (architect)"),
        brain_b_fn=_safe(brain_b, "brain B (reviewer)"),
        decide_fn=_safe(decide, "decider"),
    )


def _run_rounds(graph, state: dict, max_rounds: int, observer=None) -> dict:
    """Run one round of the graph up to `max_rounds` times, carrying the state
    forward so each round's brains see the prior positions + synthesis. The
    decider finalizes on the last round (its prompt changes)."""
    for r in range(1, max_rounds + 1):
        state["round"] = r
        state["max_rounds"] = max_rounds
        state = run_graph(graph, state, observer=observer)
    return state


def run_deliberation(waku: Waku | None = None, task: str = "", work: str = "",
                     observer=None, max_rounds: int | None = None,
                     roles: dict | None = None) -> dict:
    """Run one deliberation to completion. Returns the final state; never raises.

    `work` present → review mode (S9); absent → design mode (S6–S8). The state
    carries `decision` (the final call, from the last round) and per-round
    `errors` if any. `max_rounds` defaults to WAKU_DELIBERATE_MAX_ROUNDS (3).
    `roles` override the bound harness (or the env-var defaults)."""
    own = waku is None
    waku = waku or Waku()
    try:
        def notify(kind: str, ev: dict) -> None:
            waku.tracer.event(kind, ev)
            if observer:
                observer(kind, ev)

        rounds = max_rounds if max_rounds is not None else int(
            _env("WAKU_DELIBERATE_MAX_ROUNDS", "3"))
        return _run_rounds(_build_bound_graph(waku, roles), {"task": task, "work": work},
                           rounds, observer=notify)
    finally:
        if own:
            waku.close()


def run_build_review(waku: Waku | None = None, task: str = "", cwd: str = "",
                     observer=None, max_rounds: int | None = None,
                     roles: dict | None = None) -> dict:
    """S9: the engineer role builds, then the deliberation graph reviews it.

    The build is one delegate_task call on the engineer role in `cwd`; its
    summary becomes `work` for the review-mode deliberation (same bounded loop)."""
    own = waku is None
    waku = waku or Waku()
    try:
        from waku.tools import experimental

        roles = dict(roles) if roles else _resolve_roles(waku)
        engineer = roles.get("engineer") or _default_roles()["engineer"]
        tool = experimental.make_delegate_tool(waku.settings)
        summary = tool.fn(task=task, agent=engineer.get("runtime", "claude"),
                          model=engineer.get("model", ""), cwd=cwd,
                          _notify=(observer or (lambda k, e: None)))
        return run_deliberation(waku, task=task, work=summary, observer=observer,
                                max_rounds=max_rounds, roles=roles)
    finally:
        if own:
            waku.close()


def main(argv: list[str] | None = None) -> None:
    console = Console()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('usage: python -m waku deliberate "<task>" [--build] [--cwd <path>]')
        sys.exit(1)

    cwd, build = "", False
    task_parts: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--build":
            build = True
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
            state = run_build_review(waku, task=task, cwd=cwd)
        else:
            console.print("[dim]two brains deliberate, the decider decides…[/dim]")
            state = run_deliberation(waku, task=task)
        console.print(state.get("decision") or "(no decision — every brain failed)")
        for node, err in (state.get("errors") or {}).items():
            console.print(f"[dim]{node}: {err}[/dim]")
    finally:
        waku.close()


if __name__ == "__main__":
    main()
