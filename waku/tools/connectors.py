"""Connectors: a person adds their own MCP server in one step (spec 025).

A connector is one entry in WAKU_HOME/mcp.json, the file mcp_client.py reads.
`add` writes the entry and `remove` deletes it, and three surfaces call them:

    waku mcp add linear https://mcp.linear.app/mcp     the command line
    connect_tool("https://mcp.linear.app/mcp")         chat, after the owner's yes
    the "Connectors" card                              the dashboard

A remote server signs in one of two ways. `oauth` is the browser flow on the
server's own page (mcp_oauth.py). The other is a key the person holds in an
environment variable: the entry names the variable and never holds the key,
because mcp.json is a file people paste into bug reports.

A local server is a command this machine runs. Only the command line adds
one: a chat message or a web page that makes Waku run a program is the attack
that rule exists to stop.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

OAUTH, NONE = "oauth", "none"
# A server's name becomes the prefix of its tools (`linear_create_issue`) and
# the name of its token file, so it is one short plain word.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# Plain http is for a server on this machine only.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class Refused(ValueError):
    """Why an entry was not written or removed, in words for the person."""


@dataclass(frozen=True)
class Added:
    """What `add` wrote, and what the person does next."""
    name: str
    spec: dict
    auth: str    # "oauth", "none", or the name of an environment variable
    next: str


def _config(home: Path) -> Path:
    return Path(home) / "mcp.json"


def _read(home: Path) -> dict:
    """mcp.json as a dictionary with a `servers` list. A file that does not
    parse is refused and left alone: it may hold servers a rewrite would
    lose."""
    path = _config(home)
    if not path.exists():
        return {"servers": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refused(f"{path} is not valid JSON. Fix or delete it first.") from exc
    if not isinstance(data, dict) or not isinstance(data.setdefault("servers", []), list):
        raise Refused(f"{path} does not hold a list of servers. Fix or delete it first.")
    return data


def _write(home: Path, data: dict) -> None:
    """Write to a second file and rename it, so a write that is interrupted
    leaves the file that was there."""
    path = _config(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _plugin_servers(home: Path) -> list[dict]:
    from waku.plugins import store

    try:
        return [s for s in store.servers(Path(home)) if isinstance(s, dict)]
    except (OSError, ValueError):
        return []


def _same(url: object) -> str:
    return str(url or "").rstrip("/")


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.match(name):
        raise Refused("A connector's name is one word of letters, digits, '-' and '_', "
                      "32 characters at most, for example 'linear'.")
    return name


def check_url(url: str) -> str:
    """The address as it is written to the file: https, or http on this
    machine, with a host and with no credential inside it."""
    url = (url or "").strip()
    try:
        parts = urlparse(url)
        host = parts.hostname or ""
    except ValueError:
        parts, host = None, ""
    if parts is None or not host or parts.scheme not in ("https", "http"):
        raise Refused("A connector's address starts with https:// and names a host, "
                      "for example https://mcp.linear.app/mcp.")
    if parts.username or parts.password:
        raise Refused("The address holds a credential. Give the address alone, and name "
                      "an environment variable for the key.")
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise Refused("A connector's address starts with https://. Plain http is for a "
                      "server on this machine only.")
    return url


def _next(name: str, auth: str) -> str:
    if auth == OAUTH:
        return f"Sign in on the computer Waku runs on: waku mcp login {name}"
    if auth == NONE:
        return "Restart Waku to load its tools."
    return (f"Set {auth} in .env on the computer Waku runs on, then restart Waku "
            "to load its tools.")


def add(home: Path, name: str, url: str | None = None, command: str | None = None,
        args: list[str] | None = None, auth: str | None = None) -> Added:
    """Write one entry to mcp.json and say what the person does next.

    `url` is a remote server and `command` is a local one; exactly one is
    given. `auth` is for a remote server: "oauth" (the default), "none", or
    the name of the environment variable that holds its key. A name or an
    address that is already there is refused, and nothing is written.
    """
    name = check_name(name)
    if bool(url) == bool(command):
        raise Refused("Give one of the two: an address (https://...) for a remote server, "
                      "or a command for a server this machine runs.")
    if url:
        url = check_url(url)
        auth = (auth or OAUTH).strip()
        if auth not in (OAUTH, NONE) and not _ENV_NAME.match(auth):
            # What was given may be the key itself, so it is not repeated.
            raise Refused("Name the environment variable that holds the key, for example "
                          "LINEAR_API_KEY, not the key. Put the key in .env.")
        spec: dict = {"name": name, "url": url}
        if auth == OAUTH:
            spec["oauth"] = True
        elif auth != NONE:
            spec["auth_env"] = auth
    else:
        if auth not in (None, "", NONE):
            raise Refused("A server this machine runs has no sign-in. Leave the key out.")
        auth = NONE
        spec = {"name": name, "command": str(command).strip()}
        if args:
            spec["args"] = [str(a) for a in args]

    data = _read(home)
    for existing in data["servers"]:
        if not isinstance(existing, dict):
            continue
        if existing.get("name") == name:
            raise Refused(f"A connector called '{name}' is already in {_config(home)}. "
                          f"Remove it first (waku mcp remove {name}) or pick another name.")
        if url and _same(existing.get("url")) == _same(url):
            raise Refused(f"{url} is already connected as '{existing.get('name')}'.")
    for brought in _plugin_servers(home):
        if brought.get("name") == name:
            raise Refused(f"The plugin '{brought.get('plugin') or '?'}' already brings a "
                          f"server called '{name}'. Pick another name.")

    data["servers"].append(spec)
    _write(home, data)
    return Added(name, spec, auth, _next(name, auth))


def remove(home: Path, name: str) -> dict:
    """Delete one entry from mcp.json, and the token its sign-in left in
    mcp-auth/. Returns the entry that was removed."""
    name = (name or "").strip()
    data = _read(home)
    found = next((s for s in data["servers"]
                  if isinstance(s, dict) and s.get("name") == name), None)
    if found is None:
        brought = next((s for s in _plugin_servers(home) if s.get("name") == name), None)
        if brought is not None:
            raise Refused(f"'{name}' comes with the plugin '{brought.get('plugin') or '?'}'. "
                          "Remove the plugin instead: waku plugin for the commands.")
        names = ", ".join(str(s.get("name")) for s in data["servers"] if isinstance(s, dict))
        raise Refused(f"No connector called '{name}' in {_config(home)}. "
                      f"Connected: {names or 'none'}.")
    data["servers"] = [s for s in data["servers"] if s is not found]
    _write(home, data)

    from waku.tools.mcp_oauth import AUTH_DIR, _sanitise

    (Path(home) / AUTH_DIR / f"{_sanitise(name)}.json").unlink(missing_ok=True)
    return found
