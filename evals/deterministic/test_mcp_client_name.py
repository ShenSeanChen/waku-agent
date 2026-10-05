"""DETERMINISTIC EVAL — the name Waku Agent gives every MCP server.

Waku Memory names a call's harness from the `clientInfo.name` a client sends
in `initialize`: any name containing "waku" is the `waku` harness. In the
Oct 5 rehearsal the agent's memory searches showed up under "Other searches"
on waku.one Matches, because the trace list only read a harness from a
session.hook the agent never sends; the backend now falls back to this name.
So the name, and a version beside it, must keep going out on every session.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest

from waku import __version__
from waku.tools import mcp_client


def test_the_client_name_says_waku_agent():
    # runs without the [mcp] extra, so CI checks it
    assert mcp_client.CLIENT_NAME == "waku-agent"


def test_every_session_introduces_itself_with_the_name_and_version(tmp_path, monkeypatch):
    mcp = pytest.importorskip("mcp", reason="the MCP connector is an optional extra")
    seen = {}

    class Introduced(Exception):
        pass

    class FakeSession:
        def __init__(self, read, write, client_info=None, **_):
            seen["info"] = client_info

        async def __aenter__(self):
            raise Introduced

        async def __aexit__(self, *exc):
            return False

    async def no_streams(self, spec):
        return None, None

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(mcp_client.MCPBridge, "_open_streams", no_streams)
    bridge = mcp_client.MCPBridge(tmp_path / "mcp.json")

    async def connect():
        bridge._stack = AsyncExitStack()
        with pytest.raises(Introduced):
            await bridge._connect_one({"name": "waku_memory", "url": "https://example.test/mcp"})

    asyncio.run(connect())
    assert (seen["info"].name, seen["info"].version) == ("waku-agent", __version__)
