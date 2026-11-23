"""DETERMINISTIC EVAL -- three faults from Sean's rehearsal, 2026-10-05 02:18 UTC.

One hosted research turn ("Research Mem0's top competitors with treg, and
save a report to the Company brain.") showed three faults:

  1. Cost. The reply said "This run cost $0.00, because I used two free
     TinyFish searches through treg", and the report had a "Cost of this run
     $0.00" tile. The receipt said $0.726: the model's own tokens. The model
     cannot see that number, so the skill now says to name only the treg
     cost and never a total, and the report's sources line says "treg cost".
  2. Two reports. Waku Memory got two waku-report v1 memories, 8 seconds
     apart (ec6f80b7 at 02:18:37, d03537af at 02:18:45). Read back, the first
     was the whole report, saved by the model itself with
     waku_memory_memory_remember; the second was the shorter report in its
     reply, which reports.save sent too, and which named the first's id. The
     bodies differed, so Waku Memory's dedupe (same body and scope) kept
     both. Not the MCP session retry (#284): that resends only a call the
     server refused as "Session not found", and a resend would be the same
     body, which the server would have deduped.
  3. Context. 328.5k tokens in, against 165k for an earlier similar turn:
     the read-first search answers with a snippet of each long report, so
     the model fetched whole reports with memory_get, and every whole report
     was re-sent on each later call of the turn.

A fake bridge stands in for the MCP session, a scripted client for the model.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.loop.agent import run_loop
from waku.memory import brain, reports
from waku.tools import waku_memory
from waku.tools.registry import Tool, ToolRegistry

SKILL = Path(__file__).resolve().parents[2] / "skills" / "research-report" / "SKILL.md"
GATE = response([text_block('{"retrieve": false, "query": "", "reason": "research"}')])
COMPANY = response([text_block('{"company_research": true}')])
REMEMBER = "waku_memory_memory_remember"


def _report(title: str, rows: int = 1) -> str:
    """A waku-report v1 body; `rows` findings rows make it as long as a real one."""
    findings = "\n".join(
        f"| Vendor {i} | Agent memory with a knowledge graph and vector search, row {i} "
        f"| $19 a month (2026-10-03) | Seed $10M (2024-08-01) | Earlier report 2026-10-04 |"
        for i in range(rows))
    sources = json.dumps([{"title": f"Source {i}", "url": f"https://s{i}.example/pricing",
                           "via": "treg:tinyfish.web.search", "cost_usd": 0}
                          for i in range(max(rows // 4, 1))])
    return f"""<!-- waku-report v1 -->
# {title}

## Summary
- Mem0's closest direct competitors are Supermemory, Cognee, Letta and Zep.
- Mem0 says it raised $24M across a Seed and a Series A, announced 2025-10-28.
- Cognee Cloud is free to 1M tokens, then $1.00 per 1M tokens.

## Findings
| Company | What it sells | Price | Funding | Source |
|---|---|---|---|---|
{findings}

## Numbers
```waku-metrics
[{{"label": "Mem0 funding", "value": "$24M", "note": "2025-10-28"}}, {{"label": "Cost of this run", "value": "$0.00", "note": "two free TinyFish searches"}}]
```

## Sources
```waku-sources
{sources}
```
"""


FULL = _report("Mem0's top competitors: Mem0 funding and Cognee pricing added, 2026-10-05", 40)
SHORT = _report("Mem0's top competitors: Mem0 funding and Cognee pricing added, 2026-10-05")
REPLY = ("Mem0's closest competitors are still Supermemory, Cognee, Letta and Zep.\n\n"
         + SHORT)


class FakeBridge:
    """Waku Memory over MCP: remember hands out ids in order and keeps each
    body, search finds the reports kept in `stored`, get returns one whole."""

    def __init__(self, config_path, stored=None):
        self.config_path = config_path
        self.stored = dict(stored or {})
        self.calls: list[tuple[str, dict]] = []

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        self.calls.append((tool, args))
        if tool == "memory.remember":
            memory_id = f"mem-{len(self.stored) + 1}"
            self.stored[memory_id] = args["body"]
            return json.dumps({"memory": {"id": memory_id, "body": args["body"]},
                               "deduped": False})
        if tool == "memory.search":
            entries = [{"id": i, "kind": "semantic", "body": b[:180], "body_truncated": True,
                        "created_at": "2026-10-04T16:15:13+00:00"}
                       for i, b in self.stored.items()][:args.get("limit", 10)]
            return json.dumps({"entries": entries, "scope_effective": "all"})
        if tool == "memory.get":
            return json.dumps({"memory": {"id": args["id"], "kind": "semantic",
                                          "scope": "project:Company brain",
                                          "created_at": "2026-10-04T16:15:13+00:00",
                                          "body": self.stored[args["id"]]}})
        return "{}"

    def close(self):
        pass

    def remembered(self):
        return [args["body"] for tool, args in self.calls if tool == "memory.remember"]


def _bridge(tmp_path, stored=None):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    return FakeBridge(path, stored)


def _app(tmp_path, monkeypatch, script, bridge):
    """A Waku whose registry has Waku Memory's tools as the bridge loads
    them: <server>_<tool>, each calling the server's own tool name."""
    import waku.app

    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        for name in ("memory.remember", "memory.get"):
            registry.register(Tool(
                name=waku_memory.tool_name(bridge, name), description="[MCP:waku_memory]",
                input_schema={"type": "object", "properties": {}},
                fn=lambda name=name, **kw: bridge.call("waku_memory", name, kw)))
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    return make_waku(tmp_path / "home", client=ScriptedClient(script), consolidate_every=50)


