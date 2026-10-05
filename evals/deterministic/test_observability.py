"""DETERMINISTIC EVAL — the Observability page and the v2 trace event (spec 012).

A trace is the record of one turn: its steps in order, each with its input,
output, time and cost. These cases pin what a `tool` and an `llm` line now
record, that secrets never reach the trace file or the page, how tool calls
group by source, span kind and treg endpoint, that a trace written before
this spec still reads, and the OTel GenAI names the export uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from evals.helpers import ScriptedClient, response, text_block, tool_block
from waku.config import Settings
from waku.loop.agent import run_loop
from waku.ops import observability as obs
from waku.ops.tracing import Tracer
from waku.tools.registry import Tool, ToolRegistry

SECRET = "sk-live-abcdef0123456789"
TREG_OUT = json.dumps({"status": 200, "endpoint_id": "tomba.companies.similar",
                       "body": {"data": []}, "call_id": "c1", "cost_usd": 0.0089})
SEARCH_OUT = json.dumps({"entries": [{"id": "m1", "body": "a"}, {"id": "m2", "body": "b"},
                                     {"id": "m3", "body": "c"}],
                         "retrieval_trace_id": "rt_42"})


def _lines(home):
    out = []
    for path in sorted((home / "traces").glob("*.jsonl")):
        out += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return out


def _settings(tmp_path):
    settings = Settings(home=tmp_path, provider="anthropic", model="claude-sonnet-5")
    settings.ensure_home()
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"servers": [{"name": "waku_memory"}, {"name": "treg"}, {"name": "github"}]}))
    return settings


# ---- 1. the v2 tool line and the llm line ---------------------------------

def test_a_loop_turn_writes_v2_tool_and_llm_lines(tmp_path):
    settings = _settings(tmp_path)
    tracer = Tracer(settings)
    registry = ToolRegistry()
    registry.register(Tool(name="treg_call", description="", input_schema={"type": "object"},
                           fn=lambda **kw: TREG_OUT))
    registry.register(Tool(name="waku_memory_memory_search", description="",
                           input_schema={"type": "object"}, fn=lambda **kw: SEARCH_OUT))
    client = ScriptedClient([
        response([tool_block("treg_call", {"endpoint_id": "tomba.companies.similar"}, "tu_1"),
                  tool_block("waku_memory_memory_search", {"query": "mem0 competitors"}, "tu_2")],
                 "tool_use"),
        response([text_block("done")]),
    ])
    with tracer.turn("research mem0"):
        run_loop(client, "claude-sonnet-5", "sys", [{"role": "user", "content": "x"}],
                 registry, observer=tracer.event)
        turn_id = tracer.turn_id
    tracer.end_turn("done", 2)

    lines = _lines(tmp_path)
    treg, search = [e for e in lines if e["type"] == "tool"]
    assert treg["v"] == 2 and treg["turn_id"] == turn_id and turn_id.startswith("t_")
    assert treg["source"] == "treg" and treg["span"] == "tool" and treg["ok"] is True
    assert isinstance(treg["duration_ms"], int) and treg["duration_ms"] >= 0
    assert treg["cost_usd"] == 0.0089 and treg["endpoint_id"] == "tomba.companies.similar"
    assert treg["provider"] == "tomba"
    assert treg["gen_ai.operation.name"] == "execute_tool"
    assert treg["gen_ai.tool.call.id"] == "tu_1" and treg["gen_ai.tool.type"] == "extension"
    assert "call_id" not in treg
    assert search["source"] == "waku_memory" and search["span"] == "retrieval"
    assert search["query"] == "mem0 competitors" and search["results"] == 3
    assert search["memory_ids"] == ["m1", "m2", "m3"] and search["retrieval_trace_id"] == "rt_42"
    assert search["gen_ai.operation.name"] == "search_memory"
    assert search["gen_ai.tool.type"] == "datastore"
    llm = [e for e in lines if e["type"] == "llm"]
    assert len(llm) == 2
    assert all(e["turn_id"] == turn_id and isinstance(e["cost_usd"], float) for e in llm)
    assert all(e["span"] == "llm" and e["gen_ai.operation.name"] == "chat" for e in llm)


def test_a_failed_tool_line_says_so_with_a_trimmed_error(tmp_path):
    rec = obs.trace_record({"tool": "search_web", "args": {"q": "x"},
                            "output": "Search failed: " + "x" * 500}, turn_id="t_1")
    assert rec["ok"] is False and rec["error"].startswith("Search failed")
    assert len(rec["error"]) <= obs.ERROR_CHARS
    ok = obs.trace_record({"tool": "search_web", "args": {},
                           "output": "Results:\n1. Why the launch failed, an essay"}, turn_id="t_1")
    assert ok["ok"] is True, "a word in a result's body is not the tool failing"
    bad = obs.trace_record({"tool": "treg_call", "args": {},
                            "output": json.dumps({"status": 402, "error": "insufficient balance"})},
                           turn_id="t_1")
    assert bad["ok"] is False and bad["error"] == "insufficient balance"


# ---- 2. redaction ----------------------------------------------------------

def test_secrets_never_reach_the_trace_file_or_the_page(tmp_path):
    settings = _settings(tmp_path)
    tracer = Tracer(settings)
    args = {"endpoint_id": "x.y", "headers": {"Authorization": "Bearer abcdefghijklmnop",
                                              "X-Api-Key": "k-12345678"},
            "api_key": "plain-secret-value", "note": f"my key is {SECRET}",
            "long": "z" * 2000}
    with tracer.turn("t"):
        tracer.event("tool", {"tool": "treg_call", "args": args, "output": "{}"})
    tracer.end_turn("", 1)
    raw = "".join(p.read_text() for p in (tmp_path / "traces").glob("*.jsonl"))
    for secret in ("abcdefghijklmnop", "k-12345678", "plain-secret-value", SECRET):
        assert secret not in raw
    line = next(e for e in _lines(tmp_path) if e["type"] == "tool")
    assert isinstance(line["args"], str) and len(line["args"]) <= obs.ARGS_CHARS

    # a v1 line from before this spec still holds raw args; the page redacts it
    (tmp_path / "traces" / "2026-01-01.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "turn_start", "user_message": "old", "ts": "2026-01-01T10:00:00+00:00"},
        {"type": "tool", "tool": "treg_call", "args": {"api_key": "old-secret-123"},
         "output": f"echo {SECRET}", "ts": "2026-01-01T10:00:01+00:00"},
        {"type": "turn_end", "reply": "", "iterations": 1, "ts": "2026-01-01T10:00:02+00:00"},
    ]) + "\n")
    page = json.dumps(obs.payload(tmp_path, window="all"))
    assert "old-secret-123" not in page and SECRET not in page


# ---- 3. source and span kind ------------------------------------------------

def test_source_and_span_kind():
    servers = ("waku_memory", "treg", "github")
    assert obs.tool_source("treg_call", servers) == "treg"
    assert obs.tool_source("waku_memory_memory_search", servers) == "waku_memory"
    assert obs.tool_source("github_read", servers) == "mcp:github"
    assert obs.tool_source("save_note", servers) == "local"
    assert obs.span_kind("waku_memory_memory_search") == "retrieval"
    assert obs.span_kind("waku_memory_memory_get") == "retrieval"
    assert obs.span_kind("waku_memory_memory_remember") == "memory_write"
    assert obs.span_kind("treg_call") == "tool"


# ---- 4. aggregation ----------------------------------------------------------

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _tool(name, output, args=None, ms=None, ago=timedelta(hours=1)):
    ev = {"type": "tool", "tool": name, "args": args or {}, "output": output,
          "ts": (NOW - ago).isoformat()}
    if ms is not None:
        ev.update(v=2, duration_ms=ms)
    return ev


def test_tool_calls_group_by_source_tool_and_treg_endpoint():
    serp = json.dumps({"status": 200, "endpoint_id": "spyfu.google.domain.competitors",
                       "cost_usd": 0.02})
    events = [
        _tool("treg_call", TREG_OUT, ms=800), _tool("treg_call", TREG_OUT, ms=400),
        _tool("treg_call", serp, ms=600),
        _tool("treg_catalog_search", json.dumps({"results": []}), ms=100),
        _tool("treg_catalog_get", json.dumps({"endpoint": {}}), {"endpoint_id": "tomba.companies.similar"}),
        _tool("treg_call", "MCP call treg.call failed: timeout", ms=50),
        _tool("waku_memory_memory_search", SEARCH_OUT, {"query": "pricing"}, ms=200),
        _tool("waku_memory_memory_recall", json.dumps({"entries": [{"id": "a"}]}), ms=100),
        _tool("waku_memory_memory_remember", json.dumps({"id": "m9"}), {"body": "x"}),
        _tool("save_note", "saved", ms=5),
        _tool("github_read", "Error running github_read: nope"),
        _tool("treg_call", TREG_OUT, ago=timedelta(days=30)),   # outside 7 days
    ]
    out = obs.tool_stats(events, ("github",), "7d", NOW)
    treg = {r["tool"]: r for r in out["treg"]["tools"]}
    assert treg["treg_call"]["calls"] == 4 and treg["treg_call"]["errors"] == 1
    assert treg["treg_call"]["usd"] == round(0.0089 * 2 + 0.02, 6)
    assert treg["treg_call"]["avg_ms"] == round((800 + 400 + 600 + 50) / 4)
    assert treg["treg_catalog_search"]["usd"] is None, "no price named is not the same as free"
    ends = {e["endpoint_id"]: e for e in out["treg"]["endpoints"]}
    assert ends["tomba.companies.similar"]["calls"] == 2
    assert ends["spyfu.google.domain.competitors"] == {
        "endpoint_id": "spyfu.google.domain.competitors", "provider": "spyfu",
        "calls": 1, "errors": 0, "usd": 0.02}
    mem = {r["tool"]: r for r in out["waku_memory"]["tools"]}
    assert mem["memory_search"]["avg_results"] == 3 and mem["memory_search"]["span"] == "retrieval"
    assert mem["memory_remember"]["span"] == "memory_write"
    assert out["waku_memory"]["recent_queries"][0]["query"] == "pricing"
    assert out["waku_memory"]["recent_queries"][0]["retrieval_trace_id"] == "rt_42"
    other = {r["tool"]: r for r in out["other"]["tools"]}
    assert other["save_note"]["source"] == "local"
    assert other["github_read"]["source"] == "mcp:github" and other["github_read"]["errors"] == 1
    assert obs.tool_stats(events, (), "all", NOW)["treg"]["calls"] == 7


def test_today_starts_at_local_midnight():
    start = obs.window_start("today", NOW)
    assert (start.hour, start.minute) == (0, 0) and start <= NOW
    assert obs.window_start("all", NOW) is None


# ---- 5 and 6. old traces and the waterfall -----------------------------------

def _v1_trace():
    t0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    at = lambda s: (t0 + timedelta(seconds=s)).isoformat()  # noqa: E731
    return [
        {"type": "turn_start", "user_message": "research mem0", "ts": at(0)},
        {"type": "gate", "decision": "retrieve", "reason": "names a company", "ts": at(1)},
        {"type": "llm", "iteration": 1, "provider": "anthropic", "model": "claude-sonnet-5",
         "stop_reason": "tool_use", "usage": {"in": 1000, "out": 100}, "ts": at(3)},
        {"type": "tool", "tool": "treg_call", "args": {"endpoint_id": "tomba.companies.similar"},
         "output": TREG_OUT, "ts": at(4)},
        {"type": "tool", "tool": "waku_memory_memory_search", "args": {"query": "mem0"},
         "output": SEARCH_OUT, "ts": at(5)},
        {"type": "consolidation", "new_facts": 2,
         "kept": [{"content": "a", "sent": True}, {"content": "b", "sent": False}], "ts": at(7)},
        {"type": "turn_end", "reply": "ok", "iterations": 2, "ts": at(8)},
        {"type": "turn_start", "user_message": "hi", "ts": at(20)},
        {"type": "turn_end", "reply": "hey", "iterations": 1, "ts": at(21)},
    ]


def test_an_old_trace_builds_the_same_turns_with_durations_empty():
    turns = obs.group_turns(_v1_trace())
    assert [t["user_message"] for t in turns] == ["research mem0", "hi"]
    turn = obs.build_turn(turns[0], provider="anthropic", model="claude-sonnet-5")
    assert [s["kind"] for s in turn["steps"]] == ["gate", "llm", "tool", "memory", "consolidation"]
    assert [s["span"] for s in turn["steps"]] == ["gate", "llm", "tool", "retrieval", "memory_write"]
    tool = turn["steps"][2]
    assert tool["duration_ms"] is None and tool["span_ms"] == 1000
    assert tool["usd"] == 0.0089 and tool["endpoint_id"] == "tomba.companies.similar"
    assert turn["steps"][3]["results"] == 3
    consolidation = turn["steps"][4]
    assert consolidation["sent"] == 1 and consolidation["local_only"] == 1
    assert turn["latency_ms"] == 8000 and turn["turn_id"] == "" and turn["kept"] == 2
    assert turn["usd"] == round(turn["steps"][1]["usd"] + 0.0089, 6)
    assert turn["scores"] == []


def test_a_receipt_sets_the_turns_total_and_memory_counts():
    events = _v1_trace()[:6] + [
        {"type": "receipt", "turn_id": "t_1", "total_usd": 0.5, "credits": 12500,
         "model": {"estimate": False}, "memory": {"used": 4, "kept": [{}, {}]},
         "ts": events_ts()},
        _v1_trace()[6],
    ]
    turn = obs.build_turn(obs.group_turns(events)[0])
    assert turn["usd"] == 0.5 and turn["has_receipt"] and turn["used"] == 4 and turn["kept"] == 2
    assert turn["steps"][-1] == {**turn["steps"][-1], "kind": "receipt", "span": "receipt",
                                 "credits": 12500, "estimate": False}


def events_ts():
    return datetime(2026, 9, 28, 10, 0, 7, 500000, tzinfo=UTC).isoformat()


# ---- scores ------------------------------------------------------------------

def test_scores_attach_to_their_turn(tmp_path):
    trace = [{**e, "turn_id": "t_9"} if e["type"] in ("turn_start", "turn_end") else e
             for e in _v1_trace()[:7]]
    trace.append({"type": "score", "turn_id": "t_9", "source": "judge", "name": "relevance",
                  "value": 0.8, "note": "answered the question"})
    trace.append({"type": "score", "turn_id": "t_9", "source": "robot", "value": "high"})
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "2026-09-28.jsonl").write_text("\n".join(json.dumps(e) for e in trace))
    turn = obs.payload(tmp_path, window="all")["turns"][0]
    assert turn["scores"] == [{"source": "judge", "name": "relevance", "value": 0.8,
                               "note": "answered the question"}]


# ---- 7. spend ----------------------------------------------------------------

def test_spend_shows_charged_only_when_receipts_say_so(tmp_path):
    (tmp_path / "usage.jsonl").write_text(json.dumps(
        {"provider": "anthropic", "model": "claude-sonnet-5", "in": 1_000_000, "out": 0}) + "\n")
    estimated = obs.spend(tmp_path, [{"type": "receipt", "total_usd": 1.0,
                                      "model": {"estimate": True}, "credits": None}])
    assert estimated["charged_usd"] is None and estimated["credits"] is None
    assert estimated["estimated_usd"] > 0
    charged = obs.spend(tmp_path, [
        {"type": "receipt", "total_usd": 0.1, "model": {"estimate": False}, "credits": 2500},
        {"type": "receipt", "total_usd": 0.2, "model": {"estimate": False}, "credits": 5000}])
    assert charged["charged_usd"] == 0.3 and charged["credits"] == 7500
    assert charged["charged_turns"] == 2


# ---- evals -------------------------------------------------------------------

def test_evals_are_counted_from_the_repo_and_hosted_says_where_they_run(tmp_path):
    info = obs.evals_info(tmp_path)
    assert info["deterministic"]["tests"] > 100
    assert "response_quality" in info["judge_suites"]
    hosted = obs.evals_info(tmp_path, repo=tmp_path, hosted=True)
    assert hosted["deterministic"] is None and hosted["hosted"] is True and hosted["last"] is None


# ---- OTel GenAI names ----------------------------------------------------------

def test_otel_export_uses_the_genai_names():
    llm = obs.genai_attributes("llm", {"provider": "anthropic", "model": "claude-sonnet-5",
                                       "usage": {"in": 10, "out": 2}, "stop_reason": "end_turn"})
    assert llm == {"gen_ai.provider.name": "anthropic", "gen_ai.request.model": "claude-sonnet-5",
                   "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 2,
                   "gen_ai.response.finish_reasons": ["end_turn"],
                   "gen_ai.operation.name": "chat"}
    assert obs.genai_attributes("llm", {"cost_usd": 0.1})["waku.cost.usd"] == 0.1
    tool = obs.genai_attributes("tool", {"tool": "waku_memory_memory_search", "span": "retrieval",
                                         "source": "waku_memory", "query": "q", "results": 3})
    assert tool["gen_ai.tool.name"] == "waku_memory_memory_search"
    assert tool["gen_ai.operation.name"] == "search_memory"
    assert tool["gen_ai.memory.query.text"] == "q" and tool["gen_ai.memory.record.count"] == 3
    assert tool["gen_ai.tool.type"] == "datastore"
    assert tool["mcp.method.name"] == "tools/call"
    assert tool["gen_ai.memory.store.id"] == "waku_memory"
    priced = obs.genai_attributes("tool", {"tool": "treg_call", "span": "tool", "source": "treg",
                                           "cost_usd": 0.02})
    assert priced["waku.cost.usd"] == 0.02 and priced["gen_ai.operation.name"] == "execute_tool"
    local = obs.genai_attributes("tool", {"tool": "save_note", "span": "tool", "source": "local"})
    assert "mcp.method.name" not in local and local["gen_ai.tool.type"] == "function"


# ---- 8. the route -----------------------------------------------------------

def test_the_route_answers_from_the_home(tmp_path, monkeypatch):
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "2026-09-28.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _v1_trace()))
    out = dashboard.observability_data("all")
    assert set(out) >= {"turns", "tools", "memory", "spend", "evals"}
    assert out["tools"]["treg"]["endpoints"][0]["endpoint_id"] == "tomba.companies.similar"


def test_the_otel_span_carries_genai_names_and_waku_cost(tmp_path):
    from contextlib import contextmanager

    seen = []

    class FakeOtel:
        @contextmanager
        def start_as_current_span(self, name, attributes=None):
            seen.append((name, attributes))
            yield None

    tracer = Tracer(_settings(tmp_path))
    tracer._otel_tracer, tracer._span_ctx = FakeOtel(), object()
    tracer.event("tool", {"tool": "treg_call", "args": {"endpoint_id": "a.b"}, "output": TREG_OUT,
                          "call_id": "tu_7", "duration_ms": 5})
    _, attrs = seen[-1]
    assert attrs["gen_ai.tool.name"] == "treg_call" and attrs["gen_ai.tool.call.id"] == "tu_7"
    assert attrs["gen_ai.operation.name"] == "execute_tool" and attrs["mcp.method.name"] == "tools/call"
    assert attrs["waku.cost.usd"] == 0.0089 and "waku.cost_usd" not in attrs
    assert all(not isinstance(v, dict) for v in attrs.values())


# ---- the visual pass: loops, graph, summary strip, spend per day -------------

def test_steps_group_by_loop_iteration():
    t0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    at = lambda s: (t0 + timedelta(seconds=s)).isoformat()  # noqa: E731
    events = [
        {"type": "turn_start", "turn_id": "t_1", "user_message": "x", "ts": at(0)},
        {"type": "graph_start", "workflow": "triage", "nodes": ["classify", "full_agent"], "ts": at(0)},
        {"type": "node_start", "node": "classify", "ts": at(0)},
        {"type": "llm", "kind": "triage", "usage": {"in": 5, "out": 1}, "node": "classify", "ts": at(1)},
        {"type": "gate", "decision": "skip", "node": "full_agent", "ts": at(2)},
        {"type": "llm", "kind": "gate", "usage": {"in": 5, "out": 1}, "ts": at(2)},
        {"type": "llm", "kind": "loop", "iteration": 1, "stop_reason": "tool_use",
         "usage": {"in": 100, "out": 10}, "node": "full_agent", "ts": at(3)},
        {"type": "tool", "tool": "save_note", "args": {}, "output": "saved", "ts": at(4)},
        {"type": "llm", "kind": "loop", "iteration": 2, "stop_reason": "tool_use",
         "usage": {"in": 200, "out": 20}, "ts": at(5)},
        {"type": "tool", "tool": "treg_call", "args": {}, "output": TREG_OUT, "ts": at(6)},
        {"type": "llm", "kind": "loop", "iteration": 3, "stop_reason": "end_turn",
         "usage": {"in": 300, "out": 30}, "ts": at(7)},
        {"type": "llm", "kind": "consolidation", "usage": {"in": 50, "out": 5}, "ts": at(8)},
        {"type": "consolidation", "new_facts": 1, "kept": [], "ts": at(8)},
        {"type": "graph_end", "workflow": "triage", "ms": 8000, "steps": 2,
         "path": ["classify", "full_agent"], "ts": at(8)},
        {"type": "turn_end", "turn_id": "t_1", "reply": "ok", "iterations": 3, "ts": at(9)},
    ]
    turn = obs.build_turn(obs.group_turns(events)[0], provider="anthropic", model="claude-sonnet-5")
    assert turn["loops"] == 3
    placed = [(s["kind"], s.get("call"), s["phase"], s["loop"]) for s in turn["steps"]]
    assert placed == [
        ("llm", "triage", "before", 0), ("gate", None, "before", 0), ("llm", "gate", "before", 0),
        ("llm", "loop", "loop", 1), ("tool", None, "loop", 1),
        ("llm", "loop", "loop", 2), ("tool", None, "loop", 2),
        ("llm", "loop", "loop", 3),
        ("llm", "consolidation", "after", 3), ("consolidation", None, "after", 3)]
    assert turn["tokens_in"] == 660 and turn["tokens_out"] == 67
    assert turn["graph"] == {"workflow": "triage", "path": ["classify", "full_agent"],
                             "ms": 8000, "error": None}
    assert turn["steps"][3]["node"] == "full_agent"


def test_an_old_turn_without_kinds_still_counts_its_loops():
    turn = obs.build_turn(obs.group_turns(_v1_trace())[0])
    assert turn["loops"] == 1 and turn["graph"] is None
    assert [s["phase"] for s in turn["steps"]] == ["before", "loop", "loop", "loop", "after"]


def test_the_summary_strip_and_spend_per_day(tmp_path):
    day = lambda d, h=10: datetime(2026, 10, d, h, tzinfo=UTC).isoformat()  # noqa: E731
    (tmp_path / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": day(4), "provider": "anthropic", "model": "claude-sonnet-5", "in": 1_000_000, "out": 0,
         "turn_id": "t_a"},
        {"ts": day(3), "provider": "anthropic", "model": "claude-sonnet-5", "in": 0, "out": 100_000},
        {"ts": day(1), "provider": "anthropic", "model": "claude-sonnet-5", "in": 500, "out": 0,
         "turn_id": "t_b"},
    ]) + "\n")
    serp = json.dumps({"status": 200, "endpoint_id": "spyfu.x", "cost_usd": 0.02})
    events = [
        {"type": "turn_start", "turn_id": "t_a", "user_message": "a", "ts": day(4, 9)},
        {"type": "llm", "kind": "loop", "iteration": 1, "stop_reason": "tool_use",
         "usage": {"in": 1, "out": 1}, "ts": day(4, 9)},
        {"type": "tool", "tool": "treg_call", "args": {}, "output": serp, "ts": day(4, 9)},
        {"type": "tool", "tool": "waku_memory_memory_search", "args": {"query": "q"},
         "output": "MCP call failed: down", "ts": day(4, 9)},
        {"type": "llm", "kind": "loop", "iteration": 2, "stop_reason": "end_turn",
         "usage": {"in": 1, "out": 1}, "ts": day(4, 9)},
        {"type": "receipt", "turn_id": "t_a", "total_usd": 0.5, "credits": 1000, "ts": day(4, 9),
         "model": {"estimate": False, "usd": 0.48}, "tools": [{"tool": "treg_call", "usd": 0.02}]},
        {"type": "turn_end", "turn_id": "t_a", "reply": "", "iterations": 2, "ts": day(4, 9)},
        {"type": "turn_start", "turn_id": "t_b", "user_message": "b", "ts": day(1)},
        {"type": "tool", "tool": "treg_call", "args": {}, "output": serp, "ts": day(1)},
        {"type": "turn_end", "turn_id": "t_b", "reply": "", "iterations": 1, "ts": day(1)},
    ]
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    turns = [obs.build_turn(t) for t in obs.group_turns(events)]
    tools = obs.tool_stats(events, (), "7d", now)
    m = obs.summary(tmp_path, events, turns, tools, "7d", now)
    sonnet_in, sonnet_out = obs.price_for("anthropic", "claude-sonnet-5")
    # t_a was charged: its receipt's exact $0.48 replaces its ledger estimate
    assert m["spend"]["model_usd"] == round(0.48 + 0.1 * sonnet_out + 500 / 1e6 * sonnet_in, 6)
    assert m["spend"]["treg_usd"] == 0.04 and m["spend"]["memory_usd"] == 0
    assert m["spend"]["charged_usd"] == 0.5 and m["spend"]["credits"] == 1000
    assert m["tokens"] == {"in": 1_000_500, "out": 100_000, "calls": 3}
    assert m["tools"]["treg"] == {"calls": 2, "errors": 0}
    assert m["tools"]["waku_memory"] == {"calls": 1, "errors": 1}
    assert m["turns"] == {"count": 2, "avg_loops": 2.0}

    days = obs.spend_by_day(tmp_path, events)
    assert [r["date"] for r in days] == ["2026-10-04", "2026-10-03", "2026-10-01"]
    assert days[0]["model"] == 0.48 and days[0]["treg"] == 0.02 and days[0]["charged"] == 0.5
    assert days[2]["treg"] == 0.02 and days[2]["total"] == round(days[2]["model"] + 0.02, 6)


# ---- follow-up: cards = tabs; Evals as its own page ---------------------------

def test_the_memory_card_counts_reads_writes_kept_and_the_gate(tmp_path):
    ts = datetime(2026, 10, 4, 9, tzinfo=UTC).isoformat()
    one = json.dumps({"entries": [{"id": "m1"}]})
    events = [
        {"type": "turn_start", "turn_id": "t_a", "user_message": "a", "ts": ts},
        {"type": "gate", "decision": "retrieve", "ts": ts},
        {"type": "tool", "tool": "waku_memory_memory_search", "args": {"query": "q"},
         "output": SEARCH_OUT, "ts": ts},
        {"type": "tool", "tool": "waku_memory_memory_recall", "args": {"query": "r"},
         "output": one, "ts": ts},
        {"type": "tool", "tool": "waku_memory_memory_recall", "args": {"query": "s"},
         "output": one, "ts": ts},
        {"type": "tool", "tool": "waku_memory_memory_remember", "args": {"body": "x"},
         "output": json.dumps({"id": "m9"}), "ts": ts},
        {"type": "receipt", "total_usd": 0, "memory": {"used": 1, "kept": ["f1", "f2"]}, "ts": ts},
        {"type": "turn_end", "turn_id": "t_a", "reply": "", "ts": ts},
        {"type": "turn_start", "turn_id": "t_b", "user_message": "b", "ts": ts},
        {"type": "gate", "decision": "skip", "ts": ts},
        {"type": "turn_end", "turn_id": "t_b", "reply": "", "ts": ts},
    ]
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    turns = [obs.build_turn(t) for t in obs.group_turns(events)]
    m = obs.summary(tmp_path, events, turns, obs.tool_stats(events, (), "7d", now), "7d", now)
    # 3 + 1 + 1 results over 3 reads; the weighted average, not the per-tool one
    assert m["memory"] == {"retrievals": 3, "avg_results": round(5 / 3, 1), "writes": 1,
                           "kept": 2, "gate_retrieve": 1, "gate_skip": 1}


def _static(name):
    from pathlib import Path
    return (Path(obs.__file__).parent / "static" / name).read_text(encoding="utf-8")


def test_the_cards_are_the_tabs_in_one_order_and_evals_has_its_own_page():
    import re
    js = _static("js/observe.js")
    tabs = re.search(r"const OBS_TABS = (\[.*?\]);\n", js).group(1)
    assert json.loads(tabs) == [["turns", "Turns"], ["tools", "Tools"],
                                ["memory", "Memory"], ["spend", "Spend"]]
    # the cards are drawn from OBS_TABS itself, each opening its own tab,
    # so their order and names cannot drift from the tabs again
    cards = js[js.index("function obsCards"):js.index("const obsWindowBar")]
    assert "OBS_TABS.map(([k, label])" in cards and 'href="#observability/${k}"' in cards
    assert "${label}" in cards and 'aria-current="page"' in cards
    assert "VIEWS.evals = function" in js
    assert "Evals judge whether a turn or a release was good: tests, an AI judge, a human." in js

    nav = _static("index.html")
    links = re.findall(r'<a href="#([a-z/]+)" data-v=', nav)
    assert links[links.index("observability") + 1] == "evals"

    main = _static("js/main.js")
    assert 'hashView === "observability" && subRaw === "evals"' in main
    assert 'history.replaceState(null, "", "#evals")' in main

    diagram = _static("js/diagram.js")
    assert '"Trace",s.trace_files+" file(s) · always on","observability/turns"' in diagram
    assert '"Eval","deterministic + judge","evals"' in diagram
    assert "observability/evals" not in diagram


def test_tokens_sit_with_spend_per_model_and_per_day(tmp_path):
    day = lambda d: datetime(2026, 10, d, 10, tzinfo=UTC).isoformat()  # noqa: E731
    (tmp_path / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": day(4), "provider": "anthropic", "model": "claude-sonnet-5", "in": 1000, "out": 10},
        {"ts": day(4), "provider": "anthropic", "model": "claude-sonnet-5", "in": 2000, "out": 20},
        {"ts": day(3), "provider": "openai", "model": "gpt-5", "in": 5, "out": 5},
    ]) + "\n")
    models = obs.spend(tmp_path, [])["by_model"]
    sonnet = next(r for r in models if r["model"] == "claude-sonnet-5")
    assert (sonnet["calls"], sonnet["in"], sonnet["out"]) == (2, 3000, 30)
    days = obs.spend_by_day(tmp_path, [])
    assert [(r["date"], r["in"], r["out"]) for r in days] == [("2026-10-04", 3000, 30), ("2026-10-03", 5, 5)]
    # no Tokens card or tab: the Spend card carries tokens as its second line
    js = _static("js/observe.js")
    assert "Tokens are what model calls are charged by; tools and memory are charged per call." in js
    spend_card = js[js.index("const sp = m.spend"):js.index("function obsCards")]
    assert "m.tokens.in" in spend_card and "m.tokens.out" in spend_card


def test_the_evals_page_counts_come_from_the_last_gate_run(tmp_path):
    run = {"deterministic": "pass", "judge": "pass", "ran_at": "2026-10-05T01:42:57+00:00",
           "suites": {"deterministic": {"passed": 3094, "failed": 0}, "judge": {"passed": 21, "failed": 0}}}
    older = {**run, "ran_at": "2026-09-22T19:09:09+00:00",
             "suites": {"deterministic": {"passed": 878, "failed": 0}}}
    # no gate run: nothing passes for a result, the page falls back to disk counts
    assert obs.evals_info(tmp_path, repo=tmp_path)["last_run"] is None
    (tmp_path / "eval_runs.jsonl").write_text(json.dumps(older) + "\n" + json.dumps(run) + "\n")
    (tmp_path / "eval_report.json").write_text(json.dumps(run))
    last = obs.evals_info(tmp_path, repo=tmp_path)["last_run"]
    assert last["ran_at"] == "2026-10-05T01:42:57+00:00"
    assert last["deterministic"] == {"passed": 3094, "failed": 0}
    assert last["judge"] == {"passed": 21, "failed": 0}
    # a report without suite counts falls back to the newest run that has them
    (tmp_path / "eval_report.json").write_text(json.dumps({"deterministic": "pass", "judge": "pass"}))
    assert obs.evals_info(tmp_path, repo=tmp_path)["last_run"]["deterministic"]["passed"] == 3094
    # a run whose judge did not run says so instead of borrowing a count
    (tmp_path / "eval_report.json").write_text(json.dumps(older))
    assert obs.evals_info(tmp_path, repo=tmp_path)["last_run"]["judge"] is None

    js = _static("js/observe.js")
    evals = js[js.index("function obsEvals"):js.index("function obsCardBody")]
    assert "run.deterministic" in evals and "passed" in evals and "failed" in evals
    # the static fallback names what it counted and says it is not a run
    assert "test functions in ${det.files} files, counted on disk; no gate run recorded" in evals
    assert "suite files, counted on disk; no gate run recorded" in evals

# ---- the Spend card adds up (Oct 5 rehearsal) ----------------------------------
# The card read "SPEND · CHARGED $1.81" over "model $8.62 · treg $0.27 · est
# $8.89": charged covered only the turns with exact receipts while the split
# covered every turn. Now the headline is the window's best total, and both
# splits (by source, and charged + estimated) add up to it.

def _mixed_home(tmp_path):
    ts = datetime(2026, 10, 4, 9, tzinfo=UTC).isoformat()
    treg = json.dumps({"status": 200, "endpoint_id": "spyfu.x", "cost_usd": 0.27})
    (tmp_path / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in [
        # the charged turn's two calls: estimated at list price, replaced by its receipt
        {"ts": ts, "provider": "anthropic", "model": "claude-sonnet-5", "in": 400_000, "out": 0, "turn_id": "t_paid"},
        {"ts": ts, "provider": "anthropic", "model": "claude-sonnet-5", "in": 100_000, "out": 0, "turn_id": "t_paid"},
        # a turn the platform did not price: its estimate stays
        {"ts": ts, "provider": "anthropic", "model": "claude-sonnet-5", "in": 1_000_000, "out": 0, "turn_id": "t_est"},
    ]) + "\n")
    return [
        {"type": "turn_start", "turn_id": "t_paid", "user_message": "a", "ts": ts},
        {"type": "tool", "tool": "treg_call", "args": {}, "output": treg, "ts": ts},
        {"type": "receipt", "turn_id": "t_paid", "total_usd": 1.81, "credits": 300, "ts": ts,
         "model": {"estimate": False, "usd": 1.54}, "tools": [{"tool": "treg_call", "usd": 0.27}]},
        {"type": "turn_end", "turn_id": "t_paid", "ts": ts},
        {"type": "turn_start", "turn_id": "t_est", "user_message": "b", "ts": ts},
        {"type": "receipt", "turn_id": "t_est", "total_usd": 3.0, "ts": ts,
         "model": {"estimate": True, "usd": 3.0}, "tools": []},
        {"type": "turn_end", "turn_id": "t_est", "ts": ts},
    ]


def test_the_spend_split_adds_up_to_the_headline(tmp_path):
    events = _mixed_home(tmp_path)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    turns = [obs.build_turn(t) for t in obs.group_turns(events)]
    sp = obs.summary(tmp_path, events, turns, obs.tool_stats(events, (), "7d", now), "7d", now)["spend"]
    sonnet_in, _ = obs.price_for("anthropic", "claude-sonnet-5")
    assert sp["model_usd"] == round(1.54 + sonnet_in, 6)   # exact for t_paid, list price for t_est
    assert sp["treg_usd"] == 0.27
    assert sp["total_usd"] == round(sp["model_usd"] + sp["treg_usd"] + sp["memory_usd"] + sp["other_usd"], 6)
    assert sp["charged_usd"] == 1.81 and sp["charged_turns"] == 1 and sp["credits"] == 300
    assert sp["estimated_usd"] == round(sonnet_in, 6)
    assert round(sp["charged_usd"] + sp["estimated_usd"], 6) == sp["total_usd"]


def test_the_spend_tab_and_days_follow_the_same_rule(tmp_path):
    events = _mixed_home(tmp_path)
    tab = obs.spend(tmp_path, events)
    assert round(tab["model_usd"] + tab["treg_usd"] + tab["memory_usd"] + tab["other_usd"], 6) == tab["total_usd"]
    assert round(tab["charged_usd"] + tab["estimated_usd"], 6) == tab["total_usd"]
    days = obs.spend_by_day(tmp_path, events)
    assert round(sum(d["total"] for d in days), 6) == tab["total_usd"]
    assert round(sum(d["charged"] for d in days), 6) == tab["charged_usd"]
    # no receipt priced anything: everything is estimated and nothing is called charged
    plain = obs.spend(tmp_path, [e for e in events if e["type"] != "receipt"])
    assert plain["charged_usd"] is None and plain["estimated_usd"] == plain["total_usd"] > 0


def test_the_spend_card_shows_total_as_charged_plus_estimated():
    js = _static("js/observe.js")
    card = js[js.index("const sp = m.spend"):js.index("function obsCards")]
    assert "sp.total_usd" in card and "sp.charged_usd" in card and "sp.estimated_usd" in card
    assert "of(\"total\")" in card and "charged + " in card
    # the card no longer adds its own sum beside a different headline
    assert "sp.model_usd + sp.treg_usd" not in card
    tab = js[js.index("function obsSpend"):js.index("// ---------- Evals")]
    assert "sp.total_usd" in tab and "sp.charged_usd" in tab and "sp.estimated_usd" in tab
