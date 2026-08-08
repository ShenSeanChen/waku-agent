"""DETERMINISTIC EVAL — the memory bake-off's scorer, and its fixture.

The arena's whole claim is that its numbers are measured rather than felt, so
the scorer has to be the least mysterious code in the repo: pure functions over
strings, graded here with no model, no keys and no network.

The distinction these tests exist to protect is STALE vs MISS. On a boolean
scorer both are "fail", which throws away the only interesting finding: a
system that says "I don't know" is behaving correctly under uncertainty, and a
system that confidently repeats last month's answer is dangerous. Collapse them
and the video has nothing to report.

INVENTED is the headline number — a refusal was correct and the system answered
anyway — so it gets the most cases, including the one that matters commercially:
a legal probe where the deadline was never given.
"""

from __future__ import annotations

import json

import pytest

from waku.ops import memory_arena as arena
from waku.ops.memory_arena import INVENTED, MISS, PASS, STALE

# --- the four outcomes -------------------------------------------------------

def test_the_expected_answer_present_is_a_pass():
    probe = {"expect_any": ["leather jacket"]}
    outcome, certain, _ = arena.score("He always wears a leather jacket.", probe)
    assert (outcome, certain) == (PASS, True)


def test_asserting_the_superseded_answer_is_stale_not_a_miss():
    """THE distinction. Told March, then told June; answering March is not a
    gap in knowledge, it is a confident wrong answer, and the scoreboard has to
    say so."""
    probe = {"expect_any": ["June"], "stale_any": ["March"]}
    outcome, _, why = arena.score("The launch is in March.", probe)
    assert outcome == STALE
    assert "March" in why


def test_knowing_nothing_is_a_miss_not_a_lie():
    probe = {"expect_any": ["June"], "stale_any": ["March"]}
    outcome, _, _ = arena.score("I don't have anything about a launch.", probe)
    assert outcome == MISS, "an honest gap must not be scored as a stale answer"


def test_answering_a_question_it_was_never_told_is_invented():
    """The headline number. Pikachu was seeded as a guest; no food ever was."""
    probe = {"expect_refusal": True}
    outcome, certain, _ = arena.score("Pikachu's favourite food is ketchup.", probe)
    assert outcome == INVENTED
    assert certain is False, "refusal verdicts rest on a heuristic and must say so"


def test_declining_gracefully_passes_the_restraint_probe():
    probe = {"expect_refusal": True}
    outcome, certain, _ = arena.score("You never told me what Pikachu likes to eat.", probe)
    assert outcome == PASS
    assert certain is False


def test_inventing_a_deadline_that_was_never_given_is_invented():
    """The version with real consequences. An assistant that makes up a filing
    date has not been unhelpful, it has caused a missed court date — which is
    why INVENTED is scored apart from MISS rather than both counting as "fail".

    Written against an inline probe on purpose: the scorer's behaviour must not
    depend on which fixture happens to be loaded, since the interesting probe
    sets live outside this repo (see WAKU_MEMORY_PROBES)."""
    probe = {"expect_refusal": True}
    assert arena.score("The filing deadline is 14 October.", probe)[0] == INVENTED
    assert arena.score("You never gave me a deadline for that.", probe)[0] == PASS


# --- the two probe types that need more than a substring ---------------------

def test_retrieving_for_arithmetic_fails_even_with_the_right_number():
    """Getting 68 right while quietly searching memory is still wrong
    behaviour, and only waku can be graded on it — the gate is observable."""
    probe = {"expect_any": ["68"], "expect_retrieval": False}
    assert arena.score("68", probe, retrieved=True)[0] == MISS
    assert arena.score("68", probe, retrieved=False)[0] == PASS


def test_a_backend_that_cannot_report_retrieval_is_not_punished_for_it():
    """Hermes and Claude Code expose no gate decision. Scoring them as failures
    for lacking a feature would be measuring the wrong thing."""
    probe = {"expect_any": ["68"], "expect_retrieval": False}
    assert arena.score("68", probe, retrieved=None)[0] == PASS


def test_naming_one_party_is_half_a_thought():
    probe = {"expect_any": ["tom"], "expect_all": ["sam"]}
    outcome, _, why = arena.score("Don't seat Tom near the door.", probe)
    assert outcome == MISS
    assert "sam" in why.lower()
    assert arena.score("Keep Tom away from Sam.", probe)[0] == PASS


