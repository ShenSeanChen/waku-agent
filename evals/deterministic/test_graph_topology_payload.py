"""The dashboard's graph chart can never lie about the engine's topology.

archSVG had to be byte-frozen because a hand-drawn picture and its animation
ids drift apart silently. The graph chart avoids that fate structurally: it is
rendered from Graph.describe(), and this eval pins that the payload the
dashboard serves IS the graph app.py actually runs.
"""

from __future__ import annotations

from waku.graph.workflows.triage import build_triage_graph, triage_topology


def real_graph():
    return build_triage_graph(classify_fn=lambda m: ("full", ""),
                              calendar_fn=lambda: "", quick_fn=lambda s: "",
                              full_fn=lambda s: None)


def test_topology_payload_matches_the_real_graph():
    payload = triage_topology()
    graph = real_graph()
    assert {n["name"] for n in payload["nodes"]} == set(graph.nodes)
    assert payload["name"] == graph.name == "triage"
    described = {(e["src"], e["dst"]) for e in payload["edges"]}
    rebuilt = {(e["src"], e["dst"]) for e in graph.describe()["edges"]}
    assert described == rebuilt


def test_topology_has_the_shape_the_readme_promises():
    """classify and check_calendar fan out from START in parallel; the router
    (a conditional edge) picks quick_reply or full_agent; both reach END."""
    payload = triage_topology()
    static = {(e["src"], e["dst"]) for e in payload["edges"] if not e["conditional"]}
    conditional = {(e["src"], e["dst"]) for e in payload["edges"] if e["conditional"]}
    assert ("START", "classify") in static and ("START", "check_calendar") in static
    assert conditional == {("gather", "quick_reply"), ("gather", "full_agent")}
    assert ("quick_reply", "END") in static and ("full_agent", "END") in static


def test_node_kinds_are_labelled_for_the_chart():
    kinds = {n["name"]: n["kind"] for n in triage_topology()["nodes"]}
    assert kinds["classify"] == "llm"
    assert kinds["check_calendar"] == "tool"
    assert kinds["full_agent"] == "agent"      # THE loop, as a node — the story
