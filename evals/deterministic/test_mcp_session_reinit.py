"""DETERMINISTIC EVAL -- an MCP server that forgets our session gets a new one.

2026-10-04, agent.waku.one: api.waku.one was redeployed during a research
turn and forgot every MCP session it held. From then on it answered each call
from the agent with HTTP 404 "Session not found", and the agent kept sending
the dead session's id. The read-first search failed, the research report was
not saved, and none of the six facts the turn kept reached Waku Memory. The
MCP spec says a client that gets a 404 for its session must start a new one.

The contract under test:
  - a call answered "Session not found" (or the SDK's "Session terminated")
    opens a new session and is sent once more, and its answer is returned;
  - any other failure is returned as it is, with no new session;
  - calls that find the session dead at the same moment open ONE new session;
  - a new session that cannot open leaves the call failed, and the next call
    tries again;
  - the paths that failed on 2026-10-04 work: saving a report, keeping a
    fact, and the read-first search.

The first group runs against fake sessions, so it runs in CI, where the `mcp`
extra is not installed. The last group restarts a real Streamable HTTP server
between two calls and skips without `mcp`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from waku.memory import reports
from waku.tools import waku_memory
from waku.tools.mcp_client import MCPBridge, _session_expired

REPO = Path(__file__).resolve().parents[2]
MEMORY_SERVER = REPO / "evals" / "fixtures" / "mcp_memory_server.py"

REPORT_REPLY = """Three vendors sell hosted agent memory.

<!-- waku-report v1 -->
# Agent memory vendors, 2026-10-04

