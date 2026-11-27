"""The metering proxy: spec 001's "The proxy, per model call", steps 1 to 10.

Every tenant container's `waku-platform` provider points here. The proxy
resolves the tenant from their token, refuses what the free tier does not
allow, reserves the call's worst case against the person's monthly dollar cap,
forwards with the platform key, and settles the real cost from the usage
Anthropic reports. Every step before the forward costs nothing upstream, so a
tenant that calls the proxy in a loop spends only its own slots and rate.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from hosted import jsonsock, log
from hosted.core.quota import utc_month
from hosted.ports.upstream import Upstream
from hosted.proxy import credits, prices
from hosted.proxy.admission import Refused, admit, forward_headers
from hosted.proxy.config import ProxyConfig
from hosted.proxy.ledger import Ledger
from hosted.proxy.turns import TurnCharges, turn_id
from hosted.proxy.wallet import Balance

_LOG = log.get(__name__)

SIGN_IN_AGAIN = "This key is not valid. Sign in to Waku again."
QUEUE_SECONDS = 30.0
# Where the treg relay answers (spec 004 E). Here rather than in treg.py, which
# imports this module, so the router can name them.
RELAY_PATHS = ("/treg/mcp", "/treg/mcp/")
# Where a container's Slack sender posts (spec 026 C3; hosted/proxy/slack.py).
# Here for RELAY_PATHS' reason: that module imports this one.
SLACK_SEND_PATH = "/slack/send"
# Where a container's slack_history tool asks (spec 029 D; hosted/proxy/slack.py).
SLACK_HISTORY_PATH = "/slack/history"
# Where a container's MCP client reaches a connector, and where it asks for
# the sign-in address (spec 029 B; hosted/proxy/connect.py).
CONNECT_MCP_PATH = "/connect/{name}/mcp"
CONNECT_LINK_PATH = "/connect/link"
# Where a container's three mail tools ask (spec 031 A; hosted/proxy/mail.py).
# The three actions are the whole route: /mail/send matches nothing.
MAIL_PATH = "/mail/{action:search|read|draft}"
RATE_WINDOW_SECONDS = 60.0
# Waku-agent spec 011 B1: what one chat turn was charged.
CHARGES_PATH = re.compile(r"/v1/turns/([^/]+)/charges")
TURN_HEADER = "X-Waku-Turn"

Resolve = Callable[[str], Awaitable[tuple[str, str] | None]]
# Spec 004 E: the treg relay's handler (hosted/proxy/treg.py), or None when
# config/proxy.env carries no treg token and the route does not exist.
# Spec 026 C3: the Slack send relay's handler has the same shape.
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Wallet(Protocol):
    """The person's Waku Memory credits (spec 004 D; hosted/proxy/wallet.py)."""

    async def balance(self, key: str) -> Balance | None: ...
    # A future that answers the credits the charge took, or None (turns.py).
    def charge(self, key: str, *, turn_id: str, model: str,
               usd: float) -> asyncio.Future | None: ...


# What the person's Waku Memory balance says about a call (spec 004 D, spec 042).
OUT_OF_CREDITS = "out"      # Waku Memory says no: refused, on any plan
CREDITS_GOVERN = "credits"  # a balance answered: credits are the limit
NO_BALANCE = "unknown"      # no key yet, or Waku Memory unreachable: the dollar cap


@dataclass(frozen=True)
class Verdict:
    kind: str                # OUT_OF_CREDITS, CREDITS_GOVERN or NO_BALANCE
    notice: str = ""         # what to tell the person when kind is OUT_OF_CREDITS
    state: str | None = None  # "ok", "low" or "empty", when Waku Memory said


async def wallet_verdict(wallet: Wallet | None, key: str, at: float) -> Verdict:
    """Spec 042: Waku Memory decides, and the proxy obeys. A balance with
    `allowed: false` is refused on any plan, with Waku Memory's own `notice`.
    A balance from an older Waku Memory, without `allowed`, keeps the rule
    from before: a Free person at zero is refused, and a Pro person may go
    negative. Only when no balance can be read does the monthly dollar cap
    stand in for it (spec 004 D).

    Until 2026-11-26 the proxy held the rule itself, "Free at zero", and a Pro
    account reached -132,565 credits because nothing refused it."""
    if not key or wallet is None:
        return Verdict(NO_BALANCE)
    try:
        balance = await wallet.balance(key)
    except Exception as exc:
        _LOG.info("credits balance unavailable: %s", exc)
        return Verdict(NO_BALANCE)
    if balance is None or balance.plan == "unknown":
        # "unknown" is what a Waku Memory that could not read the balance
        # once answered with a 200; the $1 cap must still apply to it.
        return Verdict(NO_BALANCE)
    if balance.allowed is None:
        refused = balance.plan == "free" and balance.credits_left <= 0
    else:
        refused = not balance.allowed
    if refused:
        notice = balance.notice or credits.used_up_notice(balance.plan, at)
        return Verdict(OUT_OF_CREDITS, notice, balance.state)
    return Verdict(CREDITS_GOVERN, state=balance.state)


