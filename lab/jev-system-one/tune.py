"""Find each system's best policy on the same grid, from answers already paid for.

Both systems feed the identical `decide` logic, so a threshold tuned on Jev's
scale would quietly punish the LLM. Each gets the same sweep over its own
answers and is reported at its own best setting.

These numbers are tuned on the same 44 cases they are scored on. That is what
you would do with your own data, and it is also why this is not a benchmark.
"""

import json
import math
from pathlib import Path

from suites import SUITES

HERE = Path(__file__).parent
SOURCES = {"jev": "answers.json", "llm": "answers_llm.json"}

GRIDS = {
    "leads":   [("buying gate", g) for g in (0.02, 0.05, 0.1, 0.2, 0.4)],
    "refunds": [("fraud gate", g) for g in (0.2, 0.3, 0.4, 0.5, 0.6)],
    "support": [("round", round), ("floor", math.floor)],
    "memory":  [("keep at", g) for g in (0.5, 0.8, 1.0, 1.2, 1.5, 1.8)],
}


def decide(name, a, knob):
    if name == "leads":
        return ("ignore" if a["intent"] == "nothing" or a["buying"] < knob
                else "call_today" if a["heat"] >= 2.0 and a["budget_signal"] >= 0.5
                else "nurture")
    if name == "refunds":
        return ("human" if a["fraud_smell"] >= knob or a["kind"] == "unclear"
                else "refund" if a["kind"] == "refund" and a["within_policy"] >= 0.6
                else "human" if a["kind"] == "refund" else "route")
    if name == "support":
        return knob(a["urgency"])
    return "keep" if a["earns_slot"] >= knob else "drop"


print(f"{'suite':9s} {'best policy':18s} {'Jev':>8s} {'Haiku 4.5':>10s}")
totals = {"jev": 0, "llm": 0}
for name, suite in SUITES.items():
    want = {s: e for s, e, _ in suite["cases"]}
    raw = {k: json.loads((HERE / f).read_text())[name] for k, f in SOURCES.items()}
    best, label = None, ""
    for knob_name, knob in GRIDS[name]:
        got = {k: sum(decide(name, a, knob) == want[s] for s, a in raw[k].items())
               for k in SOURCES}
        if best is None or sum(got.values()) > sum(best.values()):
            best, label = got, f"{knob_name} {knob if not callable(knob) else ''}".strip()
    n = len(suite["cases"])
    print(f"{name:9s} {label:18s} {best['jev']:>4d}/{n:<3d} {best['llm']:>6d}/{n:<3d}")
    for k in totals:
        totals[k] += best[k]
total = sum(len(s["cases"]) for s in SUITES.values())
print(f"{'total':9s} {'':18s} {totals['jev']:>4d}/{total:<3d} {totals['llm']:>6d}/{total:<3d}")