## Summary
- Three vendors sell hosted memory for AI agents.
"""


class Expired(Exception):
    """What the SDK raises for a 404 on a request that carried a session id."""


class FakeSession:
    """One MCP session. `dead` makes every call fail the way an expired one does."""

    def __init__(self, name: str, dead: bool = False, delay: float = 0.0):
        self.name, self.dead, self.delay = name, dead, delay
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.dead:
            raise Expired("Session not found")
        if tool == "memory.remember":
            text = json.dumps({"memory": {"id": f"mem-{len(self.calls)}"}, "deduped": False})
        elif tool == "memory.search":
            text = json.dumps({"entries": []})
        else:
            text = f"{self.name}:{tool}"
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _bridge(tmp_path, first: FakeSession, server="waku_memory", reopen=None):
    """A started bridge whose `server` holds `first`. Opening a new session
    calls `reopen()` for the session to install (a live one by default)."""
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": [
        {"name": server, "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    bridge = MCPBridge(config, call_timeout=10)
    bridge.opened = 0

    async def connect_one(spec):
        bridge.opened += 1
        await asyncio.sleep(0.05)  # long enough for other callers to pile up
        bridge._sessions[spec["name"]] = (reopen or (lambda: FakeSession("new")))()
        return []

    bridge._connect_one = connect_one
    bridge._sessions[server] = first
    bridge._specs[server] = {"name": server, "url": waku_memory.URL}
    bridge._thread.start()
    return bridge


def _stop(bridge):
    bridge._loop.call_soon_threadsafe(bridge._loop.stop)


# --- which failures mean the session is gone -----------------------------------------


@pytest.mark.parametrize("message", ["Session not found", "Session terminated",
                                     "MCPError: session not found"])
def test_the_two_ways_the_sdk_reports_a_forgotten_session(message):
    assert _session_expired(Exception(message))


@pytest.mark.parametrize("message", ["Server returned an error response", "Not Found",
                                     "unknown tool memory.forget", "timed out", ""])
def test_other_failures_are_not_a_forgotten_session(message):
    assert not _session_expired(Exception(message))


# --- the bridge opens a new session and sends the call once more ---------------------


def test_a_forgotten_session_is_replaced_and_the_call_answered(tmp_path):
    old = FakeSession("old", dead=True)
    bridge = _bridge(tmp_path, old, server="treg")
    try:
        assert bridge.call("treg", "catalog_search", {"q": "seo"}) == "new:catalog_search"
        assert bridge.opened == 1
        assert old.calls == [("catalog_search", {"q": "seo"})], "the dead session is asked once"
        assert bridge.call("treg", "catalog_search", {}) == "new:catalog_search"
        assert bridge.opened == 1, "the new session is kept for later calls"
    finally:
        _stop(bridge)


def test_another_failure_is_returned_and_opens_nothing(tmp_path):
    class Broken(FakeSession):
        async def call_tool(self, tool, args):
            self.calls.append((tool, args))
            raise RuntimeError("unknown tool memory.forget")

    old = Broken("old")
    bridge = _bridge(tmp_path, old)
    try:
        text = bridge.call("waku_memory", "memory.forget", {})
        assert text == "MCP call waku_memory_memory.forget failed: unknown tool memory.forget"
        assert bridge.opened == 0 and len(old.calls) == 1
    finally:
        _stop(bridge)


def test_a_new_session_that_also_fails_is_not_retried_again(tmp_path):
    """One retry, not a loop: a server that forgets every session it opens
    must not keep the call spinning."""
    bridge = _bridge(tmp_path, FakeSession("old", dead=True),
                     reopen=lambda: FakeSession("also-dead", dead=True))
    try:
        assert "Session not found" in bridge.call("waku_memory", "memory.search", {})
        assert bridge.opened == 1
    finally:
        _stop(bridge)


def test_calls_that_find_the_session_dead_together_open_one_new_session(tmp_path):
    bridge = _bridge(tmp_path, FakeSession("old", dead=True, delay=0.02))
    results: list[str] = []
    try:
        threads = [threading.Thread(target=lambda: results.append(
            bridge.call("waku_memory", "memory.search", {"query": "mem0"}))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert results == ['{"entries": []}'] * 5
        assert bridge.opened == 1
    finally:
        _stop(bridge)


def test_a_server_still_down_fails_the_call_and_the_next_call_tries_again(tmp_path):
    attempts = iter([ConnectionError("connection refused"), None])
    bridge = _bridge(tmp_path, FakeSession("old", dead=True))

    async def connect_one(spec):
        bridge.opened += 1
        error = next(attempts)
        if error:
            raise error
        bridge._sessions[spec["name"]] = FakeSession("new")
        return []

    bridge._connect_one = connect_one
    try:
        assert "connection refused" in bridge.call("waku_memory", "memory.search", {})
        assert bridge.call("waku_memory", "memory.search", {}) == '{"entries": []}'
        assert bridge.opened == 2
    finally:
        _stop(bridge)


# --- the three paths that failed on 2026-10-04 ----------------------------------------


def test_the_report_is_saved_after_waku_memory_forgot_the_session(tmp_path):
    bridge = _bridge(tmp_path, FakeSession("old", dead=True))
    try:
        remember = waku_memory.remember_via(bridge)
        reply, event = reports.save(REPORT_REPLY, remember, lambda report: True)
        assert event is not None, "the report was not saved"
        assert event["memory_id"] == "mem-1" and event["scope"] == "project:Company brain"
        assert "Report saved" in reply and "<!-- waku-report v1 -->" not in reply
    finally:
        _stop(bridge)


def test_a_kept_fact_reaches_waku_memory_after_it_forgot_the_session(tmp_path):
    bridge = _bridge(tmp_path, FakeSession("old", dead=True))
    try:
        remember = waku_memory.remember_via(bridge)
        assert remember("Sean films on Fridays.", "global") == "mem-1"
    finally:
        _stop(bridge)


def test_the_read_first_search_runs_after_waku_memory_forgot_the_session(tmp_path):
    bridge = _bridge(tmp_path, FakeSession("old", dead=True))
    try:
        search = waku_memory.search_via(bridge)
        assert json.loads(search({"query": "mem0"})) == {"entries": []}
    finally:
        _stop(bridge)


# --- a real server that restarts between two calls ------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> subprocess.Popen:
    server = subprocess.Popen([sys.executable, str(MEMORY_SERVER), "--port", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(80):
        if server.poll() is not None:
            pytest.fail("the memory server exited before it listened")
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return server
        except urllib.error.HTTPError:
            return server  # listening; a bare GET is refused by design
        except OSError:
            time.sleep(0.25)
    server.terminate()
    pytest.fail(f"the memory server never listened on {port}")


def _restart(server: subprocess.Popen, port: int) -> subprocess.Popen:
    server.terminate()
    server.wait(timeout=10)
    return _serve(port)


def test_a_restarted_server_is_reached_through_a_new_session(tmp_path):
    """The 2026-10-04 redeploy, for real: the server restarts, forgets the
    session, answers 404, and the report is saved anyway."""
    pytest.importorskip("mcp", reason="the MCP connector is an optional extra")
    port = _free_port()
    server = _serve(port)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": f"http://127.0.0.1:{port}/mcp"}]}), encoding="utf-8")
    bridge = MCPBridge(config, timeout=30.0)
    try:
        assert "waku_memory_memory_remember" in [t.name for t in bridge.start()]
        remember = waku_memory.remember_via(bridge)
        assert remember("Sean films on Fridays.", "global").startswith("mem-")

        server = _restart(server, port)
        reply, event = reports.save(REPORT_REPLY, remember, lambda report: False)
        assert event is not None and event["memory_id"].startswith("mem-"), reply
        assert waku_memory.search_via(bridge)({"query": "mem0"}) == '{"entries": []}'
    finally:
        bridge.close()
        server.terminate()
        server.wait(timeout=10)
