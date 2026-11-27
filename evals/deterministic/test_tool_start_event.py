"""DETERMINISTIC EVAL -- the loop says a tool has started before it runs (spec 039 E1).

A person who asks the phone a question waited about 15 seconds with nothing
on the screen, because the loop emitted its `tool` event only after a tool
finished (F3). Now each tool call emits `tool_start` {tool, call_id} before
it runs, and the existing `tool` event follows with the same call id. A
client draws a status line from the first one.
"""

from __future__ import annotations

from evals.helpers import ScriptedClient, response, text_block, tool_block
from waku.loop.agent import run_loop
from waku.tools.registry import Tool, ToolRegistry


def _registry(seen: list) -> ToolRegistry:
    registry = ToolRegistry()

    def search(**_):
        seen.append(("ran", "search"))
        return "found Emily"

    registry.register(Tool(name="search", description="search",
                           input_schema={"type": "object", "properties": {}}, fn=search))
    return registry


def test_tool_start_comes_before_the_tool_runs_and_tool_after_it():
    events: list = []
    script = [response([tool_block("search", {"q": "Emily"}, "tu_7")], "tool_use"),
              response([text_block("Emily is your designer.")])]
    run_loop(ScriptedClient(script), "m", "system",
             [{"role": "user", "content": "Who is Emily?"}], _registry(events),
             observer=lambda kind, ev: events.append((kind, ev)))
    order = [kind for kind, _ in events if kind in ("tool_start", "tool", "ran")]
    assert order == ["tool_start", "ran", "tool"]
    start = next(ev for kind, ev in events if kind == "tool_start")
    done = next(ev for kind, ev in events if kind == "tool")
    assert start == {"tool": "search", "call_id": "tu_7"}
    assert done["call_id"] == start["call_id"]


def test_each_of_two_calls_gets_its_own_tool_start():
    events: list = []
    script = [response([tool_block("search", {}, "tu_1"), tool_block("search", {}, "tu_2")],
                       "tool_use"),
              response([text_block("done")])]
    run_loop(ScriptedClient(script), "m", "system",
             [{"role": "user", "content": "x"}], _registry([]),
             observer=lambda kind, ev: events.append((kind, ev)))
    kinds = [(kind, ev.get("call_id")) for kind, ev in events if kind in ("tool_start", "tool")]
    assert kinds == [("tool_start", "tu_1"), ("tool", "tu_1"),
                     ("tool_start", "tu_2"), ("tool", "tu_2")]


def test_the_trace_keeps_the_finished_tool_and_not_the_start(tmp_path):
    """`tool_start` is for the live screen. The trace already has the `tool`
    event, with the same call id and its duration."""
    from evals.helpers import make_waku

    script = [response([text_block('{"retrieve": false, "query": "", "reason": "t"}')]),
              response([tool_block("save_note", {"subject": "a", "content": "b"}, "tu_3")],
                       "tool_use"),
              response([text_block("saved")])]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    seen: list = []
    app.respond("remember a is b", observer=lambda kind, ev: seen.append(kind))
    assert "tool_start" in seen
    written = "".join(p.read_text(encoding="utf-8")
                      for p in (tmp_path / "home" / "traces").glob("*.jsonl"))
    assert '"type": "tool"' in written
    assert '"type": "tool_start"' not in written
