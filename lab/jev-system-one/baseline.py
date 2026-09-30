"""The same 44 cases, same policy code, judged by an LLM instead.

The only thing that changes is where the answers come from. Claude is asked to
return the same shape Jev returns, and the suite's own `decide` runs unchanged,
so any difference in score is a difference in judgment, not in plumbing.

We use Haiku 4.5, the fast cheap model -- comparing against a big reasoning
model would flatter Jev on both latency and price and prove nothing.

    python baseline.py [suite ...]
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

from jev import _key as _jev_key  # noqa: F401  (same .env reader, different name below)
from suites import SUITES

MODEL = "claude-haiku-4-5-20251001"
IN_PER_TOK, OUT_PER_TOK = 1.0 / 1e6, 5.0 / 1e6      # Haiku 4.5 list price


def _anthropic_key() -> str:
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    for parent in (Path.cwd(), *Path(__file__).resolve().parents):
        if (env := parent / ".env").exists():
            for line in env.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("set ANTHROPIC_API_KEY")


def _spec(qid: str, q: dict) -> str:
    if q["type"] == "noul":
        return f'"{qid}": a probability from 0 to 1 that this is true -- {q["instructions"]}'
    if q["type"] == "choice":
        opts = "; ".join(f"{k} = {v}" for k, v in q["criteria"].items())
        return f'"{qid}": exactly one of [{", ".join(q["criteria"])}] -- {q["instructions"]} ({opts})'
    levels = "; ".join(f"{i} = {lv}" for i, lv in enumerate(q["criteria"]))
    return (f'"{qid}": a number from 0 to {len(q["criteria"]) - 1}, decimals allowed -- '
            f'{q["instructions"]} ({levels})')


def ask_llm(state: str, questions: dict) -> tuple[dict, float, float]:
    spec = "\n".join(_spec(qid, q) for qid, q in questions.items())
    body = json.dumps({
        "model": MODEL, "max_tokens": 400,
        "messages": [{"role": "user", "content":
            f"Answer every question about the state below. Reply with one JSON object and "
            f"nothing else.\n\nSTATE:\n{state}\n\nQUESTIONS:\n{spec}"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": _anthropic_key(), "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    started = time.perf_counter()
    out = json.loads(urllib.request.urlopen(req, timeout=60).read())
    ms = (time.perf_counter() - started) * 1000
    text = out["content"][0]["text"].strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    raw = json.loads(text)                       # the step Jev does not have
    cost = out["usage"]["input_tokens"] * IN_PER_TOK + out["usage"]["output_tokens"] * OUT_PER_TOK
    answers = {}
    for qid, q in questions.items():
        v = raw[qid]
        answers[qid] = {"noul": v} if q["type"] == "noul" else \
                       {"choice": v} if q["type"] == "choice" else {"score": float(v)}
    return answers, ms, cost


def run(name: str) -> None:
    suite = SUITES[name]
    cache_path = Path(__file__).parent / "answers_llm.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    hits, ms, cost, broke = 0, [], 0.0, 0
    for state, expected, _note in suite["cases"]:
        try:
            answers, t, c = ask_llm(state, suite["questions"])
        except (json.JSONDecodeError, KeyError, ValueError):
            broke += 1                            # the failure mode Jev cannot have
            continue
        ms.append(t); cost += c
        cache.setdefault(name, {})[state] = {k: list(v.values())[0] for k, v in answers.items()}
        hits += suite["decide"](answers) == expected
    cache_path.write_text(json.dumps(cache, indent=1))
    n = len(suite["cases"])
    print(f"{name:9s} {hits}/{n}   p50 {statistics.median(ms):.0f} ms   "
          f"${cost / n * 1000:.2f} per 1000   unparseable: {broke}")


if __name__ == "__main__":
    for n in sys.argv[1:] or list(SUITES):
        run(n)
