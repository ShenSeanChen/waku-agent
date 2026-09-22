"""Run a suite and print the scoreboard.

    python run.py            every suite
    python run.py leads      just one

Nothing here is clever. It sends each case, lets the suite's own `decide` turn
the answers into a label, and compares that to the label written by hand.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from jev import ask, value
from suites import SUITES

CACHE = Path(__file__).parent / "answers.json"   # raw answers, so re-tuning costs nothing


def run(name: str) -> tuple[int, int]:
    suite = SUITES[name]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    hits, misses, ms, cost = 0, [], [], 0.0
    for state, expected, note in suite["cases"]:
        answers = ask(state, suite["questions"])
        cache.setdefault(name, {})[state] = {k: value(v) for k, v in answers.items()
                                             if not k.startswith("_")}
        got = suite["decide"](answers)
        ms.append(answers["_ms"])
        cost += answers["_cost"]
        if got == expected:
            hits += 1
        else:
            misses.append((state, expected, got, note,
                           {k: value(v) for k, v in answers.items() if not k.startswith("_")}))

    CACHE.write_text(json.dumps(cache, indent=1))
    total = len(suite["cases"])
    print(f"\n{name}  {hits}/{total}   p50 {statistics.median(ms):.0f} ms   "
          f"${cost / total * 1000:.3f} per 1000 decisions")
    for state, expected, got, note, raw in misses:
        first = state.splitlines()[-1] if "\n" in state else state
        print(f"   wanted {expected!r}, got {got!r}   ({note})")
        print(f"     {first[:88]}")
        print(f"     {raw}")
    return hits, total


if __name__ == "__main__":
    names = sys.argv[1:] or list(SUITES)
    scored = [run(n) for n in names]
    if len(scored) > 1:
        print(f"\ntotal  {sum(h for h, _ in scored)}/{sum(t for _, t in scored)}")
