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
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from waku.tools.registry import Tool

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


def starter() -> dict[str, dict]:
    """The starter list, waku/connectors.toml: one table per connector, by
    name. The file says what each field holds."""
    path = Path(__file__).resolve().parents[1] / "connectors.toml"
    with path.open("rb") as handle:
        return tomllib.load(handle)


def starter_for(spec: dict) -> dict | None:
    """The starter entry a server in mcp.json is, or None. A server counts by
    its address, never by its name: a person may name any server 'linear'."""
    url = _same(spec.get("url")) if isinstance(spec, dict) else ""
    return next((entry for entry in starter().values() if url and _same(entry["url"]) == url),
                None)


MEMORY, STARTER, OWN = "memory", "starter", "own"


@dataclass(frozen=True)
class Connector:
    """What the approval rules need to know about the server a tool comes
    from (alwayson/approvals.py, `mode`).

      memory    Waku Memory: its tools run without a question
      starter   a server on the starter list: `writes` says which tools ask
      own       any other server: a tool asks unless the server marks it
                read-only
    """
    name: str
    kind: str
    writes: tuple[str, ...] = ()


def connector_of(home: Path, server: str) -> Connector | None:
    """The connector a server in mcp.json, or one a plugin brings, is. None
    when no such server is configured."""
    from waku.tools.mcp_client import merge
    from waku.tools.waku_memory import is_waku_memory

    try:
        config = _read(home)["servers"]
    except Refused:
        config = []
    spec = next((s for s in merge(config, _plugin_servers(home))[0]
                 if s.get("name") == server), None)
    if spec is None:
        return None
    if is_waku_memory(spec):
        return Connector(server, MEMORY)
    listed = starter_for(spec)
    if listed is not None:
        return Connector(server, STARTER, tuple(listed["writes"]))
    return Connector(server, OWN)


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
    listed = None
    if not url and not command:
        # A name alone is a name on the starter list.
        listed = starter().get((name or "").strip().lower())
        if listed is None:
            raise Refused(f"'{(name or '').strip()}' is not on the starter list "
                          f"({', '.join(starter())}). Give its address: "
                          f"waku mcp add {(name or '').strip() or '<name>'} <https address>")
        name, url, auth = listed["name"], listed["url"], auth or listed["auth"]
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
    step = _next(name, auth)
    if listed is not None and auth not in (OAUTH, NONE):
        step += f" Where the key comes from: {listed['docs']}"
    return Added(name, spec, auth, step)


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
    # The rule that let this connector write goes with it: a server added
    # later under the same name starts by asking.
    if (Path(home) / "state.db").exists():
        _store_rule(home, name, allow=False)
    return found


# ---- the owner lets one connector write (decision 4) ---------------------------

def _store_rule(home: Path, name: str, allow: bool) -> None:
    from waku.alwayson import approvals
    from waku.db import connect as open_db

    conn = open_db(Path(home))
    try:
        approvals.store(conn, name, allow)
    finally:
        conn.close()


def writes_allowed(home: Path) -> frozenset[str]:
    """The names of the connectors the owner let write without asking."""
    from waku.alwayson import approvals
    from waku.db import connect as open_db

    if not (Path(home) / "state.db").exists():
        return frozenset()
    conn = open_db(Path(home))
    try:
        rules = approvals.stored(conn)
    finally:
        conn.close()
    return frozenset(rule[:-2] for rule in rules if rule.endswith("_*"))


def set_writes(home: Path, name: str, allow: bool) -> str:
    """Let every tool of one connector run without a question, or make its
    writes ask again. Returns what the person reads."""
    name = (name or "").strip()
    listed = starter().get(name.lower())
    if connector_of(home, name) is None and listed is not None:
        name = listed["name"]   # "Linear" for the server named linear
    if connector_of(home, name) is None:
        raise Refused(f"no connector called '{name}' is connected.")
    _store_rule(home, name, allow)
    if allow:
        return (f"{name}'s tools now run without asking first. To take that back, "
                f"say: make {name} ask before it writes.")
    return f"{name}'s tools that write ask first again."


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
            try:
                if self._on_done is not None:
                    self._on_done(self)
            finally:
                self.done.set()
                self._has_link.set()

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


# ---- which servers connect without a browser ---------------------------------

def signed_in(home: Path, spec: dict) -> bool:
    """Whether a sign-in has left a token for this `oauth` server."""
    from waku.tools.mcp_oauth import FileTokenStorage

    storage = FileTokenStorage(Path(home), str(spec.get("name", "")), spec.get("url"))
    return storage._path.exists() and storage.matches()


