"""The Slack send relay's door on the tenant network (spec 026 C3).

A hosted container holds no Slack token. Its Slack sender posts each outgoing
message here, to POST /slack/send on the metering proxy, with the container's
own platform token. The proxy is where it arrives because the proxy is the
one service on the tenant network: the gateway listens on 127.0.0.1 and no
container can reach it.

PER REQUEST, IN ORDER:

  1. Who. The bearer token resolves to a tenant through the gateway, exactly
     as a model call's x-api-key does. Unknown or disabled is a 401.
  2. What. A JSON object with `channel` and `text`, and `thread_ts` or not.
     Any other field is refused: nothing in a request names a tenant, a
     workspace or a token.
  3. The gateway is asked over run/gateway/gateway.sock to post it for that
     tenant. The gateway holds each workspace's bot token and calls Slack;
     this process never sees a Slack token, and neither does the container.

The answer is {"ok": true}, or {"ok": false, "error": <code>}: Slack's own
shape, so the container's sender reads both setups the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from hosted import jsonsock, log
from hosted.proxy.app import SLACK_SEND_PATH, Resolve

_LOG = log.get(__name__)

SEND_PATH = SLACK_SEND_PATH
FIELDS = frozenset({"channel", "text", "thread_ts"})
# The longest text one request carries. Slack asks for messages under 4,000
# characters. The limit also keeps the request the gateway is sent under
# jsonsock.MAX_LINE: 8,000 characters are at most 48,000 bytes of JSON.
MAX_TEXT_CHARS = 8000
# Slack is given ten seconds by the gateway; the socket waits a little longer.
GATEWAY_TIMEOUT_SECONDS = 15.0


def _answer(status: int, error: str = "") -> web.Response:
    body = {"ok": False, "error": error} if error else {"ok": True}
    return web.json_response(body, status=status)


def _bearer(request: web.Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class SlackSend:
    def __init__(self, *, resolve: Resolve, gateway_socket: Path) -> None:
        self._resolve = resolve
        self._socket = gateway_socket

    async def handle(self, request: web.Request) -> web.Response:
        if request.method != "POST":
            return _answer(405, "post_only")
        try:
            resolved = await self._resolve(_bearer(request))
        except jsonsock.Unreachable:
            return _answer(503, "relay_unreachable")
        if resolved is None or resolved[1] != "active":
            return _answer(401, "not_signed_in")
        tenant = resolved[0]
        try:
            message = json.loads(await request.read())
        except (ValueError, web.HTTPRequestEntityTooLarge):
            message = None
        if not isinstance(message, dict) or set(message) - FIELDS:
            return _answer(400, "bad_request")
        channel, text = message.get("channel"), message.get("text")
        thread_ts = message.get("thread_ts", "")
        if not (isinstance(channel, str) and isinstance(text, str)
                and isinstance(thread_ts, str) and 0 < len(text) <= MAX_TEXT_CHARS):
            return _answer(400, "bad_request")
        try:
            answer = await jsonsock.ask(
                self._socket, {"op": "slack_send", "tenant": tenant, "channel": channel,
                               "text": text, "thread_ts": thread_ts},
                timeout=GATEWAY_TIMEOUT_SECONDS)
        except jsonsock.Unreachable:
            return _answer(503, "relay_unreachable")
        if answer.get("ok") is True:
            return _answer(200)
        error = answer.get("error")
        if "ok" not in answer or not isinstance(error, str):
            # The gateway could not do it at all (an older gateway, or its
            # handler failed), which is not Slack saying no.
            _LOG.warning("tenant=%s: the gateway did not relay a Slack message", tenant)
            return _answer(503, "relay_unreachable")
        return _answer(400 if error == "bad_request" else 200, error)
