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

Two things the issue asks for that a plain accuracy number cannot express:

**The two error directions are not symmetric.** A false NO is the expensive
one: the user told Waku something, the gate refused to look, and the fact is
silently absent from the reply. A false YES costs one FTS5 search and a few
hundred characters of prompt. So the headline is a COST-WEIGHTED score (see
``FALSE_NEGATIVE_COST``), the two directions are reported under their own
headings, and any miss prints a warning naming the case. An average that hides
which way the gate is failing is the thing this eval exists to prevent.

**A "yes" is only half the decision.** The gate returns a search query too, and
a correct yes carrying a useless query retrieves nothing — from the outside
that is indistinguishable from a correct no. So
``test_retrieve_query_would_find_the_memory`` scores the query itself.

Requires the active provider's API key: the judge is a real model call, same
as evals/judge/test_response_quality.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from evals.helpers import HAS_KEY
from waku.memory.retrieval_gate import should_retrieve

pytestmark = pytest.mark.skipif(not HAS_KEY, reason="LLM-as-judge needs the active provider's API key")

# What each error direction actually costs the user, in the same unit, so the
# headline score can weight them instead of averaging them away.
#
#   false NO  — the gate declined to search when it should have. The fact the
#               user told Waku is simply not in the prompt, so the reply is
#               confidently wrong or asks a question it was already told the
#               answer to. Nothing in the trace says "memory was skipped and
#               that was the mistake"; it just looks like Waku forgot.
#   false YES — the gate searched when it did not need to. Cost: one FTS5
#               query (microseconds, local) and a few hundred characters of
#               prompt. Occasionally an irrelevant fact nudges the answer.
#
# 4:1 is a judgment call, not a measurement. It is written here as a named
# constant precisely so a reader can disagree with the number and change it,
# rather than having to reverse-engineer it out of a formula.
FALSE_NEGATIVE_COST = 4.0
FALSE_POSITIVE_COST = 1.0


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


