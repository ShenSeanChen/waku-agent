"""Dashboard — every pillar on one local page. Zero new dependencies.

    make dashboard        # → http://localhost:7777

One stdlib HTTP server reading the files Waku already writes:
  loop + harness   traces/*.jsonl   (turns, gate decisions, tool calls, tokens)
  memory           state.db         (facts, episodes, chat log, consolidation)
  tools            state.db + calendar.ics + outbox/
  eval             eval_report.json (written by `make gate`)

The overview mirrors the architecture diagram — every box is clickable and
opens that section's live data. The chat dock is a real gateway: type (or speak)
a message and watch the same harness (gate, loop, tools, memory) that the CLI/
voice/telegram gateways drive light up in the browser as it runs.

The frontend is plain static files (static/index.html + style.css + app.js)
served as-is — no build step, no framework. This file is just the server + API.
Bound to 127.0.0.1 only. For deep trace waterfalls use Phoenix (`make trace`).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from waku.config import load_settings
from waku.db import connect
from waku.ops import compare_history, scoring
from waku.ops import judge as judge_mod
from waku.ops.tracing import TraceEncodingError, iter_trace_lines

PORT = 7777
# The frontend lives in its own files (static/index.html + style.css + app.js),
# served as-is by this stdlib server — no build step, no framework. Edit those
# to change the UI; edit this file to change the server/API.
STATIC = Path(__file__).resolve().parent / "static"

# One shared agent for the browser gateway. Built lazily (first chat), reused
# across the threaded server's workers via a cross-thread connection + a lock
# so chats run one at a time — correct for a single-user local tool.
_agent = None
_agent_lock = threading.Lock()
_dashboard_session = None  # this dashboard run's chat thread (dated; stable across refreshes)


def _dash_session() -> str:
    """The thread new dashboard chats belong to. Resolved once per process:
    RESUME the most recent recent dashboard thread (so a restart keeps the chat
    on screen), else start a fresh dated one. Never the eternal 'default'."""
    global _dashboard_session
    if _dashboard_session is None:
        try:
            conn = connect(load_settings().home)
            _dashboard_session = _resume_or_new_session(conn)
            conn.close()
        except Exception:
            _dashboard_session = datetime.now().strftime("dashboard-%Y%m%d-%H%M%S")
    return _dashboard_session


def _resume_or_new_session(conn) -> str:
    """Pick this run's thread: RESUME the most recent dashboard thread if its
    last message is still fresh (within the idle window), else start a new dated
    one. Without this, every server restart minted a brand-new empty thread and
    the visible chat 'vanished' (it was only parked under the old id). An idle
    gap still rotates — that's _maybe_rotate_session's job once we're running."""
    idle_min = int(os.getenv("WAKU_SESSION_IDLE_MINUTES", "60"))
    # Match by source, not id prefix: "+ New chat" makes 's-...' ids, so a
    # 'dashboard-%' filter would orphan those threads on restart. Every dashboard
    # message is tagged source='dashboard' — that's the reliable signal.
    row = conn.execute(
        "SELECT session_id, MAX(created_at) AS last_at FROM chat_log "
        "WHERE source='dashboard' GROUP BY session_id "
        "ORDER BY last_at DESC LIMIT 1"
    ).fetchone()
    if row and row["last_at"]:
        try:
            last = datetime.strptime(row["last_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if idle_min <= 0 or (datetime.now(UTC) - last).total_seconds() <= idle_min * 60:
                return row["session_id"]
        except ValueError:
            pass
    return datetime.now().strftime("dashboard-%Y%m%d-%H%M%S")


def _get_agent():
    global _agent, _dashboard_session
    if _agent is None:
        from waku.app import Waku

        settings = load_settings()
        settings.ensure_home()
        conn = connect(settings.home, check_same_thread=False)
        _agent = Waku(settings=settings, conn=conn)
        # A dashboard run resumes its last recent thread (so a restart/refresh
        # keeps the chat on screen), or starts fresh if that thread is idle.
        # Same id collect() reports, so the dock restores the right conversation.
        _dashboard_session = _resume_or_new_session(conn)
        _agent.session.session_id = _dashboard_session
    return _agent


def _maybe_rotate_session(agent) -> None:
    """A returning user should get a FRESH thread, not last week's. If the
    current session's newest message is older than WAKU_SESSION_IDLE_MINUTES
    (default 60), rotate to a new dated session id — the old thread stays one
    click away in History. Live bug: a tester came back days later and their
    new chat landed in a week-old 32-message thread."""
    idle_min = int(os.getenv("WAKU_SESSION_IDLE_MINUTES", "60"))
    if idle_min <= 0:
        return
    row = agent.conn.execute("SELECT MAX(created_at) FROM chat_log WHERE session_id=?",
                             (agent.session.session_id,)).fetchone()
    if not row or not row[0]:
        return
    try:  # sqlite datetime('now') is UTC "YYYY-MM-DD HH:MM:SS"
        last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return
    if (datetime.now(UTC) - last).total_seconds() > idle_min * 60:
        agent.session.start_new(datetime.now().strftime("dashboard-%Y%m%d-%H%M%S"))


def chat(message: str) -> dict:
    """Run one real turn through the harness and return the structured result —
    gate decision, tool calls, reply, latency — so the browser can render the
    pipeline as it happened. Writes traces + memory like any other gateway."""
    events: list[dict] = []
    with _agent_lock:
        agent = _get_agent()
        _maybe_rotate_session(agent)
        start = datetime.now(UTC)
        result = agent.respond(message, observer=lambda kind, ev: events.append({"kind": kind, **ev}),
                               source="dashboard")
        latency_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)

    gate = next((e for e in events if e["kind"] == "gate"), None)
    cons = next((e for e in events if e["kind"] == "consolidation"), None)
    return {
        "reply": result.reply,
        "gate": {"decision": gate["decision"], "reason": gate.get("reason")} if gate else None,
        "tools": [
            {"tool": c["tool"], "args": c["args"], "output": c["output"],
             "status": _tool_status(c["output"]), "summary": (c["output"] or "").split(". ")[0][:120]}
            for c in result.tool_calls
        ],
        "consolidation": {"new_facts": cons["new_facts"]} if cons else None,
        "iterations": result.iterations,
        "latency_ms": latency_ms,
    }


def chat_stream(message: str, emit) -> None:
    """Run one turn, calling emit(kind, event) for every harness event AS it
    happens — gate decision, tool calls, and the reply text token by token —
    so the browser can show thinking stream in (like the CLI/voice do). Ends
    with a 'done' event carrying the final structured result."""
    events: list[dict] = []

    def observer(kind, ev):
        if kind in ("gate", "consolidation"):
            events.append({"kind": kind, **ev})
        emit(kind, ev)

    with _agent_lock:
        agent = _get_agent()
        _maybe_rotate_session(agent)
        start = datetime.now(UTC)
        result = agent.respond(message, observer=observer, source="dashboard", stream=True)
        latency_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)

    gate = next((e for e in events if e["kind"] == "gate"), None)
    cons = next((e for e in events if e["kind"] == "consolidation"), None)
    emit("done", {
        "reply": result.reply,
        "gate": {"decision": gate["decision"], "reason": gate.get("reason")} if gate else None,
        "tools": [{"tool": c["tool"], "args": c["args"], "output": c["output"],
                   "status": _tool_status(c["output"]),
                   "summary": (c["output"] or "").split(". ")[0][:120]} for c in result.tool_calls],
        "consolidation": {"new_facts": cons["new_facts"]} if cons else None,
        "iterations": result.iterations,
        "latency_ms": latency_ms,
        "model": agent.settings.model,   # which brain answered — shown per card
    })


