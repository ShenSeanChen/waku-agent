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


def _gate_and_reply() -> list:
    return [response([text_block('{"retrieve": false, "query": "", "reason": "chat"}')]),
            response([text_block("Noted.")])]


def _distilled(content: str = FACT):
    return response([text_block(json.dumps({"facts": [{"subject": "deploys", "content": content}],
                                            "episode": "Talked about deploys."}))])


def _turn_script(turns: int) -> list:
    """Per turn: the retrieval gate, the reply, then consolidation's answer,
    one fact a turn, each with its own words."""
    out = []
    for n in range(turns):
        out += _gate_and_reply() + [_distilled(FACT if n == 0 else f"Fact number {n + 1}.")]
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

    def build(bridge, turns, every=1, script=None, mode="plugin"):
        home = tmp_path / "home"
        install_into(home, waku_plugin(tmp_path / "src"))
        real = waku.app.build_registry

        def with_bridge(*args, **kwargs):
            registry = real(*args, **kwargs)
            registry.mcp_bridge = bridge
            return registry

        monkeypatch.setattr(waku.app, "build_registry", with_bridge)
        client = ScriptedClient(script if script is not None else _turn_script(turns))
        return make_waku(home, client=client, consolidate_every=every, memory_mode=mode)

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


def test_the_prompts_answer_decides_not_the_session_starts(agent):
    """SessionStart said paused, then this turn's UserPromptSubmit said on:
    the turn was captured, so its fact is not sent, and on is stored."""

    class StartSaysPaused(Bridge):
        def call_json(self, server, tool, args, timeout):
            if args.get("event") == "SessionStart":
                self.calls.append("hook:SessionStart")
                return {"waku": {"capture": "paused"}}, False, ""
            return super().call_json(server, tool, args, timeout)

    app = agent(StartSaysPaused(["on"]), turns=1)
    _say(app)
    assert "hook:SessionStart" in app.mcp_bridge.calls
    assert _metas(app)[0]["capture"] == "on"
    assert _kept(app) == [FACT] and app.mcp_bridge.remembered() == 0


# --- E2: consolidation sends the facts of paused turns -------------------------------


def test_a_paused_turn_sends_its_fact(agent):
    app = agent(Bridge(["paused"]), turns=1)
    _say(app)
    assert _kept(app) == [FACT]
    assert app.mcp_bridge.remembered() == 1


@pytest.mark.parametrize("state", ["on", "off", None])
def test_an_on_off_or_unknown_turn_keeps_its_fact_here_and_sends_none(agent, state):
    app = agent(Bridge([state]), turns=1)
    _say(app)
    assert _kept(app) == [FACT]
    assert app.mcp_bridge.remembered() == 0


def test_a_paused_answer_from_another_address_sends_nothing(agent):
    app = agent(Bridge(["paused"], url="https://elsewhere.example/mcp"), turns=1)
    _say(app)
    assert _kept(app) == [FACT]
    assert app.mcp_bridge.remembered() == 0


def test_a_batch_with_one_paused_turn_sends_its_facts(agent):
    script = _gate_and_reply() + _gate_and_reply() + [_distilled()]
    app = agent(Bridge(["on", "paused"]), turns=2, every=2, script=script)
    _say(app, "first")
    assert app.mcp_bridge.remembered() == 0          # not due yet
    _say(app, "second")
    assert _kept(app) == [FACT]
    assert app.mcp_bridge.remembered() == 1


@pytest.mark.parametrize("state, sends", [("paused", 1), ("on", 0)])
def test_the_runtime_guard_asks_the_same_question_under_agent_mode(agent, state, sends):
    """Review fix 10: with the plugin's Stop hook installed, consolidation
    defers to the server whatever mode is saved. A paused turn still sends."""
    app = agent(Bridge([state]), turns=1, mode="agent")
    assert app.memory.plugin_captures is True
    _say(app)
    assert _kept(app) == [FACT]
    assert app.mcp_bridge.remembered() == sends


def test_with_the_default_batch_a_paused_turn_sends_at_once_and_an_on_turn_waits(agent):
    """Under plugin a laptop consolidates every 6 exchanges. A batch that holds
    a paused turn is due at once (spec 021 amendment E2)."""
    paused = agent(Bridge(["paused"]), turns=1, every=6)
    _say(paused)
    assert _kept(paused) == [FACT] and paused.mcp_bridge.remembered() == 1
    assert paused.conn.execute("SELECT count(*) FROM chat_log WHERE consolidated = 0").fetchone()[0] == 0


def test_with_the_default_batch_an_on_turn_is_not_consolidated_yet(agent):
    on = agent(Bridge(["on"]), turns=1, every=6, script=_gate_and_reply())
    _say(on)
    assert _kept(on) == [] and on.mcp_bridge.remembered() == 0
    assert on.conn.execute("SELECT count(*) FROM chat_log WHERE consolidated = 0").fetchone()[0] == 2


