"""DETERMINISTIC EVAL -- three faults from Sean's rehearsal of the hosted
Waku Agent on www.waku.one, 2026-10-05.

  1. Context. "Using treg, search Reddit and X ... pull the comments from the
     top YouTube video ... save an audience brief to the Company brain" made
     8 treg calls in 8 iterations and sent 626.5k tokens in for 8.4k out:
     model $1.334, treg $0.007. Each search and comment JSON the model had
     already read was re-sent on every later call. The loop's `trim` step now
     also cuts any long result the model has read to its opening
     (waku/loop/trim.py), and leaves the newest results whole.
  2. No report. That turn left five loose `reference` memories in the
     Company brain, one of them starting "AUDIENCE BRIEF", and no waku-report
     v1 report. The research-report skill never loaded: the message shared one
     word ("company") with its description, and a skill needs two. Without
     the skill the model had no report format, and consolidation filed the
     findings as separate facts. The description now names a saved brief,
     summary or snapshot, and the skill says each one is one report.
  3. Narration. An AI-visibility turn's reply said the report "Waku should
     save to Waku Memory after this reply; I haven't seen a save
     confirmation". The skill and the refusal text now say never to write
     about the save: the chat shows "Report saved" under the reply.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.loop import trim
from waku.loop.agent import run_loop
from waku.memory import brain, reports
from waku.memory.procedural.loader import SkillLoader
from waku.tools import waku_memory
from waku.tools.registry import Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "research-report" / "SKILL.md"
REHEARSAL = ("Using treg, search Reddit and X for what people said this month about "
             "'agent observability' and 'Mem0 alternative', and pull the comments from "
             "the top YouTube video about AI agent memory. Group what people ask for and "
             "save an audience brief to the Company brain.")


# --- 1. long results the model has read are cut ---------------------------------------


def _comments(call: int, size: int = 50_000) -> str:
    """A treg comments answer about `size` characters long, unique per call."""
    row = {"author": f"user{call}", "text": "I want an agent memory I can inspect. " * 4,
           "likes": 12}
    rows, text = [], ""
    while len(text) < size:
        rows.append(row)
        text = json.dumps({"call": call, "comments": rows})
    return text


class CountingClient(ScriptedClient):
    """The scripted model, which also counts the tool-result characters each
    call sends and keeps what the newest results looked like."""

    def __init__(self, script):
        super().__init__(script)
        self.sent: list[int] = []
        self.newest: list[list[str]] = []

    def _create(self, **kwargs):
        results = [[b["content"] for b in m["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"]
                   for m in kwargs["messages"] if isinstance(m["content"], list)]
        results = [r for r in results if r]
        self.sent.append(sum(len(c) for r in results for c in r))
        self.newest.append(list(results[-1]) if results else [])
        return super()._create(**kwargs)


def _six_loop_turn(step) -> CountingClient:
    """Six iterations that each call one tool returning 50 KB, then a reply."""
    outputs = [_comments(i) for i in range(6)]
    registry = ToolRegistry()
    registry.register(Tool(name="treg_call", description="treg",
                           input_schema={"type": "object", "properties": {}},
                           fn=lambda call: outputs[call]))
    script = [response([tool_block("treg_call", {"call": i}, f"tu_{i}")], "tool_use")
              for i in range(6)]
    script.append(response([text_block("Done.")]))
    client = CountingClient(script)
    run_loop(client, "m", "Soul.", [{"role": "user", "content": REHEARSAL}],
             registry, max_iterations=10, trim=step)
    return client


def test_a_six_loop_turn_resends_far_less_tool_output():
    """before: 50 KB results ride along on every later call, 750k characters.
    after:  each result is sent whole once, then as its 2,000-character
            opening: under 400k characters, and the drop grows with every loop."""
    before = _six_loop_turn(None)
    after = _six_loop_turn(trim.chain(reports.shrink_read, trim.shrink_seen))
    assert sum(before.sent) > 700_000, before.sent
    # each result whole once (6 x 50k) plus a cut copy on each later call
    bound = 6 * 51_000 + 15 * (trim.KEEP_CHARS + len(trim.NOTE) + 10)
    assert sum(after.sent) < bound, (sum(after.sent), bound)
    assert sum(after.sent) < 0.5 * sum(before.sent), (sum(before.sent), sum(after.sent))


def test_the_newest_results_reach_the_model_whole():
    after = _six_loop_turn(trim.chain(reports.shrink_read, trim.shrink_seen))
    for call, newest in enumerate(after.newest[1:]):
        assert newest == [_comments(call)], f"call {call + 1} got its newest result cut"


def test_a_cut_result_says_how_long_it_was_and_is_cut_once():
    whole = _comments(0)
    messages = [{"role": "user", "content": "search"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                              "content": whole}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b",
                                              "content": whole}]}]
    trim.shrink_seen(messages)
    cut = messages[1]["content"][0]["content"]
    assert cut.startswith(whole[:trim.KEEP_CHARS])
    assert f"Cut from {len(whole):,} characters" in cut and "trace" in cut
    trim.shrink_seen(messages)
    assert messages[1]["content"][0]["content"] == cut, "a cut result is not cut again"
    assert messages[2]["content"][0]["content"] == whole


def test_a_short_result_is_left_whole():
    short = json.dumps({"events": ["Standup 9am"]})
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                              "content": short}]},
                {"role": "user", "content": "next"}]
    trim.shrink_seen(messages)
    assert messages[0]["content"][0]["content"] == short


def test_an_earlier_report_still_becomes_its_digest():
    """shrink_read runs first, so a whole report read with memory_get is cut
    to its digest, not to its first 2,000 characters."""
    body = (f"{reports.MARKER}\n# Agent memory vendors\n\n## Summary\n- Three vendors.\n\n"
            "## Findings\n" + "| a | b |\n" * 1500)
    whole = json.dumps({"memory": {"id": "rep-1", "kind": "semantic", "body": body}})
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                              "content": whole}]},
                {"role": "user", "content": "next"}]
    trim.chain(reports.shrink_read, trim.shrink_seen)(messages)
    cut = json.loads(messages[0]["content"][0]["content"])["memory"]
    assert cut["id"] == "rep-1" and cut["body"].endswith(reports.TRIMMED)


# --- 2. a saved brief, summary or snapshot is one report ------------------------------


LOADER = SkillLoader([ROOT / "skills"])


def test_the_rehearsal_message_loads_the_research_skill():
    assert brain.is_research(LOADER.match(REHEARSAL))


def test_the_skill_says_a_saved_brief_is_one_report():
    skill = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "save a brief, summary, snapshot or report" in skill
    assert "never a set of separate memories" in skill


class FakeBridge:
    """Waku Memory over MCP: remember hands out ids in order; search finds nothing."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.calls: list[tuple[str, dict]] = []

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        self.calls.append((tool, args))
        if tool == "memory.remember":
            return json.dumps({"memory": {"id": f"mem-{len(self.calls)}"}, "deduped": False})
        if tool == "memory.search":
            return json.dumps({"entries": [], "scope_effective": "all"})
        return "{}"

    def close(self):
        pass