def _compare_one(message: str, spec: str) -> dict:
    """Run ONE message through ONE model in a throwaway temp home (same isolation
    as `make shootout`, so it never touches your real memory/calendar), and
    return its receipts — reply, gate, tools, latency, tokens, cost. A broken
    contestant returns an {error} dict; it never raises."""
    import tempfile
    import time

    from waku.app import Waku
    from waku.config import Settings

    provider, _, model = spec.partition(":")
    home = Path(tempfile.mkdtemp(prefix=f"compare-{provider}-"))
    gate: dict = {}
    try:
        settings = Settings(provider=provider, model=model, small_model="",
                            home=home, apple_calendar=False)
        app = Waku(settings=settings)
        t0 = time.perf_counter()
        result = app.respond(message, source="compare",
                             observer=lambda k, ev: gate.update(
                                 decision=ev.get("decision"), reason=ev.get("reason"))
                             if k == "gate" else None)
        ms = int((time.perf_counter() - t0) * 1000)
        tin = tout = 0
        ledger = home / "usage.jsonl"
        if ledger.exists():
            for line in ledger.read_text().splitlines():
                try:
                    r = json.loads(line)
                    tin, tout = tin + r.get("in", 0), tout + r.get("out", 0)
                except json.JSONDecodeError:
                    pass
        pin, pout = price_for(provider, settings.model)
        return {"spec": spec, "provider": provider, "model": settings.model, "reply": result.reply,
                "gate": (gate or None), "iterations": result.iterations, "latency_ms": ms,
                "tools": [{"tool": c["tool"]} for c in result.tool_calls],
                "tokens_in": tin, "tokens_out": tout,
                "cost_usd": round(tin / 1e6 * pin + tout / 1e6 * pout, 4),
                "cutoff": cutoff_for(settings.model)}
    except (Exception, SystemExit) as exc:   # a broken contestant (incl. a missing
        # key, which get_client raises as SystemExit) fails alone, not the whole race
        return {"spec": spec, "provider": provider, "model": model, "error": str(exc)[:200]}


def compare_models(payload: dict) -> dict:
    """Race ONE message through several models AT ONCE (parallel threads) and
    return every result together. Non-streaming; the dashboard uses the SSE
    version so columns fill in as each finishes."""
    from concurrent.futures import ThreadPoolExecutor

    message = (payload.get("message") or "").strip()
    specs = payload.get("models") or []
    if not message or not specs:
        return {"error": "message and models required"}
    with ThreadPoolExecutor(max_workers=min(len(specs), 6)) as ex:
        results = list(ex.map(lambda s: _compare_one(message, s), specs))
    return {"ok": True, "message": message, "results": results}


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

    from waku.app import Waku
    from waku.config import Settings

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
            # EACH model writes its own event to the real 'Waku' calendar.
            settings = Settings(provider=provider, model=model, small_model="",
                                home=home, apple_calendar=apple, experimental=coding)
            app = Waku(settings=settings)
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
                for line in ledger.read_text().splitlines():
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


def _compare_history_response(runs: list[dict]) -> dict:
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
    return _compare_history_response(runs)


def compare_delete_run(payload: dict) -> dict:
    """Delete ONE race (by timestamp) from the scoreboard — its models drop out of
    the totals — leaving every other race intact. Returns the refreshed history."""
    home = load_settings().home
    ts = payload.get("ts")
    runs = [r for r in compare_history.load_runs(home) if r.get("ts") != ts]
    compare_history.save_runs(home, runs)
    return _compare_history_response(runs)


# Rough $/million tokens (in, out) for a dollar ESTIMATE — the number humans
# actually feel. Keyed by provider; deliberately approximate and labelled "est".
PRICING = {
    "anthropic": (3.0, 15.0), "openai": (2.5, 15.0), "gemini": (0.3, 2.5),
    "deepseek": (0.435, 0.87), "minimax": (0.30, 1.20), "kimi": (0.6, 2.5), "glm": (0.6, 2.2),
    "xai": (3.0, 15.0),   # Grok — rough est; keyed users get exact from the catalog
    # openrouter fallback for paid models when the live catalog is unreachable
    # (rough mid-catalog guess). ":free" ids and catalog-priced models never
    # hit this: see price_for().
    "openrouter": (1.0, 3.0),
}

# model id -> exact ($/M in, $/M out), filled from the live catalog fetch in
# list_models(). OpenRouter reports per-model pricing, so cost estimates can
# be exact per call instead of one number per provider.
_price_cache: dict[str, tuple[float, float]] = {}


# Known per-model prices ($/M in, out) for endpoints with no listable catalog
# (the anthropic wire has no /models), checked before the provider-level
# fallback. Within a provider, models diverge a LOT — fable-5 is ~2x opus,
# gemini-flash undercuts gemini-pro — so pricing per *model* is the only honest
# way; a provider-level guess made fable-5 look cheaper than opus. Rates are
# standard short-context list prices (cache/batch discounts not modelled),
# fact-checked Jul 2026 against each vendor's pricing page. See docs/benchmarks.md.
MODEL_PRICING = {
    # Anthropic — platform.claude.com/docs/.../pricing
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),            # Mythos-class flagship, ~2x opus
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # OpenAI — openai.com pricing (Sol = flagship; chat-latest = non-reasoning)
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.3-chat-latest": (1.75, 14.0),
    # Google Gemini — ai.google.dev pricing (standard <200k tier)
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "gemini-3.5-flash": (1.5, 9.0),
    # Moonshot Kimi — platform.kimi.ai (highspeed = 2x the standard k2.7 rate)
    "kimi-k3": (3.0, 15.0),
    "kimi-k2.7-code-highspeed": (1.9, 8.0),
    "kimi-k2.7": (0.95, 4.0),
    # xAI Grok — docs.x.ai/developers/pricing
    "grok-4.5": (2.0, 6.0),
    "grok-4.3": (1.25, 2.5),
}


# Knowledge cutoff (YYYY-MM) per model — the arena discloses when each brain's
# world knowledge ends, so stale knowledge isn't misread as low capability
# (gemini-3.1-pro will confidently deny that 2026 models exist: its cutoff is
# 2025-01, a year before them). Values are each vendor's published knowledge
# cutoff (for Anthropic, the "reliable knowledge cutoff"; training data runs
# later). None = the vendor hasn't published one. Fact-checked Jul 2026 against
# vendor model cards/docs. Every MODEL_PRICING id must have an entry here —
# enforced by evals/deterministic/test_providers.py.
MODEL_CUTOFF = {
    # Anthropic — support.claude.com "How up-to-date is Claude's training data?"
    "claude-opus-4-8": "2026-01",
    "claude-fable-5": "2026-01",
    "claude-sonnet-5": "2026-01",
    "claude-haiku-4-5-20251001": "2025-02",   # trained on data through 2025-07
    # OpenAI — developers.openai.com model pages
    "gpt-5.6-sol": "2026-02",
    "gpt-5.3-chat-latest": "2025-08",
    # Google Gemini — deepmind.google model cards / ai.google.dev
    "gemini-3.1-pro-preview": "2025-01",
    "gemini-3.5-flash": "2025-01",
    # Moonshot Kimi — K3 reported "early 2026"; K2.7 cutoffs unpublished
    "kimi-k3": "2026-01",
    "kimi-k2.7-code-highspeed": None,
    "kimi-k2.7": None,
    # xAI Grok — docs.x.ai model list
    "grok-4.5": "2026-02",
    "grok-4.3": "2025-12",
}


