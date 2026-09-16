"""Waku Memory: one memory shared by every agent you use.

The Waku agent's own memory is local, and it stays that way. Waku Memory is a
hosted MCP server at https://api.waku.one/mcp. Claude Code, Codex, Grok Bot and
this agent can all connect to it, so a fact saved in one can be recalled in
another. The two stores are separate: nothing here copies local memory up.

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
