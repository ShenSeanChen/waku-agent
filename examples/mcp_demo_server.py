"""A tiny, self-contained MCP server — the demo connector for milli-agent.

Most MCP examples need Node/npx. This one is pure Python (only the `mcp` extra),
so the connector story runs with zero extra installs:

    pip install -e '.[mcp]'
    cp examples/mcp.demo.json .milli/mcp.json
    make dashboard          # its tools appear under Tools > Available > MCP servers

Its tools register as `demo_word_count` and `demo_reverse_text`. Swap in your own
@mcp.tool() functions, or point mcp.json at any real MCP server the same way —
that's the whole point: connectors plug in without changing Milli's code.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def word_count(text: str) -> str:
    """Count the words and characters in a piece of text."""
    return f"{len(text.split())} words, {len(text)} characters"


@mcp.tool()
def reverse_text(text: str) -> str:
    """Reverse a string (handy for proving the connector round-trips)."""
    return text[::-1]


if __name__ == "__main__":
    mcp.run()  # stdio transport — how Milli's MCPBridge talks to it
