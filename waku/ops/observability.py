"""Observability: what each turn did, what it cost, what it remembered (spec 012).

Four words, as the dashboard's Observability page uses them:

- A **trace** is the record of one turn: its steps in order, each with its
  input, output, time and cost. `tracing.py` writes it to
  `traces/<date>.jsonl`.
- **Observability** answers "what did it do, what did it cost, and what did
  it remember" from traces and the spend ledger (`usage.jsonl`).
- **Evals** judge "was it good": deterministic tests, an AI judge, a human.
- The **release gate** is the evals deciding whether a change ships.

This module has two halves. The first describes one tool call (its source,
whether it worked, what it cost, what it found) and redacts its arguments;
the tracer calls it as each `tool` event is written (schema `v: 2`), and the
page calls the same functions on old (v1) events, so an old trace shows
everything it can. The second half reads the trace files and the ledger and
returns the page's data: turns with their steps, tool use grouped by source,
memory per turn, spend, and evals.

PRIVACY. Arguments pass through `redact()` before they are written or
served: a secret-named key's value and any key-shaped string become `***`.
This module reads no environment variable and sends nothing anywhere.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from waku.ops.pricing import price_for, usage_summary
from waku.ops.receipt import tool_cost

SCHEMA = 2
ARGS_CHARS = 500
OUTPUT_CHARS = 200
ERROR_CHARS = 200
MAX_MEMORY_IDS = 20
MAX_TURNS = 50

# ---------------------------------------------------------------------------
# One tool call
# ---------------------------------------------------------------------------

TREG_PREFIX = "treg_"
WAKU_MEMORY_PREFIX = "waku_memory_"

# Span kinds: what each step in a turn is. The five a turn is made of, plus
# two that only describe its bookkeeping. Every waterfall step carries one,
# and the Tools tab groups Waku Memory calls by it.
SPAN_KINDS = ("llm", "tool", "retrieval", "memory_write", "gate", "route", "receipt")
_RETRIEVAL_TOOLS = {"memory_search", "memory_recall", "memory_get"}
_MEMORY_WRITE_TOOLS = {"memory_remember", "memory_forget", "memory_import"}

# OpenTelemetry GenAI semantic conventions, checked on 2026-10-04 against
# open-telemetry/semantic-conventions-genai at e07f4eb
# (docs/registry/attributes/gen-ai.md; every name there is "Development").
# This is the one place a Waku field is mapped to its semconv name. The JSONL
# keeps Waku's short keys and adds only the semconv keys Waku had no name for
# (gen_ai.operation.name, gen_ai.tool.call.id, gen_ai.tool.type); the OTel
# export (tracing.py) sends the semconv names. The conventions define no cost
# attribute, so cost is exported as waku.cost.usd. A call that went through an
# MCP server (treg, Waku Memory, mcp:<server>) carries mcp.method.name on the
# same execute_tool span, never a second span. Every other field with no
# semconv name (endpoint_id, source, duration_ms, retrieval_trace_id) keeps
# Waku's name, exported as waku.<name>.
GENAI = {
    "llm": {"provider": "gen_ai.provider.name", "model": "gen_ai.request.model",
            "usage.in": "gen_ai.usage.input_tokens", "usage.out": "gen_ai.usage.output_tokens",
            "stop_reason": "gen_ai.response.finish_reasons"},
    "tool": {"tool": "gen_ai.tool.name", "call_id": "gen_ai.tool.call.id",
             "query": "gen_ai.memory.query.text", "results": "gen_ai.memory.record.count"},
    "score": {"name": "gen_ai.evaluation.name", "value": "gen_ai.evaluation.score.value",
              "note": "gen_ai.evaluation.explanation"},
}
# gen_ai.operation.name per span kind; memory calls use the memory operations
# the conventions define rather than execute_tool.
OPERATION = {"llm": "chat", "tool": "execute_tool", "retrieval": "search_memory",
             "memory_write": "create_memory"}

# A key whose name says it holds a secret. Matched on the key with every
# separator removed, so "api-key", "API_KEY" and "apiKey" all match.
_SECRET_KEY = re.compile(r"(authorization|apikey|token|secret|password|passwd|cookie|"
                         r"credential|privatekey|accesskey|xapikey|bearer)", re.IGNORECASE)
# A string shaped like a key, wherever it appears.
_SECRET_VALUE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
    r"|\bxox[abposr]-[A-Za-z0-9-]{8,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
MASK = "***"


def redact(value):
    """A copy of `value` with every secret-named key's value and every
    key-shaped string replaced by `***`. Lists and dicts are walked."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = re.sub(r"[^A-Za-z]", "", str(k))
            out[k] = MASK if _SECRET_KEY.search(key) and v not in (None, "") else redact(v)
        return out
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(MASK, value)
    return value


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def preview(value, limit: int = ARGS_CHARS) -> str:
    """Redacted, one-line, at most `limit` characters: what the page shows."""
    if isinstance(value, str):
        parsed = _json_any(value)
        text = (json.dumps(redact(parsed), ensure_ascii=False, default=str)
                if parsed is not None else redact(value))
    else:
        text = json.dumps(redact(value), ensure_ascii=False, default=str)
    return _trim(" ".join(str(text).split()), limit)


