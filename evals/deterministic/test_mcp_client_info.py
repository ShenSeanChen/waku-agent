"""DETERMINISTIC EVAL -- the name Waku gives itself on every MCP connection.

Waku Memory files each write under an Origin it reads from `clientInfo.name`
(spec 006 E). A client that sends no name of its own is filed under the SDK's
default and shows as `web`, so the agent's own writes would be indistinguishable
from a browser's. The server is asked what it saw, over a real stdio session.

Both tests run the bridge in a FRESH interpreter. pytest has already imported
`mcp` by the time a test runs, which hides the second bug pinned here: the
first import of `mcp` happening on two threads at once.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import waku
from waku.tools.mcp_client import CLIENT_NAME

pytest.importorskip("mcp", reason="the MCP connector is an optional extra")

REPO = Path(__file__).resolve().parents[2]
WHOAMI = REPO / "evals" / "fixtures" / "mcp_whoami_server.py"

# Started the way build_registry starts it: nothing has imported `mcp` yet.
SCRIPT = """
import json, sys
from pathlib import Path
from waku.tools.mcp_client import MCPBridge
bridge = MCPBridge(Path(sys.argv[1]))
names = [t.name for t in bridge.start()]
print(json.dumps({"tools": names, "whoami": bridge.call("whoami", "whoami", {})}))
bridge.close()
"""


def _run_bridge(tmp_path) -> dict:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": [
        {"name": "whoami", "command": sys.executable, "args": [str(WHOAMI)]}]}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(config)], cwd=REPO,
                         capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_session_introduces_itself_as_waku_agent(tmp_path):
    assert CLIENT_NAME == "waku-agent"
    seen = _run_bridge(tmp_path)["whoami"]
    assert seen.strip() == f"waku-agent {waku.__version__}", (
        f"the server saw {seen!r}; Waku Memory maps a name containing 'waku' to "
        "Origin waku, and anything else to web")


def test_a_fresh_process_connects_without_a_circular_import(tmp_path):
    """Live on main until spec 006: the loop thread and the caller both
    imported `mcp` for the first time at once, the import failed as circular,
    and build_registry reported it as "the 'mcp' package is missing" -- so no
    server connected in a freshly started Waku."""
    assert _run_bridge(tmp_path)["tools"] == ["whoami_whoami"]


def test_a_slow_tool_call_says_it_timed_out_and_may_still_be_charged(tmp_path):
    """2026-10-02: two treg calls outlived the 30s limit, treg charged both,
    and the agent saw only an empty "failed:"."""
    import asyncio

    from waku.tools.mcp_client import MCPBridge

    bridge = MCPBridge(tmp_path / "mcp.json", call_timeout=0.05)

    async def slow(server, tool, args):
        await asyncio.sleep(1)
        return "late"

    bridge._acall = slow
    bridge._thread.start()
    try:
        text = bridge.call("treg", "catalog_call_write", {})
    finally:
        bridge._loop.call_soon_threadsafe(bridge._loop.stop)
    assert "timed out after 0.05s" in text
    assert "may still be charged" in text


def test_the_call_limit_is_separate_from_the_connect_limit(tmp_path, monkeypatch):
    from waku.tools.mcp_client import MCPBridge

    monkeypatch.delenv("WAKU_MCP_CALL_TIMEOUT", raising=False)
    bridge = MCPBridge(tmp_path / "mcp.json")
    assert (bridge.timeout, bridge.call_timeout) == (30.0, 120.0)
    monkeypatch.setenv("WAKU_MCP_CALL_TIMEOUT", "45")
    assert MCPBridge(tmp_path / "mcp.json").call_timeout == 45.0
