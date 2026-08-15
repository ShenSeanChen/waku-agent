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
import os
from typing import ClassVar

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

    built: ClassVar[list] = []   # every instance a run created, so a test can inspect it
    settles: ClassVar[bool] = True   # what facts.settle() reports back

    def __init__(self, settings=None, **kw):
        from types import SimpleNamespace
        self.settings = settings
        self.seen: list[str] = []
        self.home = settings.home
        self.home.mkdir(parents=True, exist_ok=True)
        self._answers = dict(_FakeWaku.script)
        self.client = object()
        # The runner flushes consolidation and then CLEARS the session before
        # probing, so a seeded fact can only be reached through the store. A
        # fake that cannot do that would let the real thing regress silently —
        # which is the whole reason the bypass survived until a live race.
        # A store that reports readiness, and records WHEN it was asked. The
        # runner must ask after the last seed and before the first probe;
        # anywhere else and it is timing the network instead of the memory.
        self.settled_after: list[int] = []
        facts = SimpleNamespace(settle=lambda timeout=120.0: (
            self.settled_after.append(len(self.seen)) or _FakeWaku.settles))
        self.memory = SimpleNamespace(conn=object(), facts=facts, episodes=object())
        self.cleared_after: list[int] = []
        self.session = SimpleNamespace(
            start_new=lambda sid: self.cleared_after.append(len(self.seen)))
        _FakeWaku.built.append(self)

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


def _offline(monkeypatch, declined=None):
    """No network. The consolidation flush and the referee are both real calls
    in run_arena now; `declined=None` is the adjudicator's own "judge
    unreachable" answer, which must leave the heuristic verdict untouched."""
    import tempfile
    from pathlib import Path

    from waku.memory import consolidation

    monkeypatch.setattr(consolidation, "consolidate_if_due", lambda *a, **k: 0)
    monkeypatch.setattr(arena, "adjudicate_refusal", lambda *a, **k: declined)
    # Arena homes are now NAMED for their seed, so a real run reuses one and
    # skips re-telling. That is the point of them — and it would make these
    # tests depend on whether an earlier run left a `.seeded` marker behind,
    # which is a suite that passes only on a machine that has never raced.
    # A fresh directory per call restores isolation; the reuse behaviour gets
    # its own test below, where the home is pinned deliberately.
    monkeypatch.setattr(arena, "arena_home",
                        lambda backend, track, seed, model:
                        Path(tempfile.mkdtemp(prefix=f"arena-test-{backend}-")))


def _run(monkeypatch, tmp_path, script, gate=False, backends=("sqlite",)):
    import waku.app

    _FakeWaku.script = script
    _FakeWaku.gate = gate
    _FakeWaku.built = []
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
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
    _offline(monkeypatch)
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
    _offline(monkeypatch)
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


# --- the two fixes this file exists to hold in place -------------------------

def test_the_conversation_is_cleared_before_any_probe_is_asked(monkeypatch, tmp_path):
    """history_turns is 12 (24 messages), and a track seeds far fewer than that,
    so every seeded fact was still in the context window when the probes ran.
    Three probes in the first honest race passed while the gate reported "no
    lookup" — the store was never consulted. A benchmark whose subject can be
    bypassed is not measuring it."""
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "the new one"})
    assert [e for k, e in events if k == "probe"], "the run must have happened"
    app = _FakeWaku.built[0]
    assert app.cleared_after == [2], (
        "the session must be cleared exactly once, after the 2 seed lines and "
        f"before any probe — got {app.cleared_after}"
    )


def test_an_unreachable_judge_leaves_the_heuristic_verdict_alone(monkeypatch, tmp_path):
    """adjudicate_refusal returns None when the referee cannot be reached. That
    must not be read as either verdict — an unavailable judge cannot turn "I
    could not check" into "it passed" or "it lied"."""
    fx = {"tracks": {"t": {"label": "T", "seed": ["s"], "probes": [
        {"id": "p", "test": "restraint", "question": "q?", "expect_refusal": True, "note": "n"}]}}}
    import waku.app

    _FakeWaku.script = {"q?": "Pikachu loves ketchup."}
    _FakeWaku.gate = False
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    events = []
    arena.run_arena(["sqlite"], "t", lambda k, e: events.append((k, e)), fixture=fx)
    row = next(e for k, e in events if k == "probe")
    assert row["outcome"] == INVENTED
    assert row["certain"] is False, "an unchecked verdict must still say it is unchecked"