def needs_sign_in(home: Path, spec: dict) -> bool:
    """Whether connecting this server would open a browser first."""
    return bool(isinstance(spec, dict) and spec.get("oauth")) and not signed_in(home, spec)


def ready(home: Path) -> frozenset[str]:
    """The names of the servers in mcp.json that connect without a browser:
    every server but an `oauth` one nobody has signed in to. The always-on
    agent is built again when this set changes, so a server the owner just
    signed in to has its tools in the next run."""
    try:
        servers = _read(home)["servers"]
    except Refused:
        return frozenset()
    return frozenset(str(s.get("name")) for s in servers
                     if isinstance(s, dict) and s.get("name") and not needs_sign_in(home, s))


# ---- chat: connect_tool and disconnect_tool ----------------------------------

# A label a host starts with that says nothing about whose server it is.
_HOST_NOISE = ("mcp", "api", "www", "app")


def name_for(url: str) -> str:
    """A name for a server nobody named: `example` for mcp.example.com,
    `local` for one on this machine."""
    host = urlparse(url).hostname or ""
    if host in _LOCAL_HOSTS:
        return "local"
    labels = [part for part in host.split(".") if part]
    if not labels or all(part.isdigit() for part in labels) or ":" in host:
        return "server"   # an address made of numbers names nobody
    while len(labels) > 2 and labels[0] in _HOST_NOISE:
        labels.pop(0)
    # `example` of example.com, and of anything.example.com.
    word = labels[-2] if len(labels) >= 2 else labels[0]
    word = re.sub(r"[^A-Za-z0-9_-]", "_", word).strip("_-")[:32]
    return word if _NAME.match(word or "") else "server"


# Names Waku gives a meaning of its own (waku_memory.py, treg.py). A server
# added from chat or from the card never takes one: only the command line
# and the person's own edit of mcp.json can.
RESERVED = frozenset({"waku_memory", "treg"})


COMMAND_REFUSED = ("this takes the address of an MCP server (https://...) or a name on "
                   "the starter list ({names}). A server that is a program on this machine "
                   "is added from the command line only: waku mcp add <name> -- <command>")
NEEDS_YES = ("{done}: this needs the owner's yes, and this conversation cannot ask for it. "
             "On the computer Waku runs on, run `{command}`, or use the Connectors card "
             "on the dashboard.")


def resolve(target: str) -> tuple[str, str, str]:
    """(name, address, auth) for what a chat message or the card asked to
    connect: a name on the starter list, or an https address. Anything else
    is refused, a command above all."""
    target = (target or "").strip()
    listed = starter().get(target.lower())
    if listed is not None:
        return listed["name"], listed["url"], listed["auth"]
    if "://" not in target or any(c.isspace() for c in target):
        raise Refused(COMMAND_REFUSED.format(names=", ".join(starter())))
    url = check_url(target)
    return name_for(url), url, OAUTH


def find(home: Path, url: str) -> dict | None:
    """The entry in mcp.json at this address, under any name."""
    return next((s for s in _read(home)["servers"]
                 if isinstance(s, dict) and _same(s.get("url")) == _same(url)), None)


def _free_name(home: Path, name: str) -> str:
    taken = {s.get("name") for s in _read(home)["servers"] if isinstance(s, dict)}
    taken |= {s.get("name") for s in _plugin_servers(home)} | RESERVED
    if name not in taken:
        return name
    return next(f"{name[:29]}_{n}" for n in range(2, 100) if f"{name[:29]}_{n}" not in taken)


def tell_owner(home: Path, flow: SignIn) -> None:
    """One message to the owner when a sign-in that chat started is done."""
    if not flow.ok:
        return
    from waku.alwayson import outbox
    from waku.db import connect

    conn = connect(Path(home))
    try:
        stamp = outbox.utc_now().isoformat(timespec="seconds")
        for channel, target in outbox.owner_addresses():
            outbox.insert(conn, run_id="", channel=channel, target=target,
                          body=connected_line(flow.name, flow.tools or []),
                          idempotency_key=f"connector:{flow.name}:{channel}:{stamp}")
    finally:
        conn.close()


def link_words(name: str, flow: SignIn | None) -> str:
    """What the person does to sign in, for a chat reply and the card."""
    if flow is not None and flow.link:
        return ("Open this link on the computer Waku runs on, in its browser. It does "
                f"not work from another device:\n{flow.link}")
    return f"Sign in on the computer Waku runs on: waku mcp login {name}"


