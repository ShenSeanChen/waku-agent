"""A stand-in for Waku Memory's two tools the agent calls on its own.

Used by test_mcp_session_reinit.py. It answers `memory.remember` and
`memory.search` in Waku Memory's JSON shape and keeps nothing between runs.
Over Streamable HTTP it keeps each session in memory, as api.waku.one does,
so restarting it makes it forget every session, which is what a redeploy of
api.waku.one did on 2026-10-04.

    python evals/fixtures/mcp_memory_server.py --port 8932
"""

from __future__ import annotations

import argparse
import json
import uuid

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("waku_memory")


@mcp.tool(name="memory.remember")
def remember(body: str, kind: str = "fact", scope: str = "global") -> str:
    """Store one memory and answer with its id."""
    return json.dumps({"memory": {"id": f"mem-{uuid.uuid4().hex[:8]}", "body": body,
                                  "kind": kind, "scope": scope}, "deduped": False})


@mcp.tool(name="memory.search")
def search(query: str, kind: str | None = None, scope: str | None = None,
           limit: int = 5) -> str:
    """Answer with no matches, in memory.search's shape."""
    return json.dumps({"entries": []})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="a stand-in for Waku Memory's MCP tools")
    parser.add_argument("--port", type=int, default=8932)
    args = parser.parse_args()
    mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
