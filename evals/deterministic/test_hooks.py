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


# --- hooks in a turn (C2) ------------------------------------------------------------


class RecordingClient:
    """A model that answers the loop with the next reply and every other
    call (the retrieval gate) with "no retrieval", and keeps each call."""

    def __init__(self, replies):
        from types import SimpleNamespace

        self.replies, self.calls, self.loop_calls = list(replies), [], []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        from evals.helpers import response, text_block

        # the loop appends to `messages` after the call, so keep them as sent
        kwargs = {**kwargs, "messages": list(kwargs.get("messages", []))}
        self.calls.append(kwargs)
        if "tools" in kwargs:
            self.loop_calls.append(kwargs)
            reply = self.replies.pop(0)
            if isinstance(reply, list):          # tool calls
                return response(reply, stop_reason="tool_use")
            return response([text_block(reply)])
        return response([text_block('{"retrieve": false, "reason": "test"}')])


def _waku(tmp_path, bridge, replies=("You ship on Fridays.",)):
    from evals.helpers import make_waku

    client = RecordingClient(replies)
    waku = make_waku(tmp_path, client=client)
    plugin = manifest.load(waku_plugin(tmp_path / "plugin-src"))
    waku.hooks = hooks.HookRunner([plugin], bridge)
    return waku, client


def _stopped(waku):
    if waku.stop_thread is not None:
        waku.stop_thread.join(5)


def test_a_gateway_turn_runs_session_start_once_then_prompt_and_stop(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_HOOK_PROJECT", raising=False)
    bridge = FakeBridge({"SessionStart": brief("SessionStart", "Project brief."),
                         "UserPromptSubmit": brief("UserPromptSubmit", "Ian deploys on Fridays.")})
    waku, client = _waku(tmp_path, bridge, replies=("You ship on Fridays.", "Yes."))
    result = waku.respond("can I ship the backend today?", source="telegram")
    _stopped(waku)
    events = [c["args"]["event"] for c in bridge.calls]
    assert events == ["SessionStart", "UserPromptSubmit", "Stop"]
    turn_id = bridge.calls[0]["args"]["turn_id"]
    assert turn_id.startswith("t_") and all(c["args"]["turn_id"] == turn_id for c in bridge.calls)
    session = bridge.calls[0]["args"]["session_id"]
    assert session.startswith("waku:") and session.endswith(":telegram") and bridge.calls[0]["args"]["cwd"] == ""
    assert bridge.calls[0]["args"]["source"] == "startup"
    assert bridge.calls[2]["args"]["last_assistant_message"] == result.reply == "You ship on Fridays."

    # the context reached the model in the user's turn, never the system prompt
    sent = client.loop_calls[0]
    last_user = sent["messages"][-1]["content"]
    assert "Project brief." in last_user and "Ian deploys on Fridays." in last_user
    assert last_user.endswith("can I ship the backend today?")
    assert "Ian deploys on Fridays." not in sent["system"]
    assert "Ian deploys on Fridays." in result.recalled

    # the second turn of the same gateway runs no SessionStart
    bridge.calls.clear()
    waku.respond("and tomorrow?", source="telegram")
    _stopped(waku)
    assert [c["args"]["event"] for c in bridge.calls] == ["UserPromptSubmit", "Stop"]


def test_brief_and_arena_turns_run_no_hooks(tmp_path):
    bridge = FakeBridge()
    waku, _ = _waku(tmp_path, bridge, replies=("a", "b"))
    waku.respond("morning brief", source="brief")
    waku.respond("race", source="compare")
    _stopped(waku)
    assert bridge.calls == []


def test_a_failing_hook_leaves_the_turn_as_it_would_have_been(tmp_path):
    bridge = FakeBridge(raises=RuntimeError("server down"))
    waku, client = _waku(tmp_path, bridge)
    result = waku.respond("can I ship today?", source="cli")
    _stopped(waku)
    assert result.reply == "You ship on Fridays."
    assert client.loop_calls[0]["messages"][-1]["content"] == "can I ship today?"


def test_stop_does_not_hold_the_reply(tmp_path):
    bridge = FakeBridge(delay=1.0)
    bridge.answers = {}
    waku, _ = _waku(tmp_path, bridge)
    waku.hooks.plugins[0].hooks = {"Stop": waku.hooks.plugins[0].hooks["Stop"]}
    started = time.perf_counter()
    waku.respond("hi", source="cli")
    assert time.perf_counter() - started < 0.9
    _stopped(waku)
    assert [c["args"]["event"] for c in bridge.calls] == ["Stop"]


def test_the_hook_project_names_the_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOOK_PROJECT", "waku-agent")
    bridge = FakeBridge()
    waku, _ = _waku(tmp_path, bridge)
    waku.respond("hi", source="dashboard")
    _stopped(waku)
    assert {c["args"]["cwd"] for c in bridge.calls} == {"waku-agent"}


# --- PreToolUse (C3) ---------------------------------------------------------------------


def _with_pre_tool_use(waku):
    plugin = waku.hooks.plugins[0]
    plugin.hooks["PreToolUse"] = [{"type": "mcp_tool", "server": "waku_memory", "tool": "session.hook",
                                  "input": {"harness": "waku", "event": "${hook_event_name}",
                                            "tool_name": "${tool_name}", "command": "${command}"}}]


def _note_turn(tmp_path, answers):
    from evals.helpers import tool_block

    bridge = FakeBridge(answers)
    note = tool_block("save_note", {"subject": "deploys", "content": "Fridays only"})
    waku, client = _waku(tmp_path, bridge, replies=([note], "Done."))
    _with_pre_tool_use(waku)
    result = waku.respond("remember we deploy on Fridays", source="cli")
    _stopped(waku)
    saved = waku.conn.execute("SELECT content FROM facts WHERE subject = 'deploys'").fetchall()
    return result, bridge, client, saved


def test_a_pre_tool_use_deny_stops_the_call_and_the_model_reads_why(tmp_path):
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "notes are read-only today"}}
    result, bridge, _client, saved = _note_turn(tmp_path, {"PreToolUse": deny})
    assert saved == []
    assert result.tool_calls[0]["denied"] is True
    assert "notes are read-only today" in result.tool_calls[0]["output"]
    pre = next(c for c in bridge.calls if c["args"]["event"] == "PreToolUse")
    assert pre["args"]["tool_name"] == "save_note"