def credits_used_up(notice: str) -> Refused:
    """The one refusal for a person out of credits, for model calls and treg
    calls alike: Anthropic's 403 shape, the code credits_used_up, and the
    sentence the person reads."""
    return Refused(403, "permission_error", notice, code=credits.CREDITS_USED_UP)


async def credits_refusal(wallet: Wallet | None, key: str, at: float) -> Refused | None:
    """The refusal for a person Waku Memory says is out of credits, or None."""
    verdict = await wallet_verdict(wallet, key, at)
    return credits_used_up(verdict.notice) if verdict.kind == OUT_OF_CREDITS else None


def _answer(refused: Refused) -> web.Response:
    return web.json_response(refused.body(), status=refused.status)


#: Bytes per token in the fallback estimate. English text runs about 4 bytes
#: a token and JSON about 3; a CJK character is 3 bytes and about 1 token. Two
#: keeps the estimate above the real count for all of them.
ESTIMATE_BYTES_PER_TOKEN = 2


def estimate_input_tokens(body: dict) -> int:
    """An upper bound on a request's input tokens, for when count_tokens fails."""
    payload = {k: body[k] for k in ("model", "messages", "system", "tools", "tool_choice") if k in body}
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return size // ESTIMATE_BYTES_PER_TOKEN + 1


def _overloaded(message: str) -> Refused:
    # 529, which the SDK retries, rather than a 401 or a 500 that would read as
    # a bad key or a broken platform.
    return Refused(529, "overloaded_error", message)


class _StreamUsage:
    """Reads the usage out of an SSE stream as it passes, without holding it.

    `message_start` carries the input side, `message_delta` the output so far
    (cumulative). A stream is settled from these only once a message_delta with
    usage has arrived; a stream that ends before one is charged its whole
    reservation (spec 001, step 10).
    """

    def __init__(self) -> None:
        self.usage: dict[str, int] = {}
        self.final = False
        self._pending = b""

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                self.usage.update({k: v for k, v in usage.items() if isinstance(v, int)})
            elif event.get("type") == "message_delta" and isinstance(event.get("usage"), dict):
                self.usage.update({k: v for k, v in event["usage"].items() if isinstance(v, int)})
                self.final = True


