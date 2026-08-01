"""THE GRAPH — nodes, edges, waves. This file is the whole mechanism.

A graph workflow is a map of steps: each node does one job, each edge says
"when this finishes, go there". The engine runs it in waves:

    while some nodes are ready:
        run every ready node (at the same time, if there are several)
        merge what they wrote into the shared state
        fire their edges / ask their routers where to go next

That's it. Three ideas carry everything:

  state    one plain dict (the blackboard). Every node reads it, returns the
           keys it wants to merge. Parallel nodes must write DISJOINT keys —
           a collision raises instead of silently losing a write.
  routers  plain Python functions over state. Models write state; code reads
           it and picks the edge. No LLM ever decides control flow directly.
  guards   the loop's two-guardrail pattern, generalized: per-node max_visits
           (bounded cycles) + global max_steps (never spin forever). A node
           exception is recorded and surfaced, never raised out of the run —
           same surface-don't-crash rule as ToolRegistry.execute.

Waves trade a little pipelining for a lot of legibility: execution order is
deterministic, so traces read the same way twice and evals can pin the path.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

# Same observer protocol as the loop: notify(kind, event). Graph runs emit
# graph_start / node_start / node_end / route / graph_end, and pass through
# whatever a node emits (an agent_node's llm/tool events) tagged with node=.
Observer = Callable[[str, dict], None]

START = "START"
END = "END"

NodeFn = Callable[[dict], dict]  # reads state, returns keys to merge
RouteFn = Callable[[dict], str]  # reads state, returns a target label


class GraphStateCollision(Exception):
    """Two nodes in the same wave wrote the same key — a graph bug, not a race."""


@dataclass
class Node:
    name: str
    fn: NodeFn
    kind: str = "fn"          # tool / llm / agent / router label for describe()
    max_visits: int = 1       # >1 only on nodes inside an intended cycle
    on_error: str | None = None  # node to jump to if fn raises (default: drain to END)


@dataclass
class Graph:
    name: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    routers: dict[str, tuple[RouteFn, dict[str, str]]] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        if node.name in (START, END):
            raise ValueError(f"'{node.name}' is reserved")
        self.nodes[node.name] = node

    def add_edge(self, src: str, dst: str) -> None:
        """Unconditional: when src finishes, dst gets one step closer to ready.
        Add nodes before edges — unknown endpoints fail here, not mid-run."""
        for end in (src, dst):
            if end not in self.nodes and end not in (START, END):
                raise ValueError(f"unknown node '{end}'")
        self.edges.append((src, dst))

    def add_router(self, src: str, route: RouteFn, targets: dict[str, str]) -> None:
        """Conditional: when src finishes, `route(state)` returns a label and
        execution jumps to targets[label]. Routers are code, never models."""
        if src not in self.nodes:
            raise ValueError(f"unknown node '{src}'")
        for label, dst in targets.items():
            if dst not in self.nodes and dst != END:
                raise ValueError(f"router target '{dst}' (label '{label}') is unknown")
        self.routers[src] = (route, targets)

    def describe(self) -> dict[str, Any]:
        """The topology as data — what the dashboard draws. Rendering from this
        (never from a hand-copied picture) is what keeps the chart honest."""
        edges = [{"src": s, "dst": d, "conditional": False} for s, d in self.edges]
        for src, (_route, targets) in self.routers.items():
            edges += [{"src": src, "dst": dst, "conditional": True}
                      for dst in dict.fromkeys(targets.values())]
        return {"name": self.name,
                "nodes": [{"name": n.name, "kind": n.kind} for n in self.nodes.values()],
                "edges": edges}


def run_graph(graph: Graph, state: dict, observer: Observer | None = None,
              max_steps: int = 25) -> dict:
    """Run one graph workflow to completion. Returns the final state; errors
    land in state["errors"] and the graph_end event, they are never raised."""
    lock = threading.Lock()
    raw = observer or (lambda kind, ev: None)

    def notify(kind: str, ev: dict) -> None:
        with lock:  # pool threads share the tracer's file append — keep lines whole
            raw(kind, ev)

    deps: dict[str, set] = defaultdict(set)      # static in-edges per node
    for src, dst in graph.edges:
        if dst != END:
            deps[dst].add(src)
    fired: dict[str, set] = defaultdict(set)     # which in-edges have fired
    runs: dict[str, int] = defaultdict(int)
    path: list[str] = []
    errors: dict[str, str] = state.setdefault("errors", {})
    t0 = time.perf_counter()
    notify("graph_start", {"workflow": graph.name, "nodes": list(graph.nodes)})

    def next_wave(jumps: list[str]) -> list[str]:
        """Forcible router/error jumps + nodes whose static deps have all fired."""
        wave: list[str] = []
        for name in jumps + [n for n in graph.nodes
                             if deps[n] and deps[n] <= fired[n] and runs[n] == 0]:
            if name == END or name in wave:
                continue
            if runs[name] >= graph.nodes[name].max_visits:
                errors.setdefault(name, f"max_visits={graph.nodes[name].max_visits} reached")
                continue
            wave.append(name)
        return wave

    def run_one(name: str) -> tuple[str, dict | None, str | None, int]:
        node = graph.nodes[name]
        snapshot = dict(state)
        snapshot["_notify"] = lambda kind, ev, _n=name: notify(kind, {**ev, "node": _n})
        t = time.perf_counter()
        try:
            out = node.fn(snapshot)
            return name, out or {}, None, int((time.perf_counter() - t) * 1000)
        except Exception as exc:  # surface, don't crash — the run drains cleanly
            return name, None, repr(exc), int((time.perf_counter() - t) * 1000)

    for src, dst in graph.edges:  # START fires its edges before the first wave
        if src == START:
            fired[dst].add(START)
    wave = next_wave([])

    while wave:
        if len(path) + len(wave) > max_steps:
            errors.setdefault("engine", f"max_steps={max_steps} reached")
            break
        for name in wave:
            runs[name] += 1
            notify("node_start", {"workflow": graph.name, "node": name, "visit": runs[name]})
        if len(wave) == 1:  # solo node runs on this thread: trace order stays exact
            results = [run_one(wave[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                results = [f.result() for f in [pool.submit(run_one, n) for n in wave]]

        jumps: list[str] = []
        wave_writes: dict[str, str] = {}
        for name, out, error, ms in results:  # merge in wave order — deterministic
            path.append(name)
            keys = [k for k in (out or {}) if not k.startswith("_")]
            for key in keys:
                if wave_writes.get(key, name) != name:
                    raise GraphStateCollision(
                        f"'{name}' and '{wave_writes[key]}' both wrote '{key}' — "
                        f"parallel branches must write disjoint keys")
                wave_writes[key] = name
                state[key] = out[key]
            notify("node_end", {"workflow": graph.name, "node": name,
                                "ms": ms, "keys": keys, "error": error})
            if error:
                errors[name] = error
                if graph.nodes[name].on_error:
                    jumps.append(graph.nodes[name].on_error)
                continue  # no on_error → fire nothing; the run drains to END
            if name in graph.routers:
                route, targets = graph.routers[name]
                label = route(state)
                target = targets.get(label)
                notify("route", {"workflow": graph.name, "router": name,
                                 "target": target or END, "reason": label})
                if target is None:
                    errors[name] = f"router returned unknown label '{label}'"
                elif target != END:
                    jumps.append(target)  # a route is a jump, not a dependency
            else:
                for src, dst in graph.edges:
                    if src == name and dst != END:
                        fired[dst].add(name)
        wave = next_wave(jumps)

    first_error = next(iter(errors.values()), None)
    notify("graph_end", {"workflow": graph.name, "ms": int((time.perf_counter() - t0) * 1000),
                         "steps": len(path), "path": path, "error": first_error})
    return state
