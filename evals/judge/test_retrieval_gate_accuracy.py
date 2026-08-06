"""LLM-AS-JUDGE EVAL — does the retrieval gate decide correctly?

The deterministic suite (evals/deterministic/test_retrieval_gate.py) pins the
plumbing: JSON parsing, fail-open posture, exactly-one-call. None of those
tests ask whether a "yes" was the *right answer*. This eval does.

We present the gate with messages paired with a memory context, each labeled
``should_retrieve: true | false``. A judge model scores whether the gate's
decision was *reasonable* — not whether it matches the label, but whether a
thoughtful observer would defend it. Covers the four cases from issue #77:

  1. Chitchat with a full memory store — should NOT retrieve
  2. A direct question about a stored fact — SHOULD retrieve
  3. A follow-up whose referent is only in history, not memory — should NOT
  4. A question whose answer memory does not contain — SHOULD retrieve

The judge does NOT see the expected label — it independently evaluates whether
the gate's decision was defensible given the message and what's in memory.
This is a quality-scored eval (DeepEval GEval, 0–1 with threshold), not a 0/1
assertion — the difference matters, see CLAUDE.md.

A summary test at the end reports accuracy, precision, recall, and F1 against
the ground-truth labels, so the gate's performance is measurable over time.

Requires the active provider's API key: the judge is a real model call, same
as evals/judge/test_response_quality.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from evals.helpers import HAS_KEY
from waku.memory.retrieval_gate import should_retrieve

pytestmark = pytest.mark.skipif(not HAS_KEY, reason="LLM-as-judge needs the active provider's API key")


# ──────────────────────────────────────────────────────────────────────
#  Dataset — each case is a message + the memory that exists at the time,
#  labeled with the correct gate decision. Hand-curated to cover the four
#  categories from issue #77.
#
#  `memory_snippets` is shown to the JUDGE so it can reason about whether
#  retrieval would help. The gate itself never sees memory — it decides
#  from the message alone — so the snippets are not passed to should_retrieve.
#  `should_retrieve` is the ground-truth label for the summary metrics and
#  is NEVER shown to the judge.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GateCase:
    """A single labeled retrieval-gate test case.

    ``message`` is what the user says. ``memory_snippets`` is a short list of
    facts that exist in the store at the time — enough for the judge to
    reason about whether retrieval would help. ``should_retrieve`` is the
    ground-truth label used for the summary metrics, NOT shown to the judge.
    """

    id: str
    category: str
    message: str
    memory_snippets: list[str]
    should_retrieve: bool


DATASET: list[GateCase] = [
    # ── Category 1: Chitchat — should NOT retrieve
    GateCase(
        id="chitchat-1",
        category="chitchat",
        message="Hey, how's it going?",
        memory_snippets=[
            "User works at a startup called AlgoWars",
            "User prefers morning meetings",
            "User has a dog named Rex",
            "User is based in Bangalore",
        ],
        should_retrieve=False,
    ),
    GateCase(
        id="chitchat-2",
        category="chitchat",
        message="That's awesome lol",
        memory_snippets=[
            "User likes Rust and TypeScript",
            "User is building pods.ml",
            "User had coffee with Alex last Tuesday",
        ],
        should_retrieve=False,
    ),
    GateCase(
        id="chitchat-3",
        category="chitchat",
        message="Thanks! Appreciate it",
        memory_snippets=[
            "User's birthday is March 15",
            "User prefers concise responses",
            "User is 19 years old",
        ],
        should_retrieve=False,
    ),

    # ── Category 2: Direct question about a stored fact — SHOULD retrieve
    GateCase(
        id="direct-fact-1",
        category="direct-fact",
        message="When is my meeting with Alex?",
        memory_snippets=[
            "Meeting with Alex scheduled for Friday at 10am",
            "Alex prefers morning meetings",
        ],
        should_retrieve=True,
    ),
    GateCase(
        id="direct-fact-2",
        category="direct-fact",
        message="What's my dog's name?",
        memory_snippets=[
            "User has a dog named Rex",
            "User is based in Bangalore",
        ],
        should_retrieve=True,
    ),
    GateCase(
        id="direct-fact-3",
        category="direct-fact",
        message="Which language do I prefer for backend work?",
        memory_snippets=[
            "User prefers Rust for systems programming",
            "User uses TypeScript for frontend",
            "User is building AlgoWars",
        ],
        should_retrieve=True,
    ),

    # ── Category 3: Follow-up whose referent is in chat history, not memory
    #
    # The user is continuing a conversation — the context is in the chat
    # history (which the gate does not see), not in the memory store. The
    # gate should NOT retrieve — the answer is in the conversation, not memory.
    GateCase(
        id="followup-history-1",
        category="followup-history",
        message="Can you make it earlier?",
        memory_snippets=[
            "User prefers morning meetings",
            "User has a dog named Rex",
        ],
        should_retrieve=False,
    ),
    GateCase(
        id="followup-history-2",
        category="followup-history",
        message="Yeah that one, tell me more about it",
        memory_snippets=[
            "User is building pods.ml",
            "User likes Rust",
        ],
        should_retrieve=False,
    ),
    GateCase(
        id="followup-history-3",
        category="followup-history",
        message="What about the second option?",
        memory_snippets=[
            "User is based in Bangalore",
            "User's birthday is March 15",
        ],
        should_retrieve=False,
    ),

    # ── Category 4: A question whose answer memory does not contain
    #
    # The user asks something personal that Waku has no memory of. The gate
    # SHOULD still retrieve — the gate's job is to decide "does this need
    # memory?", not "will memory succeed?". A personal question always
    # warrants a search.
    GateCase(
        id="missing-memory-1",
        category="missing-memory",
        message="What did I have for breakfast yesterday?",
        memory_snippets=[
            "User is based in Bangalore",
            "User prefers morning meetings",
        ],
        should_retrieve=True,
    ),
    GateCase(
        id="missing-memory-2",
        category="missing-memory",
        message="Who was at the party last Saturday?",
        memory_snippets=[
            "User has a dog named Rex",
            "User works at AlgoWars",
        ],
        should_retrieve=True,
    ),
    GateCase(
        id="missing-memory-3",
        category="missing-memory",
        message="What's my friend Sarah's phone number?",
        memory_snippets=[
            "User prefers Rust",
            "User is 19 years old",
        ],
        should_retrieve=True,
    ),
]


# ──────────────────────────────────────────────────────────────────────
#  Gate runner — calls the real retrieval gate once per case, cached
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gate_results():
    """Run the gate on every case once, share the results across tests.

    Module scope means 12 small-model calls total, not 12-per-test.
    The gate is stateless (it decides from the message alone), so
    caching is safe.
    """
    from waku.config import load_settings
    from waku.loop.models import PROVIDERS, get_client

    settings = load_settings()
    client = get_client(settings)
    # Fall back to the provider's default small model when WAKU_SMALL_MODEL
    # is unset — the gate needs a concrete model id to call.
    small_model = settings.small_model or PROVIDERS[settings.provider].small_model

    results: dict[str, bool] = {}
    for case in DATASET:
        retrieve, _query, _reason = should_retrieve(client, small_model, case.message)
        results[case.id] = retrieve
    return results


# ──────────────────────────────────────────────────────────────────────
#  Scored eval — DeepEval GEval scores the gate's decision quality
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gate_metric():
    """Build a GEval metric that scores whether the gate's decision was
    reasonable, given the user's message and what's in memory.

    The judge sees the message (input), the gate's decision (actual_output),
    and the memory context (retrieval_context) — but NOT the expected label.
    It scores 0–1 whether the decision was defensible.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    from evals.judge.anthropic_judge import AnthropicJudge

    judge = AnthropicJudge()
    return GEval(
        name="GateDecisionQuality",
        criteria=(
            "Given the user's message and the memories currently in the store, "
            "evaluate whether the assistant's retrieval decision was reasonable. "
            "A decision to RETRIEVE is correct when the message references the "
            "user's personal life, people, plans, or history. A decision NOT to "
            "retrieve is correct when the message is chitchat, general knowledge, "
            "or a self-contained follow-up whose context is in the conversation, "
            "not in memory. The gate's job is to decide whether to SEARCH, not "
            "whether the search will succeed — so a personal question that memory "
            "doesn't have the answer to should still be a retrieve."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.RETRIEVAL_CONTEXT,
        ],
        model=judge,
        threshold=0.6,
    )


