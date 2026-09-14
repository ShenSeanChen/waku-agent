"""The deliberation graph — two brains think in parallel, a decider chooses.

One graph serves two jobs (design and review), so the review pass follows the
SAME shape as the design pass — the same pair of brains, the same decider,
only the prompts differ:

    START ── brain_a ──┐
          └─ brain_b ──┴─► decide → END

brain_a (a coding agent: Claude Code) and brain_b (a direct model call:
DeepSeek) hold DIFFERENT lenses — architect vs product/systems reviewer — so
their disagreement is real, not cosmetic. The engine runs them in one wave
(they share no dependencies). `decide` waits on both and reconciles the two
positions into ONE output: an ADR in design mode, a verdict in review mode.

Two rules this file obeys:

1. ROUTERS STAY OUT. There is no router here: the decider's output is the whole
   point of the graph, and it is a plain string the caller reads. Control flow
   is still code (the edges); the decider decides the ANSWER, never the path.

2. A DEAD BRAIN MUST NOT KILL THE DELIBERATION. A node that raises fires no
   edges, so `decide`'s dependencies would never complete and the run would
   produce nothing. The BINDER (waku/ops/deliberate.py) wraps each brain so it
   returns honest text instead of raising — the gather lesson, applied here.
"""

from __future__ import annotations

from collections.abc import Callable

from waku.graph.engine import END, START, Graph, Node

# --- design mode (S6–S8): two positions → one decision -----------------------

BRAIN_A_PROMPT = """\
You are the SOFTWARE ARCHITECT on a two-person team. For the task below, give
your independent position: the load-bearing choices, the boundaries and seams,
the trade-offs (isolation, cost, latency, correctness), and a clear
recommendation. Be concrete and decisive.

Task:
{task}"""

BRAIN_B_PROMPT = """\
You are the PRODUCT AND SYSTEMS REVIEWER on a two-person team. For the task
below, give your independent position: the requirements and risks, the
operational and cost consequences, what could fail, and a clear recommendation.
Be concrete and decisive.

Task:
{task}"""

DECIDE_PROMPT = """\
Two specialists considered this task and reached these positions.

POSITION A (software architect):
{position_a}

POSITION B (product and systems reviewer):
{position_b}

Synthesize ONE decision: the recommendation, what both agree on, where they
disagree and which side you take, and the residual risk. Be decisive — no
"on the other hand" without a call."""

# --- review mode (S9): two reviews → one verdict -----------------------------

REVIEW_PROMPT = """\
You are the {lens} reviewing work just produced for this task. List what is
wrong, missing, or risky — each finding concrete and tied to the work. End with
a clear recommendation (ship / change).

Task:
{task}

Work to review:
{work}"""

VERDICT_PROMPT = """\
Two reviewers considered this work and reached these positions.

POSITION A (software architect):
{position_a}

POSITION B (product and systems reviewer):
{position_b}

Issue ONE verdict: APPROVE or REQUEST CHANGES, then the top concrete changes
required (numbered), then a one-line justification. Be decisive."""


def build_deliberation_graph(*, brain_a_fn: Callable[[dict], str],
                             brain_b_fn: Callable[[dict], str],
                             decide_fn: Callable[[dict], str]) -> Graph:
    """Callables injected exactly as triage/gather do it: evals script them with
    lambdas, deliberation_topology() passes stubs to describe the shape without
    running it, and waku/ops/deliberate.py binds the real ones."""
    g = Graph("deliberate")

    g.add_node(Node("brain_a", lambda s: {"position_a": brain_a_fn(s)}, kind="agent"))
    g.add_node(Node("brain_b", lambda s: {"position_b": brain_b_fn(s)}, kind="llm"))
    g.add_node(Node("decide", lambda s: {"decision": decide_fn(s)}, kind="agent"))

    g.add_edge(START, "brain_a")
    g.add_edge(START, "brain_b")
    g.add_edge("brain_a", "decide")
    g.add_edge("brain_b", "decide")
    g.add_edge("decide", END)
    return g


def deliberation_topology() -> dict:
    """The topology as data, for the dashboard — built with stubs, never run."""
    return build_deliberation_graph(
        brain_a_fn=lambda s: "", brain_b_fn=lambda s: "", decide_fn=lambda s: "",
    ).describe()
