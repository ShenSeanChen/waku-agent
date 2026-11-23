"""Review fix 10: a turn never reaches Waku Memory twice. Installing the Waku
plugin by any route saves `plugin`, and whatever mode is saved, consolidation
sends nothing while an enabled plugin's Stop hook sends the turn."""

from __future__ import annotations

import json
import os

import pytest

from evals.deterministic.plugin_fixtures import (
    WAKU_MEMORY_URL,
    install_into,
    marketplace,
    official,
    waku_plugin,
)
from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.plugins import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.delenv("WAKU_MEMORY_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    official(monkeypatch, marketplace(tmp_path / "market"))
    yield tmp_path
    os.environ.pop("WAKU_MEMORY_MODE", None)


def _yes(shown):
    return True


def test_installing_the_plugin_saves_plugin_and_removing_it_goes_back(home):
    reply = cli.run(["install", "waku@waku-memory"], home, _yes)
    assert "extracts each turn from the plugin's Stop hook" in reply
    assert os.environ["WAKU_MEMORY_MODE"] == "plugin" and "plugin" in (home / ".env").read_text()

    # no mcp.json entry to fall back to: the mode stays, and the reply says so
    reply = cli.run(["disable", "waku"], home, _yes)
    assert "nothing reaches Waku Memory" in reply and os.environ["WAKU_MEMORY_MODE"] == "plugin"

    cli.run(["enable", "waku"], home, _yes)
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "mine", "url": WAKU_MEMORY_URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}))
    reply = cli.run(["remove", "waku"], home, _yes)
    assert "sends each fact it keeps again" in reply and os.environ["WAKU_MEMORY_MODE"] == "agent"


def test_off_is_never_changed_by_a_plugin(home, monkeypatch):
    monkeypatch.setenv("WAKU_MEMORY_MODE", "off")
    cli.run(["install", "waku@waku-memory"], home, _yes)
    assert os.environ["WAKU_MEMORY_MODE"] == "off"


def test_a_plugin_command_on_another_folder_saves_no_mode(home, tmp_path):
    other = tmp_path / "elsewhere"
    cli.run(["install", "waku@waku-memory"], other, _yes)
    assert "WAKU_MEMORY_MODE" not in os.environ


class Bridge:
    def __init__(self):
        self.servers = [{"name": "waku_memory", "url": WAKU_MEMORY_URL, "plugin": "waku", "oauth": True}]
        self.config_path, self.calls = None, []

    def connected(self, name):
        return True

    def call(self, server, tool, args):
        self.calls.append(tool)
        return json.dumps({"memory": {"id": "m1"}})

    def call_json(self, server, tool, args, timeout):
        self.calls.append(f"hook:{args.get('event')}")
        return {}, False, ""

    def close(self):
        pass


@pytest.mark.parametrize("installed, sends", [(True, False), (False, True)])
def test_consolidation_sends_nothing_while_a_plugin_captures_whatever_the_mode(tmp_path, monkeypatch,
                                                                                installed, sends):
    import waku.app

    home = tmp_path / "home"
    if installed:
        install_into(home, waku_plugin(tmp_path / "src"))
    bridge = Bridge()
    real = waku.app.build_registry

    def with_bridge(*args, **kwargs):
        registry = real(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", with_bridge)
    # build_registry would start a real bridge for the plugin's server, and
    # an OAuth server with no token opens a browser: stand it in
    import sys
    import types

    class Idle:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return []

        def close(self):
            pass

    from waku.tools import mcp_client

    fake = types.ModuleType("waku.tools.mcp_client")
    fake.__dict__.update(vars(mcp_client))
    fake.MCPBridge = Idle
    monkeypatch.setitem(sys.modules, "waku.tools.mcp_client", fake)
    distilled = json.dumps({"facts": [{"subject": "deploys", "content": "We deploy on Fridays."}],
                            "episode": "Talked about deploys."})
    app = make_waku(home, client=ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "chat"}')]),
        response([text_block("Noted.")]),
        response([text_block(distilled)]),
    ]), consolidate_every=1, memory_mode="agent")
    app.respond("we deploy on Fridays", source="cli")
    if app.stop_thread is not None:
        app.stop_thread.join(5)
    assert ("memory.remember" in bridge.calls) is sends
    assert ("hook:Stop" in bridge.calls) is installed
    assert app.conn.execute("SELECT content FROM facts").fetchall()[0][0] == "We deploy on Fridays."