def test_pre_tool_use_context_is_added_to_the_result_and_the_call_runs(tmp_path):
    allow = brief("PreToolUse", "Last time this note was saved twice.")
    result, _, _, saved = _note_turn(tmp_path, {"PreToolUse": allow})
    assert [r["content"] for r in saved] == ["Fridays only"]
    assert result.tool_calls[0]["output"].endswith("Last time this note was saved twice.")
    assert "denied" not in result.tool_calls[0]


def test_run_loop_without_before_tool_runs_every_call():
    from evals.helpers import tool_block
    from waku.loop.agent import run_loop
    from waku.tools.registry import Tool, ToolRegistry

    ran = []
    registry = ToolRegistry()
    registry.register(Tool(name="t", description="", input_schema={"type": "object", "properties": {}},
                           fn=lambda **kw: ran.append(kw) or "ok"))
    client = RecordingClient([[tool_block("t", {})], "done"])
    result = run_loop(client, "m", "s", [{"role": "user", "content": "x"}], registry)
    assert ran == [{}] and result.reply == "done"


def test_close_waits_for_the_last_stop_before_closing_the_bridge(tmp_path):
    """Review fix 11: the terminal exits right after the last reply, and the
    Stop hook still sending that turn was cut off with the bridge."""
    order = []

    class Bridge(FakeBridge):
        def call_json(self, server, tool, args, timeout):
            time.sleep(0.3)
            order.append(f"sent {args.get('event')}")
            return {}, False, ""

        def close(self):
            order.append("closed")

    bridge = Bridge()
    waku, _ = _waku(tmp_path, bridge, replies=("a", "b"))
    waku.mcp_bridge = bridge
    waku.respond("one", source="cli")
    waku.respond("two", source="cli")
    waku.close()
    assert order[-1] == "closed" and order.count("sent Stop") == 2


def test_the_terminal_gateway_closes_waku_on_exit():
    import inspect

    from waku.gateway import cli, voice

    assert "waku.close()" in inspect.getsource(cli.main)
    assert inspect.getsource(voice.main).count("waku.close()") == 2


