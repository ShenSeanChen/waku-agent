"""A tiny, self-contained MCP server — the demo connector for waku-agent.

Most MCP examples need Node/npx. This one is pure Python (only the `mcp` extra),
so the connector story runs with zero extra installs:

    pip install -e '.[mcp]'
    cp examples/mcp.demo.json .waku/mcp.json
    make dashboard          # its tools appear under Tools > Available > MCP servers

Its tools register as `demo_word_count` and `demo_reverse_text`. Swap in your own
@mcp.tool() functions, or point mcp.json at any real MCP server the same way —
that's the whole point: connectors plug in without changing Waku's code.
"""

from __future__ import annotations

# `MCPServer` is what the SDK's 2.x line calls the class 1.x shipped as
# `mcp.server.fastmcp.FastMCP`. The old import path is gone, not deprecated,
# so this file raised ModuleNotFoundError under the installed SDK until
# 2026-08-26. The decorator API below is unchanged.
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("demo")


@mcp.tool()
def word_count(text: str) -> str:
    """Count the words and characters in a piece of text."""
    return f"{len(text.split())} words, {len(text)} characters"


@mcp.tool()
def reverse_text(text: str) -> str:
    """Reverse a string (handy for proving the connector round-trips)."""
    return text[::-1]


if __name__ == "__main__":
    # stdio by default — how Waku's MCPBridge talks to a local server.
    # `--http` runs the same two tools over Streamable HTTP instead, which is
    # how it talks to a remote one. Same tools, same code: only the transport
    # differs, which is the thing worth seeing.
    #
    #   python examples/mcp_demo_server.py --http --port 8931
    #   # then in .waku/mcp.json:
    #   {"servers": [{"name": "demo", "url": "http://127.0.0.1:8931/mcp"}]}
    import argparse

    parser = argparse.ArgumentParser(description="waku-agent's demo MCP server")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP")
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()
