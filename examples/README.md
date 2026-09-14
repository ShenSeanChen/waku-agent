# examples — short lessons about Waku itself

Each file here teaches one thing and runs in one command. Material about other
projects lives in [../lab/](../lab/README.md).

| File | What it teaches | Run it |
|---|---|---|
| [tiny_memory_agent.py](tiny_memory_agent.py) | the loop's three steps with memory, and nothing else in frame | `python examples/tiny_memory_agent.py` |
| [mcp.demo.json](mcp.demo.json) | connecting Waku to an MCP server, with no remote host | `cp examples/mcp.demo.json .waku/mcp.json && make dashboard` |

The MCP demo server itself is `evals/fixtures/mcp_demo_server.py`, because the
test suite runs the same server.

The rules for this folder are in
[conventions §6](../docs/context/conventions.md#6-examples-and-video-material).