def cutoff_for(model: str) -> str | None:
    """Knowledge-cutoff date ('YYYY-MM') for a model id, or None when the
    vendor hasn't published one (the UI shows a dash rather than a guess)."""
    return MODEL_CUTOFF.get(model)


def price_for(provider: str, model: str) -> tuple[float, float]:
    """$/M tokens (in, out) for one call: the catalog's per-model price when
    known, $0 for ":free" ids, a known MODEL_PRICING id, else the provider-level
    PRICING estimate."""
    if model in _price_cache:
        return _price_cache[model]
    if model.endswith(":free"):
        return (0.0, 0.0)
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    return PRICING.get(provider, (3.0, 15.0))


def usage_summary(home) -> dict:
    """Read the PERMANENT spend ledger (usage.jsonl) → all-time tokens + dollar
    cost, plus per-day and per-provider breakdowns. Cost is derived from tokens
    with PRICING (approximate, labelled 'est'). This survives demo resets, so the
    number is the real running total — trustworthy, not a per-session guess."""
    recs = []
    path = home / "usage.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    def cost(r) -> float:
        # the ledger stores tokens + provider/model, so old rows reprice too
        pin, pout = price_for(r.get("provider", ""), r.get("model", ""))
        return r.get("in", 0) / 1e6 * pin + r.get("out", 0) / 1e6 * pout

    def add(bucket, key, extra):
        b = bucket.setdefault(key, {**extra, "calls": 0, "in": 0, "out": 0, "cost": 0.0})
        b["calls"] += 1
        b["in"] += r.get("in", 0)
        b["out"] += r.get("out", 0)
        b["cost"] += cost(r)

    by_day, by_provider = {}, {}
    for r in recs:
        day = (r.get("ts") or "")[:10]
        add(by_day, day, {"date": day})
        add(by_provider, r.get("provider", "?"), {"provider": r.get("provider", "?")})

    return {
        "calls": len(recs),
        "total_in": sum(r.get("in", 0) for r in recs),
        "total_out": sum(r.get("out", 0) for r in recs),
        "total_cost": round(sum(cost(r) for r in recs), 4),
        "by_day": sorted(by_day.values(), key=lambda x: x["date"], reverse=True)[:30],
        "by_provider": sorted(by_provider.values(), key=lambda x: -x["cost"]),
    }


def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _tool_status(output: str) -> str:
    """Classify a tool result for the UI: ok / warn / error — from the output
    string alone (tools already report honestly, so trust their words)."""
    low = (output or "").lower()
    if "failed" in low or "timed out" in low or low.startswith("error"):
        return "error"
    if "already exists" in low or "not synced" in low or "skipped" in low:
        return "warn"
    return "ok"


# Notion-backed episodes live across the network, so the client AND the result
# are cached with a short TTL — collect() runs on every dashboard auto-refresh
# and must not round-trip to Notion every few seconds (rate limits + latency).
# The sqlite path is a local query and doesn't need this.
_NOTION_EPISODES_TTL = 30.0   # seconds; the page polls ~every 5s
_notion_lock = threading.Lock()
_notion_store = None                       # built once (its constructor calls Notion)
_notion_episodes: tuple[float, list] | None = None   # (fetched_at, items)


def _get_notion_store():
    """The ONE NotionEpisodeStore for the whole dashboard process. Its
    constructor round-trips to Notion (data-source resolution), so it's built
    lazily and cached. Callers must hold _notion_lock."""
    global _notion_store
    if _notion_store is None:
        from waku.memory.episodic.notion_store import NotionEpisodeStore

        _notion_store = NotionEpisodeStore()
    return _notion_store


