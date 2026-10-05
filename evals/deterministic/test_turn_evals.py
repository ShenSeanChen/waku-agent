"""DETERMINISTIC EVAL — turn checks that grade a person's own turns (spec 015).

Five code checks read one turn's trace and answer pass (1), fail (0) or n/a
(None). The fixtures in evals/fixtures/turns/ are real or rebuilt turns, each
line passed through redact(): the hosted rehearsal turn whose reply said the
research cost "$0.00" while treg charged $0.0089 must fail, and Sean's
2026-10-02 turn that truthfully called LeadsForge's preview free must pass.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waku.ops import observability as obs
from waku.ops import turn_evals as te

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "turns"
REHEARSAL = "2026-10-05-rehearsal-zero-claim.jsonl"
LEADSFORGE = "2026-10-02-leadsforge-free-preview.jsonl"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def _events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]


def _turn(name: str) -> dict:
    turns = obs.group_turns(_events(name))
    assert len(turns) == 1
    return turns[0]


def _scores(turn: dict) -> dict[str, dict]:
    built = obs.build_turn(turn)
    return {s["name"]: s for s in built["scores"] if s["source"] == "code"}


def _tool(name, output, *, ok=None, turn_id="t_abc123", **extra):
    ev = obs.trace_record({"tool": name, "args": {}, "output": output}, turn_id=turn_id)
    if ok is not None:
        ev["ok"] = ok
    return {"type": "tool", **ev, **extra}


def _made(message, reply, events=(), turn_id="t_abc123"):
    return {"turn_id": turn_id, "ts": "2026-10-05T10:00:00+00:00", "user_message": message,
            "reply": reply, "events": list(events)}


def _check(name, turn, usd=0.1):
    return te.CHECKS[name](te.view(turn, usd=usd))


TREG = json.dumps({"status": 200, "endpoint_id": "tomba.companies.similar", "cost_usd": 0.0089})


# ---- B0: the fixtures load --------------------------------------------------

@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.jsonl")))
def test_every_fixture_loads_through_group_turns(name):
    turn = _turn(name)
    assert turn["reply"] and not turn.get("unfinished")
    # redacted: nothing key-shaped survives
    assert obs.redact(_events(name)) == _events(name)


# ---- 1, 2. spend_claim ------------------------------------------------------

def test_spend_claim_fails_the_rehearsal_turn_and_names_both_figures():
    check = _scores(_turn(REHEARSAL))["spend_claim"]
    assert check["value"] == 0
    assert check["note"] == "reply says $0.00; this turn's treg calls cost $0.0089"


def test_spend_claim_passes_the_leadsforge_free_preview_turn():
    """"Both useful lists came from LeadsForge's free preview ($0.00)" and
    "still billed $0.0089" are both true: a check that fails this is wrong."""
    check = _scores(_turn(LEADSFORGE))["spend_claim"]
    assert check["value"] == 1, check["note"]


def test_spend_claim_is_na_for_a_price_offered_for_later():
    turn = _made("research mem0", "Want me to enrich them? That'd run ~$0.002 each.",
                 [_tool("treg_call", TREG)])
    assert _check("spend_claim", turn)["value"] is None


def test_spend_claim_checks_a_named_treg_figure_against_the_trace():
    ok = _made("x", "The treg cost was $0.0089.", [_tool("treg_call", TREG)])
    off = _made("x", "The treg cost was $0.40.", [_tool("treg_call", TREG)])
    assert _check("spend_claim", ok)["value"] == 1
    bad = _check("spend_claim", off)
    assert bad["value"] == 0 and "$0.40" in bad["note"] and "$0.0089" in bad["note"]


def test_spend_claim_passes_a_free_claim_when_treg_charged_nothing():
    free = json.dumps({"status": 200, "endpoint_id": "leadsforge.companies.lookalike.preview",
                       "cost_usd": 0.0})
    turn = _made("x", "This research cost nothing: it used a free preview.",
                 [_tool("treg_call", free)])
    assert _check("spend_claim", turn)["value"] == 1


# ---- 3. one_report ----------------------------------------------------------

def test_one_report_fails_a_research_turn_with_no_report():
    turn = _made("research the competitors of Letta", "Letta competes with Zep.")
    check = _check("one_report", turn)
    assert check["value"] == 0


def test_one_report_fails_two_reports():
    assert _scores(_turn("two-reports.jsonl"))["one_report"]["value"] == 0


def test_one_report_passes_one_report_or_the_marker():
    turn = _made("research the competitors of Letta", "Letta competes with Zep.",
                 [{"type": "report", "turn_id": "t_abc123", "memory_id": "m1", "title": "Letta"}])
    assert _check("one_report", turn)["value"] == 1
    marker = _made("research the competitors of Letta",
                   "Letta competes with Zep.\n\n<!-- waku-report v1 -->\n# Letta\n")
    assert _check("one_report", marker)["value"] == 1
    assert _scores(_turn(REHEARSAL))["one_report"]["value"] == 1


def test_one_report_is_na_for_an_everyday_message():
    assert _check("one_report", _made("what's on my calendar", "Nothing today."))["value"] is None


# ---- 4. grounded_numbers ----------------------------------------------------

SOURCE = json.dumps({"revenue_usd": 1200, "share": 0.04, "founded": "2026-10-05",
                     "employees": 37, "growth_pct": 12})


def test_grounded_numbers_matches_other_formats():
    reply = "They earn $1,200 a month, hold $0.04 of share, have 37 staff, grew 12% and launched Oct 5, 2026."
    check = _check("grounded_numbers", _made("x", reply, [_tool("search_web", SOURCE)]))
    assert check["value"] == 1, check["note"]


def test_grounded_numbers_fails_an_invented_figure_and_names_it():
    reply = "They earn $1,200 a month, raised $4.2M, have 37 staff and grew 12%."
    check = _check("grounded_numbers", _made("x", reply, [_tool("search_web", SOURCE)]))
    assert check["value"] == 0 and "$4.2M" in check["note"]


def test_grounded_numbers_is_na_below_three_numbers_and_skips_code_and_lists():
    assert _check("grounded_numbers", _made("x", "They have 37 staff."))["value"] is None
    reply = "1. First\n2. Second\n```\nlimit=55 offset=99 page=12\n```\nThey have 37 staff, the 21st entry."
    assert _check("grounded_numbers", _made("x", reply, [_tool("search_web", SOURCE)]))["value"] is None


def test_grounded_numbers_reads_the_users_message_too():
    reply = "You asked about 2026 budgets of $500, $700 and $900."
    check = _check("grounded_numbers", _made("compare budgets of 500, 700 and 900 for 2026", reply))
    assert check["value"] == 1, check["note"]


# ---- 5. errors_handled ------------------------------------------------------

FAILED = "Search failed: the request timed out"


def test_errors_handled_passes_a_retried_failure():
    turn = _made("x", "Supermemory shipped an SDK.",
                 [_tool("search_web", FAILED), _tool("search_web", "Web results: Supermemory SDK")])
    assert _check("errors_handled", turn)["value"] == 1


def test_errors_handled_passes_a_mentioned_failure():
    turn = _made("x", "The web search didn't work, so this is from memory.", [_tool("search_web", FAILED)])
    assert _check("errors_handled", turn)["value"] == 1
    # the 2026-10-02 reply: "Memory save hit a session error"
    assert _scores(_turn(LEADSFORGE))["errors_handled"]["value"] == 1


def test_errors_handled_fails_a_silent_failure():
    check = _scores(_turn("silent-failure.jsonl"))["errors_handled"]
    assert check["value"] == 0 and "search_web" in check["note"]


def test_errors_handled_is_na_with_no_failure():
    turn = _made("x", "Done.", [_tool("search_web", "Web results: fine")])
    assert _check("errors_handled", turn)["value"] is None


# ---- 6. under_budget --------------------------------------------------------

def test_under_budget_fails_a_134_turn_at_the_default(monkeypatch):
    monkeypatch.delenv(te.BUDGET_ENV, raising=False)
    check = _scores(_turn("over-budget.jsonl"))["under_budget"]
    assert check["value"] == 0 and check["note"] == "$1.34 is over the $1.00 budget"
    assert _scores(_turn(REHEARSAL))["under_budget"]["value"] == 1


def test_under_budget_reads_the_budget_from_the_environment(monkeypatch):
    monkeypatch.setenv(te.BUDGET_ENV, "2.50")
    check = _scores(_turn("over-budget.jsonl"))["under_budget"]
    assert check["value"] == 1 and "$2.50" in check["note"]
    monkeypatch.setenv(te.BUDGET_ENV, "not a number")
    assert te.budget_usd() == te.DEFAULT_BUDGET_USD


# ---- 7. build_turn ----------------------------------------------------------

def test_build_turn_returns_the_five_code_scores():
    built = obs.build_turn(_turn(REHEARSAL))
    code = [s for s in built["scores"] if s["source"] == "code"]
    assert [s["name"] for s in code] == list(te.TURN_CHECKS)
    assert all(set(s) == {"source", "name", "value", "note"} for s in code)


def test_a_check_that_raises_is_na_and_the_page_still_builds(monkeypatch, tmp_path):
    def broken(_view):
        raise RuntimeError("boom")

    monkeypatch.setitem(te.CHECKS, "spend_claim", broken)
    built = obs.build_turn(_turn(REHEARSAL))
    check = next(s for s in built["scores"] if s["name"] == "spend_claim")
    assert check["value"] is None and check["note"] == "check error"
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "2026-10-05.jsonl").write_text((FIXTURES / REHEARSAL).read_text())
    assert obs.payload(tmp_path, window="all", now=NOW)["turns"]


# ---- 8. an old (v1) trace ---------------------------------------------------

def test_a_v1_trace_gets_the_checks_and_v2_only_checks_are_na():
    scores = _scores(_turn(LEADSFORGE))
    assert set(scores) == set(te.TURN_CHECKS)
    assert scores["one_report"]["value"] is None and scores["grounded_numbers"]["value"] is None
    assert scores["spend_claim"]["value"] == 1 and scores["under_budget"]["value"] == 1


# ---- 9. Your turns on the Evals page ----------------------------------------

def _home(tmp_path, names):
    (tmp_path / "traces").mkdir()
    lines = [line for n in names for line in (FIXTURES / n).read_text(encoding="utf-8").splitlines()]
    (tmp_path / "traces" / "2026-10-05.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_the_evals_payload_counts_each_check_per_window(tmp_path):
    home = _home(tmp_path, [LEADSFORGE, REHEARSAL, "two-reports.jsonl", "silent-failure.jsonl"])
    yours = obs.payload(home, window="7d", now=NOW)["evals"]["your_turns"]
    assert set(yours) == set(obs.WINDOWS)
    rows = {r["name"]: r for r in yours["all"]["checks"]}
    assert list(rows) == list(te.TURN_CHECKS)
    spend = rows["spend_claim"]
    assert (spend["passed"], spend["failed"], spend["na"]) == (1, 1, 2)
    assert spend["pass_rate"] == 0.5
    assert spend["newest_fail"]["turn_id"] == "t_5e1f0c2a9b7d4e36"
    assert spend["newest_fail"]["note"] == "reply says $0.00; this turn's treg calls cost $0.0089"
    # the 2026-10-02 trace's turn ran at 2026-10-03 18:58 UTC: inside 7 days, not today
    assert yours["7d"]["turns"] == 4 and yours["today"]["turns"] == 3
    assert yours["all"]["judge"] == {"judged": 0, "average": None, "passed": 0}
    # the Overview's "your turns n/m": only the 2026-10-02 turn passed every check
    assert (yours["all"]["turns_passed"], yours["all"]["turns_scored"]) == (1, 4)


# ---- 6. the command ---------------------------------------------------------

def test_waku_evals_turns_prints_the_failing_rehearsal_turn(tmp_path, capsys):
    home = _home(tmp_path, [LEADSFORGE, REHEARSAL])
    print(te.report(home, "all"))
    out = capsys.readouterr().out
    assert "spend_claim" in out and "t_5e1f0c2a9b7d4e36" in out
    assert "reply says $0.00; this turn's treg calls cost $0.0089" in out
    assert te.cli_main(["--window", "year"]) == 2


def test_the_cli_routes_evals_turns(monkeypatch, capsys):
    import waku.__main__ as entry

    seen = []
    monkeypatch.setattr(te, "cli_main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["waku", "evals", "turns", "--window", "all"])
    with pytest.raises(SystemExit) as done:
        entry.main()
    assert done.value.code == 0 and seen == [["--window", "all"]]
