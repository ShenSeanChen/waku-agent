"""Spec 021 A2: a plugin's MCP servers join mcp.json's, as one list. The
bridge starts when either source names a server, mcp.json wins an id
conflict, and Waku Memory is found whichever file named it. Offline: the
bridge is a stand-in, so no `mcp` session is opened."""

from __future__ import annotations

import json
import sys
import types
from typing import ClassVar

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, install_into, waku_plugin
from waku.config import Settings
from waku.db import connect
from waku.tools import build_registry
from waku.tools import waku_memory as wm
from waku.tools.mcp_client import merge


def test_merge_keeps_mcp_json_and_adds_the_rest():
    config = [{"name": "fs", "command": "npx"}]
    plugin = [{"name": "docs", "url": "https://docs/mcp", "plugin": "p"}]
    merged, skipped = merge(config, plugin)
    assert [s["name"] for s in merged] == ["fs", "docs"] and skipped == []


def test_an_id_conflict_keeps_mcp_json_and_names_the_skipped_server():
    config = [{"name": "waku_memory", "url": "https://elsewhere/mcp", "auth_env": "K"}]
    plugin = [{"name": "waku_memory", "url": WAKU_MEMORY_URL, "plugin": "waku"}]
    merged, skipped = merge(config, plugin)
    assert merged == config and skipped == [("waku", "waku_memory")]


def test_the_same_url_under_another_id_is_the_same_server_and_skipped_silently():
    config = [{"name": "mine", "url": WAKU_MEMORY_URL + "/", "oauth": True}]
    plugin = [{"name": "waku_memory", "url": WAKU_MEMORY_URL, "plugin": "waku"}]
    merged, skipped = merge(config, plugin)
    assert merged == config and skipped == []


class _FakeBridge:
    built: ClassVar[list] = []

    def __init__(self, config_path, skip=None, extra_servers=(), **kwargs):
        self.config_path, self.skip, self.extra = config_path, skip, list(extra_servers)
        type(self).built.append(self)

    def start(self):
        return []


def _build(tmp_path, monkeypatch, mode=""):
    fake = types.ModuleType("waku.tools.mcp_client")
    fake.MCPBridge = _FakeBridge
    monkeypatch.setitem(sys.modules, "waku.tools.mcp_client", fake)
    _FakeBridge.built = []
    registry = build_registry(connect(tmp_path), Settings(home=tmp_path, memory_mode=mode))
    return registry, _FakeBridge.built


def test_a_plugin_starts_the_bridge_with_no_mcp_json(tmp_path, monkeypatch):
    install_into(tmp_path, waku_plugin(tmp_path / "src"))
    assert not (tmp_path / "mcp.json").exists()
    _, built = _build(tmp_path, monkeypatch)
    assert len(built) == 1
    assert [s["name"] for s in built[0].extra] == ["waku_memory"]


def test_no_mcp_json_and_no_plugin_starts_no_bridge(tmp_path, monkeypatch):
    _, built = _build(tmp_path, monkeypatch)
    assert built == []


def test_a_disabled_plugin_brings_no_server(tmp_path, monkeypatch):
    install_into(tmp_path, waku_plugin(tmp_path / "src"), enabled=False)
    _, built = _build(tmp_path, monkeypatch)
    assert built == []


def test_off_leaves_the_plugins_waku_memory_unconnected_too(tmp_path, monkeypatch):
    install_into(tmp_path, waku_plugin(tmp_path / "src"))
    _, built = _build(tmp_path, monkeypatch, mode="off")
    assert built[0].skip(built[0].extra[0]) is True


class _Connected:
    """A started bridge, as remember_via sees it: a merged list and a call."""

    def __init__(self, servers):
        self.servers, self.calls = servers, []
        self.config_path = None

    def connected(self, name):
        return True

    def call(self, server, tool, args):
        self.calls.append((server, tool, args))
        return json.dumps({"memory": {"id": "m1"}})


def test_remember_via_finds_a_plugins_waku_memory_server():
    bridge = _Connected([{"name": "fs", "command": "npx"},
                         {"name": "waku_memory", "url": WAKU_MEMORY_URL, "plugin": "waku"}])
    remember = wm.remember_via(bridge)
    assert remember is not None and remember("we deploy on Fridays", "global") == "m1"
    assert bridge.calls[0][:2] == ("waku_memory", "memory.remember")
    assert wm.tool_name(bridge, "memory.search") == "waku_memory_memory_search"


def test_connections_reports_the_plugins_server(tmp_path):
    install_into(tmp_path, waku_plugin(tmp_path / "src"))
    assert wm.status(tmp_path).startswith("configured · not signed in")
    (tmp_path / "mcp-auth").mkdir()
    (tmp_path / "mcp-auth" / "waku_memory.json").write_text(json.dumps({"tokens": {}}), encoding="utf-8")
    assert wm.status(tmp_path).startswith("connected")


def test_a_plugins_server_not_signed_in_is_told_to_log_in_not_to_connect(tmp_path):
    """`waku connect waku-memory` would add a second entry to mcp.json beside
    the plugin's server; the plugin's server signs in with `waku mcp login`."""
    install_into(tmp_path, waku_plugin(tmp_path / "src"))
    assert wm.status(tmp_path) == "configured · not signed in: run waku mcp login waku_memory"


def test_an_mcp_json_server_not_signed_in_keeps_the_connect_hint(tmp_path):
    wm.add_server(tmp_path)
    assert wm.status(tmp_path) == "configured · not signed in: run waku connect waku-memory"