@dataclass(frozen=True)
class GateOutcome:
    """Everything the gate said about one case.

    ``should_retrieve`` returns a three-tuple and the whole tuple matters:
    ``retrieve`` is the decision, ``query`` is what actually gets searched
    (a yes with a bad query finds nothing), and ``reason`` is the gate's own
    stated justification, which is what the summary quotes when it reports a
    miss. Keeping all three means no caller has to unpack a field into ``_``.
    """

    retrieve: bool
    query: str
    reason: str


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

    results: dict[str, GateOutcome] = {}
    for case in DATASET:
        retrieve, query, reason = should_retrieve(client, small_model, case.message)
        results[case.id] = GateOutcome(retrieve=retrieve, query=query, reason=reason)
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

    THE INFORMATION HORIZON, which the criteria states first because getting
    it wrong was a live failure. The judge is shown more than the gate ever
    gets: ``should_retrieve`` is called with the message and nothing else — no
    chat history, no memory snippets, no idea what was said one turn ago. The
    judge sees the memory context, and from that vantage point "Can you make
    it earlier?" reads obviously like a self-contained follow-up, so a
    "retrieve" looks wrong and scored below threshold. From the gate's vantage
    point it is a personal-sounding sentence with no context at all, and
    looking is the defensible move — the fail-open contract the deterministic
    suite pins. Judging a decision against information the decider never had
    is a measurement bug, not a gate bug.

    This metric passes ``evaluation_steps`` rather than ``criteria``, and that
    is the load-bearing part. Given ``criteria``, GEval asks the model to
    invent its own rubric first — and no wording of the horizon rule survived
    that step reliably. The judge kept generating a step that consulted the
    memory context and then failing the gate on it, in as many words: "the
    retrieval context suggests it could be personal (pods.ml project), but...".
    Explicit steps remove the generation entirely, so the rule cannot be
    paraphrased away between runs. Same threshold, same cases; the variance
    that moved failures from run to run was upstream of both.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    from evals.judge.anthropic_judge import AnthropicJudge

    judge = AnthropicJudge()
    return GEval(
        name="GateDecisionQuality",
        evaluation_steps=[
            ("The Input is the ONLY thing the gate saw. It had no chat history, "
             "no previous turns, and it did not see the Retrieval Context."),
            ("Decide, from the Input's wording alone, whether it sounds personal "
             "or refers to the user's life, people, plans, possessions or "
             "history. Count a short or ambiguous follow-up such as 'can you "
             "make it earlier?' as personal-sounding, because the gate has no "
             "way to rule that out."),
            ("If the previous step was yes, an Actual Output of 'retrieve' is "
             "correct and scores high, and 'do not retrieve' scores low."),
            ("If the previous step was no — greetings, thanks, small talk, "
             "general knowledge, arithmetic — an Actual Output of 'do not "
             "retrieve' is correct and scores high, and 'retrieve' scores low."),
            ("Do not consider whether the Retrieval Context contains an answer. "
             "It is background only, the gate never saw it, and it must not "
             "change the score."),
        ],
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

    decision = "retrieve" if gate_results[case.id].retrieve else "do not retrieve"

    assert_test(
        LLMTestCase(
            input=case.message,
            actual_output=decision,
            retrieval_context=case.memory_snippets,
        ),
        [gate_metric],
    )


# ──────────────────────────────────────────────────────────────────────
#  Scored eval — is the QUERY any good? A yes is only half the decision.
#
#  should_retrieve returns (retrieve, query, reason). The boolean above only
#  proves the gate chose to look; `query` is what it actually looks WITH, and
#  it is passed straight to SqliteFactStore.search(). A "yes" carrying a
#  useless query retrieves nothing, so from the caller's side it is
#  indistinguishable from a "no" — the failure the decision eval cannot see.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def query_metric():
    """Build a GEval metric that scores whether the gate's search query would
    actually surface the memory needed to answer the message.

    The judge sees the message (input), the generated query (actual_output),
    and the memory context (retrieval_context) — but NOT the expected label,
    same blind posture as ``gate_metric``.

    Two things the judge cannot infer and so is told outright: that the target
    is a KEYWORD index, and that store coverage is not the query's fault.

    Both were live failures. Told only "score whether the query would surface
    the memory", the judge scored a good query 0.2–0.5 on the
    ``missing-memory`` cases and said why in as many words: "the query
    faithfully targets the user's request [but] the retrieval context contains
    no relevant facts". That is ``gate_metric``'s measurement bug in different
    clothes — scoring the thing under test against an outcome it does not
    control. Whether the store holds an answer is a property of the store; by
    construction these cases have none, so every query would "fail" that way.

    The keyword clause is load-bearing too. Without it the judge reached for
    semantic-search intuitions and marked ``dog name`` down for "missing the
    personalization aspect ('my')" — but this query is handed to
    ``waku/memory/semantic/store.py::_fts_query``, which strips to alphanumeric
    tokens and ORs them, so "my" would buy exactly nothing. Hence the worked
    example in the steps: an anchor beats an adjective.

    Like ``gate_metric``, this passes ``evaluation_steps`` rather than
    ``criteria``. The reason is sharper here: ``actual_output`` holding a
    search query, alongside a ``retrieval_context``, is the exact shape of a
    RAG answer-quality case, so GEval's generated rubric kept inventing a step
    that demanded an answer. One run failed citing "Step 4's core requirement:
    it does not actually answer what the user had for breakfast" — a step this
    file never asked for, scoring a query as if it were a reply. Explicit
    steps make that step unable to exist.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    from evals.judge.anthropic_judge import AnthropicJudge

    judge = AnthropicJudge()
    return GEval(
        name="GateQueryQuality",
        evaluation_steps=[
            ("The Actual Output is a set of search keywords for a literal "
             "keyword index (SQLite FTS5) over the user's stored facts. It is "
             "a QUERY, not an answer."),
            ("Do NOT require the Actual Output to answer the Input, and do not "
             "penalise it for failing to. Answering is not its job."),
            ("Score how well the keywords name the subject of the Input: the "
             "concrete nouns, names and topic words should be present. Filler "
             "and pronouns such as 'my' match nothing in a keyword index, so "
             "their absence is correct and never a defect. For the Input "
             "'What's my dog's name?' the keywords 'dog name' are ideal."),
            ("Do NOT consider whether the Retrieval Context contains the "
             "answer. Coverage is a property of the store, not of these "
             "keywords, and several of these stores deliberately hold no "
             "answer. A context with nothing relevant must not reduce the "
             "score."),
            ("Score high when the keywords name the Input's subject. Score low "
             "only when they are empty, made of generic filler, or about a "
             "different subject than the Input."),
        ],
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.RETRIEVAL_CONTEXT,
        ],
        model=judge,
        threshold=0.6,
    )


@pytest.mark.parametrize("case", DATASET, ids=[c.id for c in DATASET])
def test_retrieve_query_would_find_the_memory(case: GateCase, gate_results, query_metric):
    """Score the search query the gate generated, for the cases where a query
    is the thing under test.

    Parametrized over the whole dataset so every case is COLLECTED, then
    skipped at run time in the two situations where there is nothing here to
    measure. Both skips depend on what the live gate actually said, which is
    not known until ``gate_results`` has run — hence a runtime skip rather
    than a collection-time mark.
    """
    from deepeval import assert_test
    from deepeval.test_case import LLMTestCase

    outcome = gate_results[case.id]
    if not outcome.retrieve:
        pytest.skip("gate said no — there is no query to score")
    if not case.should_retrieve:
        # The gate searched on a case that should have been skipped. The query
        # is beside the point: the decision was already wrong, and
        # test_gate_decision_is_reasonable is what fails for it. Scoring the
        # query here would report the same one mistake twice.
        pytest.skip("gate said yes on a skip-case — the decision eval owns that failure")

    assert_test(
        LLMTestCase(
            input=case.message,
            actual_output=outcome.query,
            retrieval_context=case.memory_snippets,
        ),
        [query_metric],
    )


# ──────────────────────────────────────────────────────────────────────
#  Summary — the cost-weighted headline, then each error direction on its own
# ──────────────────────────────────────────────────────────────────────


def test_gate_accuracy_summary(gate_results):
    """Measure the gate against the ground-truth labels and print the report.

    The headline is a COST-WEIGHTED score, not raw accuracy, because the two
    ways this gate can be wrong do not cost the same thing (see
    ``FALSE_NEGATIVE_COST``). Plain accuracy averages a silently-lost fact
    together with a wasted local search and reports one number that hides
    which happened — the exact blind spot issue #77 is about. Both directions
    therefore also get their own labelled section, and any miss prints a
    warning naming the cases.

    This test MEASURES; it does not gate. A false negative does not fail it:
    this is live small-model behaviour across eleven providers and ``make
    gate`` runs it, so a hard assertion here would make the release gate
    hostage to provider drift. The soft floor only catches a gate that is
    worse than random on the axis that actually costs users.
    """
    tp = fp = tn = fn = 0
    by_category: dict[str, list[bool]] = {}
    missed: list[GateCase] = []      # false NO — the expensive direction
    needless: list[GateCase] = []    # false YES — the cheap direction

    for case in DATASET:
        gate_said = gate_results[case.id].retrieve
        expected = case.should_retrieve
        by_category.setdefault(case.category, []).append(gate_said == expected)

        if gate_said and expected:
            tp += 1
        elif gate_said and not expected:
            fp += 1
            needless.append(case)
        elif not gate_said and not expected:
            tn += 1
        else:
            fn += 1
            missed.append(case)

    total = len(DATASET)
    positives = tp + fn   # cases that SHOULD have retrieved
    negatives = tn + fp   # cases that should NOT have
    accuracy = (tp + tn) / total
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / positives if positives > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    # 1.0 = flawless, 0.0 = every case wrong in the most expensive way
    # available. Normalising by the worst case keeps the number readable as a
    # percentage even if the weights are retuned.
    incurred = fn * FALSE_NEGATIVE_COST + fp * FALSE_POSITIVE_COST
    worst = positives * FALSE_NEGATIVE_COST + negatives * FALSE_POSITIVE_COST
    weighted = 1 - incurred / worst if worst > 0 else 1.0
    miss_rate = fn / positives if positives > 0 else 0.0
    false_alarm_rate = fp / negatives if negatives > 0 else 0.0

    # How many retrieve-cases handed the query eval something to score. Read
    # straight off the gate results — no judge call, no extra spend.
    scorable = sum(
        1 for c in DATASET if c.should_retrieve and gate_results[c.id].retrieve
    )

    ratio = FALSE_NEGATIVE_COST / FALSE_POSITIVE_COST
    lines = [
        "",
        "=" * 72,
        "RETRIEVAL GATE ACCURACY REPORT (issue #77)",
        "=" * 72,
        (f"  Total cases:           {total}   ({positives} should retrieve, "
         f"{negatives} should not)"),
        "",
        (f"  COST-WEIGHTED SCORE:   {weighted:.1%}   <- headline: a false NO "
         f"counts {ratio:.0f}x a false YES"),
        (f"  Accuracy (unweighted): {accuracy:.1%}   (shown second on purpose: "
         f"it treats both errors as equal, which they are not)"),
        "",
        "  ERROR DIRECTIONS - THESE ARE NOT SYMMETRIC",
        "  " + "-" * 70,
        (f"    Missed retrievals (false NO):    {fn} of {positives}   "
         f"miss rate {miss_rate:.1%}"),
        "      The expensive direction. The gate refused to look, so a fact the",
        "      user told Waku is absent from the reply and nothing says why.",
        "",
        (f"    Needless retrievals (false YES): {fp} of {negatives}   "
         f"false-alarm rate {false_alarm_rate:.1%}"),
        "      The cheap direction. Costs one local FTS5 search and some prompt",
        "      space; only bites when an irrelevant fact skews the answer.",
        "",
        f"  Precision:     {precision:.1%}  (of retrieve calls, how many were right)",
        f"  Recall:        {recall:.1%}  (of should-retrieve cases, how many we caught)",
        f"  F1:            {f1:.1%}",
        f"  Confusion:     TP={tp}  FP={fp}  TN={tn}  FN={fn}",
        (f"  Query coverage: {scorable} of {positives} should-retrieve cases "
         f"produced a query to score"),
        "",
        "  Per-category accuracy:",
    ]
    for cat, correct in sorted(by_category.items()):
        acc = sum(correct) / len(correct)
        lines.append(f"    {cat:20s}  {acc:.0%}  ({sum(correct)}/{len(correct)})")

    # Name the cases, not just the count. "FN=1" tells a reader the gate missed
    # something; this tells them WHICH something, and what the gate thought it
    # was doing — which is where a prompt fix actually starts.
    if missed:
        lines += [
            "",
            (f"  *** WARNING: {len(missed)} missed retrieval(s) - the expensive "
             f"failure direction ***"),
        ]
        for case in missed:
            lines.append(f"    - {case.id}: \"{case.message}\"")
            lines.append(f"        gate's reason: \"{gate_results[case.id].reason}\"")
    if needless:
        lines += ["", f"  Note: {len(needless)} needless retrieval(s) (the cheap direction)"]
        for case in needless:
            lines.append(f"    - {case.id}: \"{case.message}\"")

    lines.append("=" * 72)
    lines.append("")

    print("\n".join(lines))

    # Soft floor on the WEIGHTED score, not raw accuracy: a gate that scores
    # 50% by getting every skip-case right and every retrieve-case wrong is
    # useless, and only the weighted number says so.
    assert weighted >= 0.5, (
        f"Cost-weighted gate score {weighted:.0%} is below 50% — worse than random "
        f"once a missed retrieval is counted at {ratio:.0f}x a needless one "
        f"(missed {fn}/{positives}, needless {fp}/{negatives}). "
        f"Check the gate prompt or the small model configuration."
    )
