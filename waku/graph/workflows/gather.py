"""The morning gather — the workflow that is genuinely a graph.

Every morning the same four questions: what is open on the repo, what is being
said about the things it touches, what is on today, and what do I already know
about it. You do not discover that list — you know it before you wake up. So
letting a model work it out one tool call at a time buys nothing and costs four
round trips, and on a bad day it forgets one.

    START ─┬─ scan_github  ─┐
           ├─ scan_web      │
           ├─ scan_calendar ├─► synthesize ──router──► draft_digest → END
           └─ scan_memory  ─┘                    └──── quiet ──────► END

The four scans have no dependencies on each other, so the engine puts them in
one wave and runs them together. That is the whole argument for a graph here:
the shape is known in advance, so it can be drawn, and once drawn the
independent parts are obviously independent.

Contrast with the loop, which is still the right tool for "work this PR": read
the diff, run the tests, discover it needs a rebase, decide what next. You
cannot draw that in advance — any shape you commit to is wrong by step three.

TWO RULES THIS FILE OBEYS, both load-bearing:

1. IT PROPOSES, IT NEVER ACTS. There is no agent_node here, no run_loop, no
   ToolRegistry. The one model call is a bare messages.create with NO tools
   parameter — so the model is never handed a schema it could use to send,
   merge, or create anything. Not "instructed not to": *unable to*. The only
   write in the entire graph is a markdown file in the outbox for a human to
   read. test_gather_workflow.py enforces this by reading this file's source.

2. EVERY BRANCH CATCHES ITS OWN FAILURE. A node that raises fires no edges, so
   synthesize's dependencies would never complete and the run would produce
   NOTHING — no error, no digest, an empty outbox. Silence is the worst
   possible failure mode for a morning routine, so each scan returns honest
   text instead of raising. See _safe below.
"""

from __future__ import annotations

from collections.abc import Callable

from waku.graph.engine import END, START, Graph, Node

# Each scan writes only keys carrying its own prefix. Parallel nodes that write
# the same key raise GraphStateCollision — the engine's backstop — but a
# convention a reviewer can check by eye is the actual defence.
SCAN_KEYS = {
    "scan_github": ("gh_text", "gh_open_prs", "gh_open_issues"),
    "scan_web": ("web_text",),
    "scan_calendar": ("cal_text", "cal_event_count"),
    "scan_memory": ("mem_text",),
}

DIGEST_PROMPT = """You are preparing a morning briefing for the maintainer of an
open-source project. Below is everything gathered a moment ago.

OPEN PULL REQUESTS AND ISSUES:
{gh_text}

WHAT THE WEB SAYS:
{web_text}

TODAY'S CALENDAR:
{cal_text}

WHAT YOU ALREADY KNOW:
{mem_text}

Write a short briefing in markdown:
- Lead with the two or three things that actually matter today, and say why.
- Then anything waiting on the maintainer, grouped, one line each.
- End with a single suggested focus for the day.

You are DRAFTING A PROPOSAL for a human to act on. You have not done anything
and cannot do anything — never write as if you have replied, merged, or sent.
Be brief. If a section gathered nothing, say so in a few words and move on."""


def _safe(fn: Callable[[], dict], fallback: dict) -> dict:
    """Run a scan, or return honest text saying why it could not.

    Chosen over the engine's `on_error` jumps deliberately. `on_error` would
    happen to work here — all four scans share one wave, so their jumps are
    collected in a single merge pass — but that is an accident of this
    topology, and it would break silently if someone re-shaped the graph. A
    wrapper is correct regardless of shape. Same surface-don't-crash rule as
    ToolRegistry.execute.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — the whole point is to not propagate
        return {**fallback, **{k: f"unavailable ({type(exc).__name__}: {exc})"
                               for k in fallback if isinstance(fallback[k], str)}}


def needs_action(state: dict) -> str:
    """The router: CODE, over counts the scans wrote — never the digest's prose.

    The engine's rule is "routers are code, never models". Routing on the
    model's wording would hand control flow to the model AND make the branch
    impossible to test without a scripted response per case. Counts are exact,
    cheap, and a test can drive them directly.
    """
    if (state.get("gh_open_prs") or state.get("gh_open_issues")
            or state.get("cal_event_count")):
        return "propose"
    return "quiet"


def build_gather_graph(*, github_fn: Callable[[], dict],
                       web_fn: Callable[[], str],
                       calendar_fn: Callable[[], dict],
                       memory_fn: Callable[[], str],
                       synth_fn: Callable[[dict], str],
                       draft_fn: Callable[[dict], str]) -> Graph:
    """Callables injected exactly as triage does it: evals script them with
    lambdas, gather_topology() passes stubs to describe the shape without
    running it, and waku/ops/gather.py binds the real ones."""
    g = Graph("gather")

    g.add_node(Node("scan_github", lambda s: _safe(
        github_fn, {"gh_text": "", "gh_open_prs": 0, "gh_open_issues": 0}), kind="tool"))
    g.add_node(Node("scan_web", lambda s: _safe(
        lambda: {"web_text": web_fn()}, {"web_text": ""}), kind="tool"))
    g.add_node(Node("scan_calendar", lambda s: _safe(
        calendar_fn, {"cal_text": "", "cal_event_count": 0}), kind="tool"))
    g.add_node(Node("scan_memory", lambda s: _safe(
        lambda: {"mem_text": memory_fn()}, {"mem_text": ""}), kind="tool"))

    # One model call, no tools. See rule 1 in the module docstring.
    g.add_node(Node("synthesize", lambda s: {"digest": synth_fn(s)}, kind="llm"))
    # The one write in the whole graph, and it is a file a human opens.
    g.add_node(Node("draft_digest", lambda s: {"draft_path": draft_fn(s)}, kind="tool"))

    for scan in SCAN_KEYS:
        g.add_edge(START, scan)          # no deps between them -> one wave
        g.add_edge(scan, "synthesize")   # synthesize waits for all four

    # The router lives on synthesize: it IS the barrier, so triage's separate
    # join node would be dead weight here.
    g.add_router("synthesize", needs_action,
                 {"propose": "draft_digest", "quiet": END})
    g.add_edge("draft_digest", END)
    return g


def gather_topology() -> dict:
    """The topology as data, for the dashboard — built with stubs, never run."""
    return build_gather_graph(
        github_fn=lambda: {"gh_text": "", "gh_open_prs": 0, "gh_open_issues": 0},
        web_fn=lambda: "",
        calendar_fn=lambda: {"cal_text": "", "cal_event_count": 0},
        memory_fn=lambda: "",
        synth_fn=lambda s: "",
        draft_fn=lambda s: "",
    ).describe()