def collect() -> dict:
    """Everything the page shows, in one JSON blob."""
    settings = load_settings()
    settings.ensure_home()
    home = settings.home
    conn = connect(home)

    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    def episodes_payload() -> dict:
        """Episodes from the active backend: sqlite (default) or notion.
        A Notion outage must not take down the whole dashboard payload."""
        if settings.episodic_store != "notion":
            return {
                "source": "sqlite",
                "error": "",
                "items": rows(
                    "SELECT id, happened_at, summary FROM episodes ORDER BY happened_at DESC"
                ),
            }
        try:
            global _notion_episodes
            with _notion_lock:
                store = _get_notion_store()
                if _notion_episodes and time.time() - _notion_episodes[0] < _NOTION_EPISODES_TTL:
                    return {"source": "notion", "error": "", "items": _notion_episodes[1]}
                items = store.list()
                _notion_episodes = (time.time(), items)
                return {"source": "notion", "error": "", "items": items}
        except Exception as exc:
            # Degrade gracefully: never take the payload down, and serve the
            # last good fetch if we have one (an outage shouldn't blank the tab).
            stale = _notion_episodes[1] if _notion_episodes else []
            return {"source": "notion", "error": str(exc), "items": stale}

    episodes_data = episodes_payload()

    # --- traces → turns (group events between turn_start and turn_end)
    events = []
    trace_errors = []
    trace_files = sorted((home / "traces").glob("*.jsonl"))
    for path in trace_files:
        try:
            lines = list(iter_trace_lines(path))
        except TraceEncodingError as exc:
            trace_errors.append({"file": path.name, "error": str(exc)})
            continue
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    turns, current, wake_scans = [], None, []
    for ev in events:
        kind = ev.get("type")
        if kind == "turn_start":
            current = {"user_message": ev.get("user_message"), "ts": ev.get("ts"),
                       "gate": None, "llm_calls": [], "tools": [], "reply": None}
        elif kind == "wake_scan":
            wake_scans.append(ev)
        elif current is not None:
            if kind == "gate":
                current["gate"] = ev
            elif kind == "llm":
                current["llm_calls"].append(ev)
            elif kind == "tool":
                current["tools"].append(ev)
            elif kind == "consolidation":
                current["consolidation"] = ev
            elif kind == "turn_end":
                current["reply"] = ev.get("reply")
                current["iterations"] = ev.get("iterations")
                turns.append(current)
                current = None
    if current is not None:  # a turn that never ended = the smoking gun for hangs
        current["reply"] = "TURN NEVER FINISHED — check for a hang after this point"
        current["unfinished"] = True
        turns.append(current)

    # --- derive per-turn latency + dollar cost (the ops numbers humans feel)
    if settings.base_url or settings.provider == "openrouter":
        list_models()  # warm the per-model price cache (5-min cached fetch)
    price_in, price_out = price_for(settings.provider, settings.model or "")
    for t in turns:
        start, end = _parse_ts(t["ts"]), None
        last = t["llm_calls"][-1]["ts"] if t["llm_calls"] else None
        end = _parse_ts(last)
        t["latency_ms"] = int((end - start).total_seconds() * 1000) if start and end else None
        tin = sum(c.get("usage", {}).get("in", 0) for c in t["llm_calls"])
        tout = sum(c.get("usage", {}).get("out", 0) for c in t["llm_calls"])
        t["cost"] = tin / 1e6 * price_in + tout / 1e6 * price_out
        for x in t["tools"]:
            x["status"] = _tool_status(x.get("output", ""))
            x["summary"] = (x.get("output", "") or "").split(". ")[0][:120]

    latencies = sorted(t["latency_ms"] for t in turns if t["latency_ms"] is not None)
    total_cost = sum(t["cost"] for t in turns)

    def pct(p: float) -> int:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0

    from waku.memory import REPO_SKILLS
    from waku.memory.procedural.loader import SkillLoader

    skills = [{"name": s.name, "description": s.description, "body": s.body,
               "path": str(s.path),
               # relative path (for reveal) + whether it lives in the editable home dir
               "rel": _rel_to_home(s.path, home),
               "editable": str((home / "skills").resolve()) in str(s.path.resolve())}
              for s in SkillLoader([REPO_SKILLS, home / "skills"]).skills]

    eval_report = None
    report_path = home / "eval_report.json"
    if report_path.exists():
        eval_report = json.loads(report_path.read_text())

    eval_history = []
    hist_path = home / "eval_runs.jsonl"
    if hist_path.exists():
        for line in hist_path.read_text().splitlines()[-20:]:
            try:
                eval_history.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    eval_history.reverse()

    outbox = [{"name": p.name, "text": p.read_text()[:400]}
              for p in sorted((home / "outbox").glob("*.txt"), reverse=True)[:20]]

    # --- state.db introspection: the actual SQLite tables, so the persistence
    # layer is visible (not just its contents). Table names are hard-coded, so
    # the f-string SQL is safe.
    def table_info(name):
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        cols = [r["name"] for r in info]
        types = {r["name"]: r["type"] for r in info}
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        # up to 200 newest rows so each table has its own scrollable tab
        sample = [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT 200").fetchall()]
        return {"name": name, "columns": cols, "types": types, "count": count, "sample": sample}

    db_path = home / "state.db"
    all_tables = [r["name"] for r in
                  conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    db_info = {
        "path": str(db_path.resolve()),
        "size": db_path.stat().st_size if db_path.exists() else 0,
        "tables": [table_info(n) for n in ("calendar_events", "facts", "episodes", "chat_log")],
        "fts": [t for t in all_tables if t.endswith("_fts")],
        "all_tables": all_tables,
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "home": str(home.resolve()),
        "provider": settings.provider,
        "model": settings_info()["model"],
        "stats": {
            "turns": len(turns),
            "tool_calls": sum(len(t["tools"]) for t in turns),
            "tool_errors": sum(1 for t in turns for x in t["tools"] if x["status"] == "error"),
            "gate_skips": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "skip"),
            "gate_retrieves": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "retrieve"),
            "tokens_in": sum(c.get("usage", {}).get("in", 0) for t in turns for c in t["llm_calls"]),
            "tokens_out": sum(c.get("usage", {}).get("out", 0) for t in turns for c in t["llm_calls"]),
            "cost": round(total_cost, 4),
            "latency_avg": int(sum(latencies) / len(latencies)) if latencies else 0,
            "latency_p95": pct(0.95),
            "trace_files": len(trace_files),
        },
        "turns": turns[::-1][:50],
        "wake_scans": wake_scans[::-1][:25],
        # last raw trace lines, so Ops shows traces inline (no folder needed)
        "trace_tail": [{"type": e.get("type"), "ts": e.get("ts"),
                        "detail": (e.get("user_message") or e.get("decision") or e.get("tool")
                                   or e.get("reply") or "")}
                       for e in events[-18:]][::-1],
        "trace_file": (trace_files[-1].name if trace_files else None),
        "trace_errors": trace_errors,
        "facts": rows("SELECT id, subject, content, source, created_at FROM facts ORDER BY id DESC"),
        "episodes": episodes_data["items"],
        "episodes_source": episodes_data["source"],
        "episodes_error": episodes_data["error"],
        "soul": (home / "SOUL.md").read_text() if (home / "SOUL.md").exists() else "",
        "chat_pending": conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated=0").fetchone()[0],
        "chat_log": rows("SELECT role, content, consolidated, source, session_id, created_at FROM chat_log ORDER BY id DESC LIMIT 80")[::-1],
        "sessions": session_list(conn),
        "current_session": (_agent.session.session_id if _agent is not None else _dash_session()),
        "consolidate_every": settings.consolidate_every,
        "calendar": rows('SELECT title, start, "end", attendees, created_at FROM calendar_events ORDER BY start'),
        "outbox": outbox,
        "skills": skills,
        "eval_report": eval_report,
        "eval_history": eval_history,
        "db": db_info,
        "settings": settings_info(),
        "tools": tools_info(),
        "usage": usage_summary(home),
    }


def _rel_to_home(path, home) -> str:
    """Path relative to WAKU_HOME if it lives there, else the repo-relative
    'skills/...' path — either way something reveal_path can open."""
    try:
        return str(path.resolve().relative_to(home.resolve()))
    except ValueError:
        return str(path)


