"""run/gateway/gateway.sock -- what the proxy may ask the gateway.

The proxy is the one service tenant code can reach directly, so it gets no
access to control.db. It asks six things here, and nothing else:

  token       it sends a SHA-256 hash and gets back a tenant id and a status,
              and the tenant's Waku Memory key so it can charge that person's
              credits (spec 004 D). For a team home the key is its space's
              agent's, so Waku Memory charges the space's payer (spec 032 A4).
  slack_send  it sends a tenant id it resolved itself, a channel and a text,
              and the gateway posts the message with that tenant's Slack
              token (spec 026 C3). It gets back `ok` or an error code. The
              token stays in this process.

  slack_history  it sends a tenant id it resolved itself and a channel, and
              the gateway reads that channel's messages with that tenant's
              Slack token (spec 029 D). It gets back names and text, or an
              error code.
  connect_mcp   it sends a tenant id it resolved itself, a connector's name
                and one MCP message from that tenant's container, and the
                gateway makes the call with that tenant's sign-in (spec 029
                B1). It gets back the answer. The vendor's token stays in
                this process.
  connect_link  it sends a tenant id and a connector's name, and gets back
                the address where that person starts the sign-in (B3).

  mail          it sends a tenant id it resolved itself, one of three
                actions (search, read, draft) and that action's arguments,
                and the gateway calls Gmail with that tenant's sign-in (spec
                031 A). It gets back the answer. The Google token stays in
                this process, and no action sends a mail.

It cannot enumerate tenants, read a session, read a Slack token or a
connector's token, or issue anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from hosted import jsonsock
from hosted.gateway.connect_mcp import McpRelay
from hosted.ports.control import ControlStore

GATEWAY_SOCKET_MODE = 0o660

# The whole request, field for field, the way core.requests does it for the
# spawner. An extra field is a caller that thinks this socket takes something
# it does not, and answering it anyway teaches that it does.
TOKEN_FIELDS = frozenset({"op", "hash"})
SLACK_SEND_FIELDS = frozenset({"op", "tenant", "channel", "text", "thread_ts"})
SLACK_HISTORY_FIELDS = frozenset({"op", "tenant", "channel", "thread_ts", "limit",
                                  "days", "person"})
CONNECT_MCP_FIELDS = frozenset({"op", "tenant", "server", "message"})
CONNECT_LINK_FIELDS = frozenset({"op", "tenant", "server"})
MAIL_FIELDS = frozenset({"op", "tenant", "action", "args"})

# (tenant id, channel, text, thread_ts) -> {"ok": True} or {"ok": False,
# "error": code}. hosted/gateway/slack.SlackRelay.send.
SlackSend = Callable[[str, str, str, str], Awaitable[dict]]
# (tenant id, channel, thread_ts, limit, days, person) -> {"ok": True,
# "channel": name, "messages": [...], "cut": bool} or {"ok": False, "error":
# code}. hosted/gateway/slack.SlackHistory.read.
SlackHistory = Callable[[str, str, str, int, int, str], Awaitable[dict]]
# (tenant id, action, arguments) -> {"ok": True, ...} or {"ok": False,
# "error": code}. hosted/gateway/mail.Mail.handle.
MailCall = Callable[[object, object, object], Awaitable[dict]]


async def serve_token_lookup(path: Path, store: ControlStore, *,
                             slack_send: SlackSend | None = None,
                             slack_history: SlackHistory | None = None,
                             connect: McpRelay | None = None,
                             mail: MailCall | None = None,
                             memory_key: Callable[[str], str] | None = None,
                             ) -> asyncio.Server:
    """`slack_send` is the relay, or None when Slack is not configured: the
    operation then answers that, and the socket is otherwise the same.
    `slack_history` is the reader and `connect` is the connectors' relay, or
    None on the same terms, and so is `mail`, which makes a container's mail
    calls. `memory_key` answers the key a tenant's model
    calls are charged with (team_homes.HomeKeys.memory_key, which knows a
    team home from a person). Left out, every tenant is a person and the
    key is the one in memory_key."""

    def key_of(tenant_id: str) -> str:
        return (memory_key(tenant_id) if memory_key is not None
                else store.memory_key(tenant_id))
    async def connect_mcp(request: dict) -> dict:
        unknown = sorted(set(request) - CONNECT_MCP_FIELDS)
        if unknown:
            return {"error": f"connect_mcp does not take {unknown}"}
        if connect is None:
            return {"ok": False, "error": "connectors_not_configured"}
        # The relay checks every value's shape itself.
        return await connect.call(request.get("tenant", ""), request.get("server"),
                                  request.get("message"))

    async def connect_link(request: dict) -> dict:
        unknown = sorted(set(request) - CONNECT_LINK_FIELDS)
        if unknown:
            return {"error": f"connect_link does not take {unknown}"}
        if connect is None:
            return {"ok": False, "error": "connectors_not_configured"}
        return connect.link(request.get("tenant", ""), request.get("server"))

    async def mail_call(request: dict) -> dict:
        unknown = sorted(set(request) - MAIL_FIELDS)
        if unknown:
            return {"error": f"mail does not take {unknown}"}
        if mail is None:
            return {"ok": False, "error": "not_offered"}
        # Mail.handle checks every value's shape itself.
        return await mail(request.get("tenant", ""), request.get("action"),
                          request.get("args"))

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
                                   request.get("thread_ts", ""), request.get("limit", 0),
                                   request.get("days", 0), request.get("person", ""))

    async def handler(request: dict) -> dict:
        # Six allowed questions, named here; everything else refused. A list
        # of forbidden ops would need editing every time the store grows a
        # method.
        if request.get("op") == "slack_send":
            return await send(request)
        if request.get("op") == "slack_history":
            return await history(request)
        if request.get("op") == "connect_mcp":
            return await connect_mcp(request)
        if request.get("op") == "connect_link":
            return await connect_link(request)
        if request.get("op") == "mail":
            return await mail_call(request)
        if request.get("op") != "token":
            return {"error": "this socket answers six questions: op=token, op=slack_send, "
                             "op=slack_history, op=connect_mcp, op=connect_link and "
                             "op=mail"}
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
        # A team home's is its space's agent's key (spec 032 A4): the same
        # key its container holds, so a charge lands on the space's payer.
        return {"tenant": tenant_id, "status": status,
                "memory_key": key_of(tenant_id)}

    # The directory, the way admin.serve_admin does it. F1's Compose mounts
    # run/gateway/ so the deployment never needs this -- but without it the
    # first person to run `python -m hosted.gateway` by hand gets a bare
    # OSError out of asyncio.start_unix_server, pointing at the socket layer
    # rather than at the missing directory. The two socket servers in this
    # package should not differ on it.
    path.parent.mkdir(parents=True, exist_ok=True)
    return await jsonsock.serve(path, handler, mode=GATEWAY_SOCKET_MODE)