def test_the_judge_overrules_a_phrase_the_list_never_had(monkeypatch, tmp_path):
    """The live miss: LangMem replied "Nothing shared about Pikachu's food
    preferences" — a correct refusal — and scored INVENTED, because _REFUSALS
    holds "nothing about" and not "nothing shared"."""
    fx = {"tracks": {"t": {"label": "T", "seed": ["s"], "probes": [
        {"id": "p", "test": "restraint", "question": "q?", "expect_refusal": True, "note": "n"}]}}}
    import waku.app

    _FakeWaku.script = {"q?": "Nothing shared about Pikachu's food preferences."}
    _FakeWaku.gate = False
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch, declined=True)
    events = []
    arena.run_arena(["sqlite"], "t", lambda k, e: events.append((k, e)), fixture=fx)
    row = next(e for k, e in events if k == "probe")
    assert row["outcome"] == PASS, "a correct refusal must not be published as INVENTED"
    assert row["certain"] is True


def test_the_control_contestant_is_never_seeded(monkeypatch, tmp_path):
    """The control is told nothing and asked everything. If it is ever handed
    the seed, it stops being a control and silently agrees with whatever the
    other columns say."""
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "the new one"},
                  backends=(arena.CONTROL,))
    assert [e["line"] for k, e in events if k == "seeded"] == [], (
        "the control must receive no facts at all"
    )
    assert [e["probe"] for k, e in events if k == "probe"] == ["p-recall", "p-update"], (
        "but it must still be asked every probe"
    )


def test_a_probe_the_control_passes_is_reported_as_leaked(monkeypatch, tmp_path):
    """A probe the model can answer with no memory measures training data, not
    the store. Three of the dinner track's seven did exactly that, and nothing
    on screen said so — which is the whole reason this contestant exists."""
    events = _run(monkeypatch, tmp_path, {"q1?": "alpha", "q2?": "nothing useful"},
                  backends=(arena.CONTROL,))
    done = next(e for k, e in events if k == "done")
    assert done["leaked"] == ["p-recall"], (
        "p-recall was answered without any memory, so it must be named a leak; "
        f"got {done['leaked']}"
    )


def test_probes_designed_to_need_no_memory_are_not_called_leaks(monkeypatch, tmp_path):
    """The control caught this in itself on its first live run. Two probe kinds
    are SUPPOSED to be answerable with an empty store: expect_retrieval=False
    ("what's 17 times 4") and expect_refusal ("what's the filing deadline",
    which is passed BY declining, and a contestant with nothing stored declines
    every time). Flagging those would fire in every run forever and mean
    nothing — the badge has to stay rare enough to be worth reading."""
    fx = {"tracks": {"t": {"label": "T", "seed": ["s"], "probes": [
        {"id": "p-arith", "test": "restraint", "question": "q1?",
         "expect_any": ["68"], "expect_retrieval": False, "note": "n"},
        {"id": "p-decline", "test": "restraint", "question": "q2?",
         "expect_refusal": True, "note": "n"},
        {"id": "p-real", "test": "recall", "question": "q3?",
         "expect_any": ["alpha"], "note": "n"},
    ]}}}
    import waku.app

    _FakeWaku.script = {"q1?": "68", "q2?": "I don't have that saved.", "q3?": "alpha"}
    _FakeWaku.gate = False
    _FakeWaku.built = []
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    events = []
    arena.run_arena([arena.CONTROL], "t", lambda k, e: events.append((k, e)), fixture=fx)

    done = next(e for k, e in events if k == "done")
    assert done["leaked"] == ["p-real"], (
        "only the probe that asserts recalled content is a leak; the arithmetic "
        f"and refusal probes are designed to pass without memory — got {done['leaked']}"
    )


def test_store_is_asked_to_settle_between_seeding_and_probing(tmp_path, monkeypatch):
    """The runner must wait for the store to become searchable, and must do it
    AFTER the last seed and BEFORE the first probe.

    Both hosted backends are eventually consistent and both understate it:
    mem0 offers no readiness signal at all (measured 14s from add() to
    queryable), and Zep's per-add `processed` flag went green while the graph
    derived from those episodes still held zero matching nodes. Probe in that
    window and the store answers a question about a fact it is still filing —
    which scores as MISS. The benchmark then reports amnesia and is measuring
    the network.

    Ordering is the whole assertion. Settling before the seeds finish waits for
    nothing; settling after the probes is decoration.
    """
    import waku.app

    fx = _arena_fixture(tmp_path)          # 2 seed lines, 2 probes
    _FakeWaku.script = {"q1?": "alpha", "q2?": "new"}
    _FakeWaku.gate = True
    _FakeWaku.built = []
    _FakeWaku.settles = True
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    arena.run_arena(["sqlite"], "t", lambda k, e: None, fixture=fx)

    app = _FakeWaku.built[0]
    assert app.settled_after == [2], (
        "settle() must be called exactly once, after both seed lines and before "
        f"the first probe — got {app.settled_after} (2 seeds, 2 probes)"
    )


