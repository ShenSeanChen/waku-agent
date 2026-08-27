"""MCP connector — plug any Model Context Protocol server into Waku's tools.

Waku's loop is synchronous; the MCP SDK is async. The bridge below runs one
asyncio event loop on a daemon thread, holds every server's session on that loop
via a single AsyncExitStack (anyio requires the stack be entered/exited on the
same task), and lets the sync loop call tools via run_coroutine_threadsafe.

Config: WAKU_HOME/mcp.json — two transports, picked by which key is present.

  stdio: the client launches the server as a local subprocess.
  {"servers": [{"name": "fs", "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {}}]}

  Streamable HTTP: the server is already running, somewhere else.
  {"servers": [{"name": "waku_memory", "url": "https://host/mcp",
                "auth_env": "WAKU_MEMORY_API_KEY"}]}

A remote server is authorised one of two ways, and naming both is refused:

  "auth_env": "VAR"   a long-lived key, held in an environment variable
  "oauth": true       the browser flow — no key to issue, none to hand over

`auth_env` names an environment variable, and never holds the credential
itself: mcp.json is a config file people paste into bug reports, and a
bearer token in one is a leaked credential. The variable's value is sent as
`Authorization: Bearer <value>`.

`oauth` signs in through the server's own page on first use and keeps the
result under WAKU_HOME/mcp-auth/. Nothing has to be issued out of band, which
is what makes a remote server installable by someone who does not know us.
See mcp_oauth.py.

Streamable HTTP is the MCP spec's transport for remote servers. The older
HTTP+SSE transport is deprecated and is deliberately not supported here.

Each server's tools register as `<server>_<tool>` on the ToolRegistry. A server
that fails to connect is skipped with a warning — Waku still starts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from waku.tools.registry import Tool


def _model_safe_name(server: str, tool: str) -> str:
    """`<server>_<tool>`, reduced to what a model provider will accept.

    Tool names reach Anthropic and OpenAI under `^[a-zA-Z0-9_-]{1,64}$`. MCP
    itself places no such limit, and dotted names are a common convention --
    waku-memory publishes `memory.remember`, `memory.recall` and four more --
    so a server whose names are perfectly legal MCP produced a request the
    provider rejected, on the first turn, before the model saw anything.

    Only the name the model reads is rewritten. The name sent back to the
    server is the original, so this cannot break dispatch.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}_{tool}")
    if len(safe) <= 64:
        return safe
    # Keep the tail: the tool's own name is what disambiguates, and the
    # server prefix is the part a reader can afford to lose.
    return safe[-64:].lstrip("_")


def _auth_hint(spec: dict, auth_dir: Path) -> str:
    """What to tell someone whose remote server would not connect.

    The SDK reports a rejected HTTP request as a generic "Server returned an
    error response" with no status on the exception, so a 401 and a 500 are
    indistinguishable by the time we see it. Name what the caller can actually
    check rather than inventing a cause — and name the right thing: an `oauth`
    server has no `auth_env`, so pointing its user at one sends them to a
    setting their config does not contain.
    """
    if spec.get("oauth"):
        return (
            f"  {spec['url']} — sign-in did not complete. Run waku again to use a token "
            f"that was saved, or delete {auth_dir} to start over"
        )
    return (
        f"  {spec['url']} — if the server requires auth, check that "
        f"{spec.get('auth_env') or 'auth_env'} holds a current credential"
    )


