"""Waku Memory: one memory shared by every agent you use.

The Waku agent's own memory is local. Waku Memory is a hosted MCP server at
https://api.waku.one/mcp. Claude Code, Codex, Grok Bot and this agent can all
connect to it, so a fact saved in one can be recalled in another. Waku also
writes each of its facts to <home>/memory/<id>.md, one file per fact, which
is the shape the Waku Memory importer reads.

Spec 006: once the server is connected, remember_via() gives consolidation a
callable that sends each fact it keeps with memory.remember. Spec 007 sends a
turn's research report through the same callable, and spec 018 sends the fact `save_note`
saves the same way. Those are the only uploads,
and they go to the server the person connected, with their sign-in. Spec 009:
search_via() gives a research turn one read of memory.search before the model
starts, so research begins from what the person's brain already holds.

`waku connect waku-memory` (or `/connect waku-memory` in the dashboard chat)
adds the server to WAKU_HOME/mcp.json next to any servers already there, then
signs you in once in your browser. Its tools then load like any MCP server's,
named `waku_memory_*`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

NAME = "waku_memory"
URL = "https://api.waku.one/mcp"
DOCS = "https://www.waku.one/docs"
# The host Waku Memory answered on before 2026-08-29. It now refuses clients,
# and the README showed it until 2026-09-14, so a config copied from that
# README is moved to the current address instead of failing like an outage.
RETIRED_HOSTS = ("d1o2fv4416yi84.cloudfront.net",)


def _server(bridge) -> str | None:
    """The connected Waku Memory server's name, or None. It is the one named
    waku_memory or the one at URL; a person may have added it by hand under
    another name."""
    if bridge is None:
        return None
    try:
        servers = json.loads(bridge.config_path.read_text(encoding="utf-8")).get("servers", [])
    except (OSError, ValueError, AttributeError):
        return None
    names = [s.get("name") for s in servers if isinstance(s, dict)
             and (s.get("name") == NAME or s.get("url", "").rstrip("/") == URL)]
    return next((n for n in names if n and bridge.connected(n)), None)


def remember_via(bridge):
    """A remember(body, scope, kind=None) for consolidation and for research
    reports, or None if Waku Memory is not connected. It returns the new memory's id and raises when the send
    failed: the bridge reports a failure as text, which is not this JSON.
    """
    server = _server(bridge)
    if server is None:
        return None

    def remember(body: str, scope: str, kind: str | None = None) -> str | None:
        # A company-research finding is knowledge about the world, which
        # waku.one files under Knowledge only for the kinds semantic, decision
        # and reference; `fact` shows as Activity, "what a session observed".
        # A research report (spec 007) names its own kind, `semantic`.
        if kind is None:
            kind = "reference" if scope.startswith("project:") else "fact"
        text = bridge.call(server, "memory.remember", {"body": body, "kind": kind, "scope": scope})
        try:
            return json.loads(text)["memory"]["id"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(text[:200]) from None

    return remember


def search_via(bridge):
    """A search(args) for a research turn's read-first step (spec 009 A), or
    None if Waku Memory is not connected. `args` are memory.search's own
    (query, kind, scope, limit). It returns the server's answer as text, the
    same text the model would see from waku_memory_memory_search, and raises
    when that is not memory.search's JSON: the bridge reports a failure as text.
    """
    server = _server(bridge)
    if server is None:
        return None

    def search(args: dict) -> str:
        text = bridge.call(server, "memory.search", args)
        try:
            found = isinstance(json.loads(text)["entries"], list)
        except (ValueError, KeyError, TypeError):
            found = False
        if not found:
            raise RuntimeError(text[:200])
        return text

    return search


def get_via(bridge):
    """A get(memory_id) for the read-first step's earlier reports (spec 009 A),
    or None if Waku Memory is not connected. It returns memory.get's answer as
    text and raises when that is not a memory: the bridge reports a failure
    as text."""
    server = _server(bridge)
    if server is None:
        return None

    def get(memory_id: str) -> str:
        text = bridge.call(server, "memory.get", {"id": memory_id})
        try:
            found = isinstance(json.loads(text), dict)
        except ValueError:
            found = False
        if not found:
            raise RuntimeError(text[:200])
        return text

    return get


def tool_name(bridge, tool: str) -> str | None:
    """The name the model calls one of Waku Memory's tools by, such as
    waku_memory_memory_remember, or None if Waku Memory is not connected."""
    server = _server(bridge)
    if server is None:
        return None
    from waku.tools.mcp_client import _model_safe_name

    return _model_safe_name(server, tool)


def _has_mcp() -> bool:
    return importlib.util.find_spec("mcp") is not None


def add_server(home: Path) -> tuple[str, dict]:
    """Make sure mcp.json names Waku Memory, and say what that took.

    Returns (status, server spec). Status is "added", "present", "moved"
    (a retired address was updated) or "conflict" (a server named
    waku_memory points somewhere else on purpose, so it is left alone).
    """
    config = home / "mcp.json"
    data = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    servers = data.setdefault("servers", [])

    for spec in servers:
        if spec.get("url", "").rstrip("/") == URL:
            return "present", spec
    for spec in servers:
        if spec.get("name") != NAME:
            continue
        if any(host in spec.get("url", "") for host in RETIRED_HOSTS):
            spec["url"] = URL
            config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return "moved", spec
        return "conflict", spec

    spec = {"name": NAME, "url": URL, "oauth": True}
    servers.append(spec)
    home.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "added", spec


def status(home: Path) -> str:
    """One line for `waku connections`: is Waku Memory connected, and as whom.
    It reads files only, so listing connections never opens a browser."""
    config = home / "mcp.json"
    try:
        servers = json.loads(config.read_text(encoding="utf-8")).get("servers", []) if config.exists() else []
    except json.JSONDecodeError:
        return f"not connected · {config} is not valid JSON"
    spec = next((s for s in servers if s.get("url", "").rstrip("/") == URL), None)
    if spec is None:
        return "not connected · run: waku connect waku-memory"
    if spec.get("auth_env"):
        return f"configured · API key in ${spec['auth_env']}"
    if not _has_mcp():
        return "configured · needs the mcp extra: pip install 'waku-agent[mcp]'"

    from waku.tools.mcp_cli import _auth_file, _identity

    token = _auth_file(home, spec["name"])
    if not token.exists():
        return "configured · not signed in: run waku connect waku-memory"
    return f"connected · {_identity(token)}"


def connect(home: Path) -> str:
    if not _has_mcp():
        return ("Waku Memory connects over MCP, which needs an extra: "
                "pip install 'waku-agent[mcp]' (in a checkout: pip install -e '.[mcp]'). "
                "Then run this again.")

    status, spec = add_server(home)
    name, config = spec["name"], home / "mcp.json"
    if status == "conflict":
        return (f"'{name}' in {config} already points at {spec.get('url')}, not {URL}. "
                "Change it there if you meant the hosted Waku Memory.")

    done = {"added": f"Added Waku Memory to {config}. ",
            "moved": f"Moved '{name}' to {URL}; the old address refuses clients. ",
            "present": ""}[status]
    elsewhere = f"To use the same memory in Claude Code, Codex or Grok Bot: {DOCS}"

    if spec.get("auth_env"):
        return (f"{done}Waku Memory is set up as '{name}', using the API key in "
                f"${spec['auth_env']}. Restart Waku to load its tools. {elsewhere}")

    from waku.tools.mcp_cli import _auth_file, _identity, sign_in

    token = _auth_file(home, name)
    if token.exists() and status == "present":
        return (f"Waku Memory is already connected as {_identity(token)}. "
                f"To switch accounts: waku mcp login {name}. {elsewhere}")

    ok, message = sign_in(home, name)
    if not ok:
        return f"{done}{message.strip()} Help: {DOCS}"
    return (f"{done}Connected to Waku Memory as {_identity(token)}. "
            f"Restart Waku to load its tools (waku_memory_*). {elsewhere}")
