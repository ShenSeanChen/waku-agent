"""Plugin folders and a marketplace for the spec 021 evals, written into a
temporary folder. The Waku plugin here has the shape waku-memory spec 086
gives the real one: one server, `waku_memory`, and three `mcp_tool` hooks
that call `session.hook` with `harness: "waku"`."""

from __future__ import annotations

import json
from pathlib import Path

WAKU_MEMORY_URL = "https://api.waku.one/mcp"


def _hook(event_fields: dict) -> dict:
    return {"type": "mcp_tool", "server": "waku_memory", "tool": "session.hook",
            "timeout": 10, "input": {"harness": "waku", "event": "${hook_event_name}",
                                     "session_id": "${session_id}", "cwd": "${cwd}",
                                     "turn_id": "${turn_id}", **event_fields}}


def waku_plugin(root: Path) -> Path:
    """The Waku plugin as Waku Agent reads it, with the Claude Code files beside."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".waku-plugin").mkdir(exist_ok=True)
    (root / ".waku-plugin" / "plugin.json").write_text(json.dumps({
        "name": "waku", "version": "0.15.0",
        "description": "Waku Memory: what you type and what the agent replies is sent to Waku's servers.",
        "mcpServers": {"waku_memory": {"type": "http", "url": WAKU_MEMORY_URL}},
        "hooks": "./hooks/waku-agent.json",
        "skills": "./skills/",
    }), encoding="utf-8")
    (root / "hooks").mkdir(exist_ok=True)
    (root / "hooks" / "waku-agent.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [_hook({"source": "${source}"})]}],
        "UserPromptSubmit": [{"hooks": [_hook({"prompt": "${prompt}"})]}],
        "Stop": [{"hooks": [_hook({"last_assistant_message": "${last_assistant_message}"})]}],
    }}), encoding="utf-8")
    (root / "hooks" / "claude-code.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{**_hook({}), "server": "plugin:waku:waku"}]}]}}), encoding="utf-8")
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "waku", "hooks": "./hooks/claude-code.json", "skills": "./skills/"}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "waku": {"type": "http", "url": WAKU_MEMORY_URL}}}), encoding="utf-8")
    skill = root / "skills" / "waku"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: waku\ndescription: Search Waku Memory before answering about a past decision.\n"
        "triggers: [waku memory, past decision]\n---\nCall memory.search first.\n", encoding="utf-8")
    return root


def claude_only_plugin(root: Path) -> Path:
    """A plugin written for Claude Code alone: no Waku manifest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "docs", "version": "1.0.0", "hooks": "./hooks/hooks.json"}), encoding="utf-8")
    (root / "hooks").mkdir(exist_ok=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "mcp_tool", "server": "docs", "tool": "t"}]}]}}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"type": "http", "url": "https://docs.example/mcp"},
        "local": {"command": "npx", "args": ["-y", "docs-server"]},
        "keyed": {"type": "http", "url": "https://keyed.example/mcp",
                  "headers": {"X-Key": "abc"}}}}), encoding="utf-8")
    return root


def marketplace(root: Path, name: str = "waku-memory") -> Path:
    """Named waku-memory by default, like the real one. Waku refuses to record
    that name for another source, so a test stands it in with official()."""
    """A marketplace repository with the Waku plugin at ./plugins/waku."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": name, "plugins": [
            {"name": "waku", "source": "./plugins/waku"},
            {"name": "docs", "source": "./plugins/docs"},
            {"name": "escape", "source": "../outside"}]}), encoding="utf-8")
    waku_plugin(root / "plugins" / "waku")
    claude_only_plugin(root / "plugins" / "docs")
    return root


def install_into(home: Path, plugin_root: Path, marketplace_name: str = "waku-memory",
                 enabled: bool = True) -> str:
    """Put a plugin folder in <home>/plugins the way `waku plugin install`
    leaves it, without a marketplace. Returns its key."""
    import shutil

    from waku.plugins import manifest, store

    plugin = manifest.load(plugin_root)
    dest = store.folder(home) / marketplace_name / plugin.name
    shutil.copytree(plugin_root, dest, dirs_exist_ok=True)
    key = f"{plugin.name}@{marketplace_name}"
    records = store.read(home)
    records[key] = {"name": plugin.name, "marketplace": marketplace_name,
                    "path": f"{marketplace_name}/{plugin.name}", "ref": "main", "enabled": enabled}
    store.write(home, records)
    return key


def official(monkeypatch, market_root: Path) -> None:
    """Point the built-in waku-memory marketplace at a local folder, for the
    length of one test."""
    from waku.plugins import marketplace as market

    monkeypatch.setitem(market.KNOWN, "waku-memory", {"path": str(Path(market_root).resolve())})