def session_list(conn) -> list[dict]:
    """One row per conversation for the chat-history picker: id, its first user
    message (the title), message count, newest first. Sessions are just a
    session_id label on chat_log rows — the same table, no new storage."""
    groups = conn.execute(
        """SELECT session_id, COUNT(*) AS messages, MAX(created_at) AS last_at
           FROM chat_log GROUP BY session_id ORDER BY last_at DESC"""
    ).fetchall()
    out = []
    for g in groups:
        sid = g["session_id"]
        first = conn.execute(
            "SELECT content FROM chat_log WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
            (sid,),
        ).fetchone()
        last = conn.execute(
            "SELECT role, content FROM chat_log WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
        sources = [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM chat_log WHERE session_id=?", (sid,)).fetchall()]
        preview = ""
        if last:
            preview = ("you: " if last["role"] == "user" else "waku: ") + last["content"][:80]
        out.append({"id": sid,
                    "title": (first["content"][:60] if first else "(empty)"),
                    "last": preview,
                    "sources": sources,
                    "messages": g["messages"],
                    "last_at": g["last_at"]})
    return out


# A tool's origin, for grouping in the Tools tab (name → category).
_FLAGSHIP = {"create_event", "list_events", "save_note", "send_message"}
_SELFMGMT = {"manage_memory", "update_soul", "create_skill"}
_APPLE = {"read_apple_calendar", "read_apple_mail", "create_reminder", "create_note"}
_WEB = {"search_web"}


def _tool_source(name: str, mcp_servers: list[str]) -> str:
    if name in _FLAGSHIP:
        return "flagship"
    if name in _WEB:
        return "web"
    if name in _SELFMGMT:
        return "self-management"
    if name in _APPLE:
        return "apple"
    if any(name.startswith(f"{s}_") for s in mcp_servers):
        return "mcp"
    return "other"


def tools_info() -> dict:
    """The agent's available tools + any configured MCP servers — so the Tools
    tab shows CAPABILITIES, not just the artifacts tool calls produced. Reflects
    the live agent's registry when one exists (exact), else builds a display-only
    catalog (no MCP subprocess is spawned just to render the page)."""
    settings = load_settings()
    settings.ensure_home()
    mcp = {"configured": False, "servers": [], "live": False}
    mcp_path = settings.home / "mcp.json"
    if mcp_path.exists():
        mcp["configured"] = True
        try:
            mcp["servers"] = [s.get("name", "?") for s in json.loads(mcp_path.read_text()).get("servers", [])]
        except (json.JSONDecodeError, OSError):
            pass

    catalog = []
    if _agent is not None:
        mcp["live"] = getattr(_agent, "mcp_bridge", None) is not None
        tools = list(_agent.tools._tools.values())
    else:
        # Display-only: same tools minus MCP (building the real registry would
        # start MCP servers, which we don't want on a 5-second poll).
        from waku.memory import Memory
        from waku.tools import calendar, memory_admin, messages, notes, search

        conn = connect(settings.home)
        try:
            # Notion mode: reuse the dashboard's one cached client instead of
            # letting Memory() build a fresh one per poll (issue #20).
            episode_store = None
            if settings.episodic_store == "notion":
                with _notion_lock:
                    episode_store = _get_notion_store()
            mem = Memory(conn, settings, None, episode_store=episode_store)
        except Exception:
            # A misconfigured optional backend (notion/supabase) must not take
            # the dashboard down — drop the memory-admin tools from the
            # display-only catalog instead.
            mem = None
        tools = [calendar.make_tool(conn, settings.home, apple_calendar=settings.apple_calendar),
                 calendar.make_list_tool(conn),
                 notes.make_tool(conn), messages.make_tool(settings.home),
                 search.make_tool(),
                 memory_admin.make_update_soul_tool(settings)]
        if mem is not None:
            tools += [memory_admin.make_manage_memory_tool(mem),
                      memory_admin.make_create_skill_tool(settings, mem)]
        if settings.apple_tools:
            from waku.tools import apple

            tools += apple.make_tools()
        if settings.experimental:
            # Mirror build_registry: without this the catalog LIES after you
            # flip the experimental toggle — delegate_task is missing until the
            # first chat turn builds the real agent, so it looks like the
            # switch did nothing.
            from waku.tools import experimental as experimental_tools

            tools += experimental_tools.make_tools(settings)
    for t in tools:
        catalog.append({"name": t.name, "description": t.description,
                        "source": _tool_source(t.name, mcp["servers"])})
    catalog.sort(key=lambda c: (c["source"], c["name"]))
    from waku.tools.experimental import PLANNED

    return {"catalog": catalog, "mcp": mcp, "apple_on": settings.apple_tools,
            "planned": PLANNED}   # whiteboard boxes not wired in yet (coming soon)


def run_query(payload: dict) -> dict:
    """A tiny read-only SQL console (the Supabase-editor idea, scoped down).
    Opens state.db in read-only mode so a write can't slip through, and only
    accepts a single SELECT/WITH statement. Caps at 200 rows."""
    sql = (payload.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        return {"error": "Type a SELECT query."}
    low = sql.lower()
    if not (low.startswith(("select", "with"))):
        return {"error": "Only SELECT (or WITH … SELECT) queries are allowed."}
    if ";" in sql:
        return {"error": "One statement at a time (no semicolons)."}
    import sqlite3

    settings = load_settings()
    settings.ensure_home()
    db = (settings.home / "state.db").resolve()
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        cur = c.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        data = [[str(r[i]) if r[i] is not None else "" for i in range(len(cols))]
                for r in cur.fetchmany(200)]
        c.close()
        return {"columns": cols, "rows": data}
    except sqlite3.Error as exc:
        return {"error": str(exc)}


_whisper = None
_whisper_lock = threading.Lock()


def transcribe_audio(raw: bytes) -> dict:
    """Server-side speech-to-text for the dashboard mic button — the SAME local
    Whisper (`make voice` uses it), so voice works in the browser without any
    cloud. Needs the [voice] extra. Returns {text} or a friendly {error}."""
    if not raw:
        return {"error": "no audio received"}
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"error": "voice isn't installed — run: pip install -e '.[voice]'"}
    global _whisper
    import os as _os
    import tempfile

    with _whisper_lock:
        if _whisper is None:
            _whisper = WhisperModel(os.getenv("WAKU_WHISPER_MODEL", "base"), compute_type="int8")
    # the browser sends WAV (PCM) — Whisper/PyAV decode it reliably (WebM/Opus
    # from MediaRecorder often fails to decode).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(raw)
    try:
        segments, _ = _whisper.transcribe(tmp.name)
        return {"text": " ".join(s.text for s in segments).strip()}
    except Exception as exc:
        return {"error": f"transcription failed: {exc}"}
    finally:
        try:
            _os.unlink(tmp.name)
        except OSError:
            pass


def _thread_history(conn, sid: str) -> list[dict]:
    """The ONE way to load a thread for the chat dock: role + content + the
    per-turn meta (gate/stats/tools/model) so every card renders in full.
    id '__all__' returns the whole cross-thread timeline (like the Loop tab,
    but as chat). Every history-loading path goes through here so they can't
    drift apart (they used to: 'switch' dropped meta and showed only text)."""
    if sid == "__all__":
        rows = conn.execute(
            "SELECT role, content, meta FROM chat_log ORDER BY id DESC LIMIT 200"
        ).fetchall()[::-1]
    else:
        rows = conn.execute(
            "SELECT role, content, meta FROM chat_log WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"],
             "meta": json.loads(r["meta"]) if r["meta"] else None} for r in rows]


def session_action(payload: dict) -> dict:
    """Chat history control: start a new conversation, switch to a past one, or
    read a conversation's history (read-only, for the live inbox). Sessions live
    in chat_log."""
    action = payload.get("action")
    if action == "history":
        # read-only view of a conversation — never touches the agent, so the
        # dashboard can poll it live (e.g. to show new Telegram messages arrive).
        settings = load_settings()
        settings.ensure_home()
        conn = connect(settings.home)
        sid = payload.get("id") or "default"
        return {"ok": True, "session_id": sid, "history": _thread_history(conn, sid)}
    with _agent_lock:
        agent = _get_agent()
        if action == "new":
            sid = datetime.now().strftime("s-%Y%m%d-%H%M%S")
            agent.session.start_new(sid)
            return {"ok": True, "session_id": sid, "history": []}
        if action == "switch":
            sid = payload.get("id") or "default"
            agent.session.switch(sid)
            # Same meta-rich rows as the read-only "history" action, so a
            # switched thread renders its full turn cards (gate/stats/tools/
            # model) — not just the text. (These two paths used to disagree.)
            return {"ok": True, "session_id": sid, "history": _thread_history(agent.conn, sid)}
    return {"error": f"unknown action {action}"}


def _editor_cmd() -> list[str] | None:
    """The user's code editor CLI: $WAKU_EDITOR, then cursor, then code."""
    import shutil

    custom = os.getenv("WAKU_EDITOR")
    if custom and shutil.which(custom):
        return [custom]
    for cli in ("cursor", "code"):
        if shutil.which(cli):
            return [cli]
    return None


def reveal_path(rel: str) -> dict:
    """Open a file/folder under WAKU_HOME — in the user's code editor if one
    is on PATH (cursor/code/$WAKU_EDITOR), otherwise reveal in Finder.
    Restricted to paths inside WAKU_HOME."""
    import subprocess
    import sys

    settings = load_settings()
    settings.ensure_home()
    home = settings.home.resolve()
    target = (home / (rel or ".")).resolve()
    if target != home and home not in target.parents:
        return {"error": "path is outside the .waku home"}
    if not target.exists():
        return {"error": f"not found: {target}"}

    editor = _editor_cmd()
    if editor and target.is_file() and target.suffix != ".db":  # editors choke on sqlite
        subprocess.run([*editor, str(target)], check=False)
        return {"ok": True, "opened_in": editor[0], "path": str(target)}
    if sys.platform != "darwin":
        return {"error": f"no editor found and reveal is macOS-only — the path is {target}"}
    subprocess.run(
        ["open", "-R", str(target)] if target.is_file() else ["open", str(target)],
        check=False,
    )
    return {"ok": True, "revealed": str(target)}


