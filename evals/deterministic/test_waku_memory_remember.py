"""DETERMINISTIC EVAL -- the agent, not the model, sends kept facts to Waku Memory
(spec 006 A5).

A tool the model may call is a suggestion. With Waku Memory connected,
app.py hands consolidation a remember() that calls the server's
memory.remember for every fact it keeps. Without the server there is no
remember() and nothing is sent. A fake bridge stands in for the MCP session.
"""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.tools import waku_memory
from waku.tools.waku_memory import remember_via


class FakeBridge:
    def __init__(self, config_path, connected=("waku_memory",), reply=None):
        self.config_path = config_path
        self._connected = set(connected)
        self.reply = reply if reply is not None else json.dumps(
            {"memory": {"id": "4b1c0e2a", "body": "x"}, "deduped": False})
        self.calls: list[tuple[str, str, dict]] = []

    def connected(self, server):
        return server in self._connected

    def call(self, server, tool, args):
        self.calls.append((server, tool, args))
        return self.reply

    def close(self):
        pass


def _config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


HOSTED = {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}


def test_research_is_sent_as_knowledge_and_returns_its_waku_memory_id(tmp_path):
    """waku.one files `fact` under Activity and `reference` under Knowledge;
    a company-research finding is knowledge about the world."""
    bridge = FakeBridge(_config(tmp_path, [HOSTED]))
    remember = remember_via(bridge)
    assert remember("Descript costs $24 a month.", "project:Company brain") == "4b1c0e2a"
    assert bridge.calls == [("waku_memory", "memory.remember",
                             {"body": "Descript costs $24 a month.", "kind": "reference",
                              "scope": "project:Company brain"})]


def test_a_personal_fact_stays_a_fact(tmp_path):
    bridge = FakeBridge(_config(tmp_path, [HOSTED]))
    remember_via(bridge)("Sergey is the user's swim buddy.", "global")
    assert bridge.calls[0][2]["kind"] == "fact"


def test_a_failure_reported_as_text_raises(tmp_path):
    """The bridge turns every failure into a string. Consolidation must see a
    failure as a failure, or the fact is marked synced and never retried."""
    bridge = FakeBridge(_config(tmp_path, [HOSTED]),
                        reply="MCP call waku_memory_memory.remember failed: timed out")
    remember = remember_via(bridge)
    try:
        remember("A fact.", "global")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("a failed send must raise")


def test_no_bridge_or_no_connected_server_means_no_send(tmp_path):
    assert remember_via(None) is None
    assert remember_via(FakeBridge(_config(tmp_path, [HOSTED]), connected=())) is None
    other = {"name": "demo", "command": "python"}
    assert remember_via(FakeBridge(_config(tmp_path, [other]), connected=("demo",))) is None


def test_a_server_added_by_hand_under_another_name_is_found_by_its_url(tmp_path):
    spec = {"name": "memory", "url": waku_memory.URL + "/", "oauth": True}
    bridge = FakeBridge(_config(tmp_path, [spec]), connected=("memory",))
    remember_via(bridge)("A fact.", "global")
    assert bridge.calls[0][0] == "memory"


def test_without_waku_memory_a_turn_sends_nothing(tmp_path):
    distilled = json.dumps({"facts": [{"subject": "Alex", "content": "Alex likes mornings."}],
                            "episode": "Talked about Alex."})
    app = make_waku(tmp_path / "home", client=ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "chat"}')]),
        response([text_block("Noted.")]),
        response([text_block(distilled)]),
    ]), consolidate_every=1)
    assert app.memory.remember is None
    app.respond("Alex likes mornings")
    assert app.conn.execute("SELECT COUNT(*) FROM facts WHERE synced = 0").fetchone()[0] == 0


def test_with_waku_memory_connected_a_turn_sends_what_it_keeps(tmp_path, monkeypatch):
    """The wiring end to end: app.py builds remember() from the bridge the
    tool registry connected, and the turn's consolidation calls it."""
    import waku.app

    bridge = FakeBridge(_config(tmp_path, [HOSTED]))
    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    distilled = json.dumps({"facts": [{"subject": "Alex", "content": "Alex likes mornings."}],
                            "episode": "Talked about Alex."})
    app = make_waku(tmp_path / "home", client=ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "chat"}')]),
        response([text_block("Noted.")]),
        response([text_block(distilled)]),
    ]), consolidate_every=1)
    app.respond("Alex likes mornings")
    assert [c[2]["body"] for c in bridge.calls] == ["Alex likes mornings."]
    assert app.conn.execute("SELECT COUNT(*) FROM facts WHERE synced = 0").fetchone()[0] == 0
