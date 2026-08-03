"""Graph engine mechanics — pure-function nodes, zero LLM, zero network.

These pin the contracts everything downstream relies on: execution order
(graph_end.path), real parallelism inside a wave, the disjoint-key rule, both
cycle guards, error draining, and the observer event schemas the dashboard
renders from.
"""

from __future__ import annotations

import time

import pytest

from waku.graph import END, START, Graph, GraphStateCollision, Node, run_graph
from waku.graph.nodes import key_router


def collect_events():
    events: list[tuple[str, dict]] = []
    return events, lambda kind, ev: events.append((kind, ev))


def linear_graph() -> Graph:
    g = Graph("linear")
    g.add_node(Node("a", lambda s: {"a": 1}))
    g.add_node(Node("b", lambda s: {"b": s["a"] + 1}))
    g.add_node(Node("c", lambda s: {"c": s["b"] + 1}))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g


def test_linear_topology_runs_in_edge_order():
    events, observer = collect_events()
    state = run_graph(linear_graph(), {}, observer=observer)
    assert (state["a"], state["b"], state["c"]) == (1, 2, 3)
    end = dict(events)["graph_end"]
    assert end["path"] == ["a", "b", "c"]
    assert end["error"] is None


def test_fanout_runs_in_parallel_and_fanin_waits_for_all():
    g = Graph("fan")

    def slow(key):
        def fn(state):
            time.sleep(0.15)
            return {key: key}
        return fn

    for key in ("x", "y", "z"):
        g.add_node(Node(key, slow(key)))
        g.add_edge(START, key)
    g.add_node(Node("join", lambda s: {"joined": s["x"] + s["y"] + s["z"]}))
    for key in ("x", "y", "z"):
        g.add_edge(key, "join")
    g.add_edge("join", END)

    t0 = time.perf_counter()
    state = run_graph(g, {})
    elapsed = time.perf_counter() - t0
    assert state["joined"] == "xyz"          # fan-in saw all three keys
    assert elapsed < 0.35                    # 3 x 0.15s ran together, not in series


def routed_graph() -> Graph:
    g = Graph("routed")
    g.add_node(Node("decide", lambda s: {"route": s["want"]}))
    g.add_node(Node("quick", lambda s: {"took": "quick"}))
    g.add_node(Node("full", lambda s: {"took": "full"}))
    g.add_edge(START, "decide")
    g.add_router("decide", key_router("route", default="full"),
                 {"quick": "quick", "full": "full"})
    g.add_edge("quick", END)
    g.add_edge("full", END)
    return g


def test_router_picks_the_target_from_state():
    events, observer = collect_events()
    state = run_graph(routed_graph(), {"want": "quick"}, observer=observer)
    assert state["took"] == "quick"
    route = dict(events)["route"]
    assert route["target"] == "quick" and route["reason"] == "quick"
    assert run_graph(routed_graph(), {"want": "full"})["took"] == "full"


def test_router_unknown_label_records_error_and_drains():
    events, observer = collect_events()
    state = run_graph(routed_graph(), {"want": "sideways"}, observer=observer)
    assert "took" not in state
    assert "unknown label" in state["errors"]["decide"]
    assert dict(events)["graph_end"]["error"] is not None


def test_unknown_edge_and_router_targets_raise_at_build_time():
    g = Graph("bad")
    g.add_node(Node("a", lambda s: {}))
    with pytest.raises(ValueError):
        g.add_edge("a", "ghost")
    with pytest.raises(ValueError):
        g.add_router("a", key_router("k", default="x"), {"x": "ghost"})


def test_parallel_key_collision_raises():
    g = Graph("collide")
    g.add_node(Node("left", lambda s: {"same": 1}))
    g.add_node(Node("right", lambda s: {"same": 2}))
    g.add_edge(START, "left")
    g.add_edge(START, "right")
    with pytest.raises(GraphStateCollision):
        run_graph(g, {})