def _turn(app, message="Research Mem0's top competitors with treg, and save a report"):
    events = []
    result = app.respond(message, observer=lambda kind, ev: events.append((kind, ev)))
    return result, [ev for kind, ev in events if kind == "report"]


# --- 1. cost honesty ------------------------------------------------------------------


def _skill() -> str:
    return " ".join(SKILL.read_text(encoding="utf-8").split())


def test_the_research_skill_names_only_the_treg_cost_and_leaves_totals_to_the_receipt():
    skill = _skill()
    assert '"treg cost"' in skill
    assert "Never state a total, a model cost, or what the run or turn cost" in skill
    assert "The receipt under the reply shows the totals." in skill
    assert "No cost tile in Numbers" in skill


def test_the_skill_example_states_no_run_cost():
    """The example is what the model copies."""
    example = SKILL.read_text(encoding="utf-8").split("````markdown", 1)[1].lower()
    for claim in ("cost of this run", "this run cost", "total cost", "run cost"):
        assert claim not in example


def test_a_report_digest_drops_a_run_cost_tile_and_keeps_the_numbers():
    found = reports.digest(FULL)
    assert "Cost of this run" not in found and "$0.00" not in found
    assert "Mem0 funding: $24M (2025-10-28)" in found


# --- 2. one report a turn ---------------------------------------------------------------


def test_the_rehearsal_turn_saves_one_report(tmp_path, monkeypatch):
    """The model saves the whole report itself, then replies with a shorter
    one: Waku Memory is sent exactly one report, the reply's."""
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, [
        GATE,
        response([tool_block(REMEMBER, {"body": FULL, "kind": "semantic",
                                        "scope": "project:Company brain"})], "tool_use"),
        response([text_block(REPLY)]),
        COMPANY], bridge)
    result, cards = _turn(app)

    assert bridge.remembered() == [SHORT], "one report reached Waku Memory"
    refused = result.tool_calls[0]
    assert refused["tool"] == REMEMBER and refused["output"] == reports.REFUSAL
    assert [c["memory_id"] for c in cards] == ["mem-1"]
    assert result.reply.endswith("Report saved: " + reports.find(SHORT).title + ".")


def test_the_model_can_still_remember_a_fact(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, [
        GATE,
        response([tool_block(REMEMBER, {"body": "Sean prefers short reports.",
                                        "scope": "global"})], "tool_use"),
        response([text_block("Noted.")])], bridge)
    result, cards = _turn(app, "remember that I prefer short reports")
    assert bridge.remembered() == ["Sean prefers short reports."]
    assert cards == [] and result.reply == "Noted."


def test_a_report_the_model_did_save_is_the_turns_report(tmp_path, monkeypatch):
    """Without the guard (an agent built before it, a server under another
    name), save() still sends no second report: the card points at the
    model's memory and the reply is shortened as usual."""
    monkeypatch.setattr(reports, "guard_remember", lambda fn: fn)
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, [
        GATE,
        response([tool_block(REMEMBER, {"body": FULL, "kind": "semantic",
                                        "scope": "project:Company brain"})], "tool_use"),
        response([text_block(REPLY)])], bridge)   # no scope question is asked
    result, cards = _turn(app)

    assert bridge.remembered() == [FULL], "the model's save, and nothing after it"
    assert cards == [{"title": reports.find(FULL).title, "memory_id": "mem-1",
                      "scope": "project:Company brain",
                      "summary": reports.find(FULL).summary}]
    assert result.reply.startswith("Mem0's closest competitors") and "waku-report" not in result.reply


def test_a_failed_model_save_does_not_count_as_the_report():
    calls = [{"tool": REMEMBER, "args": {"body": FULL, "scope": "global"},
              "output": "MCP call waku_memory_memory.remember failed: timed out"},
             {"tool": REMEMBER, "args": {"body": FULL, "scope": "global"},
              "output": reports.REFUSAL}]
    assert reports.saved_by_model(calls) is None


