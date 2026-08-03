"""Graph workflows — opt-in structure AROUND the loop, not a fifth pillar.

`waku/loop/agent.py` is one agent turn: the model picks tools until it stops.
A graph workflow arranges *steps* around (and to) that loop: nodes that each do
one job, edges that say what happens next, fan-outs that run at the same time,
and routers that pick a path. The loop file never changes — a node can simply
BE a whole loop turn (see `nodes.agent_node`).

The entire mechanism is `engine.py`, same trick as the loop: read one file,
understand the whole thing. Shipped graphs live in `workflows/` — in docs they
are always "graph workflows" (two words), to keep them distinct from the
skills-as-workflows language in procedural memory.
"""

from waku.graph.engine import END, START, Graph, GraphStateCollision, Node, run_graph

__all__ = ["END", "START", "Graph", "GraphStateCollision", "Node", "run_graph"]
