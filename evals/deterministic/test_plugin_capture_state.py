"""Spec 021 amendment: fall back when capture is paused.

Waku Memory answers each of Waku Agent's hooks with
`"waku": {"capture": "on" | "off" | "paused"}` (waku-memory spec 086 C2).
A turn records the state from its own UserPromptSubmit answer (E1). Under
`plugin`, consolidation sends a batch's facts when any of its turns was
paused, and none otherwise (E2). The person is told once per change to
paused (E3).

Every Waku-level case consolidates every turn with a scripted model that
proposes one fact, and asserts that the fact was kept here, so a case that
sends nothing cannot pass because consolidation never ran.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, install_into, waku_plugin
from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.plugins import hooks, manifest

FACT = "We deploy on Fridays."


class Bridge:
    """Waku Memory as the agent sees it: remember calls are counted, and each
    UserPromptSubmit is answered with the next capture state in `states`."""

    def __init__(self, states, url=WAKU_MEMORY_URL, fail_remember=0):
        self.servers = [{"name": "waku_memory", "url": url, "plugin": "waku", "oauth": True}]
        self.config_path, self.calls = None, []
        self.states = list(states)
        self.fail_remember = fail_remember

    def connected(self, name):
        return True

    def call(self, server, tool, args):
        self.calls.append(tool)
        if tool == "memory.remember" and self.fail_remember:
            self.fail_remember -= 1
            return "failed: the server is down"
        return json.dumps({"memory": {"id": f"m{len(self.calls)}"}})

    def call_json(self, server, tool, args, timeout):
        event = args.get("event")
        self.calls.append(f"hook:{event}")
        if event == "UserPromptSubmit" and self.states:
            state = self.states.pop(0)
            return ({"waku": {"capture": state}} if state else {}), False, ""
        return {}, False, ""

    def close(self):
        pass

    def remembered(self):
        return self.calls.count("memory.remember")


def _turn_script(turns: int, facts_per_turn: int = 1) -> list:
    """Per turn: the retrieval gate, the reply, then consolidation's answer."""
    distilled = json.dumps({"facts": [{"subject": "deploys", "content": FACT}] * facts_per_turn,
                            "episode": "Talked about deploys."})
    out = []
    for _ in range(turns):
        out += [response([text_block('{"retrieve": false, "query": "", "reason": "chat"}')]),
                response([text_block("Noted.")]),
                response([text_block(distilled)])]
    return out


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A Waku with the Waku plugin installed and `bridge` as its MCP bridge."""
    import waku.app
    from waku.tools import mcp_client

    monkeypatch.delenv("WAKU_MEMORY_MODE", raising=False)

    class Idle:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return []

        def close(self):
            pass

    fake = types.ModuleType("waku.tools.mcp_client")
    fake.__dict__.update(vars(mcp_client))
    fake.MCPBridge = Idle
    monkeypatch.setitem(sys.modules, "waku.tools.mcp_client", fake)

    def build(bridge, turns, every=1, script=None):
        home = tmp_path / "home"
        install_into(home, waku_plugin(tmp_path / "src"))
        real = waku.app.build_registry

        def with_bridge(*args, **kwargs):
            registry = real(*args, **kwargs)
            registry.mcp_bridge = bridge
            return registry

        monkeypatch.setattr(waku.app, "build_registry", with_bridge)
        client = ScriptedClient(script if script is not None else _turn_script(turns))
        return make_waku(home, client=client, consolidate_every=every, memory_mode="plugin")

    return build


def _say(app, message="we deploy on Fridays"):
    result = app.respond(message, source="cli")
    if app.stop_thread is not None:
        app.stop_thread.join(5)
    return result


def _metas(app):
    return [json.loads(m) for (m,) in app.conn.execute(
        "SELECT meta FROM chat_log WHERE role = 'assistant' ORDER BY id").fetchall()]


def _kept(app):
    return [c for (c,) in app.conn.execute("SELECT content FROM facts").fetchall()]


# --- E1: each turn records its own state ---------------------------------------------


@pytest.mark.parametrize("state", ["on", "off", "paused"])
def test_a_turn_stores_the_state_from_its_own_prompt_answer(agent, state):
    app = agent(Bridge([state]), turns=1)
    _say(app)
    assert _metas(app)[0]["capture"] == state


def test_a_turn_with_no_state_stores_none(agent):
    app = agent(Bridge([None]), turns=1)
    _say(app)
    assert "capture" not in _metas(app)[0]


def test_a_waku_field_from_another_address_is_ignored(tmp_path):
    plugin = manifest.load(waku_plugin(tmp_path / "waku"))
    elsewhere = Bridge(["paused"], url="https://elsewhere.example/mcp")
    assert hooks.HookRunner([plugin], elsewhere).run("UserPromptSubmit", {}).capture is None
    at_waku_memory = Bridge(["paused"])
    assert hooks.HookRunner([plugin], at_waku_memory).run("UserPromptSubmit", {}).capture == "paused"


def test_only_known_states_are_read():
    assert hooks.capture_of({"waku": {"capture": "maybe"}}) is None
    assert hooks.capture_of({"waku": "paused"}) is None
    assert hooks.capture_of({"waku": {"capture": "off"}}) == "off"
