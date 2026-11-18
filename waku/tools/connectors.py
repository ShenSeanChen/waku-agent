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
import threading
from collections.abc import Callable
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


def connected_line(name: str, tools: list) -> str:
    """What the person reads when a server has connected."""
    count = len(tools)
    return f"{name} is connected: {count} tool{'' if count == 1 else 's'}."


# ---- the sign-in link ------------------------------------------------------

# How long `start` waits for the server to hand over its sign-in link: the
# server is found, this client registers, and the link comes back.
LINK_WAIT = 20.0


def _bridge(home: Path, name: str, open_url: Callable[[str], None]):
    """The MCP bridge of one sign-in: it connects the one server, and hands
    the sign-in link to `open_url` where it would open a browser."""
    from waku.plugins import store
    from waku.tools.mcp_client import MCPBridge

    return MCPBridge(_config(home), skip=lambda spec: spec.get("name") != name,
                     extra_servers=store.servers(Path(home)), open_url=open_url)


class SignIn:
    """One sign-in to a server that uses `oauth`, started without a browser.

    `waku mcp login` opens a browser on this machine. A chat reply and the
    dashboard card cannot, so this hands back the link (spec 025, decision
    2). The person opens it on the computer Waku runs on, because the server
    sends the browser back to a port on that computer. The sign-in then
    finishes on a thread of its own, and `on_done` hears about it.

      link    the address to open, or None: the server asked for no sign-in,
              or the sign-in failed before it had a link
      done    set when the sign-in ended, either way
      ok      whether the server connected
      tools   the tools the server listed, or None
    """

    def __init__(self, home: Path, name: str, on_done: Callable[[SignIn], None] | None = None,
                 bridge: Callable = _bridge) -> None:
        self.home, self.name = Path(home), name
        self.link: str | None = None
        self.tools: list | None = None
        self.ok = False
        self.done = threading.Event()
        self._has_link = threading.Event()
        self._on_done, self._make_bridge = on_done, bridge
        self._bridge = None

    def _open(self, link: str) -> None:
        self.link = link
        self._has_link.set()

    def _run(self) -> None:
        try:
            self._bridge = self._make_bridge(self.home, self.name, self._open)
            self.tools = list(self._bridge.start())
            # A server that failed to connect is skipped by the bridge, so no
            # tools and no session is how a failure looks from here.
            connected = getattr(self._bridge, "connected", None)
            self.ok = bool(self.tools) or bool(connected and connected(self.name))
        except Exception:   # noqa: BLE001  (the reason may quote a token, so it is dropped)
            self.tools, self.ok = None, False
        finally:
            if self._bridge is not None:
                try:
                    self._bridge.close()
                except Exception:   # noqa: BLE001
                    pass
            if not self.ok:
                self.link = None
            self.done.set()
            self._has_link.set()
            if self._on_done is not None:
                self._on_done(self)

    def start(self, wait: float = LINK_WAIT) -> SignIn:
        """Start the sign-in and return once it has a link, has ended, or
        `wait` seconds have passed."""
        threading.Thread(target=self._run, daemon=True, name=f"sign-in-{self.name}").start()
        self._has_link.wait(wait)
        return self

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the sign-in to end. True when it did."""
        return self.done.wait(timeout)


# The redirect comes back to one fixed port (mcp_oauth.CALLBACK_PORT), so one
# sign-in is in flight at a time in this process.
_SIGN_INS: dict[tuple[str, str], SignIn] = {}
_SIGN_INS_LOCK = threading.Lock()


def start_sign_in(home: Path, name: str, on_done: Callable[[SignIn], None] | None = None,
                  bridge: Callable = _bridge, wait: float = LINK_WAIT) -> SignIn:
    """The sign-in for this server: a new one, or the one already waiting for
    the person, which carries the same link."""
    key = (str(Path(home)), name)
    with _SIGN_INS_LOCK:
        for other, flow in list(_SIGN_INS.items()):
            if flow.done.is_set():
                del _SIGN_INS[other]
        if key in _SIGN_INS:
            return _SIGN_INS[key]
        if _SIGN_INS:
            waiting = next(iter(_SIGN_INS))[1]
            raise Refused(f"The sign-in to '{waiting}' is still waiting. Finish it, or "
                          "try again in five minutes.")
        flow = _SIGN_INS[key] = SignIn(home, name, on_done=on_done, bridge=bridge)
    return flow.start(wait)


def sign_in_waiting(home: Path) -> SignIn | None:
    """The sign-in in this home that still waits for the person, if any."""
    with _SIGN_INS_LOCK:
        return next((flow for (where, _), flow in _SIGN_INS.items()
                     if where == str(Path(home)) and not flow.done.is_set()), None)


def forget_sign_ins() -> None:
    """Drop every sign-in this process remembers. For the evals."""
    with _SIGN_INS_LOCK:
        _SIGN_INS.clear()