def trace_args(args):
    """The `args` a v2 trace line keeps: redacted, and when the whole thing
    is longer than ARGS_CHARS, one trimmed string instead of the object."""
    clean = redact(args if args is not None else {})
    text = json.dumps(clean, ensure_ascii=False, default=str)
    return clean if len(text) <= ARGS_CHARS else _trim(text, ARGS_CHARS)


def mcp_servers(home: Path) -> tuple[str, ...]:
    """The server names in `mcp.json`, longest first, so a tool name is
    matched to the most specific server. Empty when there is no file."""
    try:
        data = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
        names = [s.get("name", "") for s in data.get("servers", []) if isinstance(s, dict)]
    except (OSError, ValueError, AttributeError):
        return ()
    return tuple(sorted({n for n in names if isinstance(n, str) and n}, key=len, reverse=True))


def tool_source(name: str, servers: tuple[str, ...] | list[str] = ()) -> str:
    """`treg`, `waku_memory`, `mcp:<server>` or `local`."""
    name = name or ""
    if name.startswith(TREG_PREFIX):
        return "treg"
    if name.startswith(WAKU_MEMORY_PREFIX):
        return "waku_memory"
    for server in servers:
        if server not in ("treg", "waku_memory") and name.startswith(f"{server}_"):
            return f"mcp:{server}"
    return "local"


def span_kind(name: str) -> str:
    """`retrieval` for a Waku Memory read, `memory_write` for a Waku Memory
    write, `tool` for every other tool call."""
    if (name or "").startswith(WAKU_MEMORY_PREFIX):
        short = name[len(WAKU_MEMORY_PREFIX):]
        if short in _RETRIEVAL_TOOLS:
            return "retrieval"
        if short in _MEMORY_WRITE_TOOLS:
            return "memory_write"
    return "tool"


def tool_type(source: str) -> str:
    """gen_ai.tool.type: `datastore` for Waku Memory, `extension` for a
    remote tool (treg, an MCP server), `function` for one on this machine."""
    return {"waku_memory": "datastore", "local": "function"}.get(source, "extension")


def genai_attributes(kind: str, event: dict) -> dict:
    """The OTel GenAI attributes for one trace event, from the GENAI table,
    plus any gen_ai.* key the JSONL line already carries."""
    out: dict = {k: v for k, v in event.items() if k.startswith("gen_ai.")}
    for key, name in GENAI.get(kind, {}).items():
        value = event
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None or value == "":
            continue
        out[name] = [value] if name == "gen_ai.response.finish_reasons" else value
    span = "llm" if kind == "llm" else event.get("span") if kind == "tool" else None
    if span in OPERATION:
        out["gen_ai.operation.name"] = OPERATION[span]
    if kind == "tool" and event.get("source"):
        out["gen_ai.tool.type"] = tool_type(event["source"])
        if event["source"] != "local":
            out["mcp.method.name"] = "tools/call"
        if event["source"] == "waku_memory":
            out["gen_ai.memory.store.id"] = "waku_memory"
    cost = event.get("cost_usd")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        out["waku.cost.usd"] = cost
    return out


def _json_any(text):
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _json(text) -> dict | None:
    out = _json_any(text)
    return out if isinstance(out, dict) else None


def outcome(output) -> tuple[bool, str | None]:
    """(ok, error). An output whose first line says "failed" or "timed out",
    or that starts with "error", failed; so does a JSON answer whose `status`
    is 400 or more. Only the first line is read, because a web search result
    that quotes the word "failed" in an article still worked."""
    text = output if isinstance(output, str) else ""
    low = text.strip().lower()
    head = low.split("\n", 1)[0][:200]
    if "failed" in head or "timed out" in head or low.startswith("error"):
        return False, _trim(redact(text.strip()), ERROR_CHARS)
    data = _json(text)
    status = data.get("status") if data else None
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        detail = data.get("error") or data.get("message") or f"status {status}"
        return False, _trim(redact(str(detail)), ERROR_CHARS)
    return True, None