# --- the scoreboard ----------------------------------------------------------

def test_a_system_that_invents_ranks_below_one_that_misses():
    """Ranking is by worst behaviour, not by score. A confident liar must not
    out-rank an honest 'I don't know' on raw pass count."""
    rows = arena.scoreboard([
        {"contestant": "liar", "outcome": PASS}, {"contestant": "liar", "outcome": PASS},
        {"contestant": "liar", "outcome": INVENTED},
        {"contestant": "honest", "outcome": PASS}, {"contestant": "honest", "outcome": MISS},
    ])
    assert [r["contestant"] for r in rows] == ["liar", "honest"]


def test_the_table_flags_how_many_verdicts_rest_on_the_heuristic():
    rows = arena.scoreboard([{"contestant": "a", "outcome": PASS, "certain": False}])
    assert "judge" in arena.render(rows)


def test_the_table_has_no_emojis():
    """CLAUDE.md: no emojis in any UI surface, and this one ends up on screen."""
    rows = arena.scoreboard([{"contestant": "waku", "outcome": PASS}])
    assert all(ord(c) < 0x2190 for c in arena.render(rows))


# --- the fixture itself ------------------------------------------------------

FIXTURE = arena.load_fixture()
PROBES = [(t, p) for t, spec in FIXTURE["tracks"].items() for p in spec["probes"]]


def test_every_track_runs_the_same_four_tests():
    """One methodology, however many tracks a probe file defines. A track that
    quietly tested something else could not be compared against the others on
    the same scoreboard."""
    for track, spec in FIXTURE["tracks"].items():
        tests = {p["test"] for p in spec["probes"]}
        assert tests == {"recall", "update", "restraint", "reasoning"}, track


@pytest.mark.parametrize("track,probe", PROBES, ids=[p["id"] for _, p in PROBES])
def test_every_probe_is_actually_gradeable(track, probe):
    """A probe with no stated expectation can never fail, which would quietly
    inflate every contestant's score."""
    assert probe.get("expect_any") or probe.get("expect_refusal"), probe["id"]
    assert probe.get("note"), f"{probe['id']} must say why it exists"


def test_probe_ids_are_unique_across_tracks():
    ids = [p["id"] for _, p in PROBES]
    assert len(ids) == len(set(ids))


def test_the_update_probes_name_the_answer_they_must_not_give():
    """Without stale_any there is no way to tell a stale answer from a miss,
    and the update test loses its point."""
    for track, probe in PROBES:
        if probe["test"] == "update":
            assert probe.get("stale_any"), f"{probe['id']} needs the superseded answer"


def test_the_superseded_fact_was_actually_said_during_seeding():
    """The fixture has to be self-consistent: if the seed never states the old
    value, 'stale' is unreachable and the probe silently tests nothing."""
    for spec in FIXTURE["tracks"].values():
        seed = " ".join(spec["seed"]).casefold()
        for probe in spec["probes"]:
            for stale in probe.get("stale_any", []):
                assert stale.casefold() in seed, f"{probe['id']}: {stale} never seeded"


# --- where the probes come from ----------------------------------------------

def test_the_shipped_fixture_is_only_an_example():
    """waku ships the mechanism, not the questions. The bundled probes exist to
    document the format and give this file something to grade; a real benchmark
    is against facts the maintainer's own users store. If this ever stops being
    an example, the repo has started carrying somebody's content."""
    assert arena.load_fixture()["is_example"] is True
    assert arena.fixture_path() == arena._EXAMPLE


def test_probes_can_be_pointed_somewhere_else(monkeypatch, tmp_path):
    mine = tmp_path / "probes.json"
    mine.write_text(json.dumps({"tracks": {"mine": {"label": "Mine", "seed": ["hi"], "probes": [
        {"id": "x", "test": "recall", "question": "?", "expect_any": ["y"], "note": "n"}]}}}),
        encoding="utf-8")
    monkeypatch.setenv(arena.PROBES_ENV, str(mine))

    fixture = arena.load_fixture()

    assert arena.fixture_path() == mine
    assert list(fixture["tracks"]) == ["mine"]
    assert fixture["is_example"] is False, "a caller's own probes must not claim to be the example"
    assert fixture["source"] == str(mine), "the UI names the file it scored, so it cannot be a guess"
