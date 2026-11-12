"""Spec 021 A1: one plugin folder, read. Waku's own manifest wins; a Claude
Code plugin loads its servers and skills and skips its hooks."""

from __future__ import annotations

import json

from evals.deterministic.plugin_fixtures import WAKU_MEMORY_URL, claude_only_plugin, waku_plugin
from waku.plugins import manifest


def test_the_waku_manifest_wins_over_the_claude_code_one(tmp_path):
    plugin = manifest.load(waku_plugin(tmp_path / "waku"))
    assert plugin.kind == "waku" and plugin.name == "waku" and plugin.version == "0.15.0"
    # the Waku manifest's server id, not .mcp.json's `waku`
    assert plugin.servers == [{"name": "waku_memory", "plugin": "waku",
                               "url": WAKU_MEMORY_URL, "oauth": True}]
    assert sorted(plugin.hooks) == ["SessionStart", "Stop", "UserPromptSubmit"]
    stop = plugin.hooks["Stop"][0]
    assert stop["input"]["harness"] == "waku" and stop["server"] == "waku_memory"
    assert plugin.skills_dir == (tmp_path / "waku" / "skills").resolve()
    assert plugin.hooks_skipped is False


def test_a_claude_code_plugin_loads_servers_and_skips_its_hooks(tmp_path):
    plugin = manifest.load(claude_only_plugin(tmp_path / "docs"))
    assert plugin.kind == "claude" and plugin.hooks == {} and plugin.hooks_skipped is True
    by_name = {s["name"]: s for s in plugin.servers}
    assert by_name["docs"] == {"name": "docs", "plugin": "docs",
                               "url": "https://docs.example/mcp", "oauth": True}
    assert by_name["local"] == {"name": "local", "plugin": "docs", "command": "npx",
                                "args": ["-y", "docs-server"]}
    # a server that brings its own header does not sign in with OAuth
    assert by_name["keyed"]["headers"] == {"X-Key": "abc"} and "oauth" not in by_name["keyed"]


def test_an_auth_env_server_keeps_its_key_and_gets_no_oauth():
    entry = manifest.server_entry("s", {"url": "https://x/mcp", "auth_env": "KEY"}, "p")
    assert entry == {"name": "s", "plugin": "p", "url": "https://x/mcp", "auth_env": "KEY"}


def test_a_folder_with_no_manifest_is_no_plugin(tmp_path):
    assert manifest.load(tmp_path) is None


def test_a_manifest_path_outside_the_plugin_is_not_read(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"mcpServers": {"x": {"url": "https://evil/mcp"}}}), encoding="utf-8")
    root = tmp_path / "p"
    (root / ".waku-plugin").mkdir(parents=True)
    (root / ".waku-plugin" / "plugin.json").write_text(json.dumps({
        "name": "p", "mcpServers": "../secret.json", "hooks": "../secret.json"}), encoding="utf-8")
    plugin = manifest.load(root)
    assert plugin.servers == [] and plugin.hooks == {}


def test_only_the_four_events_are_kept(tmp_path):
    root = tmp_path / "p"
    (root / ".waku-plugin").mkdir(parents=True)
    hook = {"type": "mcp_tool", "server": "s", "tool": "t"}
    (root / ".waku-plugin" / "plugin.json").write_text(json.dumps({"name": "p", "hooks": {
        "PreCompact": [{"hooks": [hook]}], "PreToolUse": [{"hooks": [hook]}]}}), encoding="utf-8")
    assert list(manifest.load(root).hooks) == ["PreToolUse"]
