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


def test_token_counting_reads_the_keys_the_ledger_actually_writes(tmp_path):
    """This shipped broken and reported 0 tokens for every probe with no error:
    _tokens() read "input_tokens"/"output_tokens" while usage.jsonl writes
    "in"/"out", and `.get(name, 0)` turned the wrong name into a plausible
    number. The line below is copied from a real ledger — if the writer's schema
    ever moves, this fails instead of quietly costing nothing."""
    (tmp_path / "usage.jsonl").write_text(
        '{"ts": "2026-08-08T23:41:43.306+00:00", "provider": "anthropic", '
        '"model": "claude-fable-5", "kind": "loop", "in": 3164, "out": 94}\n'
        '{"ts": "2026-08-08T23:41:48.028+00:00", "provider": "anthropic", '
        '"model": "claude-fable-5", "kind": "loop", "in": 3241, "out": 92}\n',
        encoding="utf-8")

    tokens, calls = arena._ledger(tmp_path)
    assert tokens == 3164 + 94 + 3241 + 92
    # One ROW is one API call. The grid reports this so "why did one question
    # cost 4,783 tokens" is answered by a count, not by my inference.
    assert calls == 2


def test_the_arena_and_the_ledger_writer_agree_on_field_names():
    """Pin the two halves together. _tokens() cannot be verified by its own
    fixture alone — the fixture would just encode whatever mistake was made."""
    import inspect

    from waku.ops import tracing

    writer = inspect.getsource(tracing)
    assert 'usage.jsonl' in writer, "the ledger moved — find its new writer"
    assert '"in":' in writer and '"out":' in writer, (
        "the usage ledger no longer writes 'in'/'out' — _tokens() must follow it"
    )


def test_a_ledger_that_is_missing_is_zero_not_a_crash(tmp_path):
    assert arena._ledger(tmp_path) == (0, 0)


# --- the runner --------------------------------------------------------------
# run_arena() spends real money, so it was only ever verified by running it and
# looking — which is exactly the gap that let the token bug ship. These drive
# the whole seed -> probe -> score path with a fake Waku: no model, no keys, no
# network, and no spend.

class _FakeWaku:
    """Records what it was told, answers what the script says, and writes the
    same usage.jsonl the real app does — so token accounting is exercised too."""

    def __init__(self, settings=None, **kw):
        self.settings = settings
        self.seen: list[str] = []
        self.home = settings.home
        self.home.mkdir(parents=True, exist_ok=True)
        self._answers = dict(_FakeWaku.script)

    def respond(self, message, source="cli", observer=None, **kw):
        from types import SimpleNamespace
        self.seen.append(message)
        # every turn costs something, so a per-probe delta is observable
        with (self.home / "usage.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"in": 100, "out": 10}) + "\n")
        if observer:
            observer("gate", {"decision": _FakeWaku.gate})
        return SimpleNamespace(reply=self._answers.get(message, "I don't know."))


def _arena_fixture(tmp_path):
    fx = {"tracks": {"t": {"label": "T",
        "seed": ["fact one", "fact two"],
        "probes": [
            {"id": "p-recall", "test": "recall", "question": "q1?",
             "expect_any": ["alpha"], "note": "n"},
            {"id": "p-update", "test": "update", "question": "q2?",
             "expect_any": ["new"], "stale_any": ["old"], "note": "n"},
        ]}}}
    return fx


def _run(monkeypatch, tmp_path, script, gate=False, backends=("sqlite",)):
    import waku.app
    _FakeWaku.script = script
    _FakeWaku.gate = gate
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    events = []
    arena.run_arena(list(backends), "t", lambda k, e: events.append((k, e)),
                    fixture=_arena_fixture(tmp_path))
    return events


def test_the_runner_seeds_every_line_then_asks_every_probe(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "the new one"})
    seeded = [e["line"] for k, e in events if k == "seeded"]
    probes = [e["probe"] for k, e in events if k == "probe"]
    assert seeded == ["fact one", "fact two"], "the conversation must go in, in order"
    assert probes == ["p-recall", "p-update"]


def test_the_runner_scores_what_came_back(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "still the old one"})
    outcomes = {e["probe"]: e["outcome"] for k, e in events if k == "probe"}
    assert outcomes == {"p-recall": PASS, "p-update": STALE}


def test_tokens_are_per_probe_not_a_running_total(monkeypatch, tmp_path):
    """THE regression. The ledger is cumulative; storing the total on each row
    made scoreboard() sum a triangular number and report several times the
    tokens actually spent — wrong in a way that still looks plausible."""
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "the new one"})
    tokens = [e["tokens"] for k, e in events if k == "probe"]
    assert tokens == [110, 110], f"each probe costs one turn, got {tokens}"


def test_a_backend_that_blows_up_does_not_take_the_others_with_it(monkeypatch, tmp_path):
    """A missing key or a service outage is a fact about that contestant, not a
    reason to lose everyone else's results."""
    import waku.app
    _FakeWaku.script = {"q1?": "alpha", "q2?": "the new one"}
    _FakeWaku.gate = False
    real_init = _FakeWaku.__init__

    def explode(self, settings=None, **kw):
        if settings.semantic_store == "boom":
            raise RuntimeError("no api key")
        real_init(self, settings=settings, **kw)

    monkeypatch.setattr(_FakeWaku, "__init__", explode)
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    events = []
    arena.run_arena(["boom", "sqlite"], "t", lambda k, e: events.append((k, e)),
                    fixture=_arena_fixture(tmp_path))

    assert [e["contestant"] for k, e in events if k == "failed"] == ["boom"]
    assert {e["contestant"] for k, e in events if k == "probe"} == {"sqlite"}
    board = next(e for k, e in events if k == "done")["scoreboard"]
    assert [row["contestant"] for row in board] == ["sqlite"]


def test_the_gate_decision_reaches_the_scorer(monkeypatch, tmp_path):
    """Only waku can be graded on 'did you search memory for arithmetic', and
    that only works if the observer's gate event actually gets through."""
    fx = _arena_fixture(tmp_path)
    fx["tracks"]["t"]["probes"] = [{"id": "p-gate", "test": "restraint", "question": "q1?",
                                    "expect_any": ["68"], "expect_retrieval": False, "note": "n"}]
    import waku.app
    _FakeWaku.script = {"q1?": "68"}
    _FakeWaku.gate = True          # it retrieved when it should not have
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    events = []
    arena.run_arena(["sqlite"], "t", lambda k, e: events.append((k, e)), fixture=fx)

    row = next(e for k, e in events if k == "probe")
    assert row["outcome"] == MISS, "retrieving for arithmetic must fail even with the right number"


def test_the_live_store_is_never_switched(monkeypatch, tmp_path):
    """Each contestant runs in its own throwaway home. Moving a real user's
    memory sideways to answer a benchmark question is not an acceptable trade."""
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "the new one"},
                  backends=("sqlite", "mem0"))
    homes = {e["contestant"] for k, e in events if k == "start"}
    assert homes == {"sqlite", "mem0"}
    from waku.config import load_settings
    assert load_settings().semantic_store == "sqlite", "the real config must be untouched"