def test_a_store_that_never_settles_says_so_instead_of_scoring_silently(tmp_path, monkeypatch):
    """A timeout must reach the screen.

    If settle() gives up, every MISS after it is suspect — the contestant may
    know the answer perfectly well and simply not be queryable yet. Scoring
    that as memory failure, with nothing in the output to say the store never
    confirmed readiness, is the confident-silence bug this whole benchmark
    keeps rediscovering. Cheaper to say "I could not confirm" than to publish
    a number that looks like a verdict.
    """
    import waku.app

    fx = _arena_fixture(tmp_path)
    _FakeWaku.script = {"q1?": "alpha", "q2?": "new"}
    _FakeWaku.gate = True
    _FakeWaku.built = []
    _FakeWaku.settles = False            # the store never confirms
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    events = []
    arena.run_arena(["sqlite"], "t", lambda k, e: events.append((k, e)), fixture=fx)
    _FakeWaku.settles = True             # do not leak the flag into other tests

    warnings = [e for k, e in events if k == "warn"]
    assert warnings, "a store that never confirmed readiness must emit a warning"
    assert "readiness" in warnings[0]["message"], warnings[0]


def test_local_stores_settle_instantly(tmp_path):
    """sqlite writes the row and its FTS5 index in one transaction, so there is
    nothing to wait for. This exists so nobody 'helpfully' adds a sleep to the
    local path to match the hosted ones — the difference is the point, and it
    is one of the few places where the simplest backend genuinely wins."""
    from waku.db import connect
    from waku.memory.semantic.store import SqliteFactStore

    store = SqliteFactStore(connect(tmp_path))
    store.add("alex", "runs a robotics startup")
    assert store.settle() is True
    assert store.search("robotics"), "settled means searchable, not merely written"


def test_the_control_reports_why_it_is_empty_instead_of_crashing():
    """The control is a contestant, not a backend, and the read-stores page has
    to say so.

    It has no store behind it — that is the entire job. But _available_backends
    lists it (correct for a race) and store_contents iterates the same list, so
    it built a Settings(semantic_store="control"), got None from _conn_for, and
    the sqlite path called .execute() on it. The card rendered
    "AttributeError: 'NoneType' object has no attribute 'execute'", which reads
    as a broken control contestant when holding nothing is the point.

    A note, not an error, and not a bare "0 facts" either — this page exists
    precisely because those three mean different things.
    """
    rows = arena.store_contents()
    control = [r for r in rows if r["store"] == arena.CONTROL]
    assert control, "the control must still appear — its emptiness is the lesson"
    assert not control[0]["error"], (
        f"the control must not report an error: {control[0]['error']}"
    )
    assert "by design" in control[0]["note"], control[0]["note"]


def test_a_store_already_told_is_not_told_again(tmp_path, monkeypatch):
    """Telling is 53% of a race and perfectly deterministic. Doing it twice is
    the single biggest waste in the arena, and on camera it is the difference
    between a 40-second take and a 90-second one.

    Homes are now NAMED for what they hold — a hash of the track, the model and
    the seed lines — so the second run addresses the same directory and finds a
    `.seeded` marker. That naming is also the staleness guard, for free: change
    the probe set, the track or the model and you address a DIFFERENT directory,
    so a race can never quietly probe a store seeded for another question.
    """
    import waku.app

    fx = _arena_fixture(tmp_path)          # 2 seed lines, 2 probes
    home = tmp_path / "pinned"
    _FakeWaku.script = {"q1?": "alpha", "q2?": "new"}
    _FakeWaku.gate = True
    _FakeWaku.settles = True
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    monkeypatch.setattr(arena, "arena_home", lambda *a, **k: home)

    _FakeWaku.built = []
    arena.run_arena(["sqlite"], "t", lambda k, e: None, fixture=fx)
    first = list(_FakeWaku.built[0].seen)
    assert first[:2] == ["fact one", "fact two"], f"first run must seed: {first}"
    assert (home / ".seeded").exists(), "a settled home must record that it is ready"

    _FakeWaku.built = []
    arena.run_arena(["sqlite"], "t", lambda k, e: None, fixture=fx)
    second = list(_FakeWaku.built[0].seen)
    assert "fact one" not in second, f"already told — must not re-tell: {second}"
    assert "q1?" in second, "it must still ask, it just should not re-tell"


def test_a_home_is_only_marked_ready_once_the_store_confirms_it_is_searchable(
        tmp_path, monkeypatch):
    """The marker goes last, after settle(). A home marked ready while the store
    was still filing would be reused by the NEXT race and probed mid-ingest —
    turning one race condition into a permanent, cached one."""
    import waku.app

    fx = _arena_fixture(tmp_path)
    home = tmp_path / "never-settles"
    _FakeWaku.script = {"q1?": "alpha", "q2?": "new"}
    _FakeWaku.gate = True
    _FakeWaku.built = []
    _FakeWaku.settles = False            # the store never confirms
    monkeypatch.setattr(waku.app, "Waku", _FakeWaku)
    _offline(monkeypatch)
    monkeypatch.setattr(arena, "arena_home", lambda *a, **k: home)
    arena.run_arena(["sqlite"], "t", lambda k, e: None, fixture=fx)
    _FakeWaku.settles = True

    assert not (home / ".seeded").exists(), (
        "a store that never confirmed readiness must not be cached as ready"
    )