@pytest.mark.parametrize("case", DATASET, ids=[c.id for c in DATASET])
def test_gate_decision_is_reasonable(case: GateCase, gate_results, gate_metric):
    """For each labeled case, score the gate's decision quality with a judge
    model. The judge sees the message, the memory context, and the gate's
    decision — but NOT the expected label — and scores whether the decision
    was defensible (0–1, threshold 0.6).
    """
    from deepeval import assert_test
    from deepeval.test_case import LLMTestCase

    gate_said = gate_results[case.id]
    decision = "retrieve" if gate_said else "do not retrieve"

    assert_test(
        LLMTestCase(
            input=case.message,
            actual_output=decision,
            retrieval_context=case.memory_snippets,
        ),
        [gate_metric],
    )


# ──────────────────────────────────────────────────────────────────────
#  Summary — accuracy, precision, recall, F1 against ground-truth labels
# ──────────────────────────────────────────────────────────────────────


def test_gate_accuracy_summary(gate_results):
    """Run the gate on every case and report accuracy, precision, recall, and
    F1 against the ground-truth labels.

    This test does NOT assert a high threshold — the point is to MEASURE, not
    to pass/fail. The numbers are printed to stdout so ``make eval-judge``
    surfaces them, and the per-category breakdown shows where the gate is
    strong or weak.

    A soft floor of 50% catches a fundamentally broken gate (worse than random)
    without gating on a bar the gate hasn't been tuned to meet.
    """
    tp = fp = tn = fn = 0
    by_category: dict[str, list[bool]] = {}

    for case in DATASET:
        gate_said = gate_results[case.id]
        expected = case.should_retrieve
        correct = gate_said == expected
        by_category.setdefault(case.category, []).append(correct)

        if gate_said and expected:
            tp += 1
        elif gate_said and not expected:
            fp += 1
        elif not gate_said and not expected:
            tn += 1
        else:
            fn += 1

    total = len(DATASET)
    accuracy = (tp + tn) / total
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    lines = [
        "",
        "=" * 60,
        "RETRIEVAL GATE ACCURACY REPORT (issue #77)",
        "=" * 60,
        f"  Total cases:   {total}",
        f"  Accuracy:      {accuracy:.1%}",
        f"  Precision:     {precision:.1%}  (of retrieve calls, how many were right)",
        f"  Recall:        {recall:.1%}  (of should-retrieve cases, how many we caught)",
        f"  F1:            {f1:.1%}",
        f"  Confusion:     TP={tp}  FP={fp}  TN={tn}  FN={fn}",
        "",
        "  Per-category accuracy:",
    ]
    for cat, correct in sorted(by_category.items()):
        acc = sum(correct) / len(correct)
        lines.append(f"    {cat:20s}  {acc:.0%}  ({sum(correct)}/{len(correct)})")
    lines.append("=" * 60)
    lines.append("")

    print("\n".join(lines))

    # Soft floor — worse than random means the gate is fundamentally broken.
    assert accuracy >= 0.5, (
        f"Gate accuracy {accuracy:.0%} is below 50% — worse than random. "
        f"Check the gate prompt or the small model configuration."
    )