def _memory_fields(args, output) -> dict:
    out: dict = {}
    args = args if isinstance(args, dict) else {}
    query = args.get("query")
    if isinstance(query, str) and query:
        out["query"] = _trim(redact(query), 200)
    data = _json(output)
    if data is None:
        return out
    entries = data.get("entries")
    if isinstance(entries, list):
        out["results"] = len(entries)
        ids = [e.get("id") for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]
        out["memory_ids"] = ids[:MAX_MEMORY_IDS]
    elif isinstance(data.get("id"), str):
        out["memory_ids"] = [data["id"]]
    trace_id = data.get("retrieval_trace_id")
    if isinstance(trace_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", trace_id):
        out["retrieval_trace_id"] = trace_id
    return out


def describe(event: dict, servers: tuple[str, ...] | list[str] = ()) -> dict:
    """What a `tool` event says, derived from its tool name, args and output.
    The same function serves the tracer (v2) and the page (v1 and v2), so an
    old trace shows everything that can be read back out of it. A v2 event's
    own fields win over what is derived here."""
    name = event.get("tool") if isinstance(event.get("tool"), str) else ""
    args, output = event.get("args"), event.get("output")
    source = tool_source(name, servers)
    ok, error = outcome(output)
    out: dict = {"source": source, "span": span_kind(name), "ok": ok}
    if error:
        out["error"] = error
    cost = tool_cost(event)
    if cost is not None:
        out["cost_usd"] = round(cost[0], 6)
        if cost[1]:
            out["provider"] = cost[1]
    data = _json(output)
    endpoint = ((data or {}).get("endpoint_id")
                or (args.get("endpoint_id") if isinstance(args, dict) else None))
    if isinstance(endpoint, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", endpoint):
        out["endpoint_id"] = endpoint
        out.setdefault("provider", endpoint.split(".")[0])
    if source == "waku_memory":
        out.update(_memory_fields(args, output))
    for key in ("source", "span", "ok", "error", "cost_usd", "endpoint_id", "provider", "query",
                "results", "memory_ids", "retrieval_trace_id", "duration_ms", "turn_id"):
        if key in event and event[key] is not None and event.get("v") == SCHEMA:
            out[key] = event[key]
    return out


def trace_record(event: dict, *, turn_id: str, servers: tuple[str, ...] = ()) -> dict:
    """The v2 `tool` line the tracer writes: the event as it came, with its
    arguments redacted and trimmed and what `describe()` reads added."""
    record = {k: v for k, v in event.items() if k not in ("args", "call_id")}
    record["v"] = SCHEMA
    if turn_id:
        record["turn_id"] = turn_id
    record["args"] = trace_args(event.get("args"))
    record.update(describe(event, servers))
    record["gen_ai.operation.name"] = OPERATION[record["span"]]
    record["gen_ai.tool.type"] = tool_type(record["source"])
    if isinstance(event.get("call_id"), str) and event["call_id"]:
        record["gen_ai.tool.call.id"] = event["call_id"]
    return record


def llm_cost(provider: str, model: str, usage: dict) -> float:
    """The `pricing.py` estimate for one model call's tokens."""
    usage = usage if isinstance(usage, dict) else {}
    p_in, p_out = price_for(provider or "", model or "")
    n_in = max(int(usage.get("in") or 0), 0)
    n_out = max(int(usage.get("out") or 0), 0)
    return round(n_in / 1e6 * p_in + n_out / 1e6 * p_out, 6)


# ---------------------------------------------------------------------------
# Reading the traces
# ---------------------------------------------------------------------------


def read_events(home: Path) -> tuple[list[dict], list[dict], list[str]]:
    """(events, encoding errors, trace file names), oldest first."""
    from waku.ops.tracing import (  # noqa: PLC0415 -- tracing imports this module
        TraceEncodingError,
        iter_trace_lines,
    )

    events, errors = [], []
    files = sorted((home / "traces").glob("*.jsonl"))
    for path in files:
        try:
            lines = list(iter_trace_lines(path))
        except TraceEncodingError as exc:
            errors.append({"file": path.name, "error": str(exc)})
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events, errors, [p.name for p in files]


def _ts(value) -> datetime | None:
    try:
        out = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if out is not None and out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out


def group_turns(events: list[dict]) -> list[dict]:
    """Events between `turn_start` and `turn_end`, one dict per turn, oldest
    first. The same grouping `dashboard.collect()` has always used, so a
    trace written before spec 011 splits into the same turns."""
    turns, current = [], None
    for ev in events:
        kind = ev.get("type")
        if kind == "turn_start":
            if current is not None:
                current["unfinished"] = True
                turns.append(current)
            current = {"turn_id": ev.get("turn_id") or "", "ts": ev.get("ts"),
                       "user_message": ev.get("user_message") or "", "events": []}
        elif current is not None:
            if kind == "turn_end":
                current["end_ts"] = ev.get("ts")
                current["iterations"] = ev.get("iterations")
                turns.append(current)
                current = None
            elif kind != "text":
                current["events"].append(ev)
    if current is not None:
        current["unfinished"] = True
        turns.append(current)
    return turns


def _step(ev: dict, servers, provider: str, model: str) -> dict | None:
    """One waterfall row from one trace event, or None for an event the
    waterfall does not draw (graph bookkeeping)."""
    kind = ev.get("type")
    span = {"gate": "gate", "llm": "llm", "consolidation": "memory_write", "report": "memory_write",
            "receipt": "receipt", "triage": "route", "route": "route"}.get(kind)
    base = {"kind": kind, "span": span, "ts": ev.get("ts"),
            "node": ev.get("node") if isinstance(ev.get("node"), str) else None}
    if kind == "gate":
        return {**base, "decision": ev.get("decision"), "reason": _trim(str(ev.get("reason") or ""), 160)}
    if kind == "llm":
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        call_model = ev.get("model") or model
        usd = ev.get("cost_usd")
        if not isinstance(usd, int | float) or isinstance(usd, bool):
            usd = llm_cost(ev.get("provider") or provider, call_model, usage)
        return {**base, "call": ev.get("kind") or "loop", "model": call_model,
                "in": int(usage.get("in") or 0), "out": int(usage.get("out") or 0),
                "usd": round(float(usd), 6), "stop_reason": ev.get("stop_reason")}
    if kind == "tool":
        info = describe(ev, servers)
        step = {**base, "kind": "memory" if info["source"] == "waku_memory" else "tool",
                "span": info["span"],
                "tool": ev.get("tool") or "", "source": info["source"],
                "args": preview(ev.get("args"), ARGS_CHARS),
                "output": preview(ev.get("output"), OUTPUT_CHARS),
                "ok": info["ok"], "error": info.get("error"),
                "duration_ms": ev.get("duration_ms") if isinstance(ev.get("duration_ms"), int) else None,
                "usd": info.get("cost_usd"), "endpoint_id": info.get("endpoint_id"),
                "provider": info.get("provider"), "read_first": bool(ev.get("read_first"))}
        if info["source"] == "waku_memory":
            step.update(query=info.get("query"), results=info.get("results"),
                        retrieval_trace_id=info.get("retrieval_trace_id"))
        return step
    if kind == "consolidation":
        kept = [k for k in (ev.get("kept") or []) if isinstance(k, dict)]
        return {**base, "new_facts": ev.get("new_facts") if isinstance(ev.get("new_facts"), int) else len(kept),
                "sent": sum(1 for k in kept if k.get("sent") is True),
                "local_only": sum(1 for k in kept if k.get("sent") is False),
                "facts": [_trim(redact(str(k.get("content") or "")), 160) for k in kept][:5]}
    if kind == "report":
        return {**base, "memory_id": ev.get("memory_id"), "title": _trim(str(ev.get("title") or ""), 120)}
    if kind == "receipt":
        return {**base, "total_usd": ev.get("total_usd"), "credits": ev.get("credits"),
                "estimate": (ev.get("model") or {}).get("estimate", True)}
    if kind in ("triage", "route"):
        return {**base, "detail": _trim(str(ev.get("target") or ev.get("route") or "")
                                        + (": " + str(ev.get("reason")) if ev.get("reason") else ""), 160)}
    return None


SCORE_SOURCES = ("code", "judge", "human")


def _score(ev: dict) -> dict | None:
    """One score from a `score` trace event: source (code, judge or human),
    name, a numeric value and a short note."""
    if not isinstance(ev, dict) or ev.get("type") != "score":
        return None
    value = ev.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    source = ev.get("source") if ev.get("source") in SCORE_SOURCES else "code"
    return {"source": source, "name": _trim(str(ev.get("name") or ""), 60),
            "value": value, "note": _trim(redact(str(ev.get("note") or "")), 200)}


def build_turn(turn: dict, servers=(), provider: str = "", model: str = "",
               scores: list | None = None) -> dict:
    """A turn with its steps in order, its time and its dollars."""
    steps = [s for s in (_step(ev, servers, provider, model) for ev in turn["events"]) if s]
    start = _ts(turn.get("ts"))
    end = _ts(turn.get("end_ts")) or max((t for t in (_ts(s["ts"]) for s in steps) if t), default=None)
    # Where each step sits on the turn's timeline. A line is written when its
    # step finishes, so the time since the line before is about how long the
    # step took; a v2 tool line's own duration_ms is exact and wins.
    previous = start
    for step in steps:
        at = _ts(step["ts"])
        step["at_ms"] = int((at - start).total_seconds() * 1000) if at and start else None
        span = int((at - previous).total_seconds() * 1000) if at and previous else None
        if isinstance(step.get("duration_ms"), int):
            span = step["duration_ms"]
        step["span_ms"] = max(span, 0) if span is not None else None
        previous = at or previous
    loops = _group_loops(steps)
    receipt = next((s for s in reversed(steps) if s["kind"] == "receipt"), None)
    steps_usd = sum(s.get("usd") or 0 for s in steps if s["kind"] in ("llm", "tool", "memory"))
    total = receipt["total_usd"] if receipt and isinstance(receipt.get("total_usd"), int | float) else steps_usd
    receipt_ev = next((ev for ev in reversed(turn["events"]) if ev.get("type") == "receipt"), None)
    memory = (receipt_ev or {}).get("memory") if isinstance((receipt_ev or {}).get("memory"), dict) else None
    consolidation = next((s for s in steps if s["kind"] == "consolidation"), None)
    return {
        # Spec 012: how good the turn was, from code, an AI judge or a human.
        # Nothing writes a score yet; a `score` trace event joined by turn_id
        # (or inside the turn) lands here.
        "scores": [score for score in (_score(ev) for ev in turn["events"] + list(scores or []))
                   if score],
        "turn_id": turn.get("turn_id") or "",
        "ts": turn.get("ts"),
        "user_message": _trim(str(turn.get("user_message") or ""), 200),
        "unfinished": bool(turn.get("unfinished")),
        "latency_ms": int((end - start).total_seconds() * 1000) if start and end else None,
        "usd": round(float(total), 6),
        "has_receipt": receipt is not None,
        "used": memory.get("used") if memory else None,
        "kept": (len(memory.get("kept") or []) if memory
                 else (consolidation["new_facts"] if consolidation else None)),
        "gate": next((s["decision"] for s in steps if s["kind"] == "gate"), None),
        "tool_calls": sum(1 for s in steps if s["kind"] in ("tool", "memory")),
        "loops": loops,
        "tokens_in": sum(s.get("in") or 0 for s in steps if s["kind"] == "llm"),
        "tokens_out": sum(s.get("out") or 0 for s in steps if s["kind"] == "llm"),
        "graph": _graph(turn["events"]),
        "steps": steps,
    }


def _group_loops(steps: list[dict]) -> int:
    """Give every step its place in the loop: `loop` 0 and phase "before" for
    what runs before the model's first call (gate, routing, read-first
    searches), `loop` n and phase "loop" for iteration n (its model call and
    the tools that call asked for), and phase "after" for what follows the
    reply (consolidation, a saved report, the receipt). A loop model call
    (kind "loop", or no kind on an old line) starts the next iteration; a
    side call (gate, consolidation, …) never does. Returns the iterations."""
    n, after = 0, False
    for step in steps:
        is_loop_call = step["kind"] == "llm" and step.get("call") in ("loop", None)
        if is_loop_call:
            n, after = n + 1, False
            step["loop"], step["phase"] = n, "loop"
            after = step.get("stop_reason") not in ("tool_use", None)
            continue
        step["loop"] = n
        step["phase"] = "after" if after or (n and step["kind"] in ("consolidation", "report", "receipt")) \
            else ("loop" if n else "before")
    return n


def _graph(events: list[dict]) -> dict | None:
    """The graph workflow a turn ran through, when it ran through one: its
    name, the nodes in the order they ran, and how long it took."""
    start = next((e for e in events if e.get("type") == "graph_start"), None)
    if start is None:
        return None
    end = next((e for e in events if e.get("type") == "graph_end"), None) or {}
    path = end.get("path") if isinstance(end.get("path"), list) else [
        e.get("node") for e in events if e.get("type") == "node_start"]
    return {"workflow": start.get("workflow") or "", "path": [str(p) for p in path if p],
            "ms": end.get("ms") if isinstance(end.get("ms"), int) else None,
            "error": _trim(str(end.get("error")), 160) if end.get("error") else None}


# ---------------------------------------------------------------------------
# Tools, grouped by source
# ---------------------------------------------------------------------------

WINDOWS = ("today", "7d", "all")


def window_start(window: str, now: datetime | None = None) -> datetime | None:
    """When a window starts: local midnight for "today", 168 hours back for
    "7d", None (the beginning) for "all"."""
    now = now or datetime.now().astimezone()
    if window == "today":
        local = now.astimezone()
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "7d":
        return now - timedelta(days=7)
    return None


def _in_window(ev: dict, start: datetime | None) -> bool:
    if start is None:
        return True
    ts = _ts(ev.get("ts"))
    return ts is not None and ts >= start


def _bucket() -> dict:
    return {"calls": 0, "errors": 0, "usd": 0.0, "priced": 0, "ms_total": 0, "ms_n": 0,
            "results_total": 0, "results_n": 0}


def _add(b: dict, info: dict, ev: dict) -> None:
    b["calls"] += 1
    b["errors"] += 0 if info["ok"] else 1
    if info.get("cost_usd") is not None:
        b["usd"] += info["cost_usd"]
        b["priced"] += 1
    ms = ev.get("duration_ms")
    if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0:
        b["ms_total"] += ms
        b["ms_n"] += 1
    if isinstance(info.get("results"), int):
        b["results_total"] += info["results"]
        b["results_n"] += 1


def _row(name: str, b: dict) -> dict:
    # usd is None when no call named a price: "free" and "unknown" differ
    return {"tool": name, "calls": b["calls"], "errors": b["errors"],
            "usd": round(b["usd"], 6) if b["priced"] else None,
            "avg_ms": round(b["ms_total"] / b["ms_n"]) if b["ms_n"] else None,
            "avg_results": round(b["results_total"] / b["results_n"], 1) if b["results_n"] else None}


def tool_stats(events: list[dict], servers=(), window: str = "all",
               now: datetime | None = None) -> dict:
    """Tool calls in a window, grouped by source: treg (per tool and per
    endpoint), Waku Memory (per tool, with recent queries), and the rest."""
    start = window_start(window, now)
    treg: dict[str, dict] = {}
    endpoints: dict[str, dict] = {}
    memory: dict[str, dict] = {}
    other: dict[str, dict] = {}
    other_source: dict[str, str] = {}
    memory_span: dict[str, str] = {}
    queries: list[dict] = []
    for ev in events:
        if ev.get("type") != "tool" or not _in_window(ev, start):
            continue
        name = ev.get("tool") if isinstance(ev.get("tool"), str) else ""
        info = describe(ev, servers)
        source = info["source"]
        if source == "treg":
            _add(treg.setdefault(name, _bucket()), info, ev)
            # only a call runs an endpoint; catalog_get only reads its page
            if info.get("endpoint_id") and name.startswith(TREG_PREFIX + "call"):
                b = endpoints.setdefault(info["endpoint_id"], {**_bucket(), "provider": info.get("provider") or ""})
                _add(b, info, ev)
        elif source == "waku_memory":
            short = name[len(WAKU_MEMORY_PREFIX):] or name
            _add(memory.setdefault(short, _bucket()), info, ev)
            memory_span[short] = info["span"]
            if info.get("query"):
                queries.append({"tool": short, "query": info["query"], "results": info.get("results"),
                                "retrieval_trace_id": info.get("retrieval_trace_id"),
                                "ok": info["ok"], "ts": ev.get("ts")})
        else:
            _add(other.setdefault(name, _bucket()), info, ev)
            other_source[name] = source

    def rows(buckets: dict) -> list[dict]:
        return sorted((_row(n, b) for n, b in buckets.items()), key=lambda r: (-r["calls"], r["tool"]))

    endpoint_rows = sorted(({"endpoint_id": e, "provider": b["provider"], "calls": b["calls"],
                             "errors": b["errors"], "usd": round(b["usd"], 6) if b["priced"] else None}
                            for e, b in endpoints.items()),
                           key=lambda r: (-(r["usd"] or 0), -r["calls"], r["endpoint_id"]))
    treg_rows = [{**r, "span": "tool"} for r in rows(treg)]
    memory_rows = [{**r, "span": memory_span[r["tool"]]} for r in rows(memory)]
    other_rows = [{**r, "source": other_source[r["tool"]], "span": "tool"} for r in rows(other)]
    return {
        "window": window,
        "treg": {"tools": treg_rows, "endpoints": endpoint_rows,
                 "calls": sum(r["calls"] for r in treg_rows),
                 "usd": round(sum(r["usd"] or 0 for r in treg_rows), 6)},
        "waku_memory": {"tools": memory_rows, "recent_queries": queries[::-1][:5],
                        "calls": sum(r["calls"] for r in memory_rows)},
        "other": {"tools": other_rows, "calls": sum(r["calls"] for r in other_rows)},
    }


# ---------------------------------------------------------------------------
# Spend, memory, evals
# ---------------------------------------------------------------------------


def _charged_receipts(events: list[dict]) -> dict[str, dict]:
    """turn_id -> the receipt of every turn the metering proxy priced
    exactly (its model figure is not an estimate). A receipt that names no
    turn takes the turn it sits in."""
    out, current = {}, ""
    for n, ev in enumerate(events):
        kind = ev.get("type")
        if kind == "turn_start":
            current = ev.get("turn_id") or ""
        elif (kind == "receipt" and isinstance(ev.get("model"), dict)
              and ev["model"].get("estimate") is False):
            # a receipt outside any turn still counts, joined to nothing
            out[ev.get("turn_id") or current or f"receipt-{n}"] = ev
    return out


def _receipt_model_usd(receipt: dict) -> float:
    """The exact model dollars on a charged receipt: its own model figure,
    or its total less the tools it priced when an old receipt has none."""
    usd = receipt["model"].get("usd")
    if isinstance(usd, int | float) and not isinstance(usd, bool):
        return float(usd)
    tools = sum(float(t.get("usd") or 0) for t in receipt.get("tools") or [] if isinstance(t, dict))
    return max(float(receipt.get("total_usd") or 0) - tools, 0.0)


def spend_items(home: Path, events: list[dict], servers=()) -> list[dict]:
    """Every dollar this home spent, once: {ts, bucket, usd, charged}.

    One rule for the Spend card, the Spend tab and the days: a turn the
    platform charged exactly counts its receipt's model dollars (charged);
    every other turn counts its usage.jsonl rows at list price (estimated).
    Tool dollars are what each tool's answer said it cost, charged when
    their turn was. So the split by bucket and the split by charged and
    estimated are two views of the same items and add up to one total."""
    charged = _charged_receipts(events)
    items = [{"ts": str(ev.get("ts") or ""), "bucket": "model", "usd": _receipt_model_usd(ev),
              "charged": True} for ev in charged.values()]
    for row in _ledger_rows(home):
        if row.get("turn_id") and row["turn_id"] in charged:
            continue   # the receipt's exact figure replaces this estimate
        items.append({"ts": str(row.get("ts") or ""), "bucket": "model", "usd": _row_cost(row),
                      "charged": False})
    current = ""
    for ev in events:
        if ev.get("type") == "turn_start":
            current = ev.get("turn_id") or ""
        if ev.get("type") != "tool":
            continue
        info = describe(ev, servers)
        if info.get("cost_usd") is None:
            continue
        bucket = {"treg": "treg", "waku_memory": "memory"}.get(info["source"], "other")
        items.append({"ts": str(ev.get("ts") or ""), "bucket": bucket, "usd": float(info["cost_usd"]),
                      "charged": (ev.get("turn_id") or current) in charged})
    return items


def spend_totals(items: list[dict], start: datetime | None = None,
                 events: list[dict] | None = None) -> dict:
    """The window's dollars: by bucket (model, treg, memory, other), and as
    charged plus estimated. Both splits sum to `total_usd`."""
    inside = [i for i in items if _in_window(i, start)]

    def add(pick) -> float:
        return round(sum(i["usd"] for i in inside if pick(i)), 6)

    out = {f"{b}_usd": add(lambda i, b=b: i["bucket"] == b) for b in ("model", "treg", "memory", "other")}
    total = round(sum(out.values()), 6)
    charged = add(lambda i: i["charged"])
    receipts = [ev for ev in _charged_receipts(events or []).values() if _in_window(ev, start)]
    credits = [ev.get("credits") for ev in receipts
               if isinstance(ev.get("credits"), int) and not isinstance(ev.get("credits"), bool)]
    return {**out, "total_usd": total,
            "charged_usd": charged if receipts else None,
            "estimated_usd": round(total - (charged if receipts else 0), 6),
            "charged_turns": len(receipts),
            "credits": sum(credits) if credits else None}


def spend(home: Path, events: list[dict], servers=()) -> dict:
    """All-time spend under the one rule `spend_items` keeps, with the
    ledger's tokens and the list-price estimate per model."""
    return {**spend_totals(spend_items(home, events, servers), None, events),
            "ledger": usage_summary(home),
            "by_model": by_model(home)}


def by_model(home: Path) -> list[dict]:
    """All-time model calls, tokens and estimated dollars per provider and
    model, from usage.jsonl, most expensive first."""
    out: dict[tuple[str, str], dict] = {}
    for row in _ledger_rows(home):
        provider, model = str(row.get("provider") or "?"), str(row.get("model") or "?")
        b = out.setdefault((provider, model), {"provider": provider, "model": model,
                                               "calls": 0, "in": 0, "out": 0, "usd": 0.0})
        b["calls"] += 1
        b["in"] += int(row.get("in") or 0)
        b["out"] += int(row.get("out") or 0)
        b["usd"] += _row_cost(row)
    return sorted(({**b, "usd": round(b["usd"], 6)} for b in out.values()),
                  key=lambda r: (-r["usd"], -r["calls"], r["model"]))


def _ledger_rows(home: Path) -> list[dict]:
    rows = []
    try:
        for line in (home / "usage.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        pass
    return rows


def _row_cost(row: dict) -> float:
    return llm_cost(row.get("provider") or "", row.get("model") or "",
                    {"in": row.get("in"), "out": row.get("out")})


def summary(home: Path, events: list[dict], turns: list[dict], tools: dict,
            window: str = "7d", now: datetime | None = None, servers=()) -> dict:
    """The four cards at the top of the page, for one window, in the order of
    the tabs below them: turns with their average loop iterations, tool calls
    by source with errors, memory read and written with the gate's split, and
    spend by source with its tokens. Tokens come from usage.jsonl, which
    counts every model call; dollars follow the one rule in `spend_items`."""
    start = window_start(window, now)
    rows = [r for r in _ledger_rows(home) if _in_window(r, start)]
    in_window = [t for t in turns if _in_window(t, start)]
    loops = [t["loops"] for t in in_window if t.get("loops")]
    reads = [r for r in tools["waku_memory"]["tools"] if r["span"] == "retrieval"]
    writes = [r for r in tools["waku_memory"]["tools"] if r["span"] == "memory_write"]
    # avg_results is per tool; weight it back by calls to average the reads
    weighted = [(r["avg_results"], r["calls"]) for r in reads if r["avg_results"] is not None]
    returned = sum(n for _, n in weighted)

    def calls(group: str) -> dict:
        rows_ = tools[group]["tools"]
        return {"calls": sum(r["calls"] for r in rows_), "errors": sum(r["errors"] for r in rows_)}

    return {
        "window": window,
        "spend": spend_totals(spend_items(home, events, servers), start, events),
        "tokens": {"in": sum(int(r.get("in") or 0) for r in rows),
                   "out": sum(int(r.get("out") or 0) for r in rows),
                   "calls": len(rows)},
        "tools": {"treg": calls("treg"), "waku_memory": calls("waku_memory"), "other": calls("other")},
        "turns": {"count": len(in_window),
                  "avg_loops": round(sum(loops) / len(loops), 1) if loops else None},
        "memory": {
            "retrievals": sum(r["calls"] for r in reads),
            "avg_results": round(sum(a * n for a, n in weighted) / returned, 1) if returned else None,
            "writes": sum(r["calls"] for r in writes),
            "kept": sum(t["kept"] or 0 for t in in_window),
            "gate_retrieve": sum(1 for t in in_window if t["gate"] == "retrieve"),
            "gate_skip": sum(1 for t in in_window if t["gate"] == "skip"),
        },
    }


def spend_by_day(home: Path, events: list[dict], days: int = 14, servers=()) -> list[dict]:
    """Newest first, one row per day (UTC, as the ledger dates them): the
    dollars `spend_items` counts, by bucket and as charged, with the
    ledger's tokens."""
    out: dict[str, dict] = {}

    def day(key: str) -> dict:
        return out.setdefault(key, {"date": key, "model": 0.0, "treg": 0.0, "memory": 0.0, "other": 0.0,
                                    "charged": 0.0, "in": 0, "out": 0})

    for row in _ledger_rows(home):
        key = str(row.get("ts") or "")[:10]
        if key:
            day(key)["in"] += int(row.get("in") or 0)
            day(key)["out"] += int(row.get("out") or 0)
    for item in spend_items(home, events, servers):
        if item["ts"][:10]:
            bucket = day(item["ts"][:10])
            bucket[item["bucket"]] += item["usd"]
            if item["charged"]:
                bucket["charged"] += item["usd"]
    rows = sorted(out.values(), key=lambda r: r["date"], reverse=True)[:days]
    return [{**r, **{k: round(r[k], 6) for k in ("model", "treg", "memory", "other", "charged")},
             "total": round(r["model"] + r["treg"] + r["memory"] + r["other"], 6)} for r in rows]


def memory_per_turn(turns: list[dict]) -> list[dict]:
    """Per turn, newest first: the gate, memories used and facts kept."""
    return [{"turn_id": t["turn_id"], "ts": t["ts"], "user_message": t["user_message"],
             "gate": t["gate"], "used": t["used"], "kept": t["kept"],
             "local_only": next((s["local_only"] for s in t["steps"] if s["kind"] == "consolidation"), None),
             "searches": sum(1 for s in t["steps"] if s["kind"] == "memory")}
            for t in turns]


_TEST_DEF = re.compile(r"^(?:async\s+)?def test_", re.MULTILINE)


def evals_info(home: Path, repo: Path | None = None, *, hosted: bool = False,
               release_file: Path | None = None) -> dict:
    """What evals exist and what the release gate said last. Counted from
    `evals/` when the repository is beside the code (a laptop checkout); a
    hosted container ships no `evals/` and says so. `release_file` is the
    record of the release this container runs, when the deployment baked one
    in (see `release_info`)."""
    repo = repo or Path(__file__).resolve().parents[2]
    det_dir, judge_dir = repo / "evals" / "deterministic", repo / "evals" / "judge"
    deterministic = None
    if det_dir.is_dir():
        files = sorted(det_dir.rglob("test_*.py"))
        deterministic = {"files": len(files),
                         "tests": sum(len(_TEST_DEF.findall(p.read_text(encoding="utf-8", errors="replace")))
                                      for p in files)}
    judge = ([p.stem.removeprefix("test_") for p in sorted(judge_dir.glob("test_*.py"))]
             if judge_dir.is_dir() else None)
    report = None
    try:
        report = json.loads((home / "eval_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    history = []
    try:
        for line in (home / "eval_runs.jsonl").read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                history.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    history = history[::-1]
    return {"deterministic": deterministic, "judge_suites": judge, "hosted": hosted,
            "last": report, "history": history, "last_run": last_run(report, history),
            "release": release_info(release_file)}


_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB = "https://github.com/"


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check(value) -> dict | None:
    """One required CI check from a release record, or None if it is malformed."""
    if not isinstance(value, dict):
        return None
    name, conclusion, url = value.get("name"), value.get("conclusion"), value.get("url")
    if not (isinstance(name, str) and isinstance(conclusion, str) and isinstance(url, str)
            and url.startswith(_GITHUB)):
        return None
    tests = value.get("tests")
    if not (isinstance(tests, dict) and isinstance(tests.get("label"), str)
            and all(_count(tests.get(k)) for k in ("passed", "failed", "skipped"))):
        tests = None   # the check passed but recorded no counts: say so, never guess
    return {"name": name, "conclusion": conclusion, "url": url,
            "completed_at": value.get("completed_at") if isinstance(value.get("completed_at"), str) else None,
            "tests": tests and {k: tests[k] for k in ("label", "passed", "failed", "skipped")}}


def release_info(path: Path | None) -> dict | None:
    """The release this container runs: its commit, when it was deployed, and
    the CI checks that passed on that commit before it was allowed to ship.

    On agent.waku.one, `hosted/deploy/autodeploy.sh` writes this record from
    GitHub's public API at deploy time and the tenant image is built with it
    (`WAKU_RELEASE_FILE` names the path). On a laptop there is no record, and
    a missing or malformed one is None too: the page then shows no release
    rather than a guess."""
    if path is None:
        return None
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not _SHA.match(str(record.get("sha", ""))):
        return None
    checks = [_check(c) for c in record.get("checks") or []]
    commit_url = record.get("commit_url")
    if not checks or None in checks or not (isinstance(commit_url, str) and commit_url.startswith(_GITHUB)):
        return None
    deployed_at = record.get("deployed_at")
    return {"sha": record["sha"], "short_sha": record["sha"][:7], "commit_url": commit_url,
            "deployed_at": deployed_at if isinstance(deployed_at, str) else None,
            "checks": checks}


def _suite(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    counts = {k: value.get(k) for k in ("passed", "failed")}
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in counts.values()):
        return None
    return counts


def last_run(report: dict | None, history: list) -> dict | None:
    """What the newest `make gate` run recorded: passed and failed per suite,
    and when it ran. The page shows these, never a count of test functions,
    as the run's result. None when no run recorded suite counts."""
    for run in [report, *history]:
        if not isinstance(run, dict) or not isinstance(run.get("suites"), dict):
            continue
        suites = {name: _suite(run["suites"].get(name)) for name in ("deterministic", "judge")}
        if any(suites.values()):
            return {"ran_at": run.get("ran_at"), **suites,
                    "verdict": {"deterministic": run.get("deterministic"), "judge": run.get("judge")}}
    return None


def payload(home: Path, *, provider: str = "", model: str = "", window: str = "7d",
            hosted: bool = False, release_file: Path | None = None,
            now: datetime | None = None) -> dict:
    """Everything the Observability page draws, in one answer."""
    window = window if window in WINDOWS else "7d"
    servers = mcp_servers(home)
    events, errors, files = read_events(home)
    late: dict[str, list] = {}
    for ev in events:   # a score written after its turn ended, e.g. by a judge run
        if ev.get("type") == "score" and ev.get("turn_id"):
            late.setdefault(ev["turn_id"], []).append(ev)
    grouped = group_turns(events)
    all_turns = [build_turn(t, servers, provider, model,
                            [e for e in late.get(t.get("turn_id") or "", []) if e not in t["events"]])
                 for t in grouped]
    tools = tool_stats(events, servers, window, now)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary(home, events, all_turns, tools, window, now, servers),
        "spend_by_day": spend_by_day(home, events, servers=servers),
        "trace_files": len(files),
        "trace_file": files[-1] if files else None,
        "trace_errors": errors,
        "turns": all_turns[::-1][:MAX_TURNS],
        "tools": tools,
        "memory": memory_per_turn(all_turns[::-1][:MAX_TURNS]),
        "spend": spend(home, events, servers),
        "evals": evals_info(home, hosted=hosted, release_file=release_file),
    }
