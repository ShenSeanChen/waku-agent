"""Spec 021 C: hooks. A fake MCP bridge stands in for Waku Memory, records
each hook call and answers the way session.hook does."""

from __future__ import annotations

import time

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, waku_plugin
from waku.plugins import hooks, manifest


class FakeBridge:
    """The bridge as hooks see it: a merged server list and call_json."""

    def __init__(self, answers=None, servers=None, delay=0.0, raises=None):
        self.servers = servers if servers is not None else [
            {"name": "waku_memory", "url": WAKU_MEMORY_URL, "plugin": "waku", "oauth": True}]
        self.answers = answers or {}
        self.calls = []
        self.delay, self.raises = delay, raises

    def call_json(self, server, tool, args, timeout):
        self.calls.append({"server": server, "tool": tool, "args": args, "timeout": timeout})
        if self.raises:
            raise self.raises
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.get(args.get("event"), {})
        if answer == "error":
            return None, True, "harness 'waku' carries no plugin"
        return answer, False, ""

    def connected(self, name):
        return True


def brief(event, text, notice=None):
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if notice:
        out["systemMessage"] = notice
    return out


def _runner(tmp_path, bridge, notify=None):
    plugin = manifest.load(waku_plugin(tmp_path / "waku"))
    return hooks.HookRunner([plugin], bridge, notify)


def test_variables_are_substituted_and_the_answer_is_read(tmp_path):
    bridge = FakeBridge({"UserPromptSubmit": brief("UserPromptSubmit", "Ian deploys on Fridays.", "1 memory")})
    result = _runner(tmp_path, bridge).run("UserPromptSubmit", {
        "session_id": "waku:abc:telegram", "cwd": "", "prompt": "can I ship today?", "turn_id": "t_1"})
    assert result.context == ["Ian deploys on Fridays."] and result.messages == ["1 memory"]
    call = bridge.calls[0]
    assert call["server"] == "waku_memory" and call["tool"] == "session.hook" and call["timeout"] == 10
    assert call["args"] == {"harness": "waku", "event": "UserPromptSubmit", "session_id": "waku:abc:telegram",
                            "cwd": "", "turn_id": "t_1", "prompt": "can I ship today?"}


def test_a_missing_variable_becomes_empty_and_a_dict_becomes_json():
    assert hooks.substitute({"a": "${nope}", "b": ["x${n}"]}, {"n": {"k": 1}}) == {"a": "", "b": ['x{"k": 1}']}


def test_a_failing_or_raising_hook_is_skipped_and_reported(tmp_path):
    events = []
    bridge = FakeBridge({"Stop": "error"})
    result = _runner(tmp_path, bridge, lambda k, e: events.append((k, e))).run("Stop", {})
    assert result.context == [] and result.deny is None
    assert events[0][0] == "hook" and events[0][1]["ok"] is False
    assert "carries no plugin" in events[0][1]["error"]

    events.clear()
    raising = FakeBridge(raises=RuntimeError("connection reset"))
    result = _runner(tmp_path, raising, lambda k, e: events.append((k, e))).run("UserPromptSubmit", {})
    assert result.context == [] and "connection reset" in events[0][1]["error"]


def test_a_hook_whose_server_is_not_its_plugins_is_not_called(tmp_path):
    bridge = FakeBridge(servers=[{"name": "waku_memory", "url": "https://elsewhere/mcp", "auth_env": "K"}])
    events = []
    _runner(tmp_path, bridge, lambda k, e: events.append(e)).run("Stop", {})
    # mcp.json took the id for another address, so the hook has nowhere to go
    assert bridge.calls == [] and "not connected" in events[0]["error"]


def test_a_hook_reaches_mcp_jsons_server_at_the_same_address(tmp_path):
    bridge = FakeBridge(servers=[{"name": "mine", "url": WAKU_MEMORY_URL + "/", "oauth": True}])
    _runner(tmp_path, bridge).run("Stop", {})
    assert bridge.calls[0]["server"] == "mine"


def test_pre_tool_use_deny_is_read():
    data = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no shell"}}
    assert hooks.read_output(data, "PreToolUse").deny == "no shell"
    assert hooks.read_output(data, "Stop").deny is None


def test_a_command_hook_is_not_run(tmp_path):
    plugin = manifest.load(waku_plugin(tmp_path / "waku"))
    plugin.hooks = {"Stop": [{"type": "command", "command": "rm -rf ~"}]}
    bridge, events = FakeBridge(), []
    hooks.HookRunner([plugin], bridge, lambda k, e: events.append(e)).run("Stop", {})
    assert bridge.calls == [] and "not run by Waku" in events[0]["error"]


# --- the bridge's structured call ----------------------------------------------------


def _real_bridge(tmp_path, answer):
    import asyncio
    import json as _json
    from types import SimpleNamespace

    from waku.tools.mcp_client import MCPBridge

    class Session:
        async def call_tool(self, tool, args):
            if answer == "slow":
                await asyncio.sleep(2)
            if answer == "error":
                return SimpleNamespace(content=[SimpleNamespace(text="bad harness")], isError=True)
            return SimpleNamespace(content=[SimpleNamespace(text=_json.dumps(answer))], isError=False)

    bridge = MCPBridge(tmp_path / "mcp.json")
    bridge._sessions["waku_memory"] = Session()
    bridge._thread.start()
    return bridge


def test_call_json_returns_the_tools_json_its_error_flag_and_honours_its_timeout(tmp_path):
    ok = _real_bridge(tmp_path, brief("Stop", "x"))
    assert ok.call_json("waku_memory", "session.hook", {}, 5) == (brief("Stop", "x"), False, '{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "x"}}')
    bad = _real_bridge(tmp_path, "error")
    assert bad.call_json("waku_memory", "session.hook", {}, 5)[1:] == (True, "bad harness")
    slow = _real_bridge(tmp_path, "slow")
    started = time.perf_counter()
    _data, failed, text = slow.call_json("waku_memory", "session.hook", {}, 0.2)
    assert failed and "timed out" in text and time.perf_counter() - started < 1.5
    assert ok.call_json("other", "t", {}, 1) == (None, True, "MCP server 'other' is not connected.")
    for bridge in (ok, bad, slow):
        bridge._loop.call_soon_threadsafe(bridge._loop.stop)
