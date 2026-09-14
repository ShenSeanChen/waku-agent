"""`python -m waku deliberate "<task>"` — the two-brain deliberation, run as a graph.

S6–S8 (design) and S9 (build + review) share ONE graph: two brains hold
different lenses in parallel, a decider reconciles them. This file is the ONE
place real callables meet the pure workflow in
waku/graph/workflows/deliberate.py — mirroring ops/gather.py — so a reviewer
only has to read one function to know what a deliberation is allowed to touch.

The brains are wired through env (the caller's own model ids), never hardcoded:

    WAKU_DELIBERATE_BRAIN_A          Claude Code model for brain A (default "fable-5.1")
    WAKU_DELIBERATE_BRAIN_A_EFFORT   its reasoning effort (default "medium")
    WAKU_DELIBERATE_BRAIN_B          DeepSeek model for brain B (default "deepseek-v4-pro")
    WAKU_DELIBERATE_DECIDER          Codex model for the decider (default "astra")
    WAKU_DELIBERATE_DECIDER_EFFORT   its reasoning effort (default "high")
    WAKU_BUILD_MODEL                 Claude Code model for the S9 build (default "sonnet")

brain_a (Claude Code) and decide (Codex) are `delegate_task` calls — the same
sub-agent machinery pi uses. brain_b is a DIRECT DeepSeek call: no shell, no
tools, just reasoning. A dead brain is wrapped so it returns honest text and
the decider still runs — a broken lens costs a voice, never the whole answer.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

from waku.app import Waku
from waku.graph import run_graph
from waku.graph.workflows.deliberate import (
    BRAIN_A_PROMPT,
    BRAIN_B_PROMPT,
    DECIDE_PROMPT,
    REVIEW_PROMPT,
    VERDICT_PROMPT,
    build_deliberation_graph,
)

# Which lens each parallel brain holds — distinct on purpose, so the two
# positions can genuinely disagree instead of parroting each other.
_LENSES = {
    "brain_a": "SOFTWARE ARCHITECT",
    "brain_b": "PRODUCT AND SYSTEMS REVIEWER",
}


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _delegate(waku: Waku, state: dict, prompt: str, agent: str, model: str,
              effort: str) -> str:
    """One coding-agent brain = one delegate_task call on the configured CLI."""
    from waku.tools import experimental

    tool = experimental.make_delegate_tool(waku.settings)
    return tool.fn(task=prompt, agent=agent, model=model, effort=effort,
                   _notify=state.get("_notify"))


def _direct_brain(waku: Waku, prompt: str) -> str:
    """One reasoning brain = one direct DeepSeek call. No shell, no tools."""
    from waku.config import Settings
    from waku.loop.models import get_client

    model = _env("WAKU_DELIBERATE_BRAIN_B", "deepseek-v4-pro")
    client = get_client(Settings(provider="deepseek", model=model))
    resp = client.messages.create(model=model, max_tokens=3000,
                                  messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


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


def _prompt(kind: str, state: dict) -> str:
    """Format the right prompt for one brain, given the mode.

    Review mode (state has `work`) labels each brain with its lens; design mode
    passes the task only."""
    task = state.get("task", "")
    work = state.get("work", "")
    if work:
        return REVIEW_PROMPT.format(lens=_LENSES.get(kind, ""), task=task, work=work)
    if kind == "brain_a":
        return BRAIN_A_PROMPT.format(task=task)
    return BRAIN_B_PROMPT.format(task=task)


def _build_bound_graph(waku: Waku):
    """The pure workflow, wired to this machine and these brains."""
    brain_a_model = _env("WAKU_DELIBERATE_BRAIN_A", "fable-5.1")
    brain_a_effort = _env("WAKU_DELIBERATE_BRAIN_A_EFFORT", "medium")
    decider_model = _env("WAKU_DELIBERATE_DECIDER", "astra")
    decider_effort = _env("WAKU_DELIBERATE_DECIDER_EFFORT", "high")

    def brain_a(state: dict) -> str:
        return _delegate(waku, state, _prompt("brain_a", state),
                         agent="claude", model=brain_a_model, effort=brain_a_effort)

    def brain_b(state: dict) -> str:
        return _direct_brain(waku, _prompt("brain_b", state))

    def decide(state: dict) -> str:
        template = VERDICT_PROMPT if state.get("work") else DECIDE_PROMPT
        prompt = template.format(position_a=state.get("position_a", ""),
                                 position_b=state.get("position_b", ""))
        return _delegate(waku, state, prompt,
                         agent="codex", model=decider_model, effort=decider_effort)

    return build_deliberation_graph(
        brain_a_fn=_safe(brain_a, "brain A (architect)"),
        brain_b_fn=_safe(brain_b, "brain B (reviewer)"),
        decide_fn=_safe(decide, "decider"),
    )


def run_deliberation(waku: Waku | None = None, task: str = "", work: str = "",
                     observer=None) -> dict:
    """Run one deliberation to completion. Returns the final state; never raises.

    `work` present → review mode (S9); absent → design mode (S6–S8). The state
    carries `decision` (the reconciled output) and per-node `errors` if any."""
    own = waku is None
    waku = waku or Waku()
    try:
        def notify(kind: str, ev: dict) -> None:
            waku.tracer.event(kind, ev)
            if observer:
                observer(kind, ev)

        return run_graph(_build_bound_graph(waku), {"task": task, "work": work},
                         observer=notify)
    finally:
        if own:
            waku.close()


def run_build_review(waku: Waku | None = None, task: str = "", cwd: str = "",
                     observer=None) -> dict:
    """S9: Sonnet builds the thing, then the deliberation graph reviews it.

    The build is one delegate_task call on Sonnet (Claude Code) in `cwd`; its
    summary becomes `work` for the review-mode deliberation."""
    own = waku is None
    waku = waku or Waku()
    try:
        from waku.tools import experimental

        build_model = _env("WAKU_BUILD_MODEL", "sonnet")
        tool = experimental.make_delegate_tool(waku.settings)
        summary = tool.fn(task=task, agent="claude", model=build_model, cwd=cwd,
                          _notify=(observer or (lambda k, e: None)))
        return run_deliberation(waku, task=task, work=summary, observer=observer)
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
            console.print("[dim]building with Sonnet, then reviewing…[/dim]")
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