class MCPBridge:
    def __init__(self, config_path: Path, timeout: float = 30.0):
        self.config_path = config_path
        self.timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._stack: AsyncExitStack | None = None
        self._sessions: dict = {}

    def start(self) -> list[Tool]:
        """Connect every configured server and return their tools (as Tools)."""
        self._thread.start()
        servers = json.loads(self.config_path.read_text(encoding="utf-8")).get("servers", [])
        fut = asyncio.run_coroutine_threadsafe(self._connect_all(servers), self._loop)
        listed = fut.result(self.timeout * 2)  # {server: [tool metas]}
        tools: list[Tool] = []
        for srv, metas in listed.items():
            for meta in metas:
                tools.append(Tool(
                    name=_model_safe_name(srv, meta["name"]),
                    description=f"[MCP:{srv}] {meta.get('description','') or ''}",
                    input_schema=meta.get("inputSchema") or {"type": "object", "properties": {}},
                    # `tname` is the server's OWN name, unsanitised — the
                    # rename above is only how the model refers to the tool,
                    # never what goes back over the wire.
                    fn=(lambda srv=srv, tname=meta["name"], **kw: self.call(srv, tname, kw)),
                ))
        return tools

    async def _open_streams(self, spec: dict):
        """Connect one server and return its (read, write) streams.

        The transport is chosen by the config's shape rather than by a
        `transport` field: a server entry either names a local command or a
        remote url, and one that somehow names both is a mistake worth
        refusing rather than resolving by precedence.
        """
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        url = spec.get("url")
        if url and spec.get("command"):
            raise ValueError("server has both 'url' and 'command' — pick one transport")

        if not url:
            params = StdioServerParameters(
                command=spec["command"], args=spec.get("args", []), env=spec.get("env") or None
            )
            return await self._stack.enter_async_context(stdio_client(params))

        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        headers = {}
        auth = None
        auth_env = spec.get("auth_env")
        use_oauth = bool(spec.get("oauth"))

        if auth_env and use_oauth:
            # Same reasoning as command-vs-url above: two credentials named,
            # no stated precedence, so refusing beats picking one silently.
            raise ValueError("server names both 'auth_env' and 'oauth' — pick one")

        if auth_env:
            token = os.environ.get(auth_env)
            if not token:
                # Fail here rather than connecting anonymously: the server
                # would answer 401 and the tools would simply be missing,
                # which reads as "the server is down" instead of "you did
                # not export the key".
                raise ValueError(f"{auth_env} is not set (named by 'auth_env' in mcp.json)")
            headers["Authorization"] = f"Bearer {token}"
        elif use_oauth:
            from waku.tools.mcp_oauth import build_provider

            # An httpx auth flow, not a header: the SDK drives the 401, the
            # discovery and the refresh itself, so this is handed to the
            # client once and never thought about again.
            auth = build_provider(url, spec["name"], self.config_path.parent)

        # create_mcp_http_client applies the SDK's own timeouts and
        # follow_redirects; a header or an auth flow are the two supported ways
        # to authenticate this transport. Because we build the client rather
        # than letting the transport build one, we own its lifecycle — hence
        # entering it on the stack ourselves.
        client = await self._stack.enter_async_context(
            create_mcp_http_client(headers=headers, auth=auth)
        )
        return await self._stack.enter_async_context(
            streamable_http_client(url, http_client=client)
        )

    def _token_exists(self, spec: dict) -> bool:
        """Whether a sign-in has already produced a token for this server.

        Read from disk rather than remembered, because the token is written by
        the SDK's auth flow on a different task than this one.
        """
        if not spec.get("oauth"):
            return False
        from waku.tools.mcp_oauth import FileTokenStorage

        return FileTokenStorage(self.config_path.parent, spec["name"])._path.exists()

    async def _connect_one(self, spec: dict) -> list[dict]:
        """Open one server's session and return its tool metadata."""
        from mcp import ClientSession

        name = spec["name"]
        read, write = await self._open_streams(spec)
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session
        tools = (await session.list_tools()).tools
        # `input_schema` in the SDK's 2.x line; it was `inputSchema` in 1.x.
        # getattr covers both so a user on either pin gets a working connector
        # rather than an AttributeError reported as "failed to connect".
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None),
            }
            for t in tools
        ]

    async def _connect_all(self, servers) -> dict:
        self._stack = AsyncExitStack()
        listed: dict = {}
        for spec in servers:
            name = spec["name"]
            had_token = self._token_exists(spec)
            try:
                listed[name] = await self._connect_one(spec)
            except Exception as first_exc:  # one bad server shouldn't stop the rest
                # Bound to a name of our own: Python unbinds an `except … as`
                # variable at the end of the block, so the retry below cannot
                # reassign it.
                error = first_exc
                # A first sign-in spends human time in the browser — longer
                # than the connection that triggered it is willing to wait. By
                # the time the token is written the attempt has already failed,
                # so the very run that signs you in is the one that appears not
                # to work. Retry once, and only when a token exists now that
                # did not before: that condition is true exactly after a
                # sign-in, and false for every other failure.
                if not had_token and self._token_exists(spec):
                    print(f"  signed in — reconnecting to '{name}'")
                    try:
                        listed[name] = await self._connect_one(spec)
                        continue
                    except Exception as retry_exc:
                        error = retry_exc

                print(f"MCP server '{name}' failed to connect: {error}")
                # ValueError is this module's own config refusal above; it
                # already says exactly what is wrong, and adding an auth hint
                # to it would point at the wrong thing.
                if spec.get("url") and not isinstance(error, ValueError):
                    print(_auth_hint(spec, self.config_path.parent / "mcp-auth"))
        return listed

    def call(self, server: str, tool: str, args: dict) -> str:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._acall(server, tool, args), self._loop)
            return fut.result(self.timeout)
        except Exception as exc:
            return f"MCP call {server}_{tool} failed: {exc}"

    async def _acall(self, server: str, tool: str, args: dict) -> str:
        session = self._sessions.get(server)
        if session is None:
            return f"MCP server '{server}' is not connected."
        result = await session.call_tool(tool, args)
        parts = []
        for block in result.content:
            parts.append(getattr(block, "text", None) or "[non-text content]")
        return "\n".join(parts) or "(no output)"

    def close(self) -> None:
        if self._stack is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop).result(10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