def connect(home: Path, target: str, sign_in: Callable | None = None,
            on_done: Callable[[SignIn], None] | None = None) -> str:
    """Add the server a chat message or the card named and start its sign-in.
    Returns what the person reads. `on_done` hears when the sign-in ends."""
    sign_in = sign_in or start_sign_in
    name, url, auth = resolve(target)
    spec = find(home, url)
    if spec is None:
        listed = starter_for({"url": url})
        added = add(home, _free_name(home, name), url=url, auth=auth)
        spec, lead = added.spec, f"Added '{added.name}'."
        if added.auth != OAUTH:
            where = f" Where the key comes from: {listed['docs']}" if listed else ""
            return f"{lead} {added.next}{where}"
    elif not needs_sign_in(home, spec):
        return f"{url} is already connected as '{spec.get('name')}'."
    else:
        lead = f"'{spec.get('name')}' is added and not signed in yet."
    name = str(spec["name"])
    try:
        flow = sign_in(home, name, on_done=on_done)
    except Refused as refused:
        return f"{lead} {refused} Then: waku mcp login {name}"
    if flow.done.is_set() and flow.ok:
        return f"{lead} {connected_line(name, flow.tools or [])}"
    return f"{lead} {link_words(name, flow)}"


def _approved(home: Path) -> bool:
    """Whether the call in flight is one the owner said yes to. Only the
    worker runs such a call (alwayson/worker.py, `execute`)."""
    from waku.alwayson import outbox

    run = outbox.current_run(Path(home))
    return run is not None and bool(run.approved_key)


def make_tools(home: Path, sign_in: Callable = start_sign_in) -> list[Tool]:
    def connect_tool(name_or_url: str = "", writes: str = "") -> str:
        writes = (writes or "").strip().lower()
        if writes:
            if writes not in ("ask", "allow"):
                return "Not changed: `writes` is 'ask' or 'allow'."
            if not _approved(home):
                return ("Not changed: this needs the owner's yes, and this conversation "
                        "cannot ask for it. Use the switch on the Connectors card on "
                        "the dashboard.")
            try:
                return set_writes(home, name_or_url, allow=writes == "allow")
            except Refused as refused:
                return f"Not changed: {refused}"
        try:
            resolve(name_or_url)
            if not _approved(home):
                return NEEDS_YES.format(done="Not connected",
                                        command="waku mcp add <name> <address>")
            return connect(home, name_or_url, sign_in=sign_in,
                           on_done=lambda done: tell_owner(home, done))
        except Refused as refused:
            return f"Not connected: {refused}"

    def disconnect_tool(name: str = "") -> str:
        name = (name or "").strip()
        try:
            if not _approved(home):
                return NEEDS_YES.format(done="Not disconnected",
                                        command=f"waku mcp remove {name or '<name>'}")
            remove(home, name)
        except Refused as refused:
            return f"Not disconnected: {refused}"
        return f"Disconnected '{name}' and forgot its sign-in."

    return [
        Tool(
            name="connect_tool",
            description=(
                "Connect an MCP server, so its tools can be used. Use when the user asks "
                "to connect a service. It takes a name on the starter list ("
                + ", ".join(starter()) + ") or the server's https address, never a "
                "command to run. The owner approves it first, and then signs in through "
                "a link. With `writes`, it changes a connected server instead: 'allow' "
                "when the user says to let it write without asking, 'ask' to take that "
                "back."
            ),
            input_schema={
                "type": "object",
                "properties": {"name_or_url": {
                    "type": "string",
                    "description": ("A starter name, e.g. linear, or the server's address, "
                                    "e.g. https://mcp.example.com/mcp")},
                    "writes": {
                        "type": "string", "enum": ["ask", "allow"],
                        "description": ("Only to change a connected server: 'allow' lets "
                                        "its tools write without asking first, 'ask' "
                                        "makes them ask again. Give the server's name.")}},
                "required": ["name_or_url"],
            },
            fn=connect_tool,
        ),
        Tool(
            name="disconnect_tool",
            description=(
                "Disconnect an MCP server the user connected and forget its sign-in. Use "
                "when the user asks to disconnect or remove a connected service."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string",
                                        "description": "The connector's name, e.g. linear"}},
                "required": ["name"],
            },
            fn=disconnect_tool,
        ),
    ]
