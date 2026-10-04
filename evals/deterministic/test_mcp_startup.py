"""MCP deadline discovery must finish SDK imports before connection starts."""

import asyncio
import json
import threading

from waku.tools.mcp_client import MCPBridge


def test_deadline_initialization_finishes_before_connecting(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": [{"name": "knowledge", "url": "http://localhost/mcp"}]}))
    bridge = MCPBridge(config)
    imports_ready = threading.Event()
    connection_attempted = threading.Event()
    scheduled = False
    original_schedule = asyncio.run_coroutine_threadsafe

    def schedule(coro, loop):
        nonlocal scheduled
        scheduled = True
        return original_schedule(coro, loop)

    def deadline(servers):
        # Force the old implementation's background import to happen before
        # deadline discovery finishes, without relying on thread timing.
        if scheduled:
            assert connection_attempted.wait(3)
        imports_ready.set()
        return 3.0

    async def connect(servers):
        ready = imports_ready.is_set()
        connection_attempted.set()
        if not ready:
            raise ImportError("SDK was imported before initialization finished")
        return {"knowledge": [{"name": "search", "description": "Search knowledge"}]}

    monkeypatch.setattr(bridge, "_deadline", deadline)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    monkeypatch.setattr(bridge, "_connect_all", connect)
    try:
        assert [tool.name for tool in bridge.start()] == ["knowledge_search"]
    finally:
        bridge.close()
