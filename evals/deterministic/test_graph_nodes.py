"""Node factories — scripted LLM, real run_loop, zero network.

agent_node is the one that matters: a whole loop turn as a single graph node,
with its inner llm/tool events surfacing at the outer observer tagged node=.
"""

from __future__ import annotations

from evals.helpers import ScriptedClient, response, text_block, tool_block
from waku.graph import END, START, Graph, run_graph
from waku.graph.nodes import agent_node, llm_node, tool_node
from waku.tools.registry import Tool, ToolRegistry


def one_node_graph(node) -> Graph:
    g = Graph("solo")
    g.add_node(node)
    g.add_edge(START, node.name)
    g.add_edge(node.name, END)
    return g


def test_tool_node_maps_in_keys_to_out_key():
    node = tool_node("add", lambda a, b: a + b, in_keys=["x", "y"], out_key="total")
    state = run_graph(one_node_graph(node), {"x": 2, "y": 3})
    assert state["total"] == 5


def test_llm_node_formats_prompt_from_state_and_writes_reply():
    client = ScriptedClient([response([text_block("quick")])])
    node = llm_node("classify", "Message: {message}", out_key="route",
                    client=client, model="small-model")
    state = run_graph(one_node_graph(node), {"message": "thanks!"})
    assert state["route"] == "quick"


def test_agent_node_runs_a_real_loop_turn_with_tagged_events():
    registry = ToolRegistry()
    calls: list[dict] = []
    registry.register(Tool(
        name="save_note", description="save a note",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        fn=lambda text: (calls.append({"text": text}), "saved")[1],
    ))
    client = ScriptedClient([
        response([tool_block("save_note", {"text": "milk"})], "tool_use"),
        response([text_block("Noted!")]),
    ])
    node = agent_node("full_agent", out_key="result", client=client,
                      model="main-model", system="You are a test agent.",
                      tools=registry)

    events: list[tuple[str, dict]] = []
    state = run_graph(one_node_graph(node), {"message": "note milk"},
                      observer=lambda kind, ev: events.append((kind, ev)))

    assert calls == [{"text": "milk"}]                    # the tool really ran
    assert state["result"].reply == "Noted!"              # a real LoopResult
    assert state["result"].iterations == 2
    inner = [(k, ev) for k, ev in events if k in ("llm", "tool")]
    assert inner, "inner loop events must pass through to the outer observer"
    assert all(ev["node"] == "full_agent" for _, ev in inner)
