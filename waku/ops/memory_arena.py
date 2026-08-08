"""The Memory Arena — race several MEMORY backends through the same harness.

Sibling of waku/ops/arena.py. That one holds the harness constant and varies
the model; this one holds the harness AND the model constant and varies where
facts live. One dial each, so a result means something.

    seed conversation ──┬─→ waku + FTS5   ──┐
    (8 messages)        ├─→ waku + Mem0   ──┼─→ same 7 probes ─→ one scorer
                        └─→ waku + Zep    ──┘

Two things are deliberately borrowed from the model arena: every contestant
runs in its own throwaway home, so `.waku/` is never opened; and the results
land in their own JSONL, never state.db.

WHY FOUR OUTCOMES INSTEAD OF PASS/FAIL

Pass/fail hides the only interesting question. A system that says "I don't
know" is behaving correctly under uncertainty. A system that confidently
returns last month's answer, or invents one, is dangerous — and both look like
"fail" on a boolean.

    PASS      the expected answer is there
    STALE     the expected answer is missing and a SUPERSEDED one is asserted
              — "the launch is in March" after being told it moved to June
    INVENTED  a refusal was correct and it answered anyway — the fact was never
              given, so whatever it said, it made up
    MISS      the expected answer is missing and nothing wrong was asserted;
              the honest failure

INVENTED is the number the whole exercise exists to produce. On the business
track it is the difference between an unhelpful assistant and a legal agent
handing a client a filing deadline that does not exist.

Scoring lives here as PURE functions over strings so it can be tested offline
with no model, no keys, and no network — the runner below is the part that
costs money, and it is deliberately thin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The shipped fixture is four dull probes whose only job is to document the
# format. The interesting questions are the ones a maintainer brings — a memory
# benchmark is only meaningful against the kind of facts their users store — so
# the file is swappable and waku keeps the mechanism, not the content.
_EXAMPLE = Path(__file__).resolve().parents[2] / "evals" / "memory_arena.json"
PROBES_ENV = "WAKU_MEMORY_PROBES"

PASS, STALE, INVENTED, MISS = "pass", "stale", "invented", "miss"

# How models decline when they genuinely have nothing. Deliberately about
# ABSENCE OF KNOWLEDGE, not politeness — "I'm sorry" also opens plenty of
# confidently wrong answers, so it is not on this list.
#
# This is a heuristic and is treated as one: `score()` reports `certain=False`
# whenever it rests on these, so a run can send exactly those probes to the
# judge instead of grading every probe with a model it doesn't need.
_REFUSALS = (
    "don't know", "do not know", "not sure", "no information", "no record",
    "never told", "never mentioned", "never gave", "never shared",
    "didn't tell", "did not tell", "didn't mention", "did not mention",
    "didn't give", "did not give", "haven't told", "have not told",
    "haven't given", "have not given", "haven't mentioned",
    "you haven't", "you have not", "not in my memory",
    "don't have", "do not have", "nothing about", "no details", "wasn't specified",
    "not specified", "unable to find", "couldn't find", "could not find",
)
# This list will never be complete — models decline in more ways than anyone can
# enumerate, and a missed phrasing scores an honest refusal as INVENTED, which is
# the worst direction to be wrong in. That is exactly what `certain=False` is
# for: every verdict resting on this list is flagged so a judge can settle it.


def fixture_path() -> Path:
    """Where the probes are coming from. WAKU_MEMORY_PROBES wins, so a run can
    be pointed at a real question set without editing anything in the repo."""
    override = os.getenv(PROBES_ENV, "").strip()
    return Path(override).expanduser() if override else _EXAMPLE


def load_fixture(path: Path | None = None) -> dict:
    """The probes, plus where they came from — the UI says so on screen, because
    'which questions was this scored against' is the first thing anyone should
    ask of a benchmark, and the answer must not be a guess."""
    source = path or fixture_path()
    fixture = json.loads(source.read_text(encoding="utf-8"))
    fixture["source"] = str(source)
    fixture["is_example"] = source == _EXAMPLE
    return fixture


def _has(haystack: str, needles) -> bool:
    low = haystack.casefold()
    return any(n.casefold() in low for n in needles)


def score(answer: str, probe: dict, retrieved: bool | None = None) -> tuple[str, bool, str]:
    """Grade one answer. Returns (outcome, certain, why).

    `certain` is False when the verdict rests on the refusal heuristic above —
    those are the probes worth spending a judge call on. Everything else is a
    substring check and needs no model at all.

    `retrieved` is whether the backend went to memory for this probe. Only waku
    reports it (the retrieval gate is observable), so probes that assert on it
    are simply not graded for backends that cannot answer the question — which
    is more honest than scoring them as a failure for lacking a feature.
    """
    answer = answer or ""

    # A probe that asserts on retrieval behaviour: getting the arithmetic right
    # while quietly searching memory is still the wrong behaviour.
    if probe.get("expect_retrieval") is False and retrieved is True:
        return MISS, True, "retrieved memory for a question that needed none"

    if probe.get("expect_refusal"):
        if _has(answer, _REFUSALS):
            return PASS, False, "declined, as it should"
        return INVENTED, False, "answered a question it was never given the answer to"

    expected = probe.get("expect_any") or []
    if expected and not _has(answer, expected):
        stale = probe.get("stale_any") or []
        if stale and _has(answer, stale):
            return STALE, True, f"asserted the superseded answer ({stale[0]})"
        return MISS, True, "expected answer absent"

    # `expect_all` is for multi-hop probes where naming one party is half a
    # thought: "avoid seating Tom next to Sam" needs both names or it hasn't
    # combined anything.
    required = probe.get("expect_all") or []
    if required and not all(_has(answer, [r]) for r in required):
        missing = [r for r in required if not _has(answer, [r])]
        return MISS, True, f"only half the reasoning — missing {missing}"

    if _has(answer, probe.get("stale_any") or []) and not expected:
        return STALE, True, "asserted a superseded answer"

    return PASS, True, "correct"


def scoreboard(results: list[dict]) -> list[dict]:
    """Per-contestant tallies, worst-behaviour-first so the interesting column
    is not buried: a system that invents answers ranks below one that misses."""
    by_name: dict[str, dict] = {}
    for r in results:
        row = by_name.setdefault(
            r["contestant"],
            {"contestant": r["contestant"], PASS: 0, STALE: 0, INVENTED: 0, MISS: 0,
             "tokens": 0, "probes": 0, "needs_judge": 0},
        )
        row[r["outcome"]] += 1
        row["tokens"] += r.get("tokens", 0)
        row["probes"] += 1
        row["needs_judge"] += 0 if r.get("certain", True) else 1
    return sorted(
        by_name.values(),
        key=lambda r: (-r[INVENTED], -r[STALE], -r[MISS], -r[PASS]),
    )


def render(rows: list[dict]) -> str:
    """The table, for a terminal and for a thumbnail. No emojis (CLAUDE.md)."""
    head = f"{'contestant':<24}{'pass':>6}{'stale':>7}{'invented':>10}{'miss':>6}{'tokens':>9}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['contestant']:<24}{r[PASS]:>6}{r[STALE]:>7}{r[INVENTED]:>10}"
            f"{r[MISS]:>6}{r['tokens']:>9}"
        )
    unsure = sum(r["needs_judge"] for r in rows)
    if unsure:
        lines.append(f"\n{unsure} verdict(s) rest on the refusal heuristic — worth a judge pass.")
    return "\n".join(lines)
