"""Deterministic Completion scoring for the eval battery — the ONE scorer.

Both `scripts/shootout.py` (the CLI table) and the Compare arena (the live
dashboard scoreboard) score a model's run the same way: did the expected tool
fire, with the expected args, and did enough of the loop actually run. Keeping
that judgment here means the terminal number and the on-screen number can never
drift apart.

A "case" is one line of `evals/dataset.jsonl`: an input prompt plus its expected
outcome (`expect_tool` / `expect_in_args` / `expect_min_tool_calls` /
`setup_fact`). Completion is the honest, judge-free axis — it's the local mirror
of a tau-bench / SWE-bench style outcome check (did the end-state match), not a
vibe. See docs/benchmarks.md.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATASET = Path(__file__).resolve().parents[2] / "evals" / "dataset.jsonl"


def load_cases() -> list[dict]:
    """Every battery case in file order; empty list if the dataset is missing."""
    if not _DATASET.exists():
        return []
    lines = _DATASET.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def check_case(case: dict, tool_calls: list[dict]) -> tuple[bool, str]:
    """The deterministic contract: right tool, right args, enough calls. Returns
    (passed, why) — why is 'ok' on success, else the first failed expectation
    (short enough to show under a race column or speak)."""
    fired = [c["tool"] for c in tool_calls]
    if case.get("expect_tool") is None:
        return (not fired, "no tool expected" if not fired else f"called {fired}")
    if case["expect_tool"] not in fired:
        return (False, f"expected {case['expect_tool']}, called {fired or 'nothing'}")
    args = next(c["args"] for c in tool_calls if c["tool"] == case["expect_tool"])
    for key, needle in case.get("expect_in_args", {}).items():
        if needle.lower() not in str(args.get(key, "")).lower():
            return (False, f"'{needle}' not in args[{key}]")
    want = case.get("expect_min_tool_calls", 0)
    if len(fired) < want:
        return (False, f"only {len(fired)} tool calls, wanted >= {want}")
    return (True, "ok")


def case_for_message(message: str, cases: list[dict] | None = None) -> dict | None:
    """The battery case whose input matches this arena prompt (trimmed exact
    match), so a race on a KNOWN task gets a Completion score. Returns None for a
    free-form prompt — the arena still shows speed/cost/tokens, just no score."""
    msg = (message or "").strip()
    for case in (cases if cases is not None else load_cases()):
        if (case.get("input") or "").strip() == msg:
            return case
    return None
