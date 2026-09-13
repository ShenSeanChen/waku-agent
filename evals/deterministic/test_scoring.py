"""DETERMINISTIC EVAL — the ONE Completion scorer (waku.ops.scoring).

The CLI shootout and the live arena both score a run through this module, so the
terminal number and the on-screen number can't drift. These tests pin the
contract: the checklist logic, and matching a free-text prompt to its case."""

from __future__ import annotations

from waku.ops import scoring


def _tc(*names):
    return [{"tool": n, "args": {}} for n in names]


def test_expect_tool_none_wants_no_call():
    case = {"expect_tool": None}
    assert scoring.check_case(case, []) == (True, "no tool expected")
    ok, why = scoring.check_case(case, _tc("create_event"))
    assert not ok and "create_event" in why          # acted on a non-command


def test_expect_tool_must_fire():
    case = {"expect_tool": "create_event"}
    assert scoring.check_case(case, _tc("create_event"))[0]
    assert not scoring.check_case(case, _tc("save_note"))[0]


def test_expect_in_args_substring():
    case = {"expect_tool": "create_event", "expect_in_args": {"title": "sam"}}
    assert scoring.check_case(case, [{"tool": "create_event", "args": {"title": "Dinner w/ Sam"}}])[0]
    ok, why = scoring.check_case(case, [{"tool": "create_event", "args": {"title": "lunch"}}])
    assert not ok and "sam" in why


def test_min_tool_calls_floor():
    case = {"expect_tool": "create_event", "expect_min_tool_calls": 3}
    ok, why = scoring.check_case(case, _tc("create_event"))
    assert not ok and ">= 3" in why
    assert scoring.check_case(case, _tc("create_event", "create_event", "create_event"))[0]


def test_case_for_message_matches_trimmed_input():
    cases = [{"id": "a", "input": "Block three 25-minute focus sessions tomorrow morning"}]
    assert scoring.case_for_message("  Block three 25-minute focus sessions tomorrow morning ",
                                    cases)["id"] == "a"
    assert scoring.case_for_message("something else entirely", cases) is None


def test_scoring_handles_numeric_needle():
    # evals/dataset.jsonl's log-pnl-basic case expects a numeric arg (pnl_amount:
    # 200, an int) — the substring check must not crash on a non-string needle.
    case = {"expect_tool": "log_pnl", "expect_in_args": {"account": "IBKR", "pnl_amount": 200}}
    ok, why = scoring.check_case(case, [{"tool": "log_pnl", "args": {"account": "IBKR", "pnl_amount": 200}}])
    assert ok, why

    ok, why = scoring.check_case(case, [{"tool": "log_pnl", "args": {"account": "IBKR", "pnl_amount": 999}}])
    assert not ok and "200" in why


def test_real_dataset_loads_and_every_case_is_scoreable():
    cases = scoring.load_cases()
    assert len(cases) >= 11
    for c in cases:                       # every seeded case must have the fields the scorer reads
        assert "id" in c and "input" in c and "expect_tool" in c