@pytest.mark.parametrize("state", ["on", "off"])
def test_a_fact_whose_send_failed_earlier_is_retried_while_every_turn_is_on_or_off(agent, state):
    """Spec 021 amendment E2: an unsent fact was one the person asked to save
    or one from a paused batch, so it goes again whatever this turn's state.
    This turn's own fact is not sent."""
    app = agent(Bridge([state]), turns=1)
    app.conn.execute("INSERT INTO facts (subject, content, source, synced, scope) "
                     "VALUES ('raj', 'Raj plays tennis.', 'save_note', 0, 'global')")
    app.conn.commit()
    _say(app)
    assert app.mcp_bridge.remembered() == 1
    assert sorted(_kept(app)) == sorted(["Raj plays tennis.", FACT])
    assert app.conn.execute("SELECT count(*) FROM facts WHERE synced = 0").fetchone()[0] == 0


def test_a_failed_send_from_a_paused_batch_is_sent_on_the_next_consolidation(agent):
    app = agent(Bridge(["paused", "on"], fail_remember=1), turns=2)
    _say(app)
    assert app.mcp_bridge.remembered() == 1          # tried, and failed
    _say(app)
    # the failed fact goes again although this turn was captured; this turn's
    # own fact is not sent, because the server takes it from the Stop hook
    assert app.mcp_bridge.remembered() == 2
    assert sorted(_kept(app)) == sorted([FACT, "Fact number 2."])
    unsent = app.conn.execute("SELECT count(*) FROM facts WHERE synced = 0").fetchone()[0]
    assert unsent == 0


# --- E3: tell the person once per change ---------------------------------------------


def _paused_lines(result):
    from waku.app import CAPTURE_PAUSED_LINE

    return [n for n in result.notices if n == CAPTURE_PAUSED_LINE]


def test_the_paused_line_is_shown_once_over_three_paused_turns(agent):
    app = agent(Bridge(["paused", "paused", "paused"]), turns=3)
    shown = [len(_paused_lines(_say(app))) for _ in range(3)]
    assert shown == [1, 0, 0]


def test_the_paused_line_is_shown_again_after_an_on_turn(agent):
    app = agent(Bridge(["paused", "on", "paused"]), turns=3)
    shown = [len(_paused_lines(_say(app))) for _ in range(3)]
    assert shown == [1, 0, 1]


def test_the_line_reads_exactly_as_the_spec_quotes_it():
    from waku.app import CAPTURE_PAUSED_LINE

    assert CAPTURE_PAUSED_LINE == ("Waku Agent will send the facts it keeps from this conversation "
                                   "to Waku Memory itself until remembering from conversations is "
                                   "back on.")


def test_capture_json_is_written_in_one_step_and_never_by_the_stop_hook(agent, monkeypatch):
    """respond() runs on a gateway's own thread in production, so the test is
    not which thread writes, but that the turn writes once and the Stop
    hook's answer, read on its background thread, writes nothing."""
    import os

    from waku.plugins import store

    writers, replaced = [], []
    real_save, real_replace = store.save_capture_told, os.replace

    def save(home, state):
        writers.append(state)
        return real_save(home, state)

    def replace(src, dst):
        replaced.append(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(store, "save_capture_told", save)
    monkeypatch.setattr(store.os, "replace", replace)

    class StopSaysOn(Bridge):
        def call_json(self, server, tool, args, timeout):
            if args.get("event") == "Stop":
                self.calls.append("hook:Stop")
                return {"waku": {"capture": "on"}}, False, ""
            return super().call_json(server, tool, args, timeout)

    app = agent(StopSaysOn(["paused"]), turns=1)
    _say(app)
    assert writers == ["paused"]
    assert replaced and replaced[0].endswith("capture.json")
    # the Stop thread's "on" did not overwrite what the main thread recorded
    assert store.capture_told(app.settings.home) == "paused"


# --- E4: what leaves ------------------------------------------------------------------


def test_the_first_run_question_says_what_is_sent_while_paused():
    from waku.memory_choice import WHAT_LEAVES

    assert "If remembering from conversations is paused on your account" in WHAT_LEAVES
    assert "Waku sends the facts it keeps itself instead" in WHAT_LEAVES


def test_two_writes_in_a_row_leave_no_temporary_file(tmp_path):
    from waku.plugins import store

    store.save_capture_told(tmp_path, "paused")
    store.save_capture_told(tmp_path, "on")
    assert store.capture_told(tmp_path) == "on"
    assert sorted(p.name for p in store.folder(tmp_path).iterdir()) == ["capture.json"]


def test_a_write_whose_replace_fails_leaves_no_temporary_file(tmp_path, monkeypatch):
    from waku.plugins import store

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", refuse)
    with pytest.raises(OSError):
        store.save_capture_told(tmp_path, "paused")
    assert list(store.folder(tmp_path).iterdir()) == []
