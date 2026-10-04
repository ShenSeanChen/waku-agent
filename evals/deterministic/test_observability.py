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
