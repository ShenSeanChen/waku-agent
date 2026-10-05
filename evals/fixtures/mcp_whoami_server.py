"""An MCP server with one tool that answers with the caller's clientInfo.

Used by test_mcp_client_info.py: a server can only label a client by the name
the client sent in `initialize`, so the test asks a real server what it saw.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer

mcp = MCPServer("whoami")


@mcp.tool()
def whoami(ctx: Context) -> str:
    """The name and version the connected client sent in initialize."""
    info = ctx.session.client_params.client_info
    return f"{info.name} {info.version}"


if __name__ == "__main__":
    mcp.run()
