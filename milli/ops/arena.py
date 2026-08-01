"""The Arena — race several models through the SAME harness, live.

This is the Eval/LLM-Ops pillar made visible. One message goes to N models at
once; each contestant runs the REAL loop — retrieval gate, tools, memory — in
its own throwaway home, so a race can create events and save notes without ever
touching your actual data. That isolation is the whole reason this is safe to
demo: `.milli/` is never opened here.

    prompt ──┬─→ model A ─→ own temp home ─→ gate · tools · reply ─┐
             ├─→ model B ─→ own temp home ─→ gate · tools · reply ─┼─→ SSE
             └─→ model C ─→ own temp home ─→ gate · tools · reply ─┘

Two scores, deliberately separate:
  Completion  deterministic — did the right tool fire, with the right args?
              (milli.ops.scoring, only for prompts in the battery)
  Quality     an LLM referee's grade (milli.ops.judge), run AFTER the race as one
              gentle pass so a burst of concurrent calls can't 429 half of them.

Results land in the arena's own JSONL scoreboard (milli.ops.compare_history) —
never state.db. dashboard.py owns the HTTP/SSE plumbing; this module owns the
race.
"""

from __future__ import annotations

import json
from pathlib import Path

from milli.config import load_settings
from milli.ops import compare_history, scoring
from milli.ops import judge as judge_mod
from milli.ops.pricing import cutoff_for, price_for


def compare_stream(message: str, specs: list, emit, judge: bool = False,
                   coding: bool = False, judge_spec: str = "", apple: bool = False) -> None:
    """Race the models and stream each one's harness LIVE — gate decision and
    tool calls, per model — so every column plays out like the chat dock instead
    of a static 'racing…'. Each contestant runs the REAL loop (tools included) in
    its own isolated temp home, so it can create events / save notes / search
    without touching your real data. Parallel threads share one SSE socket, so
    emit() is serialized behind a lock; each event is tagged with its `spec` so
    the browser routes it to the right column."""
    import tempfile
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from milli.app import Milli
    from milli.config import Settings

    if not message or not specs:
        emit("done", {"error": "message and models required"})
        return

    lock = threading.Lock()
    collected: list = []   # per-model results, saved to the compare history at the end
    # If this prompt is a known battery case, every column gets a deterministic
    # Completion score (did the right tool fire, with the right args, enough
    # times). Free-form prompts still race — they just don't get a score.
    case = scoring.case_for_message(message)

    def send(kind, ev):
        with lock:
            emit(kind, ev)
            if kind == "result":
                collected.append(ev)

    def run(spec):
        provider, _, model = spec.partition(":")
        send("start", {"spec": spec, "provider": provider, "model": model,
                       "cutoff": cutoff_for(model)})
        home = Path(tempfile.mkdtemp(prefix=f"compare-{provider}-"))
        gate: dict = {}

        # Stream the STRUCTURAL harness live (gate decision, tool calls) — these
        # fire from the observer without stream=True. We deliberately DON'T
        # token-stream the reply: stream=True makes some reasoning models (gemini
        # with tools) demand a thought_signature and 400, which the plain path
        # doesn't. So the harness plays out live and the reply lands on finish.
        def obs(kind, ev):
            if kind == "gate":
                gate.update(decision=ev.get("decision"), reason=ev.get("reason"))
                send("gate", {"spec": spec, "decision": ev.get("decision"), "reason": ev.get("reason")})
            elif kind == "tool":
                send("tool", {"spec": spec, "tool": ev.get("tool")})
            elif kind == "subagent":
                # delegate_task relays pi's live event stream (see experimental.py)
                # — forward it so the card can show the sub-agent working instead
                # of a black box. Text deltas are trimmed; this is a peek, not a log.
                out = {"spec": spec, **ev}
                if out.get("type") == "text" and len(out.get("delta", "")) > 200:
                    out["delta"] = out["delta"][:200]
                send("subagent", out)

        try:
            # coding mode registers delegate_task (the pi sub-agent) so the loop
            # can hand real programming work to pi — running the FULL harness
            # (gate, memory, tools), not a bypass. pi runs on this card's model.
            # apple_calendar defaults OFF (isolation), opt-in per race — when on,
            # EACH model writes its own event to the real 'Milli' calendar.
            settings = Settings(
                provider=provider,
                model=model,
                small_model="",
                home=home,
                apple_calendar=apple,
                google_calendar=False,
                experimental=coding,
            )
            app = Milli(settings=settings)
            # A scored case may pre-load a fact (e.g. "applies memory") so every
            # model starts from the same state the checklist assumes.
            if case and case.get("setup_fact"):
                app.memory.facts.add(case["setup_fact"]["subject"], case["setup_fact"]["content"])
            t0 = time.perf_counter()
            result = app.respond(message, source="compare", observer=obs)
            ms = int((time.perf_counter() - t0) * 1000)
            tin = tout = 0
            ledger = home / "usage.jsonl"
            if ledger.exists():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                        tin, tout = tin + r.get("in", 0), tout + r.get("out", 0)
                    except json.JSONDecodeError:
                        pass
            pin, pout = price_for(provider, settings.model)
            cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 4)
            completion = None
            if case:
                passed, why = scoring.check_case(case, result.tool_calls)
                completion = {"passed": passed, "why": why, "case": case["id"]}
            # Quality (referee grade) is NOT done here — it runs as one controlled
            # pass AFTER every column finishes (see below), so the referee doesn't
            # get a burst of concurrent calls and skip some.
            send("result", {"spec": spec, "provider": provider, "model": settings.model,
                            "reply": result.reply, "gate": (gate or None),
                            "iterations": result.iterations, "latency_ms": ms,
                            "tools": [{"tool": c["tool"]} for c in result.tool_calls],
                            "tokens_in": tin, "tokens_out": tout, "cost_usd": cost,
                            "cutoff": cutoff_for(settings.model),
                            "completion": completion, "quality": None})
        except (Exception, SystemExit) as exc:
            # SystemExit (not an Exception subclass) is what get_client raises for
            # a missing/misconfigured key. Catch it too, or a keyless provider
            # would vanish from the race silently instead of showing WHY it failed.
            send("result", {"spec": spec, "provider": provider, "model": model, "error": str(exc)[:200]})

    with ThreadPoolExecutor(max_workers=min(len(specs), 6)) as ex:
        list(ex.map(run, specs))

    # Grade AFTER the race, as one gentle pass — so the referee gets a steady
    # trickle of calls (max_workers=2) instead of a burst the moment every column
    # finishes, which used to 429 and leave some models ungraded. Each grade
    # updates its card ("grade" event) and the stored result, so history + the
    # scoreboard end up with every model scored.
    if judge:
        jp, _, jm = (judge_spec or "").partition(":")
        gradable = [r for r in collected if not r.get("error") and (r.get("reply") or "").strip()]
        emit("grading", {"n": len(gradable), "judge": jm or judge_mod.JUDGE_MODEL})

        def grade(r):
            if r.get("error") or not (r.get("reply") or "").strip():
                return
            q = judge_mod.judge_reply(message, r["reply"], jp or None, jm or None,
                                      tools=[t.get("tool") for t in (r.get("tools") or [])])
            r["quality"] = q                       # fold into what gets persisted
            send("grade", {"spec": r.get("spec"), "quality": q})

        with ThreadPoolExecutor(max_workers=2) as jex:
            list(jex.map(grade, list(collected)))

    # Persist the race to the arena's own history (never the agent's real state).
    try:
        compare_history.append_run(load_settings().home, message, collected)
    except Exception:
        pass   # a history-write hiccup must never fail the race
    emit("done", {})


