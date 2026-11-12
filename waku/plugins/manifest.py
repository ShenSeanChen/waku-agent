"""One plugin folder, read into a Plugin (spec 021 A1).

Waku reads its own manifest, `.waku-plugin/plugin.json`, first. A folder
without one is read as a Claude Code plugin: `.claude-plugin/plugin.json`
for its name, `.mcp.json` for its servers and `skills/` for its skills. Its
hooks are skipped, and `hooks_skipped` says so for `waku plugin list`.

Servers are turned into the shape `<home>/mcp.json` already uses, so the MCP
bridge connects a plugin's server the way it connects the person's own:

  {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": true,
   "plugin": "waku"}

A remote server that names no `auth_env` and no `headers` signs in with
OAuth, so a plugin does not have to say `oauth: true`. Tokens are kept in
<home>/mcp-auth/<server id>.json, outside the plugin folder, so updating or
removing a plugin leaves a sign-in alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

WAKU_MANIFEST = Path(".waku-plugin") / "plugin.json"
CLAUDE_MANIFEST = Path(".claude-plugin") / "plugin.json"

# The four events Waku runs (spec 021). Any other event in a hooks file is
# kept out of `hooks`, so nothing waits on an event that never fires.
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")


@dataclass
class Plugin:
    name: str
    root: Path
    kind: str                      # "waku" or "claude": which manifest was read
    version: str = ""
    description: str = ""
    servers: list[dict] = field(default_factory=list)
    hooks: dict[str, list[dict]] = field(default_factory=dict)
    skills_dir: Path | None = None
    hooks_skipped: bool = False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _inside(root: Path, relative: str) -> Path | None:
    """`relative` resolved under `root`, or None when it points outside it.
    A manifest is somebody else's file, and `../../.ssh` is a path too."""
    target = (root / relative).resolve()
    return target if target == root.resolve() or root.resolve() in target.parents else None


def server_entry(server_id: str, spec: dict, plugin: str) -> dict | None:
    """One `mcpServers` entry in `mcp.json`'s shape, or None when it names
    neither a URL nor a command."""
    if not isinstance(spec, dict):
        return None
    entry: dict = {"name": server_id, "plugin": plugin}
    if spec.get("url"):
        entry["url"] = str(spec["url"])
        if spec.get("auth_env"):
            entry["auth_env"] = str(spec["auth_env"])
        elif isinstance(spec.get("headers"), dict) and spec["headers"]:
            entry["headers"] = {str(k): str(v) for k, v in spec["headers"].items()}
        else:
            entry["oauth"] = True
        return entry
    if spec.get("command"):
        entry["command"] = str(spec["command"])
        entry["args"] = [str(a) for a in spec.get("args") or []]
        if isinstance(spec.get("env"), dict):
            entry["env"] = {str(k): str(v) for k, v in spec["env"].items()}
        return entry
    return None


def _servers(block, root: Path, plugin: str) -> list[dict]:
    """`mcpServers` is an object of servers, or a path to a file that holds
    one (Claude Code's `.mcp.json` wraps it in {"mcpServers": ...})."""
    if isinstance(block, str):
        path = _inside(root, block)
        block = _read_json(path) if path else {}
    if isinstance(block, dict) and isinstance(block.get("mcpServers"), dict):
        block = block["mcpServers"]
    if not isinstance(block, dict):
        return []
    out = []
    for server_id, spec in block.items():
        entry = server_entry(str(server_id), spec, plugin)
        if entry is not None:
            out.append(entry)
    return out


def _hooks(block, root: Path) -> dict[str, list[dict]]:
    """Claude Code's hooks shape: {"hooks": {event: [{"matcher", "hooks": [...]}]}}.
    Matchers are not read in version 1, so every hook of an event is kept."""
    if isinstance(block, str):
        path = _inside(root, block)
        block = _read_json(path) if path else {}
    if isinstance(block, dict) and isinstance(block.get("hooks"), dict):
        block = block["hooks"]
    if not isinstance(block, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for event, groups in block.items():
        if event not in EVENTS or not isinstance(groups, list):
            continue
        for group in groups:
            for hook in (group.get("hooks") if isinstance(group, dict) else None) or []:
                if isinstance(hook, dict):
                    out.setdefault(event, []).append(hook)
    return out


def _skills(value, root: Path) -> Path | None:
    path = _inside(root, value) if isinstance(value, str) else root / "skills"
    return path if path is not None and path.is_dir() else None


def load(root: Path) -> Plugin | None:
    """The plugin in `root`, or None when the folder holds neither manifest."""
    root = Path(root)
    manifest = _read_json(root / WAKU_MANIFEST)
    if manifest.get("name"):
        name = str(manifest["name"])
        return Plugin(
            name=name, root=root, kind="waku",
            version=str(manifest.get("version", "")),
            description=str(manifest.get("description", "")),
            servers=_servers(manifest.get("mcpServers", {}), root, name),
            hooks=_hooks(manifest.get("hooks", {}), root),
            skills_dir=_skills(manifest.get("skills"), root))
    manifest = _read_json(root / CLAUDE_MANIFEST)
    if not manifest.get("name"):
        return None
    name = str(manifest["name"])
    servers = manifest.get("mcpServers")
    return Plugin(
        name=name, root=root, kind="claude",
        version=str(manifest.get("version", "")),
        description=str(manifest.get("description", "")),
        servers=_servers(servers if servers is not None else ".mcp.json", root, name),
        skills_dir=_skills(manifest.get("skills"), root),
        hooks_skipped=bool(manifest.get("hooks") or (root / "hooks").is_dir()))