class SystemClient(ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.systems: list[str] = []

    def _create(self, **kwargs):
        self.systems.append(str(kwargs.get("system", "")))
        return super()._create(**kwargs)


BRIEF = """People who build agents ask for memory they can inspect and for traces they can replay.

<!-- waku-report v1 -->
# Audience brief: agent observability and Mem0 alternatives, 2026-10

## Summary
- Builders on Reddit and X ask to see what an agent remembered and why.
- Comments under the top YouTube video ask for self-hosted memory.

## Sources
```waku-sources
[{"title": "Reddit search, agent observability", "via": "treg:tikhub.x.reddit-app-fetch-dynamic-search", "cost_usd": 0.001}]
```
"""


def test_an_audience_brief_turn_saves_one_report(tmp_path, monkeypatch):
    import waku.app

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    bridge = FakeBridge(path)
    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    client = SystemClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "research"}')]),
        response([text_block(BRIEF)]),
        response([text_block('{"company_research": true}')])])
    app = make_waku(tmp_path / "home", client=client, consolidate_every=50)
    events = []
    result = app.respond(REHEARSAL, observer=lambda kind, ev: events.append((kind, ev)))

    assert any("waku-report v1" in s for s in client.systems), "the skill reached the prompt"
    saved = [args for tool, args in bridge.calls if tool == "memory.remember"]
    assert len(saved) == 1 and saved[0]["body"].startswith(reports.MARKER)
    assert saved[0]["kind"] == "semantic" and saved[0]["scope"] == "project:Company brain"
    assert [ev["memory_id"] for kind, ev in events if kind == "report"] == ["mem-3"]
    assert result.reply.endswith("Report saved: Audience brief: agent observability "
                                 "and Mem0 alternatives, 2026-10.")


# --- 3. the reply does not narrate the save ---------------------------------------------


def test_the_skill_says_never_to_write_about_the_save():
    skill = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "Never write about the save in the reply." in skill
    assert "that you have not seen a confirmation" in skill


def test_the_refusal_says_not_to_mention_the_save():
    assert "do not mention saving it in your reply" in reports.REFUSAL