class MeteringProxy:
    def __init__(self, *, config: ProxyConfig, ledger: Ledger, resolve: Resolve,
                 upstream: Upstream, now: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 memory_key: Callable[[str], str] | None = None,
                 wallet: Wallet | None = None, treg: Handler | None = None,
                 turns: TurnCharges | None = None,
                 slack: Handler | None = None,
                 slack_history: Handler | None = None,
                 connect: Handler | None = None,
                 connect_link: Handler | None = None,
                 mail: Handler | None = None) -> None:
        self._config = config
        self._ledger = ledger
        self._resolve = resolve
        self._upstream = upstream
        self._now = now
        self._monotonic = monotonic
        self._memory_key = memory_key
        self._wallet = wallet
        self._treg = treg
        self._slack = slack
        self._slack_history = slack_history
        self._connect = connect
        self._connect_link = connect_link
        self._mail = mail
        self._turns = turns if turns is not None else TurnCharges(monotonic)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._global = asyncio.Semaphore(config.global_concurrent_calls)

    def build(self) -> web.Application:
        app = web.Application(client_max_size=self._config.max_body_bytes)
        if self._treg is not None:
            # Before the catch-all, which would answer them 404.
            for path in RELAY_PATHS:
                app.router.add_route("*", path, self._treg)
        if self._slack is not None:
            app.router.add_route("*", SLACK_SEND_PATH, self._slack)
        if self._slack_history is not None:
            app.router.add_route("*", SLACK_HISTORY_PATH, self._slack_history)
        if self._connect_link is not None:
            app.router.add_route("*", CONNECT_LINK_PATH, self._connect_link)
        if self._connect is not None:
            app.router.add_route("*", CONNECT_MCP_PATH, self._connect)
        if self._mail is not None:
            app.router.add_route("*", MAIL_PATH, self._mail)
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        return app

    # --- step 1: the two paths ---------------------------------------------

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/v1/models" and request.method == "GET":
            return web.json_response({
                "data": [{"type": "model", "id": m, "display_name": m}
                         for m in self._config.free_models],
                "has_more": False,
                "first_id": self._config.free_models[0],
                "last_id": self._config.free_models[-1]})
        charges = CHARGES_PATH.fullmatch(request.path)
        if charges is not None and request.method == "GET":
            try:
                return await self._charges(request, charges.group(1))
            except Refused as refused:
                return _answer(refused)
        if request.path != "/v1/messages" or request.method != "POST":
            return _answer(Refused(404, "not_found_error", "Only POST /v1/messages exists here."))
        try:
            return await self._messages(request)
        except Refused as refused:
            return _answer(refused)
        except web.HTTPRequestEntityTooLarge:
            return _answer(Refused(413, "request_too_large", "The request is too large."))

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        # --- step 2: who is calling ----------------------------------------
        token = request.headers.get("x-api-key", "")
        try:
            resolved = await self._resolve(token)
        except jsonsock.Unreachable:
            raise _overloaded("The free tier is restarting. Try again in a moment.") from None
        if resolved is None or resolved[1] != "active":
            raise Refused(401, "authentication_error", SIGN_IN_AGAIN)
        tenant = resolved[0]
        at = self._now()
        self._ledger.record_platform_call(tenant, at)
        # Read here, never forwarded: forward_headers is an allowlist.
        turn = turn_id(request.headers.get(TURN_HEADER))
        self._turns.seen(tenant, turn)

        # --- steps 3 and 4: what is asked -----------------------------------
        body = admit(await request.read(), models=self._config.free_models,
                     ceiling=self._config.max_tokens_ceiling)

        # --- step 5: the limit, before anything costs anything --------------
        # The person's credits when Waku Memory answers; the monthly dollar
        # cap only when it cannot (spec 004 D).
        month = utc_month(at)
        key = self._memory_key(token) if self._memory_key is not None else ""
        verdict = await wallet_verdict(self._wallet, key, at)
        self._turns.credits_state(tenant, turn, verdict.state)
        if verdict.kind == OUT_OF_CREDITS:
            raise credits_used_up(verdict.notice)
        capped = verdict.kind == NO_BALANCE
        settled, reserved = self._ledger.spend(tenant, month)
        if capped and settled + reserved >= self._config.monthly_cap_usd:
            raise self._cap_reached(at)

        # --- step 6: the tenant's slots and rate ----------------------------
        self._take_rate(tenant)
        if self._in_flight[tenant] >= self._config.concurrent_calls:
            raise Refused(429, "rate_limit_error",
                          "Too many free-tier calls at once. Try again in a moment.")
        self._in_flight[tenant] += 1
        try:
            return await self._reserved_call(request, tenant, month, body, key, capped, turn)
        finally:
            self._in_flight[tenant] -= 1

    def _cap_reached(self, at: float) -> Refused:
        # With no balance to read, the plan is not known either. The dollar cap
        # is the Free tier's limit and resets with the UTC month, which is what
        # the Free sentence says.
        return credits_used_up(credits.used_up_notice("free", at))

    def _take_rate(self, tenant: str) -> None:
        now = self._monotonic()
        recent = self._recent[tenant]
        while recent and now - recent[0] >= RATE_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= self._config.requests_per_minute:
            raise Refused(429, "rate_limit_error",
                          "Too many free-tier calls this minute. Try again in a moment.")
        recent.append(now)

    async def _reserved_call(self, request: web.Request, tenant: str, month: str,
                             body: dict, key: str, capped: bool,
                             turn: str = "") -> web.StreamResponse:
        # --- step 7: reserve the worst case ---------------------------------
        model = body["model"]
        try:
            counted = await self._upstream.count_tokens(body)
        except Exception as exc:
            # A failed count used to refuse the call, and every phone and web
            # chat turn failed with it (Sean, 2026-11-26). The reservation now
            # uses an estimate that is always higher than the real count, so
            # the cap still holds and the turn still runs.
            counted = estimate_input_tokens(body)
            _LOG.warning("tenant=%s count_tokens failed, reserving an estimate of %d: %s",
                         tenant, counted, exc)
        worst = prices.worst_case(model, input_tokens=counted, max_tokens=body["max_tokens"])
        settled, reserved = self._ledger.spend(tenant, month)
        if capped and settled + reserved + worst > self._config.monthly_cap_usd:
            raise self._cap_reached(self._now())
        self._ledger.reserve(tenant, month, worst)

        # --- step 8: the global queue ---------------------------------------
        try:
            await asyncio.wait_for(self._global.acquire(), QUEUE_SECONDS)
        except TimeoutError:
            self._ledger.release(tenant, month, worst)
            raise _overloaded("The free tier is busy. Try again in a moment.") from None
        try:
            # --- steps 9 and 10 ---------------------------------------------
            return await self._forward(request, tenant, month, body, worst, key, turn)
        finally:
            self._global.release()

    async def _forward(self, request: web.Request, tenant: str, month: str,
                       body: dict, worst: float, key: str,
                       turn: str = "") -> web.StreamResponse:
        model = body["model"]
        try:
            upstream = await self._upstream.messages(body, forward_headers(request.headers))
        except Exception as exc:
            # Nothing reached Anthropic that it could bill, as far as we know.
            self._ledger.release(tenant, month, worst)
            _LOG.warning("tenant=%s upstream unreachable: %s", tenant, exc)
            raise _overloaded("The free tier could not reach the model. Try again.") from None

        # A turn Bedrock answered is priced and charged as the model that ran
        # it, not the one the tenant asked for (spec 043).
        model = upstream.model or model
        streaming = upstream.status < 300 and "text/event-stream" in upstream.content_type
        usage = _StreamUsage() if streaming else None
        response = web.StreamResponse(status=upstream.status)
        response.content_type = upstream.content_type.split(";")[0]
        held = bytearray()
        try:
            await response.prepare(request)
            async for chunk in upstream.chunks:
                if usage is not None:
                    usage.feed(chunk)
                else:
                    held.extend(chunk)
                await response.write(chunk)
            await response.write_eof()
        finally:
            if callable(upstream.release):
                upstream.release()
            charged = self._settle(tenant, month, model, worst, upstream.status, usage,
                                   bytes(held))
            charge = None
            if charged > 0 and key and self._wallet is not None:
                charge = self._wallet.charge(key, turn_id=uuid.uuid4().hex, model=model,
                                             usd=charged)
            self._turns.model_call(tenant, turn, charged, charge)
        return response

    async def _charges(self, request: web.Request, turn: str) -> web.Response:
        """GET /v1/turns/<turn_id>/charges: what one of this tenant's turns
        was charged (waku-agent spec 011 B1; hosted/proxy/turns.py). The same
        platform token as a model call; another tenant's turn is a 404."""
        try:
            resolved = await self._resolve(request.headers.get("x-api-key", ""))
        except jsonsock.Unreachable:
            raise _overloaded("The free tier is restarting. Try again in a moment.") from None
        if resolved is None or resolved[1] != "active":
            raise Refused(401, "authentication_error", SIGN_IN_AGAIN)
        answer = await self._turns.answer(resolved[0], turn_id(turn))
        if answer is None:
            raise Refused(404, "not_found_error", "No such turn.")
        return web.json_response(answer)

    def _settle(self, tenant: str, month: str, model: str, worst: float, status: int,
                usage: _StreamUsage | None, held: bytes) -> float:
        """Settle the ledger and return the dollars charged."""
        if usage is not None:
            if usage.final:
                actual = prices.cost(model, usage.usage)
            else:
                actual = worst        # a cut stream: the call may have been billed
                _LOG.warning("tenant=%s stream ended without usage; charged %.6f", tenant, worst)
        else:
            try:
                reported = (json.loads(held or b"{}") or {}).get("usage")
            except ValueError:
                reported = None
            if isinstance(reported, dict):
                actual = prices.cost(model, reported)
            elif status >= 300:
                self._ledger.release(tenant, month, worst)   # refused, not billed
                return 0.0
            else:
                actual = worst
        if actual > worst:
            _LOG.warning("tenant=%s used more than its reservation: %.6f > %.6f",
                         tenant, actual, worst)
        self._ledger.settle(tenant, month, reserved=worst, actual=actual)
        return actual