def compare_clear(payload: dict) -> dict:
    """Wipe the Compare scoreboard/history (the Clear button). Only the arena's
    own log; nothing else is touched."""
    compare_history.clear(load_settings().home)
    return {"ok": True, "runs": [], "aggregate": []}


def history_response(runs: list[dict]) -> dict:
    """Reprice each stored result from its tokens with the CURRENT price table (so
    a pricing fix corrects past races), aggregate, and tag each row with the rate
    and knowledge cutoff (also from the current table, so a cutoff fix corrects
    past races too). The shared shape returned by /api/compare/history and the
    re-grade endpoint."""
    for run in runs:
        for r in run.get("results", []):
            r["cutoff"] = cutoff_for(r.get("model", ""))
            if r.get("error"):
                continue
            pin, pout = price_for(r.get("provider", ""), r.get("model", ""))
            r["cost_usd"] = round((r.get("tokens_in") or 0) / 1e6 * pin
                                  + (r.get("tokens_out") or 0) / 1e6 * pout, 4)
    agg = compare_history.aggregate(runs)
    for row in agg:
        row["rate_in"], row["rate_out"] = price_for(row["provider"], row["model"])
        row["cutoff"] = cutoff_for(row["model"])
    return {"runs": runs[-20:][::-1], "aggregate": agg}


def compare_regrade(payload: dict) -> dict:
    """Re-run the referee on the most recent race — for models the grader skipped
    (429'd) the first time. `only_missing` (default true) grades only the ungraded
    ones; pass false to re-grade everyone. Returns the refreshed history +
    scoreboard, same shape as /api/compare/history."""
    home = load_settings().home
    runs = compare_history.load_runs(home)
    if not runs:
        return {"runs": [], "aggregate": []}
    jp, _, jm = (payload.get("judge_model") or "").partition(":")
    only_missing = payload.get("only_missing", True)
    spec = payload.get("spec")   # grade just ONE card (the per-card button)
    last = runs[-1]
    for r in last.get("results", []):
        if r.get("error") or not (r.get("reply") or "").strip():
            continue
        if spec is not None and r.get("spec") != spec:
            continue
        if spec is None and only_missing and r.get("quality") is not None:
            continue
        q = judge_mod.judge_reply(last.get("message", ""), r["reply"], jp or None, jm or None,
                                  tools=r.get("tools"))   # history stores tools as [names]
        if q is not None:
            r["quality"] = q
    compare_history.save_runs(home, runs)
    return history_response(runs)


def compare_delete_run(payload: dict) -> dict:
    """Delete ONE race (by timestamp) from the scoreboard — its models drop out of
    the totals — leaving every other race intact. Returns the refreshed history."""
    home = load_settings().home
    ts = payload.get("ts")
    runs = [r for r in compare_history.load_runs(home) if r.get("ts") != ts]
    compare_history.save_runs(home, runs)
    return history_response(runs)
