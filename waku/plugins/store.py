"""What is installed, and what is enabled (spec 021).

    <home>/plugins/installed.json      {"plugins": {"waku@waku-memory": {...}}}
    <home>/plugins/<marketplace>/<name>/   one plugin's folder, as copied
    <home>/plugins/install_id          this install's id, for hook session ids
    <home>/plugins/capture.json        the last capture state the person was told about

Reading these files opens nothing and connects nothing, so `waku connections`
and the first-run question can ask "is the Waku plugin installed?" freely.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from waku.plugins import manifest
from waku.plugins.manifest import Plugin

DIR = "plugins"
INSTALLED = "installed.json"

# A plugin's or a marketplace's name becomes a folder name under
# <home>/plugins, so it may hold only these characters and may not start with
# a dot: "../../x" in somebody's catalog is a name too.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class UnsafePath(ValueError):
    """A name or a recorded path that would reach outside <home>/plugins."""


def folder(home: Path) -> Path:
    return Path(home) / DIR


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME.match(name)) and name not in (".", "..")


def inside(home: Path, relative: str) -> Path:
    """<home>/plugins/<relative>, or UnsafePath when it resolves anywhere else.
    Checked before every copy and every delete, because both remove a folder
    first, and a folder outside <home>/plugins is somebody's data."""
    root = folder(home).resolve()
    target = (folder(home) / str(relative)).resolve()
    if not relative or root not in target.parents:
        raise UnsafePath(f"'{relative}' is not a folder inside {root}")
    return target


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
        try:
            path = inside(home, str(record.get("path", "")))
        except UnsafePath:
            continue
        plugin = manifest.load(path)
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


CAPTURE = "capture.json"


def capture_told(home: Path) -> str | None:
    """The capture state the person was last told about (spec 021 amendment
    E3), or None when none was recorded or the file cannot be read."""
    try:
        data = json.loads((folder(home) / CAPTURE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = data.get("state") if isinstance(data, dict) else None
    return state if isinstance(state, str) else None


def save_capture_told(home: Path, state: str) -> None:
    """Record `state` as told, in one step: the file is written beside its
    place and moved over it, so a reader never sees half of it. Only the
    turn's main thread calls this; the Stop thread never does."""
    path = folder(home) / CAPTURE
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"state": state, "at": datetime.now(UTC).isoformat(timespec="seconds")})
    # a name of its own per write, so two writers never share a temporary file,
    # and the file is removed whatever happens, so a failed write leaves none
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix="capture.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(body)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
