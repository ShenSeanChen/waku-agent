"""Compare-arena history — the benchmark's OWN append-only log.

Deliberately separate from the agent's real state (state.db / MEMORY.md /
traces / usage.jsonl). A compare run is a benchmark: one prompt raced across N
models in throwaway sandboxes. It is NOT a conversation, a memory, or a
single-model trace, so it must not land in chat_log / facts / calendar (that
would pollute the Loop / Memory / Database / Ops views and undo the sandbox
isolation the arena depends on).

So it lives in its own JSONL — one line per race — mirroring the repo's other
append-only logs (usage.jsonl, traces/*.jsonl, eval_runs.jsonl). Read-mostly;
the per-model scoreboard is a simple scan, so no SQLite table is warranted.

File: ``<home>/compare/history.jsonl`` — newest last, capped to the most recent
``MAX_RUNS`` races so it stays small. This module is the single owner of that
file; the dashboard only calls append_run / load_runs / aggregate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

MAX_RUNS = 50      # keep the log small; older races roll off the front
REPLY_CAP = 1000   # truncate stored replies so the file doesn't bloat


def _path(home: Path) -> Path:
    return home / "compare" / "history.jsonl"


def _slim(r: dict) -> dict:
    """Keep only what the history list + scoreboard need, and cap the reply.
    Accepts a per-model result dict as the arena produces it (with gate as a
    {decision, reason} object and tools as [{tool}]) and flattens it."""
    gate = r.get("gate") or {}
    return {
        "spec": r.get("spec") or f"{r.get('provider')}:{r.get('model')}",
        "provider": r.get("provider"),
        "model": r.get("model"),
        "latency_ms": r.get("latency_ms"),
        "tokens_in": r.get("tokens_in"),
        "tokens_out": r.get("tokens_out"),
        "cost_usd": r.get("cost_usd"),
        "iterations": r.get("iterations"),
        "gate": gate.get("decision") if isinstance(gate, dict) else gate,
        "tools": [t.get("tool") for t in (r.get("tools") or [])],
        "error": r.get("error"),
        "completion": r.get("completion"),   # {passed, why, case} on a scored case, else None
        "quality": r.get("quality"),         # {score, reason, judge} when K3-judged, else None
        "reply": (r.get("reply") or "")[:REPLY_CAP],
    }


def append_run(home: Path, message: str, results: list[dict], ts: str | None = None) -> None:
    """Append one finished race and trim to the most recent MAX_RUNS.

    `results` is the list of per-model result dicts the arena already built.
    Rewrites the whole (capped) file — fine because it's tiny by construction."""
    record = {
        "ts": ts or datetime.now(UTC).isoformat(timespec="seconds"),
        "message": message,
        "results": [_slim(r) for r in results],
    }
    runs = load_runs(home)
    runs.append(record)
    save_runs(home, runs)


def save_runs(home: Path, runs: list[dict]) -> None:
    """Rewrite the (capped) history file — used by append_run and by re-grading,
    which mutates an existing race's stored results in place."""
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in runs[-MAX_RUNS:]) + "\n")


def clear(home: Path) -> None:
    """Wipe the compare history (the scoreboard's Clear button). Only removes the
    arena's own log — nothing else is touched."""
    _path(home).unlink(missing_ok=True)


def load_runs(home: Path, limit: int | None = None) -> list[dict]:
    """Recent races, oldest -> newest. `limit` returns only the last N."""
    path = _path(home)
    if not path.exists():
        return []
    runs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return runs[-limit:] if limit else runs


def aggregate(runs: list[dict]) -> list[dict]:
    """Per-model scoreboard across the given races: run count, successful count,
    and CUMULATIVE totals of latency / tokens / cost over the successful runs
    (errored runs count against `runs` but add nothing to the totals). Cheapest
    total first; the frontend can re-sort by any column."""
    acc: dict[str, dict] = {}
    for run in runs:
        for r in run.get("results", []):
            spec = r.get("spec") or f"{r.get('provider')}:{r.get('model')}"
            a = acc.setdefault(spec, {"spec": spec, "provider": r.get("provider"),
                                      "model": r.get("model"), "runs": 0, "ok": 0,
                                      "lat": 0, "tin": 0, "tout": 0, "cost": 0.0,
                                      "passed": 0, "scored": 0, "qsum": 0, "qn": 0})
            a["runs"] += 1
            if not r.get("error"):
                a["ok"] += 1
                a["lat"] += r.get("latency_ms") or 0
                a["tin"] += r.get("tokens_in") or 0
                a["tout"] += r.get("tokens_out") or 0
                a["cost"] += r.get("cost_usd") or 0.0
            # Completion: only races on a KNOWN battery case carry a verdict.
            c = r.get("completion")
            if c is not None:
                a["scored"] += 1
                a["passed"] += 1 if c.get("passed") else 0
            q = r.get("quality")
            if q is not None and q.get("score") is not None:
                a["qsum"] += q["score"]
                a["qn"] += 1
    out = [{"spec": a["spec"], "provider": a["provider"], "model": a["model"],
            "runs": a["runs"], "ok": a["ok"], "total_latency_ms": a["lat"],
            "total_tokens_in": a["tin"], "total_tokens_out": a["tout"],
            "total_tokens": a["tin"] + a["tout"],  # kept for back-compat / sorting
            "cases_passed": a["passed"], "cases_scored": a["scored"],
            "quality_n": a["qn"],
            "quality_avg": round(a["qsum"] / a["qn"], 1) if a["qn"] else None,
            "total_cost_usd": round(a["cost"], 4)} for a in acc.values()]
    out.sort(key=lambda x: x["total_cost_usd"])
    return out
