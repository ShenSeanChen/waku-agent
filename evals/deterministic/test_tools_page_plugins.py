"""The Tools page lists what plugins bring, and the Memory page says which
memory is which.

Until 2026-11-14 the Tools page read `mcp.json` alone. A server that came
from a plugin (Waku Memory, after `waku plugin install waku@waku-memory`) was
listed nowhere, its tools were filed under "Other", and the hooks a plugin
declared had no page at all.
"""

from __future__ import annotations

import json

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, install_into, waku_plugin
from waku.config import Settings
from waku.ops import dashboard


def _home(tmp_path, plugin=True, mcp_json=None):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    if plugin:
        install_into(home, waku_plugin(tmp_path / "src"))
    if mcp_json is not None:
        (home / "mcp.json").write_text(json.dumps({"servers": mcp_json}), encoding="utf-8")
    return home


def test_a_plugins_server_is_listed_beside_mcp_jsons(tmp_path):
    home = _home(tmp_path, mcp_json=[{"name": "fs", "command": "npx", "args": ["-y", "server"]}])
    servers = dashboard.mcp_servers(home)
    assert [s["name"] for s in servers] == ["fs", "waku_memory"]

    rows = [dashboard._mcp_row(home, s, None, []) for s in servers]
    assert rows[0]["plugin"] == "" and rows[0]["address"] == "npx (runs on this machine)"
    assert rows[1]["plugin"] == "waku" and rows[1]["address"] == WAKU_MEMORY_URL
    # no chat turn has built the agent, so the page claims no connection
    assert rows[1]["connected"] is None and rows[1]["tools"] is None
    assert rows[1]["account"] == "not signed in" and rows[0]["account"] is None


def test_a_home_with_no_mcp_json_and_no_plugin_lists_nothing(tmp_path):
    home = _home(tmp_path, plugin=False)
    assert dashboard.mcp_servers(home) == []
    (home / "mcp.json").write_text("{not json", encoding="utf-8")
    assert dashboard.mcp_servers(home) == []


def test_a_live_bridge_says_which_servers_are_connected_and_counts_their_tools(tmp_path):
    from types import SimpleNamespace

    home = _home(tmp_path)
    bridge = SimpleNamespace(connected=lambda name: name == "waku_memory")
    live = SimpleNamespace(mcp_bridge=bridge)
    tools = [SimpleNamespace(name=n) for n in ("waku_memory_memory_search", "waku_memory_memory_get", "save_note")]
    row = dashboard._mcp_row(home, dashboard.mcp_servers(home)[0], live, tools)
    assert row["connected"] is True and row["tools"] == 2


def test_every_hook_moment_is_listed_with_its_last_run(tmp_path):
    home = _home(tmp_path)
    events = [
        {"type": "hook", "plugin": "waku", "event": "Stop", "tool": "session.hook",
         "ok": False, "duration_ms": 900, "ts": "2026-11-13T17:00:00", "error": "timed out"},
        {"type": "hook", "plugin": "waku", "event": "Stop", "tool": "session.hook",
         "ok": True, "duration_ms": 310, "ts": "2026-11-13T17:19:00"},
        {"type": "tool", "tool": "save_note"},
    ]
    info = dashboard.hooks_info(home, events)
    by_event = {r["event"]: r for r in info["rows"]}
    assert list(by_event) == ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]
    assert info["count"] == 3
    # the newest run wins, and a hook that never ran says so
    assert by_event["Stop"]["last"] == {"ok": True, "duration_ms": 310,
                                        "ts": "2026-11-13T17:19:00", "error": None}
    assert by_event["SessionStart"]["last"] is None
    assert by_event["SessionStart"]["plugin"] == "waku"
    assert by_event["SessionStart"]["server"] == "waku_memory"
    assert by_event["SessionStart"]["tool"] == "session.hook"
    # the Waku plugin declares no PreToolUse, and the row is still there
    assert by_event["PreToolUse"] == {"event": "PreToolUse", "plugin": None}


def test_no_plugin_means_four_empty_moments(tmp_path):
    info = dashboard.hooks_info(_home(tmp_path, plugin=False))
    assert info["count"] == 0 and info["skipped"] == []
    assert [r["plugin"] for r in info["rows"]] == [None] * 4


def test_the_memory_page_is_told_whether_waku_memory_is_connected(tmp_path):
    connected = dashboard.waku_memory_info(Settings(home=_home(tmp_path), memory_mode="plugin"))
    assert connected == {"connected": True, "account": "not signed in",
                         "by_plugin": True, "mode": "plugin"}

    alone = dashboard.waku_memory_info(Settings(home=_home(tmp_path / "b", plugin=False), memory_mode=""))
    assert alone == {"connected": False, "account": None, "by_plugin": False, "mode": ""}


def test_the_tools_page_has_a_hooks_tab_and_the_memory_page_its_strip():
    from pathlib import Path

    views = (Path(dashboard.__file__).parent / "static" / "js" / "views.js").read_text(encoding="utf-8")
    assert '["hooks","Hooks"' in views and "function toolsHooks(t)" in views
    assert "function memWhich(d)" in views and "memWhich(d) + uiNotice" in views
    # the MCP tab reads the merged rows, not mcp.json's names alone
    assert "m.rows" in views