def test_sequential_overwrite_is_allowed():
    state = run_graph(linear_graph(), {"a": 99})  # node 'a' rewrites key 'a'
    assert state["a"] == 1


def cycle_graph(max_visits: int) -> Graph:
    g = Graph("cycle")
    g.add_node(Node("work", lambda s: {"count": s.get("count", 0) + 1},
                    max_visits=max_visits))
    g.add_edge(START, "work")
    g.add_router("work", lambda s: "again" if s["count"] < 99 else "done",
                 {"again": "work", "done": END})
    return g


def test_cycle_stops_at_max_visits():
    state = run_graph(cycle_graph(max_visits=3), {})
    assert state["count"] == 3
    assert "max_visits=3" in state["errors"]["work"]


def test_max_steps_is_the_global_hard_stop():
    events, observer = collect_events()
    state = run_graph(cycle_graph(max_visits=99), {}, observer=observer, max_steps=5)
    assert state["count"] == 5
    assert "max_steps=5" in state["errors"]["engine"]
    assert dict(events)["graph_end"]["steps"] == 5


def test_node_exception_routes_to_on_error_and_never_raises():
    g = Graph("boom")
    g.add_node(Node("explode", lambda s: 1 / 0, on_error="fallback"))
    g.add_node(Node("fallback", lambda s: {"saved": True}))
    g.add_edge(START, "explode")
    g.add_edge("fallback", END)
    state = run_graph(g, {})
    assert state["saved"] is True
    assert "ZeroDivisionError" in state["errors"]["explode"]


def test_node_exception_without_on_error_drains_to_end():
    g = Graph("boom2")
    g.add_node(Node("explode", lambda s: 1 / 0))
    g.add_node(Node("after", lambda s: {"ran": True}))
    g.add_edge(START, "explode")
    g.add_edge("explode", "after")
    events, observer = collect_events()
    state = run_graph(g, {}, observer=observer)
    assert "ran" not in state                     # downstream starved, run ended
    assert dict(events)["graph_end"]["error"] is not None


def test_observer_event_sequence_and_payload_schemas():
    """Pins the exact kinds + keys the dashboard's graph view consumes."""
    events, observer = collect_events()
    run_graph(routed_graph(), {"want": "quick"}, observer=observer)
    kinds = [k for k, _ in events]
    assert kinds[0] == "graph_start" and kinds[-1] == "graph_end"
    assert kinds == ["graph_start", "node_start", "node_end", "route",
                     "node_start", "node_end", "graph_end"]
    schemas = {"graph_start": {"workflow", "nodes"},
               "node_start": {"workflow", "node", "visit"},
               "node_end": {"workflow", "node", "ms", "keys", "error"},
               "route": {"workflow", "router", "target", "reason"},
               "graph_end": {"workflow", "ms", "steps", "path", "error"}}
    for kind, ev in events:
        assert schemas[kind] <= set(ev), f"{kind} missing {schemas[kind] - set(ev)}"


def test_describe_reports_every_node_and_edge():
    d = routed_graph().describe()
    assert d["name"] == "routed"
    assert {n["name"] for n in d["nodes"]} == {"decide", "quick", "full"}
    conditional = {(e["src"], e["dst"]) for e in d["edges"] if e["conditional"]}
    static = {(e["src"], e["dst"]) for e in d["edges"] if not e["conditional"]}
    assert conditional == {("decide", "quick"), ("decide", "full")}
    assert static == {(START, "decide"), ("quick", END), ("full", END)}


def test_reserved_underscore_keys_never_merge_or_leak():
    g = Graph("private")
    g.add_node(Node("sneaky", lambda s: {"_secret": 1, "open": 2}))
    g.add_edge(START, "sneaky")
    events, observer = collect_events()
    state = run_graph(g, {}, observer=observer)
    assert "_secret" not in state and state["open"] == 2
    assert dict(events)["node_end"]["keys"] == ["open"]
    assert "_notify" not in state                 # engine plumbing stays internal
