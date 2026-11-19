"""run/gateway/gateway.sock -- what the proxy may ask the gateway.

The proxy is the one service tenant code can reach directly, so it gets no
access to control.db. It asks three things here, and nothing else:

  token       it sends a SHA-256 hash and gets back a tenant id and a status,
              and the tenant's Waku Memory key so it can charge that person's
              credits (spec 004 D).
  slack_send  it sends a tenant id it resolved itself, a channel and a text,
              and the gateway posts the message with that tenant's Slack
              token (spec 026 C3). It gets back `ok` or an error code. The
              token stays in this process.

  slack_history  it sends a tenant id it resolved itself and a channel, and
              the gateway reads that channel's recent messages with that
              tenant's Slack token (spec 029 D). It gets back names and
              text, or an error code.

It cannot enumerate tenants, read a session, read a Slack token, or issue
anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from hosted import jsonsock
from hosted.ports.control import ControlStore

GATEWAY_SOCKET_MODE = 0o660

# The whole request, field for field, the way core.requests does it for the
# spawner. An extra field is a caller that thinks this socket takes something
# it does not, and answering it anyway teaches that it does.
TOKEN_FIELDS = frozenset({"op", "hash"})
SLACK_SEND_FIELDS = frozenset({"op", "tenant", "channel", "text", "thread_ts"})
SLACK_HISTORY_FIELDS = frozenset({"op", "tenant", "channel", "thread_ts", "limit"})

# (tenant id, channel, text, thread_ts) -> {"ok": True} or {"ok": False,
# "error": code}. hosted/gateway/slack.SlackRelay.send.
SlackSend = Callable[[str, str, str, str], Awaitable[dict]]
# (tenant id, channel, thread_ts, limit) -> {"ok": True, "channel": name,
# "messages": [...]} or {"ok": False, "error": code}.
# hosted/gateway/slack.SlackHistory.read.
SlackHistory = Callable[[str, str, str, int], Awaitable[dict]]


async def serve_token_lookup(path: Path, store: ControlStore, *,
                             slack_send: SlackSend | None = None,
                             slack_history: SlackHistory | None = None) -> asyncio.Server:
    """`slack_send` is the relay, or None when Slack is not configured: the
    operation then answers that, and the socket is otherwise the same.
    `slack_history` is the reader, on the same terms."""
    async def send(request: dict) -> dict:
        unknown = sorted(set(request) - SLACK_SEND_FIELDS)
        if unknown:
            return {"error": f"slack_send does not take {unknown}"}
        if slack_send is None:
            return {"ok": False, "error": "slack_not_configured"}
        # The relay checks every value's shape itself; a missing one is "".
        return await slack_send(request.get("tenant", ""), request.get("channel", ""),
                                request.get("text", ""), request.get("thread_ts", ""))

    async def history(request: dict) -> dict:
        unknown = sorted(set(request) - SLACK_HISTORY_FIELDS)
        if unknown:
            return {"error": f"slack_history does not take {unknown}"}
        if slack_history is None:
            return {"ok": False, "error": "slack_not_configured"}
        # The reader checks every value's shape itself.
        return await slack_history(request.get("tenant", ""), request.get("channel", ""),
                                   request.get("thread_ts", ""), request.get("limit", 0))

    async def handler(request: dict) -> dict:
        # Three allowed questions, named here; everything else refused. A list
        # of forbidden ops would need editing every time the store grows a
        # method.
        if request.get("op") == "slack_send":
            return await send(request)
        if request.get("op") == "slack_history":
            return await history(request)
        if request.get("op") != "token":
            return {"error": "this socket answers three questions: op=token, "
                             "op=slack_send and op=slack_history"}
        unknown = sorted(set(request) - TOKEN_FIELDS)
        if unknown:
            return {"error": f"token does not take {unknown}"}
        digest = request.get("hash")
        if not isinstance(digest, str) or len(digest) != 64:
            return {"tenant": None}
        found = store.tenant_for_token_hash(digest)
        if found is None:
            return {"tenant": None}
        tenant_id, status = found
        # The person's Waku Memory key (spec 004 D): the proxy charges each
        # call to their credits with it, so a charge can only land on them.
        return {"tenant": tenant_id, "status": status,
                "memory_key": store.memory_key(tenant_id)}

    # The directory, the way admin.serve_admin does it. F1's Compose mounts
    # run/gateway/ so the deployment never needs this -- but without it the
    # first person to run `python -m hosted.gateway` by hand gets a bare
    # OSError out of asyncio.start_unix_server, pointing at the socket layer
    # rather than at the missing directory. The two socket servers in this
    # package should not differ on it.
    path.parent.mkdir(parents=True, exist_ok=True)
    return await jsonsock.serve(path, handler, mode=GATEWAY_SOCKET_MODE)
