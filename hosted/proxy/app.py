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
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiohttp import web

from hosted import jsonsock, log
from hosted.core.quota import utc_month
from hosted.ports.upstream import Upstream
from hosted.proxy import prices
from hosted.proxy.admission import Refused, admit, forward_headers
from hosted.proxy.config import ProxyConfig
from hosted.proxy.ledger import Ledger

_LOG = log.get(__name__)

FREE_USED_UP = "Free tier used up. Add your own key in Models."
SIGN_IN_AGAIN = "This key is not valid. Sign in to Waku again."
QUEUE_SECONDS = 30.0
RATE_WINDOW_SECONDS = 60.0

Resolve = Callable[[str], Awaitable[tuple[str, str] | None]]


class Wallet(Protocol):
    """The person's Waku Memory credits (spec 004 D; hosted/proxy/wallet.py)."""

    async def balance(self, key: str) -> tuple[str, int] | None: ...
    def charge(self, key: str, *, turn_id: str, model: str, usd: float) -> None: ...


def _answer(refused: Refused) -> web.Response:
    return web.json_response(refused.body(), status=refused.status)


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
                 wallet: Wallet | None = None) -> None:
        self._config = config
        self._ledger = ledger
        self._resolve = resolve
        self._upstream = upstream
        self._now = now
        self._monotonic = monotonic
        self._memory_key = memory_key
        self._wallet = wallet
        self._in_flight: dict[str, int] = defaultdict(int)
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._global = asyncio.Semaphore(config.global_concurrent_calls)

    def build(self) -> web.Application:
        app = web.Application(client_max_size=self._config.max_body_bytes)
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

        # --- steps 3 and 4: what is asked -----------------------------------
        body = admit(await request.read(), models=self._config.free_models,
                     ceiling=self._config.max_tokens_ceiling)

        # --- step 5: the cap, before anything costs anything ----------------
        month = utc_month(at)
        settled, reserved = self._ledger.spend(tenant, month)
        if settled + reserved >= self._config.monthly_cap_usd:
            raise Refused(403, "permission_error", FREE_USED_UP)
        key = self._memory_key(token) if self._memory_key is not None else ""
        await self._refuse_at_zero_credits(key)

        # --- step 6: the tenant's slots and rate ----------------------------
        self._take_rate(tenant)
        if self._in_flight[tenant] >= self._config.concurrent_calls:
            raise Refused(429, "rate_limit_error",
                          "Too many free-tier calls at once. Try again in a moment.")
        self._in_flight[tenant] += 1
        try:
            return await self._reserved_call(request, tenant, month, body, key)
        finally:
            self._in_flight[tenant] -= 1

    async def _refuse_at_zero_credits(self, key: str) -> None:
        """Spec 004 D: one wallet. A Free person with no credits left is
        refused here, before anything costs anything; a Pro person may go
        negative, as everywhere else in Waku Memory. An unreachable Waku Memory
        leaves the dollar cap as the only limit."""
        if not key or self._wallet is None:
            return
        try:
            balance = await self._wallet.balance(key)
        except Exception as exc:
            _LOG.info("credits balance unavailable: %s", exc)
            return
        if balance is not None and balance[0] == "free" and balance[1] <= 0:
            raise Refused(403, "permission_error", FREE_USED_UP)

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
                             body: dict, key: str) -> web.StreamResponse:
        # --- step 7: reserve the worst case ---------------------------------
        model = body["model"]
        try:
            counted = await self._upstream.count_tokens(body)
        except Exception as exc:  # any failure to count is a refusal to spend
            _LOG.info("tenant=%s count_tokens failed: %s", tenant, exc)
            raise _overloaded("The free tier could not size this call. Try again.") from None
        worst = prices.worst_case(model, input_tokens=counted, max_tokens=body["max_tokens"])
        settled, reserved = self._ledger.spend(tenant, month)
        if settled + reserved + worst > self._config.monthly_cap_usd:
            raise Refused(403, "permission_error", FREE_USED_UP)
        self._ledger.reserve(tenant, month, worst)

        # --- step 8: the global queue ---------------------------------------
        try:
            await asyncio.wait_for(self._global.acquire(), QUEUE_SECONDS)
        except TimeoutError:
            self._ledger.release(tenant, month, worst)
            raise _overloaded("The free tier is busy. Try again in a moment.") from None
        try:
            # --- steps 9 and 10 ---------------------------------------------
            return await self._forward(request, tenant, month, body, worst, key)
        finally:
            self._global.release()

    async def _forward(self, request: web.Request, tenant: str, month: str,
                       body: dict, worst: float, key: str) -> web.StreamResponse:
        model = body["model"]
        try:
            upstream = await self._upstream.messages(body, forward_headers(request.headers))
        except Exception as exc:
            # Nothing reached Anthropic that it could bill, as far as we know.
            self._ledger.release(tenant, month, worst)
            _LOG.warning("tenant=%s upstream unreachable: %s", tenant, exc)
            raise _overloaded("The free tier could not reach the model. Try again.") from None

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
            if charged > 0 and key and self._wallet is not None:
                self._wallet.charge(key, turn_id=uuid.uuid4().hex, model=model, usd=charged)
        return response

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
