"""What is installed, and what is enabled (spec 021).

    <home>/plugins/installed.json      {"plugins": {"waku@waku-memory": {...}}}
    <home>/plugins/<marketplace>/<name>/   one plugin's folder, as copied
    <home>/plugins/install_id          this install's id, for hook session ids

Reading these files opens nothing and connects nothing, so `waku connections`
and the first-run question can ask "is the Waku plugin installed?" freely.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from waku.plugins import manifest
from waku.plugins.manifest import Plugin

DIR = "plugins"
INSTALLED = "installed.json"


def folder(home: Path) -> Path:
    return Path(home) / DIR


def read(home: Path) -> dict:
    """{key: record} for every installed plugin, enabled or not. A key is
    `<name>@<marketplace>`; a record holds name, marketplace, path (relative
    to <home>/plugins), ref and enabled."""
    try:
        data = json.loads((folder(home) / INSTALLED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def write(home: Path, plugins: dict) -> None:
    path = folder(home) / INSTALLED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": plugins}, indent=2) + "\n", encoding="utf-8")


def enabled(home: Path) -> list[Plugin]:
    """Every enabled plugin whose folder still loads, in install order."""
    out = []
    for record in read(home).values():
        if not isinstance(record, dict) or not record.get("enabled", True):
            continue
        plugin = manifest.load(folder(home) / str(record.get("path", "")))
        if plugin is not None:
            out.append(plugin)
    return out


def servers(home: Path) -> list[dict]:
    """The MCP servers every enabled plugin brings, in `mcp.json`'s shape."""
    return [server for plugin in enabled(home) for server in plugin.servers]


def install_id(home: Path) -> str:
    """A random id for this install, made once. A hook's session id carries
    it, so two installs of one person never share a session on the server."""
    path = folder(home) / "install_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if not value:
        value = secrets.token_hex(8)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return value
