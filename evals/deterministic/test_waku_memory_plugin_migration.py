"""Spec 021 D3: a person who connected Waku Memory with `waku connect
waku-memory` moves to the plugin without signing in again. An entry with
`auth_env`, the shape hosted provisioning writes, is left as it is."""

from __future__ import annotations

import json

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, marketplace
from waku.plugins import cli, store
from waku.tools import mcp_client


def _home(tmp_path, servers):
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")
    (home / "mcp-auth").mkdir()
    token = home / "mcp-auth" / "waku_memory.json"
    token.write_text('{"tokens": {"access_token": "kept"}}', encoding="utf-8")
    cli.run(["marketplace", "add", str(marketplace(tmp_path / "market"))], home, lambda s: False)
    return home, token


def _servers(home):
    return json.loads((home / "mcp.json").read_text(encoding="utf-8"))["servers"]


def test_an_oauth_entry_moves_to_the_plugin_and_the_token_stays(tmp_path):
    fs = {"name": "fs", "command": "npx"}
    home, token = _home(tmp_path, [{"name": "waku_memory", "url": WAKU_MEMORY_URL, "oauth": True}, fs])
    reply = cli.run(["install", "waku@waku-memory"], home, lambda s: True)
    assert "Moved waku_memory from mcp.json to the plugin" in reply
    assert "Sign in to waku_memory" not in reply
    assert _servers(home) == [fs]
    assert token.read_text(encoding="utf-8") == '{"tokens": {"access_token": "kept"}}'
    merged, skipped = mcp_client.merge(_servers(home), store.servers(home))
    assert [s["name"] for s in merged] == ["fs", "waku_memory"] and skipped == []


def test_an_auth_env_entry_is_left_alone_and_wins(tmp_path):
    hosted = {"name": "waku_memory", "url": WAKU_MEMORY_URL, "auth_env": "WAKU_MEMORY_API_KEY"}
    home, _ = _home(tmp_path, [hosted])
    reply = cli.run(["install", "waku@waku-memory"], home, lambda s: True)
    assert "Moved" not in reply and _servers(home) == [hosted]
    merged, _ = mcp_client.merge(_servers(home), store.servers(home))
    assert merged == [hosted]


def test_an_entry_at_another_address_is_not_moved(tmp_path):
    mine = {"name": "waku_memory", "url": "https://my-own-waku.example/mcp", "oauth": True}
    home, _ = _home(tmp_path, [mine])
    cli.run(["install", "waku@waku-memory"], home, lambda s: True)
    assert _servers(home) == [mine]


def test_a_declined_install_moves_nothing(tmp_path):
    entry = {"name": "waku_memory", "url": WAKU_MEMORY_URL, "oauth": True}
    home, _ = _home(tmp_path, [entry])
    cli.run(["install", "waku@waku-memory"], home, lambda s: False)
    assert _servers(home) == [entry]