def test_the_session_retry_resends_only_a_call_the_server_refused():
    """#284 opens a new session and resends only after "Session not found"
    (or the SDK's "Session terminated"): the server refused before running
    the call. A timeout or any other error is never resent."""
    from waku.tools.mcp_client import _session_expired

    assert _session_expired(RuntimeError("Session not found"))
    for other in ("timed out", "Server returned an error response", "Internal error",
                  "deduped"):
        assert not _session_expired(RuntimeError(other))


# --- 3. reading earlier reports costs a digest, not the whole -----------------------------


def test_read_first_puts_each_earlier_report_in_as_its_digest(tmp_path):
    bridge = _bridge(tmp_path, {f"rep-{i}": _report(f"Report {i}", 40) for i in range(3)})
    known = brain.read_first("research mem0 competitors",
                             waku_memory.search_via(bridge), waku_memory.get_via(bridge))
    assert set(known.digests) == {"rep-0", "rep-1", "rep-2"}
    for found in known.digests.values():
        assert len(found) <= reports.DIGEST_CHARS
        assert found in known.context.replace("\n  ", "\n")
    assert "Vendor 39" not in known.context, "no findings table rows"
    assert "only when the person asks to compare details" in known.context


def test_shrink_read_cuts_a_report_already_read_and_leaves_the_newest():
    whole = json.dumps({"memory": {"id": "rep-0", "kind": "semantic", "body": FULL}})
    messages = [{"role": "user", "content": "research"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                              "content": whole}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b",
                                              "content": whole}]}]
    reports.shrink_read(messages)
    cut = json.loads(messages[1]["content"][0]["content"])["memory"]
    assert cut["id"] == "rep-0" and cut["body"].endswith(reports.TRIMMED)
    assert len(cut["body"]) < reports.DIGEST_CHARS + len(reports.TRIMMED) + 2
    assert messages[2]["content"][0]["content"] == whole, "the unread result stays whole"


class SizingClient(ScriptedClient):
    """The scripted model, which also counts the characters each call sends."""

    def __init__(self, script):
        super().__init__(script)
        self.sent: list[int] = []

    def _create(self, **kwargs):
        size = len(kwargs.get("system", ""))
        for m in kwargs["messages"]:
            if isinstance(m["content"], str):
                size += len(m["content"])
                continue
            for b in m["content"]:
                if isinstance(b, dict):
                    size += len(str(b.get("content", "")))
                elif b.type == "text":
                    size += len(b.text)
                else:
                    size += len(json.dumps(b.input))
        self.sent.append(size)
        return super()._create(**kwargs)


def _research_turn(stored, *, new: bool, model_reads_reports: bool) -> int:
    """Characters sent over one research turn: read first, the model maybe
    reads every earlier report whole, four treg calls, then the reply."""
    bridge = FakeBridge(None, stored)
    search = lambda args: bridge.call("waku_memory", "memory.search", args)  # noqa: E731
    get = (lambda i: bridge.call("waku_memory", "memory.get", {"id": i})) if new else None
    known = brain.read_first("research mem0 competitors", search, get)

    registry = ToolRegistry()
    registry.register(Tool("waku_memory_memory_get", "", {}, lambda id: bridge.call(
        "waku_memory", "memory.get", {"id": id})))
    registry.register(Tool("treg_call", "", {}, lambda **kw: json.dumps(
        {"cost_usd": 0, "endpoint_id": "tinyfish.web.search", "results": ["x" * 1500]})))
    script = []
    if model_reads_reports:
        script.append(response([tool_block("waku_memory_memory_get", {"id": i}, f"g{i}")
                                for i in stored], "tool_use"))
    script += [response([tool_block("treg_call", {"q": n}, f"t{n}")], "tool_use")
               for n in range(4)]
    script.append(response([text_block("Done.")]))
    client = SizingClient(script)
    run_loop(client, "m", "Soul." + known.context, [{"role": "user", "content": "research"}],
             registry, max_iterations=10, trim=reports.shrink_read if new else None)
    return sum(client.sent)


def test_three_large_earlier_reports_cost_far_less_context(tmp_path):
    """The fixture: three earlier reports of about 7k characters each, as
    long as the rehearsal's (ec6f80b7 is 7.6k), and a six-call turn.

    before: snippets in the prompt; the model reads all three whole, and
            they ride along on every later call.
    after:  digests in the prompt. If the model still reads them whole, they
            are cut to their digests after it has read them once."""
    stored = {f"rep-{i}": _report(f"Mem0 competitors, report {i}", 40) for i in range(3)}
    assert all(6000 < len(b) < 9000 for b in stored.values())

    before = _research_turn(stored, new=False, model_reads_reports=True)
    after_reads = _research_turn(stored, new=True, model_reads_reports=True)
    after = _research_turn(stored, new=True, model_reads_reports=False)

    # measured: 148k, 57k and 24k characters; at ~4 a token, before ~37k
    # tokens in, after ~14k when the model still reads them, ~6k when not
    assert after_reads < before * 0.45, (before, after_reads)
    assert after < before * 0.25, (before, after)
