"""The deliberation graph — two brains think in parallel, a decider chooses.

This file is ONE ROUND of the debate:

    START ── brain_a ──┐
          └─ brain_b ──┴─► decide → END

brain_a (a coding agent: Claude Code) and brain_b (a direct model call:
DeepSeek) hold DIFFERENT lenses — architect vs product/systems reviewer — so
their disagreement is real, not cosmetic. The engine runs them in one wave
(they share no dependencies); `decide` waits on both.

The LOOP lives in the runner (waku/ops/deliberate.py): it runs this round up
to N times (default 3), feeding each round's synthesis back to the brains so
they converge, and the decider only makes the FINAL call on the last round.
Keeping the loop in the runner keeps this graph the same legible fan-out →
fan-in shape the engine and dashboard already understand; a round is the atom,
the runner is the repeat.

Two rules this file obeys:

1. ROUTERS STAY OUT. There is no router here: the decider's output is the whole
   point of the graph, and it is a plain string the caller reads. Control flow
   is still code (the edges); the decider decides the ANSWER, never the path.

2. A DEAD BRAIN MUST NOT KILL THE DELIBERATION. A node that raises fires no
   edges, so `decide`'s dependencies would never complete and the run would
   produce nothing. The BINDER wraps each brain so it returns honest text
   instead of raising — the gather lesson, applied here.
"""

from __future__ import annotations

from collections.abc import Callable

from waku.graph.engine import END, START, Graph, Node

# --- design mode: round 1 (independent positions) ----------------------------

BRAIN_A_PROMPT = """\
You are the {lens} on a two-person team. For the task below, give
your independent position: the load-bearing choices, the boundaries and seams,
the trade-offs (isolation, cost, latency, correctness), and a clear
recommendation. Be concrete and decisive.

Task:
{task}"""

BRAIN_B_PROMPT = """\
You are the {lens} on a two-person team. For the task
below, give your independent position: the requirements and risks, the
operational and cost consequences, what could fail, and a clear recommendation.
Be concrete and decisive.

Task:
{task}"""

# --- review mode: round 1 -----------------------------------------------------

REVIEW_PROMPT = """\
You are the {lens} reviewing work just produced for this task. List what is
wrong, missing, or risky — each finding concrete and tied to the work. End with
a clear recommendation (ship / change).

Task:
{task}

Work to review:
{work}"""

# --- debate rounds 2+ (shared by design and review) ---------------------------

BRAIN_REVISE_PROMPT = """\
You are the {lens} on a two-person team, deliberation round {round} of {max_rounds}.
Below is your prior position, the other specialist's prior position, and the
team's last synthesis. Revise your position: converge where the disagreement
was resolvable, hold your ground where it was not, and be concrete.

{context}

Your prior position:
{own}

The other specialist's prior position:
{other}

Last synthesis:
{critique}"""

# --- decider: intermediate rounds vs the final round (shared) -----------------

SYNTHESIS_PROMPT = """\
Two specialists considered this task and reached these positions.

POSITION A ({lens_a}):
{position_a}

POSITION B ({lens_b}):
{position_b}

Synthesize where they agree and disagree, and name what is still unresolved —
the critique the team will use to converge next round. Do NOT make the final
{call} yet."""

FINAL_PROMPT = """\
The two specialists deliberated for {rounds} rounds. Their final positions:

POSITION A ({lens_a}):
{position_a}

POSITION B ({lens_b}):
{position_b}

This is the FINAL round — issue the single, final {call}: the recommendation,
what they converged on, where they still disagree and which side you take, and
the residual risk. Be decisive."""


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
