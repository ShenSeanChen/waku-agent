"""Node factories — the common shapes a graph node takes.

Everything is just a NodeFn (state in, keys-to-merge out); these helpers build
the four kinds you actually reach for. The important one is `agent_node`: a
node that IS a whole loop turn. Graphs don't replace the loop — they arrange
calls around it, and to it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from waku.graph.engine import Node, NodeFn, RouteFn
from waku.loop.agent import run_loop
from waku.tools.registry import ToolRegistry


def tool_node(name: str, fn: Callable[..., object], in_keys: Iterable[str],
              out_key: str, **node_kwargs) -> Node:
    """A plain function as a node: reads `in_keys` from state, writes `out_key`.
    For DB lookups, file reads, pure computation — anything deterministic."""
    keys = list(in_keys)

    def run(state: dict) -> dict:
        return {out_key: fn(*[state.get(k) for k in keys])}

    return Node(name=name, fn=run, kind="tool", **node_kwargs)


def llm_node(name: str, prompt_template: str, out_key: str, *,
             client, model: str, max_tokens: int = 600, **node_kwargs) -> Node:
    """ONE model call, no tools: the prompt template is formatted from state,
    the reply text lands in `out_key`. For classify / score / rewrite steps.
    Control flow never lives here — a router reads what this node wrote."""

    def run(state: dict) -> dict:
        prompt = prompt_template.format(**{k: v for k, v in state.items()
                                           if not k.startswith("_")})
        response = client.messages.create(model=model, max_tokens=max_tokens,
                                          messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in response.content if b.type == "text")
        return {out_key: text}

    return Node(name=name, fn=run, kind="llm", **node_kwargs)


def agent_node(name: str, out_key: str, *, client, model: str, system: str,
               tools: ToolRegistry, max_iterations: int = 10, max_tokens: int = 2048,
               message_key: str = "message", **node_kwargs) -> Node:
    """A full run_loop turn as one node — the sub-agent story. Same loop, same
    tracing (its llm/tool events pass through tagged with node=), just a scoped
    tool registry and its own working memory. run_loop itself is untouched."""

    def run(state: dict) -> dict:
        messages = [{"role": "user", "content": state[message_key]}]
        result = run_loop(client=client, model=model, system=system,
                          messages=messages, tools=tools,
                          max_iterations=max_iterations, max_tokens=max_tokens,
                          observer=state.get("_notify"))
        return {out_key: result}

    return Node(name=name, fn=run, kind="agent", **node_kwargs)


def key_router(key: str, default: str) -> RouteFn:
    """The house router: read one state key, return it as the route label
    (falling back to `default`). Models write the key; this code reads it."""

    def route(state: dict) -> str:
        value = state.get(key)
        return value if isinstance(value, str) and value else default

    return route


def fn_node(name: str, fn: NodeFn, **node_kwargs) -> Node:
    """Escape hatch: any callable(state) -> dict as a node, unwrapped."""
    return Node(name=name, fn=fn, **node_kwargs)
