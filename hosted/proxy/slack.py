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
  3. How many. A tenant may send SENDS_PER_MINUTE messages in any 60 seconds.
     One more is a 429, `rate_limited`. The container's sender reads that as
     a failed send, so its dispatcher keeps the message and tries again after
     10 seconds and then a minute, and nothing is lost. The limit is per
     tenant, so one container in a loop cannot hold back another's replies.
  4. The gateway is asked over run/gateway/gateway.sock to post it for that
     tenant. The gateway holds each workspace's bot token and calls Slack;
     this process never sees a Slack token, and neither does the container.

The answer is {"ok": true}, or {"ok": false, "error": <code>}: Slack's own
shape, so the container's sender reads both setups the same way.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from hosted import jsonsock, log
from hosted.proxy.app import SLACK_HISTORY_PATH, SLACK_SEND_PATH, Resolve

_LOG = log.get(__name__)

SEND_PATH = SLACK_SEND_PATH
FIELDS = frozenset({"channel", "text", "thread_ts"})
# The longest text one request carries. Slack asks for messages under 4,000
# characters. The limit also keeps the request the gateway is sent under
# jsonsock.MAX_LINE: 8,000 characters are at most 48,000 bytes of JSON.
MAX_TEXT_CHARS = 8000
# The most messages one tenant sends in RATE_WINDOW_SECONDS. Slack itself
# allows about one message a second in a channel; half of that leaves room
# for a long reply sent in parts and stops a run that posts in a loop.
SENDS_PER_MINUTE = 30
RATE_WINDOW_SECONDS = 60.0
# Slack is given ten seconds by the gateway; the socket waits a little longer.
GATEWAY_TIMEOUT_SECONDS = 15.0


def _answer(status: int, error: str = "") -> web.Response:
    body = {"ok": False, "error": error} if error else {"ok": True}
    return web.json_response(body, status=status)


def _bearer(request: web.Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class SlackSend:
    def __init__(self, *, resolve: Resolve, gateway_socket: Path,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._resolve = resolve
        self._socket = gateway_socket
        self._monotonic = monotonic
        # tenant id -> when each of its sends in the last minute was taken.
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def _take(self, tenant: str) -> bool:
        """Count one send for this tenant, or answer False at the limit. The
        dict holds one short queue per tenant that ever sent, at most
        SENDS_PER_MINUTE entries long."""
        now = self._monotonic()
        recent = self._recent[tenant]
        while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= SENDS_PER_MINUTE:
            return False
        recent.append(now)
        return True

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
        # After the checks above, so only a message that would be sent counts.
        if not self._take(tenant):
            _LOG.info("tenant=%s: over %s Slack messages a minute", tenant, SENDS_PER_MINUTE)
            return _answer(429, "rate_limited")
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


# --- spec 029 D: reading the channel a run is in ------------------------------

HISTORY_PATH = SLACK_HISTORY_PATH
HISTORY_FIELDS = frozenset({"channel", "thread_ts", "limit", "days", "person"})
# The most reads one tenant makes in RATE_WINDOW_SECONDS. A run that needs
# the channel reads it once or twice; this stops one that reads in a loop.
READS_PER_MINUTE = 20
# What `limit` is when a request names none, and the most it may be.
HISTORY_DEFAULT, HISTORY_MAX = 30, 100
# The longest window `days` may ask for, and the longest `person`.
HISTORY_MAX_DAYS, HISTORY_PERSON_CHARS = 30, 80


class SlackHistory:
    """POST /slack/history: the door of the container's slack_history tool.

    The same four steps as SlackSend above, in the same order: who, what, how
    many, and the gateway is asked. The body is `channel`, and `thread_ts`,
    `limit`, `days` and `person` or not.

    ONE REQUEST IS ONE READ, however many pages of Slack's history the
    gateway fetches for it. A request with `days` costs Slack at most ten
    pages (hosted/gateway/slack.HISTORY_PAGES), so 20 reads a minute are at
    most 200 requests to Slack, and the gateway's own caps bound each one. The gateway decides whether this tenant may read
    that channel (hosted/gateway/slack.SlackHistory) and reads it with the
    workspace's token, which this process never sees.

    The answer is the gateway's: {"ok": true, "channel": <name>, "zone":
    <time zone>, "cut": <bool>, "ambiguous": [<names>], "messages": [{ts,
    when, who, text, replies}]}, or {"ok": false, "error": <code>}.
    """

    def __init__(self, *, resolve: Resolve, gateway_socket: Path,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._resolve = resolve
        self._socket = gateway_socket
        self._monotonic = monotonic
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def _take(self, tenant: str) -> bool:
        now = self._monotonic()
        recent = self._recent[tenant]
        while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= READS_PER_MINUTE:
            return False
        recent.append(now)
        return True

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
            asked = json.loads(await request.read())
        except (ValueError, web.HTTPRequestEntityTooLarge):
            asked = None
        if not isinstance(asked, dict) or set(asked) - HISTORY_FIELDS:
            return _answer(400, "bad_request")
        channel, thread_ts = asked.get("channel"), asked.get("thread_ts", "")
        limit = asked.get("limit", HISTORY_DEFAULT)
        days, person = asked.get("days", 0), asked.get("person", "")
        if not (isinstance(channel, str) and isinstance(thread_ts, str)
                and isinstance(limit, int) and not isinstance(limit, bool)
                and 1 <= limit <= HISTORY_MAX
                and isinstance(days, int) and not isinstance(days, bool)
                and 0 <= days <= HISTORY_MAX_DAYS and not (days and thread_ts)
                and isinstance(person, str) and len(person) <= HISTORY_PERSON_CHARS):
            return _answer(400, "bad_request")
        if not self._take(tenant):
            _LOG.info("tenant=%s: over %s Slack history reads a minute", tenant,
                      READS_PER_MINUTE)
            return _answer(429, "rate_limited")
        try:
            answer = await jsonsock.ask(
                self._socket, {"op": "slack_history", "tenant": tenant, "channel": channel,
                               "thread_ts": thread_ts, "limit": limit, "days": days,
                               "person": person},
                timeout=GATEWAY_TIMEOUT_SECONDS)
        except jsonsock.Unreachable:
            return _answer(503, "relay_unreachable")
        if answer.get("ok") is True and isinstance(answer.get("messages"), list):
            names = answer.get("ambiguous")
            return web.json_response({
                "ok": True, "messages": answer["messages"],
                "channel": str(answer.get("channel") or ""),
                "zone": str(answer.get("zone") or ""), "cut": answer.get("cut") is True,
                "ambiguous": [str(name) for name in names] if isinstance(names, list)
                else []})
        error = answer.get("error")
        if "ok" not in answer or not isinstance(error, str):
            _LOG.warning("tenant=%s: the gateway did not read a Slack channel", tenant)
            return _answer(503, "relay_unreachable")
        return _answer(400 if error == "bad_request" else 200, error)
