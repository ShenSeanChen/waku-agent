"""DETERMINISTIC EVAL -- the hosted agent is not offered Waku Memory's ui.* tools (spec 039 A2).

Waku Memory's MCP server can list five `ui.*` tools, about 5,100 tokens of
schemas (F10). They serve clients that draw Waku's own screens; the hosted
agent draws none, and every model call of a turn paid for them. A hosted
container (WAKU_HOSTED=1) leaves them out. A person's own machine keeps
whatever its servers list.
"""

from __future__ import annotations

import json

import pytest

from evals.helpers import ScriptedClient, make_waku
from waku.tools import build_registry, mcp_client

LISTED = {"waku_memory": [
    {"name": "memory.search", "description": "search", "inputSchema": None, "readOnly": True},
    {"name": "memory.remember", "description": "keep", "inputSchema": None, "readOnly": False},
    {"name": "ui.memories", "description": "draw", "inputSchema": None, "readOnly": True},
    {"name": "ui.state", "description": "draw", "inputSchema": None, "readOnly": True},
]}


@pytest.fixture
def tool_names(tmp_path, monkeypatch):
    home = tmp_path / "home"
    app = make_waku(home, client=ScriptedClient([]))
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": "https://memory.example/mcp"}]}), encoding="utf-8")

    async def connect_all(self, servers):
        return LISTED

    monkeypatch.setattr(mcp_client.MCPBridge, "_connect_all", connect_all)
    # _deadline imports the mcp extra, which CI does not install.
    monkeypatch.setattr(mcp_client.MCPBridge, "_deadline", lambda self, servers: 5.0)
    monkeypatch.delenv("WAKU_PLATFORM_BASE_URL", raising=False)
    monkeypatch.delenv("WAKU_PLATFORM_TOKEN", raising=False)

    def names(hosted: bool) -> set[str]:
        if hosted:
            monkeypatch.setenv("WAKU_HOSTED", "1")
        else:
            monkeypatch.delenv("WAKU_HOSTED", raising=False)
        registry = build_registry(app.conn, app.settings)
        try:
            return {schema["name"] for schema in registry.schemas()}
        finally:
            registry.mcp_bridge.close()

    return names


def test_a_hosted_agent_is_not_offered_the_ui_tools(tool_names):
    names = tool_names(hosted=True)
    assert {"waku_memory_memory_search", "waku_memory_memory_remember"} <= names
    assert not {n for n in names if n.startswith("waku_memory_ui_")}


def test_a_laptop_keeps_every_tool_its_servers_list(tool_names):
    names = tool_names(hosted=False)
    assert {"waku_memory_ui_memories", "waku_memory_ui_state"} <= names
