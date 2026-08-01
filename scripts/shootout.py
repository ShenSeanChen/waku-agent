"""Model shootout — same agent body, different brains, real receipts.

    python scripts/shootout.py kimi:kimi-k3 anthropic:claude-opus-4-8
    make shootout RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8"

For each provider:model pair this runs every case in evals/dataset.jsonl through
a REAL Milli (fresh isolated home per run, your keys from .env) and scores it the
same deterministic way the live eval tier does: did the right tool fire, with
the right arguments — 0 or 1, no judge involved. Alongside correctness it
collects what benchmarks usually hide: tokens, estimated dollars (per-model
pricing, same table the dashboard uses), latency, and loop iterations.

Output: a markdown table on stdout plus a timestamped .md + .json report in
.milli/shootout/ — publish it, and anyone can re-run it with their own keys.
That's the point: don't trust the table, reproduce it.

Honesty notes baked in: cost is an ESTIMATE from tokens x list price (cache
discounts not modeled); pass-rate is deterministic tool-behavior, not vibes —
for judged answer quality run `make eval-judge` per provider (use a third-party
judge model, never one of the contestants).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from milli.config import Settings, load_settings  # noqa: E402  (loads .env keys)
from milli.ops.pricing import price_for  # noqa: E402
from milli.ops.scoring import check_case, load_cases  # noqa: E402  (the ONE scorer)

DATASET = load_cases()


def _ledger_totals(home: Path) -> tuple[int, int]:
    total_in = total_out = 0
    path = home / "usage.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                total_in, total_out = total_in + r.get("in", 0), total_out + r.get("out", 0)
            except json.JSONDecodeError:
                pass
    return total_in, total_out


def run_one(provider: str, model: str, cases: list[dict], trials: int = 1) -> dict:
    """One contestant: real loop, every case, `trials` attempts each.

    Model tool-calling is nondeterministic — a case can pass on one run and
    fail the next (we watched kimi-k3 do exactly that). One trial is a coin
    flip; N trials per case turns the table into a pass RATE. Each trial gets
    a fresh home so no memory leaks between attempts."""
    from milli.app import Milli

    rows, t_run, resolved_model = [], time.perf_counter(), model
    for case in cases:
        hits, lat, iters_seen, tin, tout, cost, why = 0, [], [], 0, 0, 0.0, "ok"
        for _ in range(trials):
            home = Path(tempfile.mkdtemp(prefix=f"shootout-{provider}-"))
            settings = Settings(provider=provider, model=model, small_model="",
                                home=home, apple_calendar=False)
            app = Milli(settings=settings)
            resolved_model = settings.model   # get_client filled the default
            if "setup_fact" in case:
                app.memory.facts.add(case["setup_fact"]["subject"], case["setup_fact"]["content"])
            t0 = time.perf_counter()
            try:
                result = app.respond(case["input"])
                ok, w = check_case(case, result.tool_calls)
                iters_seen.append(result.iterations)
            except Exception as exc:  # a crashed turn is a failed trial, not a crashed shootout
                ok, w = False, f"error: {str(exc)[:90]}"
            lat.append(time.perf_counter() - t0)
            i1, o1 = _ledger_totals(home)
            pin, pout = price_for(provider, model or settings.model)
            tin, tout = tin + i1, tout + o1
            cost += i1 / 1e6 * pin + o1 / 1e6 * pout
            hits += ok
            if not ok:
                why = w
        rows.append({"case": case["id"], "hits": hits, "trials": trials,
                     "passed": hits == trials, "why": "ok" if hits == trials else why,
                     "avg_latency_s": round(sum(lat) / len(lat), 1),
                     "iterations": max(iters_seen or [0]),
                     "tokens_in": tin, "tokens_out": tout, "cost_usd": round(cost, 4)})
        r = rows[-1]
        print(f"  [{hits}/{trials}] {case['id']:26s} {r['avg_latency_s']:5.1f}s avg  "
              f"${cost:.4f}  {'' if r['passed'] else why}")
    n = max(len(rows), 1)
    return {"provider": provider, "model": resolved_model,
            "trials": trials, "cases": rows,
            "hit_rate": round(sum(r["hits"] for r in rows) / (n * trials), 3),
            "passed": sum(r["hits"] for r in rows), "total": len(rows) * trials,
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            "avg_latency_s": round(sum(r["avg_latency_s"] for r in rows) / n, 1),
            "wall_s": round(time.perf_counter() - t_run, 1)}


def markdown(results: list[dict]) -> str:
    lines = ["| brain | pass rate | est cost | avg latency | tokens in/out |",
             "|---|---|---|---|---|"]
    for r in results:
        tin = sum(c["tokens_in"] for c in r["cases"])
        tout = sum(c["tokens_out"] for c in r["cases"])
        lines.append(f"| {r['provider']}:{r['model']} | {r['passed']}/{r['total']} "
                     f"| ${r['cost_usd']:.4f} | {r['avg_latency_s']}s | {tin}/{tout} |")
    trials = results[0].get("trials", 1) if results else 1
    lines.append("")
    lines.append(f"Every case runs {trials} trial(s) — tool-calling is nondeterministic, "
                 "so a rate beats a coin flip. Same agent, same tasks, keys from your own "
                 ".env — re-run with: `make shootout RUNS=\"...\"`. Costs are estimates "
                 "(tokens x list price; cache discounts not modeled). Pass = deterministic "
                 "tool-behavior checks from evals/dataset.jsonl.")
    return "\n".join(lines)


def coding_shootout(runs: list[str], cases: list[dict], trials: int) -> str:
    """Cross-model CODING round: pi runs each contestant's model on each coding
    task, scored by the task's `verify` command (tests pass = 1). Prints a table."""
    from milli.ops.coding_eval import run_coding_case

    lines = ["| brain | pass rate | avg latency | detail |", "|---|---|---|---|"]
    for spec in runs:
        provider, _, model = spec.partition(":")
        print(f"\n=== coding · {provider}:{model or '(default)'} — {len(cases)} cases x {trials} ===")
        hits = total = 0
        lats, notes = [], []
        for case in cases:
            for _ in range(trials):
                passed, why, secs = run_coding_case(provider, model, case)
                hits += 1 if passed else 0
                total += 1
                lats.append(secs)
                print(f"  [{'PASS' if passed else 'fail'}] {case['id']:16} {secs:6.1f}s  {why}")
            notes.append(f"{case['id']}:{'ok' if passed else why[:24]}")
        avg = round(sum(lats) / len(lats), 1) if lats else 0
        lines.append(f"| {spec} | {hits}/{total} | {avg}s | {'; '.join(notes)} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the same tasks on different brains.")
    parser.add_argument("runs", nargs="+", metavar="provider:model",
                        help="e.g. kimi:kimi-k3 anthropic:claude-opus-4-8 "
                             "(omit :model for the provider default)")
    parser.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    parser.add_argument("--trials", type=int, default=3,
                        help="attempts per case (default 3 — rates, not coin flips)")
    parser.add_argument("--coding", action="store_true",
                        help="run the CODING battery (evals/coding.jsonl) via pi per model, "
                             "scored by each task's verify command")
    args = parser.parse_args()

    if args.coding:
        from milli.ops.coding_eval import load_coding_cases, pi_available
        if not pi_available():
            raise SystemExit("pi isn't installed — the coding battery needs it. "
                             "Install: npm install -g --ignore-scripts @earendil-works/pi-coding-agent")
        wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
        cases = [c for c in load_coding_cases() if not wanted or c["id"] in wanted]
        if not cases:
            raise SystemExit(f"no coding cases match {wanted!r} — ids: "
                             f"{[c['id'] for c in load_coding_cases()]}")
        table = coding_shootout(args.runs, cases, args.trials)
        print("\n" + table)
        out_dir = load_settings().home / "shootout"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (out_dir / f"coding-{stamp}.md").write_text(table + "\n")
        print(f"\nreport: {out_dir}/coding-{stamp}.md")
        return

    wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
    cases = [c for c in DATASET if not wanted or c["id"] in wanted]
    if not cases:
        raise SystemExit(f"no cases match {wanted!r} — ids: {[c['id'] for c in DATASET]}")

    results = []
    for spec in args.runs:
        provider, _, model = spec.partition(":")
        print(f"\n=== {provider}:{model or '(default)'} — {len(cases)} cases x {args.trials} trials ===")
        results.append(run_one(provider, model, cases, trials=args.trials))

    table = markdown(results)
    print("\n" + table)

    out_dir = load_settings().home / "shootout"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"shootout-{stamp}.json").write_text(json.dumps(results, indent=1))
    (out_dir / f"shootout-{stamp}.md").write_text(table + "\n")
    print(f"\nreport: {out_dir}/shootout-{stamp}.md (+ .json)")


if __name__ == "__main__":
    main()
