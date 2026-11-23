"""The mail tools' door on the tenant network (spec 031, part A).

A hosted container holds no Google token. Its three mail tools post here, to
POST /mail/search, /mail/read and /mail/draft on the metering proxy, with the
container's own platform token. The proxy is where they arrive because the
proxy is the one service on the tenant network.

PER REQUEST, IN ORDER:

  1. Which. The action is search, read or draft. There is no fourth: a
     request to /mail/send is a 404, like any address that is not a route.
  2. Who. The bearer token resolves to a tenant through the gateway, exactly
     as a model call's x-api-key does. Unknown or disabled is a 401.
  3. What. A JSON object with that action's fields and no other. Nothing in
     a request names a tenant, a mailbox, a recipient or a token.
  4. How many. A tenant may make CALLS_PER_MINUTE calls in any 60 seconds.
     One more is a 429.
  5. The gateway is asked over run/gateway/gateway.sock (hosted/gateway/
     mail.py). It holds the person's Google token and calls Gmail; this
     process never sees a Google token, and neither does the container.

The answer is the gateway's: {"ok": true, "mailbox": <address>, ...} or
{"ok": false, "error": <code>}.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from hosted import jsonsock, log
from hosted.proxy.app import MAIL_PATH, Resolve

_LOG = log.get(__name__)

PATH = MAIL_PATH
# The most calls one tenant makes in RATE_WINDOW_SECONDS. A question about
# mail is a search and a few reads; this stops a run that reads in a loop.
CALLS_PER_MINUTE = 30
RATE_WINDOW_SECONDS = 60.0
# The gateway gives each request to Gmail 8 seconds; the container's tool
# waits 15 for the whole answer.
GATEWAY_TIMEOUT_SECONDS = 14.0
# The longest answer taken from the gateway: a thread of ten mails.
MAX_ANSWER_LINE = 1024 * 1024
SEARCH_DEFAULT, SEARCH_MAX = 10, 20
MAX_QUERY_CHARS = 300
# Under jsonsock.MAX_LINE once it is JSON: 8,000 characters are at most
# 48,000 bytes.
MAX_DRAFT_CHARS = 8000
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _answer(status: int, error: str) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=status)


def _bearer(request: web.Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _is_text(value: object, most: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) and len(value) <= most


def arguments(action: str, sent: object) -> dict | None:
    """What the gateway is asked with, or None when the body is not this
    action's fields and nothing else."""
    if not isinstance(sent, dict):
        return None
    if action == "search":
        limit = sent.get("limit", SEARCH_DEFAULT)
        if (set(sent) - {"query", "limit"} or not _is_text(sent.get("query"), MAX_QUERY_CHARS)
                or not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= SEARCH_MAX):
            return None
        return {"query": sent["query"], "limit": limit}
    thread_id = sent.get("thread_id")
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        return None
    if action == "read":
        return None if set(sent) - {"thread_id"} else {"thread_id": thread_id}
    if set(sent) - {"thread_id", "body"} or not _is_text(sent.get("body"), MAX_DRAFT_CHARS):
        return None
    return {"thread_id": thread_id, "body": sent["body"]}


class MailDoor:
    def __init__(self, *, resolve: Resolve, gateway_socket: Path,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._resolve = resolve
        self._socket = gateway_socket
        self._monotonic = monotonic
        # tenant id -> when each of its calls in the last minute was taken.
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def _take(self, tenant: str) -> bool:
        now = self._monotonic()
        recent = self._recent[tenant]
        while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= CALLS_PER_MINUTE:
            return False
        recent.append(now)
        return True

    async def handle(self, request: web.Request) -> web.Response:
        action = request.match_info.get("action", "")
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
            sent = json.loads(await request.read())
        except (ValueError, web.HTTPRequestEntityTooLarge):
            sent = None
        args = arguments(action, sent)
        if args is None:
            return _answer(400, "bad_request")
        # After the checks above, so only a call that would be made counts.
        if not self._take(tenant):
            _LOG.info("tenant=%s: over %s mail calls a minute", tenant, CALLS_PER_MINUTE)
            return _answer(429, "rate_limited")
        try:
            answer = await jsonsock.ask(
                self._socket, {"op": "mail", "tenant": tenant, "action": action,
                               "args": args},
                timeout=GATEWAY_TIMEOUT_SECONDS, limit=MAX_ANSWER_LINE)
        except jsonsock.Unreachable:
            return _answer(503, "relay_unreachable")
        if "ok" not in answer:
            # An older gateway, or its handler failed.
            _LOG.warning("tenant=%s: the gateway did not make a mail call", tenant)
            return _answer(503, "relay_unreachable")
        return web.json_response(answer)
