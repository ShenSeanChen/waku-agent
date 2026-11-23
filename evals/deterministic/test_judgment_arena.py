"""The Judgment Arena, offline.

A race needs keys and a network, so none of that is tested here. What is tested
is everything that decides what a race MEANS: that every question carries one
label per case, that the labels are answerable, and that the right/wrong rule
for each primitive is the plain one it claims to be.
"""

from __future__ import annotations

import pytest

from waku.ops import judgment_arena, judgment_cases

SUITE_IDS = sorted(judgment_cases.SUITES)


@pytest.mark.parametrize("suite_id", SUITE_IDS)
def test_each_case_carries_one_of_each_primitive(suite_id):
    """Three questions per case, one Noul, one Choice, one Score. Five cases
    makes fifteen questions and five of each shape."""
    suite = judgment_cases.SUITES[suite_id]
    assert len(suite["cases"]) == 5, f"{suite_id}: {len(suite['cases'])} cases, expected 5"
    for i, c in enumerate(suite["cases"]):
        kinds = sorted(q["type"] for q in c["questions"])
        assert kinds == ["choice", "noul", "score"], f"{suite_id} case {i}: {kinds}"


@pytest.mark.parametrize("suite_id", SUITE_IDS)
def test_no_question_is_asked_twice(suite_id):
    """The unit is the question. A question that appears twice is the
    repetition this arena exists to avoid."""
    qs = [q for _, q in judgment_cases.questions_of(suite_id)]
    ids = [q["id"] for q in qs]
    assert len(set(ids)) == len(ids), f"{suite_id}: repeated question id"
    texts = [q["instructions"] for q in qs]
    assert len(set(texts)) == len(texts), f"{suite_id}: the same question asked twice"


@pytest.mark.parametrize("suite_id", SUITE_IDS)
def test_every_label_is_an_answer_the_question_can_give(suite_id):
    """A label outside the answer space is an unwinnable question."""
    for _i, q in judgment_cases.questions_of(suite_id):
        want = q["expected"]
        if q["type"] == "noul":
            assert isinstance(want, bool), f"{suite_id}.{q['id']}: {want!r} is not a boolean"
        elif q["type"] == "choice":
            assert want in q["criteria"], f"{suite_id}.{q['id']}: {want!r} is not an option"
        else:
            assert 0 <= want < len(q["criteria"]), f"{suite_id}.{q['id']}: no level {want}"


@pytest.mark.parametrize("suite_id", SUITE_IDS)
def test_the_nouls_are_not_all_the_same_answer(suite_id):
    """Five Nouls all labelled True would hand full marks to a judge that
    always says yes."""
    wants = [q["expected"] for _i, q in judgment_cases.questions_of(suite_id)
             if q["type"] == "noul"]
    assert len(set(wants)) > 1, f"{suite_id}: every Noul is labelled {wants[0]}"


def test_a_noul_is_right_on_the_side_of_the_coin_flip():
    right = judgment_cases.is_right
    assert right("noul", True, {"noul": 0.51})
    assert right("noul", False, {"noul": 0.49})
    assert not right("noul", True, {"noul": 0.49})
    # 0.5 exactly counts as yes, deliberately, so the rule has no dead zone.
    assert right("noul", True, {"noul": 0.5})


def test_a_choice_is_right_only_on_the_option_it_picked():
    right = judgment_cases.is_right
    assert right("choice", "refund", {"choice": "refund"})
    assert not right("choice", "refund", {"choice": "exchange"})
    # A near miss with a confident distribution is still a miss.
    assert not right("choice", "refund", {"choice": "exchange", "confidence": 0.99})


def test_a_score_is_right_when_its_mean_rounds_to_the_level():
    right = judgment_cases.is_right
    assert right("score", 2, {"score": 1.97})     # a weighted mean lands between levels
    assert right("score", 2, {"score": 2.4})
    assert not right("score", 2, {"score": 1.4})


def test_the_picker_payload_carries_the_questions_and_none_of_the_labels():
    """The instructions are the teaching content and must ship. The labels are
    the answers and must not: a viewer could read them off the page."""
    for row in judgment_cases.suite_list():
        assert row["cases"], f"{row['id']} shipped no cases"
        for c in row["cases"]:
            assert c["questions"], f"{row['id']} shipped a case with no questions"
            for q in c["questions"]:
                assert "instructions" in q
                assert "expected" not in q


def test_every_suite_is_fifteen_questions_five_of_each():
    for suite_id in judgment_cases.SUITES:
        kinds = [q["type"] for _i, q in judgment_cases.questions_of(suite_id)]
        assert len(kinds) == 15, f"{suite_id}: {len(kinds)} questions, expected 15"
        for kind in ("noul", "choice", "score"):
            assert kinds.count(kind) == 5, f"{suite_id}: {kinds.count(kind)} {kind}s"


def test_a_judge_with_no_key_is_listed_but_not_selectable(monkeypatch):
    """The reason belongs on screen. A column that silently vanishes reads as a
    bug in the arena rather than a missing key."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev = next(c for c in judgment_arena.contestants() if c["spec"] == judgment_arena.JEV_SPEC)
    assert jev["ready"] is False
    assert "TYPESAFE_API_KEY" in jev["why"]


def test_only_current_models_are_offered():
    """An older pin racing a current model measures nothing, and every extra
    column is a real API call per case."""
    offered = {c["spec"] for c in judgment_arena.contestants()}
    tiered = {f"{prov}:{m}" for prov, flagship, fast in judgment_arena.JUDGE_TIERS
              for m in (flagship, fast)}
    assert offered == tiered | {judgment_arena.JEV_SPEC}