def test_the_stores_panel_reads_the_races_own_sqlite_not_the_live_agent(monkeypatch):
    """The panel sits under a benchmark that promises every store was told the
    same thing. Reading the LIVE .waku/state.db for sqlite broke that promise:
    the first card held months of real use next to stores that had seen one run,
    which is why it needed a paragraph above it saying the counts were not a
    comparison. Apologising for a comparison in prose is worse than not making
    it — so sqlite now reads the arena's own copy.

    It also stopped putting the operator's address, colleagues and work email
    on a tab that gets filmed.
    """
    seen = {}

    def fake_home(backend, track, seed, model):
        seen["called"] = (backend, track, model)
        return __import__("pathlib").Path(__import__("tempfile").mkdtemp())

    monkeypatch.setattr(arena, "arena_home", fake_home)
    rows = arena.store_contents(track="example", model="test:model")
    sqlite = next(r for r in rows if r["store"] == "sqlite")
    assert sqlite["kind"] == "arena", (
        f"with a track and model, sqlite must be the race's copy — got {sqlite['kind']}"
    )
    assert seen.get("called"), "it must actually resolve an arena home, not just relabel"


def test_without_a_track_the_panel_still_falls_back_to_the_live_store():
    """No track means no seed, which means no home can be named. Falling back to
    the live store is right — a blank panel would be a worse answer than an
    honest one — but it must SAY 'live' so the card is not read as this race's."""
    rows = arena.store_contents()
    sqlite = next(r for r in rows if r["store"] == "sqlite")
    assert sqlite["kind"] == "live", sqlite["kind"]


def test_a_race_writes_to_its_own_hosted_partition_not_the_live_one(monkeypatch):
    """The hosted half had no isolation, and the consequence was concrete.

    mem0 and Zep read MEM0_USER_ID / ZEP_USER_ID with a default of "waku" — the
    SAME partition the live agent uses — so every race wrote its benchmark seed
    into the operator's real memory, and every probe set wrote into the same
    place as every other one. A working-week race read back `wedding party
    ballroom` and `guest in room 402` from the business track, because there
    was only ever one drawer.
    """
    monkeypatch.delenv("MEM0_USER_ID", raising=False)
    monkeypatch.delenv("ZEP_USER_ID", raising=False)
    with arena.arena_partition_env("t", ["fact one"], "m:1") as partition:
        assert partition.startswith("waku-arena-"), partition
        assert os.environ["MEM0_USER_ID"] == partition
        assert os.environ["ZEP_USER_ID"] == partition
    # Leaking this would move the LIVE agent's memory to a benchmark partition
    # — worse than the bug it fixes.
    assert "MEM0_USER_ID" not in os.environ
    assert "ZEP_USER_ID" not in os.environ


def test_a_different_track_gets_a_different_partition():
    """This is the whole point: one drawer per question set, so a race cannot
    read back facts it was never told."""
    a = arena.arena_partition("dinner", ["fact one"], "m:1")
    b = arena.arena_partition("business", ["fact one"], "m:1")
    c = arena.arena_partition("dinner", ["fact one"], "m:2")
    assert a != b, "different tracks must not share a partition"
    assert a != c, "a different model reseeds, so it must not share either"


def test_clean_refuses_when_it_cannot_name_what_it_would_delete():
    """No track means no seed, no key, and therefore no partition name. A
    cleanup that cannot name its target must do nothing at all — the failure
    mode of guessing here is deleting the live agent's memory."""
    out = arena.clean_stores(track="", model="m:1")
    assert out.get("error"), out
    assert "nothing is deleted" in out["error"]


def test_a_partition_that_was_already_gone_is_not_an_error():
    """Zep 404s when the partition does not exist, which is the NORMAL case:
    cleaning twice, or cleaning a race that only ever ran locally. The first
    live run of Clean reported a wall of Cloudflare headers as an error for
    exactly this.

    Reporting "already gone" as failure trains you to ignore the error line —
    and the day it says something real, you ignore that too."""
    class _Gone(Exception):
        status_code = 404

    assert arena._absent(_Gone("message='not found'"))
    assert arena._absent(Exception("status_code: 404, body: not found"))
    assert not arena._absent(Exception("401 unauthorized — check your API key")), (
        "an auth failure is not 'already deleted' and must still surface"
    )
