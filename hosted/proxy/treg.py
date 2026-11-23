"""treg in every tenant container, through the metering proxy (spec 004 E).

Every tenant's mcp.json has a `treg` server pointing at /treg/mcp/ on this
proxy, authenticated with the container's own platform token. The proxy holds
the platform's treg token and the container never does: a tenant can read its
own environment, and that token spends our treg balance.

PER REQUEST, IN ORDER:

  1. Who. The bearer token resolves to a tenant through the gateway, exactly
     as the model path's x-api-key does. Unknown or disabled is a 401.
  2. Can they pay. treg is paid in the person's Waku Memory credits, so a
     tenant with no Waku Memory key yet is refused (nothing could be charged),
     and a Free person at zero credits is refused (spec 004 D2's check). An
     unreachable Waku Memory fails open, as D2 does; the per-call ceiling and
     treg's own per-customer budget still hold.
  3. What. A JSON-RPC body is parsed, never passed through unread. tools/call
     may name only ALLOWED_TOOLS, and every catalog call carries the ceiling
     as its own X-Treg-Route-Max-Cost header (see CEILED_TOOLS for why the
     transport header alone is not enough).
  4. Forward to treg with our token, X-Treg-Meta: customer=<tenant id> and
     X-Treg-Route-Max-Cost. Only FORWARDED_REQUEST_HEADERS go upstream, so the
     tenant's platform token never reaches treg.
  5. Stream the answer back as it arrives (JSON or SSE), passing only
     RETURNED_HEADERS, and read each tools/call result on the way past: a
     `cost_usd` is charged to the person's credits with treg's call id as the
     turn id, so a replayed report is never billed twice.

WHY treg.to/mcp/v2/ AND NOT /mcp/. treg's team surface at /mcp/ also calls the
TEAM'S OWN tools -- every account the platform's treg team has connected
(analytics, search console, a payment API), with its credential injected --
and lets a caller publish hub tools under the team. A tenant must reach none
of that. /mcp/v2/ is treg's catalog-only surface: its call tools go to
/catalog/call, which never resolves a team's own tool. The same per-org token
works on both.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable

import aiohttp
from aiohttp import web

from hosted import jsonsock, log
from hosted.proxy.admission import Refused
from hosted.proxy.app import SIGN_IN_AGAIN, Resolve, Wallet, out_of_free_credits
from hosted.proxy.turns import TurnCharges

_LOG = log.get(__name__)

TREG_MCP_URL = "https://treg.to/mcp/v2/"
RELAY_METHODS = frozenset({"POST", "GET", "DELETE"})

# Upstream, and nothing else from the tenant's request: not its Authorization,
# not a cookie, not an X-Treg-* header it made up.
FORWARDED_REQUEST_HEADERS = ("Mcp-Session-Id", "Mcp-Protocol-Version", "Accept", "Content-Type")
# Back to the tenant, and nothing else from treg's answer.
RETURNED_HEADERS = ("Content-Type", "Mcp-Session-Id", "Retry-After")

# treg's catalog surface, minus `balance` and `resources_list`, which read the
# platform team's own balance and resources. A tool treg adds later is refused
# until it is named here.
ALLOWED_TOOLS = frozenset({
    "catalog_search", "catalog_get", "catalog_call_read", "catalog_call_write",
    "catalog_call_media", "catalog_request", "feedback", "review",
})
# The tools that spend. treg's MCP server relays a call's own `headers`
# argument to /catalog/call but not the transport's X-Treg-Route-Max-Cost, so
# the ceiling is written into each call's arguments, replacing whatever the
# model put there. The transport header is sent too, for the day treg reads it.
CEILED_TOOLS = frozenset({"catalog_call_read", "catalog_call_write", "catalog_call_media"})
MAX_COST_HEADER = "X-Treg-Route-Max-Cost"

# The most of one JSON answer held to read its cost from. An answer larger than
# this (a long audio clip, base64) is relayed in full and logged as uncharged.
METERED_BODY_LIMIT = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=10)

NO_WALLET = ("treg is paid in Waku credits, and this account has no Waku Memory key yet. "
             "Sign in to Waku again.")
NO_CREDITS = "No Waku credits left for treg. Add credits at waku.one."


def _refusal(refused: Refused) -> web.Response:
    return web.json_response(refused.body(), status=refused.status)


def _rpc_error(message_id: object, code: int, message: str) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": message_id,
                              "error": {"code": code, "message": message}})


def treg_cost(result: object) -> tuple[float, str | None, str | None] | None:
    """(dollars, treg call id, endpoint id) from one tools/call result, or None.

    treg's `call` answers with the same object twice: as structuredContent and
    as JSON inside a text content block. Either is read; the first one that
    names a positive cost wins, so the pair is never charged twice.
    """
    if not isinstance(result, dict):
        return None
    candidates: list[object] = [result.get("structuredContent")]
    content = result.get("content")
    for item in content if isinstance(content, list) else ():
        if isinstance(item, dict) and item.get("type") == "text" \
                and isinstance(item.get("text"), str):
            try:
                candidates.append(json.loads(item["text"]))
            except ValueError:
                continue
    candidates.append(result)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        usd = candidate.get("cost_usd")
        if isinstance(usd, bool) or not isinstance(usd, int | float):
            continue
        if not math.isfinite(usd) or usd <= 0:
            continue
        call_id, endpoint = candidate.get("call_id"), candidate.get("endpoint_id")
        return (float(usd), call_id if isinstance(call_id, str) and call_id else None,
                endpoint if isinstance(endpoint, str) and endpoint else None)
    return None


class _Meter:
    """Reads tools/call results out of the answer as it passes.

    `calls` maps each tools/call request id in the body (JSON-encoded, so 1 and
    "1" differ) to its endpoint id. Only responses to those ids are read: a
    result that merely mentions cost_usd in answer to anything else is not a
    charge.
    """

    def __init__(self, calls: dict[str, str], sse: bool) -> None:
        self._calls = calls
        self._sse = sse
        self._held = bytearray()
        self.overflowed = False
        self._pending = b""
        self._data: list[bytes] = []
        # rpc id -> (dollars, call id, endpoint id, result), first answer only.
        self.found: dict[str, tuple[float, str | None, str | None, object]] = {}

    def feed(self, chunk: bytes) -> None:
        if self.overflowed:
            return
        if not self._sse:
            self._held.extend(chunk)
            self._check_size(len(self._held))
            return
        self._pending += chunk
        *lines, self._pending = self._pending.split(b"\n")
        self._check_size(len(self._pending))
        for line in lines:
            line = line.rstrip(b"\r")
            if not line:
                self._dispatch()
            elif line.startswith(b"data:"):
                value = line[5:]
                self._data.append(value[1:] if value.startswith(b" ") else value)

    def finish(self) -> None:
        if self.overflowed:
            return
        if self._sse:
            self.feed(b"\n\n")
        else:
            self._parse(bytes(self._held))

    def _check_size(self, size: int) -> None:
        if size > METERED_BODY_LIMIT:
            self.overflowed = True
            self._held.clear()
            self._pending, self._data = b"", []

    def _dispatch(self) -> None:
        data, self._data = b"\n".join(self._data), []
        if data:
            self._parse(data)

    def _parse(self, data: bytes) -> None:
        try:
            value = json.loads(data)
        except ValueError:
            return
        for message in value if isinstance(value, list) else [value]:
            if not isinstance(message, dict) or "result" not in message:
                continue
            key = json.dumps(message.get("id"))
            if key not in self._calls or key in self.found:
                continue
            cost = treg_cost(message["result"])
            if cost is not None:
                usd, call_id, endpoint = cost
                self.found[key] = (usd, call_id, endpoint or self._calls[key] or None,
                                   message["result"])


class TregRelay:
    def __init__(self, *, session: aiohttp.ClientSession, token: str, max_call_usd: float,
                 resolve: Resolve, memory_key: Callable[[str], str],
                 wallet: Wallet | None, upstream_url: str = TREG_MCP_URL,
                 turns: TurnCharges | None = None) -> None:
        if not token:
            raise ValueError("the treg relay needs WAKU_TREG_TOKEN; without one it is off")
        self._session = session
        self._token = token
        self._ceiling = f"{max_call_usd:g}"
        self._resolve = resolve
        self._memory_key = memory_key
        self._wallet = wallet
        self._upstream_url = upstream_url
        # Waku-agent spec 011 B1: each charge also counts toward the tenant's
        # latest chat turn, for that turn's receipt.
        self._turns = turns

    def __repr__(self) -> str:
        # Never the token: a repr is what ends up in a traceback or a log line.
        return f"TregRelay(upstream={self._upstream_url!r}, ceiling={self._ceiling})"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method not in RELAY_METHODS:
            return web.json_response({"error": "POST, GET or DELETE"}, status=405)
        try:
            return await self._handle(request)
        except Refused as refused:
            return _refusal(refused)
        except web.HTTPRequestEntityTooLarge:
            return _refusal(Refused(413, "request_too_large", "The request is too large."))

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        # --- 1: who ---------------------------------------------------------
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        try:
            resolved = await self._resolve(token) if token else None
        except jsonsock.Unreachable:
            raise Refused(503, "overloaded_error",
                          "treg is restarting. Try again in a moment.") from None
        if resolved is None or resolved[1] != "active":
            raise Refused(401, "authentication_error", SIGN_IN_AGAIN)
        tenant = resolved[0]

        # --- 2: can they pay ------------------------------------------------
        key = self._memory_key(token)
        if not key or self._wallet is None:
            raise Refused(403, "permission_error", NO_WALLET)
        if await out_of_free_credits(self._wallet, key):
            raise Refused(403, "permission_error", NO_CREDITS)

        # --- 3: what --------------------------------------------------------
        body, calls = b"", {}
        if request.method == "POST":
            admitted = self._admit(await request.read())
            if isinstance(admitted, web.Response):
                return admitted
            body, calls = admitted

        # --- 4: forward -----------------------------------------------------
        headers = {name: request.headers[name] for name in FORWARDED_REQUEST_HEADERS
                   if name in request.headers}
        if request.method == "POST":
            headers["Content-Type"] = "application/json"
        headers.update({
            "Authorization": f"Bearer {self._token}",
            "X-Treg-Meta": f"customer={tenant}",
            MAX_COST_HEADER: self._ceiling,
            # Not decompressed here, so not compressed there: the meter reads
            # the bytes as they pass.
            "Accept-Encoding": "identity",
        })
        try:
            async with self._session.request(
                    request.method, self._upstream_url, data=body or None, headers=headers,
                    timeout=UPSTREAM_TIMEOUT, allow_redirects=False) as upstream:
                return await self._relay(request, upstream, tenant, key, calls)
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Raised before a byte went back: _relay catches its own once the
            # answer has started.
            _LOG.warning("tenant=%s treg unreachable: %s", tenant, type(exc).__name__)
            raise Refused(502, "api_error", "treg could not be reached. Try again.") from None

    def _admit(self, raw: bytes) -> tuple[bytes, dict[str, str]] | web.Response:
        """The body to send upstream and the tools/call ids in it, or a refusal."""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _rpc_error(None, -32700, "The body is not JSON.")
        batch = isinstance(parsed, list)
        calls: dict[str, str] = {}
        for message in parsed if batch else [parsed]:
            if not isinstance(message, dict) or message.get("method") != "tools/call":
                continue
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if name not in ALLOWED_TOOLS:
                refusal = f"treg's {name!r} tool is not available here."
                if batch:
                    return web.json_response({"error": refusal}, status=400)
                return _rpc_error(message.get("id"), -32601, refusal)
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            if name in CEILED_TOOLS:
                theirs = arguments.get("headers")
                theirs = theirs if isinstance(theirs, dict) else {}
                arguments["headers"] = {
                    **{k: v for k, v in theirs.items()
                       if str(k).lower() != MAX_COST_HEADER.lower()},
                    MAX_COST_HEADER: self._ceiling}
            params["arguments"] = arguments
            if "id" in message:
                endpoint = arguments.get("endpoint_id")
                calls[json.dumps(message["id"])] = endpoint if isinstance(endpoint, str) else ""
        return json.dumps(parsed, separators=(",", ":")).encode(), calls

    async def _relay(self, request: web.Request, upstream: aiohttp.ClientResponse,
                     tenant: str, key: str, calls: dict[str, str]) -> web.StreamResponse:
        if upstream.status == 401:
            # treg refused OUR token. Relayed as-is, the tenant's MCP client
            # would read it as their own platform token being bad.
            _LOG.warning("treg refused WAKU_TREG_TOKEN (401): check config/proxy.env")
            return _refusal(Refused(502, "api_error", "treg is not available right now."))
        response = web.StreamResponse(status=upstream.status)
        for name in RETURNED_HEADERS:
            if name in upstream.headers:
                response.headers[name] = upstream.headers[name]
        paid = upstream.status < 300       # treg bills nothing on a 4xx or 5xx
        sse = "text/event-stream" in upstream.headers.get("Content-Type", "")
        meter = _Meter(calls, sse) if paid and calls else None
        client_gone = False
        try:
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if meter is not None:
                    meter.feed(chunk)
                if client_gone:
                    continue
                try:
                    await response.write(chunk)
                except (ConnectionResetError, RuntimeError):
                    # Keep reading to the end when there is a cost to find:
                    # treg has already charged us for a call the tenant
                    # stopped listening to.
                    client_gone = True
                    if meter is None:
                        break
            if not client_gone:
                await response.write_eof()
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOG.warning("tenant=%s treg answer cut: %s", tenant, type(exc).__name__)
        finally:
            if paid:
                self._charge(tenant, key, upstream, meter)
        return response

    def _charge(self, tenant: str, key: str, upstream: aiohttp.ClientResponse,
                meter: _Meter | None) -> None:
        charges: list[tuple[str, str, float]] = []
        if meter is not None:
            meter.finish()
            if meter.overflowed:
                _LOG.warning("tenant=%s treg answer over %s bytes; its cost was not read",
                             tenant, METERED_BODY_LIMIT)
            for rpc_id, (usd, call_id, endpoint, result) in meter.found.items():
                charges.append((_turn_id(tenant, call_id, rpc_id, result),
                                _model(endpoint), usd))
        if not charges:
            # treg's REST answer names the charge in headers. An MCP answer
            # does not today; read it anyway, once, when the body named none.
            micro = upstream.headers.get("X-Treg-Cost-Micro", "")
            usd = int(micro) / 1_000_000 if micro.isdigit() else 0.0
            if usd > 0:
                call_id = upstream.headers.get("X-Treg-Call-Id") or None
                charges.append((_turn_id(tenant, call_id, "", micro), "treg", usd))
        for turn_id, model, usd in charges:
            _LOG.info("tenant=%s treg call %s charged %.6f", tenant, turn_id, usd)
            charge = self._wallet.charge(key, turn_id=turn_id, model=model, usd=usd)
            if self._turns is not None:
                self._turns.tool_call(tenant, charge)


def _turn_id(tenant: str, call_id: str | None, rpc_id: str, result: object) -> str:
    """treg's own call id when there is one. Otherwise a hash of what was
    answered, so the same answer replayed is the same turn and is charged once."""
    if call_id:
        return f"treg:{call_id}"[:200]
    digest = hashlib.sha256(json.dumps([tenant, rpc_id, result], sort_keys=True,
                                       default=str).encode()).hexdigest()
    return f"treg:sha256:{digest}"


def _model(endpoint: str | None) -> str:
    # Waku Memory's ledger names the row agent:<model>, at most 100 characters.
    return f"treg:{endpoint}"[:100] if endpoint else "treg"