def test_the_quick_route_reads_the_plugin_context_too(tmp_path):
    """Review fix 14: with graph workflows on, a quick turn dropped it."""
    from types import SimpleNamespace

    from evals.helpers import make_waku, response, text_block

    prompts = []

    def create(**kwargs):
        content = kwargs["messages"][-1]["content"]
        prompts.append(content)
        if "route" in content and "quick" in content and len(prompts) == 1:
            return response([text_block('{"route": "quick", "reason": "small talk"}')])
        if '"retrieve"' in content:
            return response([text_block('{"retrieve": false, "reason": "x"}')])
        return response([text_block("You ship on Fridays.")])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    bridge = FakeBridge({"UserPromptSubmit": brief("UserPromptSubmit", "Ian deploys on Fridays.")})
    waku = make_waku(tmp_path, client=client, graph_workflows=True)
    waku.hooks = hooks.HookRunner([manifest.load(waku_plugin(tmp_path / "src"))], bridge)
    routes = []
    result = waku.respond("can I ship today?", source="cli",
                          observer=lambda kind, ev: kind == "route" and routes.append(ev.get("target")))
    _stopped(waku)
    assert routes == ["quick_reply"] and result.reply == "You ship on Fridays."
    assert any("Ian deploys on Fridays." in p and "can I ship today?" in p for p in prompts[1:])


def test_a_failure_around_the_hooks_leaves_the_turn_running(tmp_path, monkeypatch):
    """Review fix 15: an install id that cannot be written raised out of
    respond() and lost the person's turn."""
    import waku.app

    def unwritable(home):
        raise PermissionError("read-only home")

    monkeypatch.setattr(waku.app.plugin_store, "install_id", unwritable)
    bridge = FakeBridge()
    waku, client = _waku(tmp_path, bridge)
    result = waku.respond("can I ship today?", source="cli")
    _stopped(waku)
    assert result.reply == "You ship on Fridays." and bridge.calls == []
    assert client.loop_calls[0]["messages"][-1]["content"] == "can I ship today?"


def test_a_plugins_line_for_the_person_is_kept_and_not_hidden_with_telemetry(tmp_path):
    """Review fix 16: systemMessage lines carried the `tele` class, which the
    telemetry toggle hides, and a reopened thread lost them."""
    import json as _json
    from pathlib import Path

    bridge = FakeBridge({"UserPromptSubmit": brief("UserPromptSubmit", "x", notice="Waku Memory: 1 memory")})
    waku, _ = _waku(tmp_path, bridge)
    result = waku.respond("hi", source="dashboard")
    _stopped(waku)
    assert result.notices == ["Waku Memory: 1 memory"]
    row = waku.conn.execute("SELECT meta FROM chat_log WHERE role = 'assistant'").fetchone()
    assert _json.loads(row[0])["notices"] == ["Waku Memory: 1 memory"]
    render = (Path(__file__).resolve().parents[2] / "waku/ops/static/js/render.js").read_text()
    line = next(x for x in render.splitlines() if "t.notices" in x)
    assert "tele" not in line and "notices:m.meta.notices" in render


def test_a_denied_tool_is_stored_as_denied_not_ok(tmp_path):
    """Review fix 18: a call a PreToolUse hook refused showed as ok."""
    import json as _json

    deny = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}
    _note_turn(tmp_path, {"PreToolUse": deny})
    from waku.db import connect

    row = connect(tmp_path).execute("SELECT meta FROM chat_log WHERE role = 'assistant'").fetchone()
    assert _json.loads(row[0])["tools"][0] == {"tool": "save_note", "status": "denied"}


REPORT_REPLY = """Three vendors sell hosted agent memory.

<!-- waku-report v1 -->
# Agent memory vendors, 2026-11-12

## Summary
- Three vendors sell hosted memory for AI agents.

## Findings
- Vendor A charges per seat.
"""


def test_stop_sends_the_reply_after_the_report_was_cut_from_it(tmp_path):
    """Review fix 19 (spec acceptance): Stop carries the reply the person
    saw. A research report goes to Waku Memory whole, and the reply keeps
    only its opening, so the report is not sent a second time inside Stop."""
    bridge = FakeBridge()
    waku, _ = _waku(tmp_path, bridge, replies=(REPORT_REPLY,))
    sent = []
    waku.memory.remember = lambda body, scope, kind=None: sent.append(body) or "mem-1"
    result = waku.respond("research hosted agent memory vendors", source="cli")
    _stopped(waku)
    stop = next(c for c in bridge.calls if c["args"]["event"] == "Stop")
    assert sent and "Vendor A charges per seat." in sent[0]                # the report went whole
    assert stop["args"]["last_assistant_message"] == result.reply
    assert "Vendor A charges per seat." not in stop["args"]["last_assistant_message"]