def memory_action(payload: dict) -> dict:
    """Human CRUD on memory from the dashboard: update/delete facts & episodes,
    rewrite SOUL.md. Writes the same sqlite file the agent uses (busy_timeout
    covers contention); changes are live for the next agent turn."""
    from waku.memory.episodic.store import SqliteEpisodeStore
    from waku.memory.semantic.store import SqliteFactStore

    settings = load_settings()
    settings.ensure_home()
    action = payload.get("action")
    if action == "save_soul":
        text = (payload.get("content") or "").strip()
        if not text:
            return {"error": "SOUL cannot be empty"}
        (settings.home / "SOUL.md").write_text(text + "\n")
        return {"ok": True}
    if action == "save_skill":
        # Edit any loaded SKILL.md by hand (same file the agent's create_skill
        # writes) — repo skills and home skills alike. Sandboxed to the two
        # skills folders; validates the frontmatter before writing.
        from pathlib import Path

        from waku.memory import REPO_SKILLS
        from waku.memory.procedural.loader import _parse_text

        text = (payload.get("content") or "").strip()
        dest = Path(payload.get("path") or "").resolve()
        allowed = [REPO_SKILLS.resolve(), (settings.home / "skills").resolve()]
        if dest.name != "SKILL.md" or not any(a in dest.parents for a in allowed):
            return {"error": "can only edit SKILL.md files inside the skills folders"}
        if _parse_text(text, dest) is None:
            return {"error": "invalid SKILL.md — needs a name and description in the frontmatter"}
        dest.write_text(text.rstrip() + "\n", encoding="utf-8")
        return {"ok": True}

    conn = connect(settings.home)
    facts, episodes = SqliteFactStore(conn), SqliteEpisodeStore(conn)
    if action == "delete_episode" and settings.episodic_store == "notion":
        global _notion_episodes
        with _notion_lock:
            ok = _get_notion_store().delete(str(payload.get("id", "")))
            # bust the TTL cache so the next collect() refetches — otherwise a
            # deleted episode would linger on the page for up to 30s
            _notion_episodes = None
        return {"ok": ok}
    try:
        rid = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return {"error": "bad id"}
    if action == "update_fact":
        return {"ok": facts.update(rid, payload.get("content", ""), payload.get("subject") or None)}
    if action == "delete_fact":
        return {"ok": facts.delete(rid)}
    if action == "delete_episode":
        return {"ok": episodes.delete(rid)}
    return {"error": f"unknown action {action}"}


_models_cache: dict[str, tuple[float, list]] = {}


def _known_default_ids(prov, out: dict, is_active: bool) -> list[dict]:
    """Best-effort model list when the live catalog is unreachable: the provider's
    flagship + fast + loop/gate defaults — so the showcase model (e.g. opus-4.8)
    is offered too, not just the two loop defaults — plus the active model when
    this is the active provider."""
    ids = [*(prov.default_pair() if prov else []),
           prov.model if prov else "", prov.small_model if prov else ""]
    if is_active:
        ids = [out.get("model"), out.get("small_model"), *ids]
    return [{"id": m} for m in dict.fromkeys(m for m in ids if m)]


def list_models(provider: str | None = None) -> dict:
    """Model ids available on a provider, for the settings model picker — the
    defaults are starting points, never the menu. Pass `provider` to list ANY
    provider's catalog (the "Your models" add-row picks a provider first);
    without it, the ACTIVE provider is used. Three sources: an explicit
    Provider.catalog_url (anthropic, kimi), GET {base_url}/models on
    OpenAI-compatible endpoints (OpenRouter, Gemini, any WAKU_BASE_URL), or the
    two known defaults when no catalog exists. OpenRouter entries carry free /
    tool-support / context metadata so the picker can surface the $0
    tool-capable models. Cached 5 minutes."""
    import time
    import urllib.request

    from waku.loop.models import PROVIDERS

    s = load_settings()
    # An explicit provider overrides the active one (and its custom base_url:
    # WAKU_BASE_URL only applies to the provider it was set for).
    name = provider or s.provider
    prov = PROVIDERS.get(name)
    base = (s.base_url if name == s.provider else None) or (prov.base_url if prov else None)
    out = {
        "provider": name,
        "model": s.model or (prov.model if prov else ""),
        "small_model": s.small_model or (prov.small_model if prov else ""),
        "endpoint": base or name,
    }
    # Where can this provider's models be listed? An explicit catalog_url wins
    # (kimi chats on the anthropic wire but lists on its OpenAI-compatible API;
    # anthropic itself has GET /v1/models); otherwise openai-wire endpoints get
    # {base_url}/models; otherwise fall back to the two known defaults.
    if prov is not None and prov.catalog_url:
        url = prov.catalog_url
    elif prov is not None and prov.kind == "openai" and base:
        url = base.rstrip("/") + "/models"
    else:
        # No catalog endpoint: fall back to the provider's own known defaults
        # (flagship + fast + loop/gate), not just the active model.
        return {**out, "listed": False,
                "models": _known_default_ids(prov, out, name == s.provider)}

    cached = _models_cache.get(url)
    if cached and time.time() - cached[0] < 300:
        _ts, cmodels, cerr = cached          # cerr None on a real listing
        r = {**out, "listed": cerr is None, "models": cmodels}
        if cerr:
            r["error"] = cerr
        return r
    # Use this provider's own key; s.api_key only holds the ACTIVE provider's.
    key = ((s.api_key if name == s.provider else "") or os.getenv(prov.key_env, "")).strip()
    # HTTP headers must be latin-1; a key with a stray non-ASCII char (a smart
    # arrow/quote or a line-break from a bad paste) would otherwise crash the
    # whole listing with an opaque codec error and silently drop back to two
    # defaults. Catch it here with a message that actually says how to fix it.
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        msg = (f"{prov.key_env} contains a non-ASCII character — re-paste the key "
               f"(no spaces, line breaks, or arrows).")
        return {**out, "listed": False,
                "models": _known_default_ids(prov, out, name == s.provider), "error": msg}
    # send both auth styles — Bearer for OpenAI-compatible catalogs, x-api-key +
    # version for Anthropic's; each server reads the header it knows
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "x-api-key": key, "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        # Surface the server's actual reason (e.g. xAI's 403 "no credits"), not
        # just "HTTP Error 403" — an HTTPError carries the body on .read().
        msg = str(exc)
        try:
            msg = f"{msg} — {exc.read().decode()[:160]}"
        except Exception:
            pass
        # still offer the provider's known defaults so the picker isn't empty
        known = _known_default_ids(prov, out, name == s.provider)
        # cache the failure (defaults + reason) for ~1 minute so an unreachable
        # catalog doesn't stall every 5-second dashboard poll for 10s — and so a
        # cache hit still shows the defaults and the reason, not a blank list.
        _models_cache[url] = (time.time() - 240, known, msg)
        return {**out, "listed": False, "models": known, "error": msg}
    models = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        params = m.get("supported_parameters")
        entry = {
            "id": mid,
            "free": mid.endswith(":free") or pricing.get("prompt") == "0",
            # None means the endpoint doesn't say (only OpenRouter reports this)
            "tools": ("tools" in params) if params is not None else None,
            # reasoning models spend tokens thinking out loud, which breaks the
            # gate's tiny budget: the UI steers them away from the gate slot
            "reasoning": ("reasoning" in params) if params is not None else None,
            "context": m.get("context_length"),
        }
        try:
            # OpenRouter prices are $/token strings; keep $/M for display + cost
            pin, pout = float(pricing["prompt"]) * 1e6, float(pricing["completion"]) * 1e6
            _price_cache[mid] = (pin, pout)
            entry["price_in"], entry["price_out"] = round(pin, 3), round(pout, 3)
        except (KeyError, TypeError, ValueError):
            pass
        models.append(entry)
    models.sort(key=lambda x: (not x["free"], x["tools"] is False, x["id"]))
    _models_cache[url] = (time.time(), models, None)   # None error = a real listing
    return {**out, "listed": True, "models": models}


