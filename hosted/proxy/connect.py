"""A connector's door on the tenant network (spec 029, group B).

A hosted container holds no Notion or Linear token. Its mcp.json names this
proxy as the server's address, so its MCP client posts each message here, to
POST /connect/<name>/mcp, with the container's own platform token. The proxy
is where it arrives because the proxy is the one service on the tenant
network: the gateway listens on 127.0.0.1 and no container can reach it.

PER REQUEST, IN ORDER:

  1. Which. <name> is a connector on the list (hosted/core/connectors.py)
     that is an MCP server, or the answer is 404. Gmail is not one.
  2. Who. The bearer token resolves to a tenant through the gateway, exactly
     as a model call's x-api-key does. Unknown or disabled is a 401. Nothing
     in a request names a tenant, a vendor's address or a token.
  3. What. One JSON-RPC message, a JSON object of at most MAX_MESSAGE_BYTES.
     A batch is refused.
  4. How many. A tenant may send CALLS_PER_MINUTE messages to one connector
     in any 60 seconds. One more is a 429. The limit is per tenant and per
     connector, so one container in a loop cannot hold back another's calls,
     or its own calls to a second connector.
  5. The gateway is asked over run/gateway/gateway.sock to make the call for
     that tenant (hosted/gateway/connect_mcp.py). The gateway holds each
     person's tokens and calls the vendor; this process never sees a vendor
     token, and neither does the container.

The answer is the JSON-RPC answer as `application/json`, or 202 with no body
for a notification. The MCP specification lets a server answer every POST
that way, without a stream, and a GET, which would open one, answers 405.

POST /connect/link, on the same terms, answers where a person starts the
sign-in to a connector: the address on agent.waku.one that "connect Notion"
in chat gives them (task B3).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from hosted import jsonsock, log
from hosted.core.connectors import is_mcp_connector
from hosted.proxy.app import CONNECT_LINK_PATH, Resolve

_LOG = log.get(__name__)

LINK_PATH = CONNECT_LINK_PATH
# The longest message one request carries. It is sent to the gateway as one
# line of JSON, and the line is at most jsonsock.MAX_LINE bytes.
MAX_MESSAGE_BYTES = 48 * 1024
# The longest answer taken from the gateway, which reads at most 1 MiB of a
# vendor's answer (connect_mcp.MAX_ANSWER_BYTES) and writes it as JSON.
MAX_ANSWER_LINE = 8 * 1024 * 1024
# The most messages one tenant sends to one connector in RATE_WINDOW_SECONDS
# (spec 029, decision 3).
CALLS_PER_MINUTE = 60
RATE_WINDOW_SECONDS = 60.0
# The gateway gives a vendor 100 seconds; the socket waits a little longer.
GATEWAY_TIMEOUT_SECONDS = 110.0
LINK_TIMEOUT_SECONDS = 5.0

TOO_MANY = "Too many calls to this connector in one minute. Try again in a minute."


def _refusal(status: int, error: str) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=status)


def _bearer(request: web.Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class ConnectCalls:
    def __init__(self, *, resolve: Resolve, gateway_socket: Path,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._resolve = resolve
        self._socket = gateway_socket
        self._monotonic = monotonic
        # (tenant id, connector) -> when each of its calls in the last minute
        # was taken. One short queue per pair that ever called.
        self._recent: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _take(self, tenant: str, name: str) -> bool:
        now = self._monotonic()
        recent = self._recent[(tenant, name)]
        while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= CALLS_PER_MINUTE:
            return False
        recent.append(now)
        return True

    async def _tenant(self, request: web.Request) -> str | web.Response:
        try:
            resolved = await self._resolve(_bearer(request))
        except jsonsock.Unreachable:
            return _refusal(503, "relay_unreachable")
        if resolved is None or resolved[1] != "active":
            return _refusal(401, "not_signed_in")
        return resolved[0]

    @staticmethod
    async def _object(request: web.Request) -> dict | None:
        try:
            raw = await request.read()
            sent = json.loads(raw) if len(raw) <= MAX_MESSAGE_BYTES else None
        except (ValueError, web.HTTPRequestEntityTooLarge):
            sent = None
        return sent if isinstance(sent, dict) else None

    async def handle(self, request: web.Request) -> web.Response:
        """POST /connect/<name>/mcp: one MCP message for one connector."""
        name = request.match_info.get("name", "")
        if not is_mcp_connector(name):
            return _refusal(404, "not_a_connector")
        if request.method != "POST":
            # A GET would open a stream for the server's own messages, and a
            # DELETE would end a session. This server has neither.
            return web.json_response({"ok": False, "error": "post_only"}, status=405,
                                     headers={"Allow": "POST"})
        tenant = await self._tenant(request)
        if isinstance(tenant, web.Response):
            return tenant
        message = await self._object(request)
        if message is None:
            return _refusal(400, "bad_request")
        # After the checks above, so only a message that would be sent counts.
        if not self._take(tenant, name):
            _LOG.info("tenant=%s: over %s calls a minute to %s", tenant,
                      CALLS_PER_MINUTE, name)
            return web.json_response(
                {"jsonrpc": "2.0", "id": message.get("id"),
                 "error": {"code": -32000, "message": TOO_MANY}}, status=429)
        try:
            answer = await jsonsock.ask(
                self._socket, {"op": "connect_mcp", "tenant": tenant, "server": name,
                               "message": message},
                timeout=GATEWAY_TIMEOUT_SECONDS, limit=MAX_ANSWER_LINE)
        except jsonsock.Unreachable:
            return _refusal(503, "relay_unreachable")
        if answer.get("ok") is not True:
            error = answer.get("error")
            if "ok" not in answer or not isinstance(error, str):
                # The gateway could not do it at all: an older gateway, or
                # its handler failed.
                _LOG.warning("tenant=%s: the gateway did not relay a call to %s",
                             tenant, name)
                return _refusal(503, "relay_unreachable")
            return _refusal(400 if error == "bad_request" else 503, error)
        reply = answer.get("message")
        if reply is None:
            return web.Response(status=202)
        return web.json_response(reply)

    async def link(self, request: web.Request) -> web.Response:
        """POST /connect/link {"server": "<name>"}: where this container's
        person starts the sign-in."""
        if request.method != "POST":
            return _refusal(405, "post_only")
        tenant = await self._tenant(request)
        if isinstance(tenant, web.Response):
            return tenant
        sent = await self._object(request)
        if sent is None or set(sent) != {"server"} or not isinstance(sent["server"], str):
            return _refusal(400, "bad_request")
        try:
            answer = await jsonsock.ask(
                self._socket, {"op": "connect_link", "tenant": tenant,
                               "server": sent["server"][:64]},
                timeout=LINK_TIMEOUT_SECONDS)
        except jsonsock.Unreachable:
            return _refusal(503, "relay_unreachable")
        if "ok" not in answer:
            return _refusal(503, "relay_unreachable")
        return web.json_response(answer)