def _models_json() -> Path:
    return load_settings().home / "models.json"


def default_pinned_specs() -> list[str]:
    """Starter shortlist before the user has curated their own: flagship + fast
    for every provider that has a key set (so the switcher only shows models you
    can actually use). Flagship comes first, so it's that provider's default."""
    from waku.loop.models import PROVIDERS

    specs = []
    for name, prov in PROVIDERS.items():
        if os.getenv(prov.key_env):
            specs += [f"{name}:{m}" for m in prov.default_pair()]
    return specs


def pinned_specs() -> list[str]:
    """The user's curated 'provider:model' shortlist (ordered), from
    .waku/models.json. The chat switcher shows exactly these. Before they've
    saved anything, fall back to the flagship+fast defaults."""
    p = _models_json()
    if p.exists():
        try:
            return json.loads(p.read_text()).get("pinned", [])
        except (json.JSONDecodeError, OSError):
            pass
    return default_pinned_specs()


def default_model_for(provider: str) -> str:
    """A provider's default model = the FIRST one the user pinned for it.
    Empty string means 'use the provider's built-in default'."""
    for spec in pinned_specs():
        p, _, m = spec.partition(":")
        if p == provider and m:
            return m
    return ""


def pin_action(payload: dict) -> dict:
    """Manage the curated model shortlist: pin / unpin / make-default."""
    action = payload.get("action")
    provider, model = payload.get("provider", ""), payload.get("model", "")
    if not provider or not model:
        return {"error": "provider and model required"}
    spec = f"{provider}:{model}"
    specs = [s for s in pinned_specs() if s != spec]
    if action == "pin":
        specs.append(spec)
    elif action == "default":
        # move to the front of its provider's group -> becomes that provider's default
        idx = next((i for i, s in enumerate(specs) if s.split(":", 1)[0] == provider), len(specs))
        specs.insert(idx, spec)
    elif action != "unpin":
        return {"error": f"unknown action {action}"}
    path = _models_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pinned": specs}, indent=1))
    return {"ok": True, **settings_info()}


def settings_info() -> dict:
    """Current provider/model + which keys are set — masked to last-4, never
    the full key. `pinned` is the user's curated model shortlist (the chat
    switcher shows exactly these, across providers)."""
    from waku.loop.models import PROVIDERS

    s = load_settings()
    prov = PROVIDERS.get(s.provider)
    # the curated shortlist, in order; the first pinned model per provider is
    # that provider's default (used when you switch providers).
    pinned, seen = [], set()
    for spec in pinned_specs():
        p, _, m = spec.partition(":")
        if m:
            pinned.append({"provider": p, "model": m, "default": p not in seen})
            seen.add(p)
    # Group by provider for display (so all of one lab's models sit together,
    # e.g. a late-added claude-fable-5 joins the other anthropic rows). A STABLE
    # sort by provider's first-appearance order keeps each provider's own order —
    # so its default (first pinned) stays on top and the 'default' flags above
    # still line up.
    prov_order: dict = {}
    for row in pinned:
        prov_order.setdefault(row["provider"], len(prov_order))
    pinned.sort(key=lambda row: prov_order[row["provider"]])
    return {
        "provider": s.provider,
        "model": s.model or (prov.model if prov else ""),
        "small_model": s.small_model or (prov.small_model if prov else ""),
        "pinned": pinned,
        # a custom endpoint (e.g. OpenRouter) set via WAKU_BASE_URL / WAKU_API_KEY
        "base_url": s.base_url or "",
        "custom_key_set": bool(s.api_key),
        "providers": [
            {"name": name, "key_env": p.key_env,
             "key_set": bool(os.getenv(p.key_env)),
             "key_last4": (os.getenv(p.key_env) or "")[-4:],
             "default_model": p.model, "default_small_model": p.small_model}
            for name, p in PROVIDERS.items()
        ],
        # experimental tools (delegate_task -> pi). The ARENA can switch this on
        # per-race, but the chat agent reads it from the environment — so without
        # a toggle here, the sidebar chat could never delegate. See settings_save.
        "experimental": s.experimental,
        "pi_installed": bool(shutil.which("pi")),
        # optional web-search key (Tavily) — same BYOK treatment as provider keys
        "search_key_env": "TAVILY_API_KEY",
        "search_key_set": bool(os.getenv("TAVILY_API_KEY")),
        "search_key_last4": (os.getenv("TAVILY_API_KEY") or "")[-4:],
        # episodic-memory backend: sqlite (default) or notion
        "episodic_store": s.episodic_store,
        "notion_token_set": bool(os.getenv("NOTION_TOKEN")),
        "notion_token_last4": (os.getenv("NOTION_TOKEN") or "")[-4:],
        "notion_db_set": bool(os.getenv("NOTION_EPISODES_DATABASE_ID")),
        "notion_db_last4": (os.getenv("NOTION_EPISODES_DATABASE_ID") or "")[-4:],
    }


def apply_settings(payload: dict) -> dict:
    """Write .env + os.environ, then rebuild the agent so the switch is live.
    Never logs keys; only whitelisted env names are writable."""
    global _agent
    from dotenv import find_dotenv, set_key

    from waku.loop.models import PROVIDERS

    provider = payload.get("provider")
    if provider not in PROVIDERS:
        return {"error": f"unknown provider {provider}"}
    episodic_store = payload.get("episodic_store")
    if episodic_store is not None and episodic_store not in ("sqlite", "notion"):
        return {"error": f"unknown episodic_store {episodic_store}"}
    before = {"provider": os.getenv("WAKU_PROVIDER", ""),
              "model": os.getenv("WAKU_MODEL", ""),
              "small_model": os.getenv("WAKU_SMALL_MODEL", "")}
    writable = ({"WAKU_PROVIDER", "WAKU_MODEL", "WAKU_SMALL_MODEL", "TAVILY_API_KEY",
                 "WAKU_EPISODIC_STORE", "WAKU_EXPERIMENTAL",
                 "NOTION_TOKEN", "NOTION_EPISODES_DATABASE_ID"}
                | {p.key_env for p in PROVIDERS.values()})
    env_path = find_dotenv(usecwd=True) or ".env"

    updates = {"WAKU_PROVIDER": provider,
               "WAKU_MODEL": payload.get("model", "") or "",
               "WAKU_SMALL_MODEL": payload.get("small_model", "") or ""}
    if episodic_store:
        updates["WAKU_EPISODIC_STORE"] = episodic_store
    # NOT `if experimental:` — turning it OFF sends "", which is falsy. Absent
    # (None) means "don't touch"; "" means "switch it off".
    experimental = payload.get("experimental")
    if experimental is not None:
        updates["WAKU_EXPERIMENTAL"] = "1" if str(experimental).strip() else ""
    # Changing provider never carries a model across endpoints (live bug:
    # kimi->gemini kept gate model kimi-k3 and every turn 404'd on Gemini). But
    # if the user didn't newly type a model, use THIS provider's default (their
    # first pinned model for it, else its built-in default) — "a default model
    # per API key". An explicit model in the payload (e.g. from the chat pill)
    # always wins.
    if provider != before["provider"]:
        if updates["WAKU_MODEL"] in ("", before["model"]):
            updates["WAKU_MODEL"] = default_model_for(provider)
        if updates["WAKU_SMALL_MODEL"] in ("", before["small_model"]):
            updates["WAKU_SMALL_MODEL"] = ""
    for k, v in (payload.get("keys") or {}).items():
        if k in writable and v:  # only non-empty keys overwrite
            if k == "NOTION_EPISODES_DATABASE_ID":
                from waku.memory.episodic.notion_store import normalize_database_id

                try:
                    v = normalize_database_id(v)
                except ValueError as exc:
                    return {"error": str(exc)}
            updates[k] = v
    for k, v in updates.items():
        if k in writable:
            set_key(env_path, k, v)
            os.environ[k] = v

    with _agent_lock:
        old = _agent
        try:
            new_settings = load_settings()
            new_settings.ensure_home()
            conn = connect(new_settings.home, check_same_thread=False)
            from waku.app import Waku

            _agent = Waku(settings=new_settings, conn=conn)
        except (Exception, SystemExit) as exc:  # get_client raises SystemExit
            _agent = old
            return {"error": str(exc)}
    if old is not None:
        old.close()
    # a model/provider switch is a RELEASE event (the whiteboard's "new model
    # config" box) — record it in the trace so brain swaps are auditable
    _agent.tracer.event("config", {
        "from": before,
        "to": {"provider": provider, "model": updates["WAKU_MODEL"],
               "small_model": updates["WAKU_SMALL_MODEL"]},
    })
    return {"ok": True, **settings_info()}


def events_since(cursor):
    """New trace events past `cursor` (a line count in today's trace file).
    Any gateway — browser, CLI, voice, Telegram — appends to this same file,
    so the live diagram lights up for all of them. cursor=None returns just
    the current tail so the browser starts fresh instead of replaying history."""
    settings = load_settings()
    settings.ensure_home()
    path = settings.home / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    if not path.exists():
        return {"events": [], "cursor": 0}
    try:
        lines = list(iter_trace_lines(path))
    except TraceEncodingError as exc:
        return {"events": [], "cursor": 0, "error": str(exc)}
    if cursor is None or cursor < 0 or cursor > len(lines):
        return {"events": [], "cursor": len(lines)}
    out = []
    for ln in lines[cursor:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return {"events": out, "cursor": len(lines)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, *, no_cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The frontend files (app.js/style.css) change as we develop; without
        # this the browser serves a stale cached copy and edits look "missing".
        if no_cache:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            self._send(json.dumps(collect(), default=str).encode(), "application/json")
        elif self.path == "/api/compare/history":
            runs = compare_history.load_runs(load_settings().home)
            self._send(json.dumps(_compare_history_response(runs)).encode(), "application/json")
        elif self.path.startswith("/api/models"):
            from urllib.parse import parse_qs, urlparse

            prov = parse_qs(urlparse(self.path).query).get("provider", [None])[0]
            self._send(json.dumps(list_models(prov)).encode(), "application/json")
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse

            raw = parse_qs(urlparse(self.path).query).get("cursor", [None])[0]
            cursor = int(raw) if raw and raw.lstrip("-").isdigit() else None
            self._send(json.dumps(events_since(cursor)).encode(), "application/json")
        elif self.path.startswith("/api/reveal"):
            from urllib.parse import parse_qs, unquote, urlparse

            rel = unquote(parse_qs(urlparse(self.path).query).get("path", [""])[0])
            self._send(json.dumps(reveal_path(rel)).encode(), "application/json")
        elif self.path.startswith("/static/"):
            self._serve_static(self.path)
        else:
            self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def _serve_static(self, path: str) -> None:  # the frontend files
        name = path.split("/static/", 1)[1].split("?")[0]
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {".css": "text/css", ".js": "text/javascript",
                 ".html": "text/html; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        self._send(target.read_bytes(), ctype, no_cache=True)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # /api/voice takes a raw audio blob, not JSON — handle it first.
        if self.path == "/api/voice":
            raw = self.rfile.read(length)
            self._send(json.dumps(transcribe_audio(raw)).encode(), "application/json")
            return
        # /api/chat/stream streams harness events (SSE) as the turn runs.
        if self.path == "/api/chat/stream":
            payload = json.loads(self.rfile.read(length) or "{}")
            message = (payload.get("message") or "").strip()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser navigated away mid-stream — fine

            if not message:
                emit("done", {"error": "empty message"})
                return
            try:
                chat_stream(message, emit)
            except Exception as exc:  # surface as a terminal event, don't 500
                emit("done", {"error": f"{type(exc).__name__}: {exc}"})
            return
        # /api/compare/stream races several models, emitting each result as it lands.
        if self.path == "/api/compare/stream":
            payload = json.loads(self.rfile.read(length) or "{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                compare_stream((payload.get("message") or "").strip(), payload.get("models") or [],
                               emit, judge=bool(payload.get("judge")), coding=bool(payload.get("coding")),
                               judge_spec=(payload.get("judge_model") or ""), apple=bool(payload.get("apple")))
            except Exception as exc:
                emit("done", {"error": f"{type(exc).__name__}: {exc}"})
            return
        routes = {"/api/chat": None, "/api/memory": memory_action, "/api/settings": apply_settings,
                  "/api/query": run_query, "/api/session": session_action, "/api/pin": pin_action,
                  "/api/compare": compare_models, "/api/compare/clear": compare_clear,
                  "/api/compare/regrade": compare_regrade, "/api/compare/delete_run": compare_delete_run}
        if self.path not in routes:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.loads(self.rfile.read(length) or "{}")
        try:
            if self.path == "/api/chat":
                message = (payload.get("message") or "").strip()
                out = chat(message) if message else {"error": "empty message"}
            else:
                out = routes[self.path](payload)
        except Exception as exc:  # surface, don't 500 — the browser shows it
            out = {"error": f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(out, default=str).encode(), "application/json")

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> None:
    # Port precedence: WAKU_DASHBOARD_PORT, then the conventional PORT (used by
    # deploy platforms and IDE preview panes), then 7777. If it's taken, walk on.
    base = int(os.getenv("WAKU_DASHBOARD_PORT") or os.getenv("PORT") or PORT)
    for port in range(base, base + 10):  # walk past a busy port instead of crashing
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            print(f"port {port} busy, trying {port + 1}…")
            continue
        # One command, many gateways: if a Telegram token is set, run the bot
        # too (background thread) so you don't need a separate `waku telegram`.
        try:
            from waku.gateway.telegram import start_in_background

            if start_in_background():
                print("Telegram gateway → listening in the background (phone messages land here too)")
        except Exception as exc:
            print(f"(telegram) not started: {exc}")
        print(f"Waku dashboard → http://localhost:{port}  (Ctrl-C to stop)")
        server.serve_forever()
        return
    raise SystemExit(f"no free port in {base}–{base + 9}")


if __name__ == "__main__":
    main()
