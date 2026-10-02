"""DETERMINISTIC EVAL -- the metering proxy, per model call (spec 004 C3, spec 001 steps 1-10).

Offline: the gateway's token lookup and Anthropic are fakes, the ledger is a
real ledger.db in a temporary directory. What is pinned is money: a call is
charged its priced usage, a cut stream its whole reservation, a refused one
nothing; and a tenant at their $1 never reaches Anthropic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import aiohttp
import pytest
from aiohttp import web

from hosted import jsonsock
from hosted.core.quota import utc_month
from hosted.ports.upstream import UpstreamResponse
from hosted.proxy import prices
from hosted.proxy.app import FREE_USED_UP, MeteringProxy
from hosted.proxy.config import ProxyConfig
from hosted.proxy.ledger import Ledger
from hosted.proxy.upstream import AnthropicUpstream

TOKEN, TENANT = "t" * 43, "k3fq7x2mza4b"
NOW = 1_790_000_000.0
MONTH = utc_month(NOW)
SONNET = "claude-sonnet-5-5"
USAGE = {"input_tokens": 1000, "output_tokens": 200}


def _config(tmp_path, **overrides) -> ProxyConfig:
    base = ProxyConfig(
        bind_host="127.0.0.1", port=0, ledger_db=tmp_path / "ledger.db",
        gateway_socket=tmp_path / "gateway.sock", proxy_socket=tmp_path / "proxy.sock",
        platform_key="sk-ant-platform", free_models=(SONNET, "claude-haiku-4-5"),
        monthly_cap_usd=1.0, concurrent_calls=4, requests_per_minute=60,
        global_concurrent_calls=16, max_tokens_ceiling=8192, max_body_bytes=4 * 1024 * 1024,
        upstream_base_url="https://api.anthropic.com")
    return dataclasses.replace(base, **overrides)


async def _chunks(parts):
    for part in parts:
        await asyncio.sleep(0)
        yield part


class FakeUpstream:
    def __init__(self, *, status=200, parts=None, count=1000, count_raises=False,
                 hold: asyncio.Event | None = None):
        self.status = status
        self.parts = parts if parts is not None else [json.dumps(
            {"type": "message", "content": [], "usage": USAGE}).encode()]
        self.count = count
        self.count_raises = count_raises
        self.hold = hold
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    async def count_tokens(self, body):
        if self.count_raises:
            raise RuntimeError("count_tokens is down")
        return self.count

    async def messages(self, body, headers):
        self.bodies.append(body)
        self.headers.append(headers)
        if self.hold is not None:
            await self.hold.wait()
        kind = "text/event-stream" if body.get("stream") else "application/json"
        return UpstreamResponse(status=self.status, content_type=kind,
                                chunks=_chunks(self.parts))


def _sse(*events) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]


STREAM = _sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1}}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 200}},
    {"type": "message_stop"},
)


class Rig:
    def __init__(self, tmp_path, upstream: FakeUpstream, *, resolve=None, **config):
        self.ledger = Ledger(tmp_path / "ledger.db")
        self.upstream = upstream
        self.clock = [NOW]
        self.mono = [100.0]
        self.config = _config(tmp_path, **config)

        async def default_resolve(token):
            return (TENANT, "active") if token == TOKEN else None

        self.proxy = MeteringProxy(config=self.config, ledger=self.ledger,
                                   resolve=resolve or default_resolve, upstream=upstream,
                                   now=lambda: self.clock[0], monotonic=lambda: self.mono[0])

    async def __aenter__(self):
        self.runner = web.AppRunner(self.proxy.build())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.runner.cleanup()

    async def call(self, body: dict | None = None, *, token=TOKEN, path="/v1/messages",
                   method="POST"):
        body = body if body is not None else {
            "model": SONNET, "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}]}
        async with self.session.request(
                method, self.base + path, data=json.dumps(body),
                headers={"x-api-key": token, "anthropic-version": "2023-06-01",
                         "content-type": "application/json",
                         "anthropic-beta": "smuggled"}) as response:
            return response.status, await response.read()


def _run(coro):
    return asyncio.run(coro)


def test_a_call_is_charged_its_priced_usage_and_the_reservation_is_released(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream()) as rig:
            status, body = await rig.call()
            return status, json.loads(body), rig.ledger.spend(TENANT, MONTH)

    status, body, (settled, reserved) = _run(go())
    assert status == 200 and body["usage"] == USAGE
    assert settled == pytest.approx(prices.cost(SONNET, USAGE))
    assert reserved == 0


def test_a_stream_passes_through_and_is_charged_from_its_final_usage(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream(parts=STREAM)) as rig:
            status, body = await rig.call({"model": SONNET, "max_tokens": 1000, "stream": True,
                                           "messages": [{"role": "user", "content": "hi"}]})
            return status, body, rig.ledger.spend(TENANT, MONTH)

    status, body, (settled, reserved) = _run(go())
    assert status == 200 and body == b"".join(STREAM)
    assert settled == pytest.approx(prices.cost(SONNET, {"input_tokens": 1000, "output_tokens": 200}))
    assert reserved == 0


def test_a_stream_cut_before_its_final_usage_is_charged_its_whole_reservation(tmp_path):
    cut = STREAM[:2]

    async def go():
        async with Rig(tmp_path, FakeUpstream(parts=cut, count=1000)) as rig:
            await rig.call({"model": SONNET, "max_tokens": 1000, "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]})
            return rig.ledger.spend(TENANT, MONTH)

    settled, reserved = _run(go())
    assert settled == pytest.approx(prices.worst_case(SONNET, input_tokens=1000, max_tokens=1000))
    assert reserved == 0


def test_an_upstream_refusal_with_no_usage_charges_nothing(tmp_path):
    error = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": "no"}}).encode()

    async def go():
        async with Rig(tmp_path, FakeUpstream(status=400, parts=[error])) as rig:
            status, body = await rig.call()
            return status, body, rig.ledger.spend(TENANT, MONTH)

    status, body, spend = _run(go())
    assert status == 400 and body == error
    assert spend == (0.0, 0.0)


def test_unknown_disabled_and_unreachable_tokens(tmp_path):
    async def disabled(token):
        return (TENANT, "disabled")

    async def unreachable(token):
        raise jsonsock.Unreachable("gateway.sock is gone")

    async def go(resolve, token):
        async with Rig(tmp_path, FakeUpstream(), resolve=resolve) as rig:
            status, _ = await rig.call(token=token)
            return status, rig.upstream.bodies

    assert _run(go(None, "x" * 43)) == (401, [])
    assert _run(go(disabled, TOKEN)) == (401, [])
    assert _run(go(unreachable, TOKEN)) == (529, [])


def test_a_tenant_at_their_dollar_never_reaches_anthropic(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream()) as rig:
            rig.ledger.reserve(TENANT, MONTH, 0.40)
            rig.ledger.settle(TENANT, MONTH, reserved=0.40, actual=1.0)
            status, body = await rig.call()
            return status, json.loads(body), rig.upstream.bodies

    status, body, sent = _run(go())
    assert status == 403 and body["error"]["message"] == FREE_USED_UP and sent == []


def test_a_call_whose_worst_case_would_pass_the_cap_is_refused_before_anthropic(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream(count=90_000)) as rig:
            rig.ledger.reserve(TENANT, MONTH, 0.10)
            rig.ledger.settle(TENANT, MONTH, reserved=0.10, actual=0.95)
            status, _ = await rig.call({"model": SONNET, "max_tokens": 8192,
                                        "messages": [{"role": "user", "content": "hi"}]})
            return status, rig.upstream.bodies, rig.ledger.spend(TENANT, MONTH)

    status, sent, (_settled, reserved) = _run(go())
    assert status == 403 and sent == [] and reserved == 0


def test_a_failed_count_is_a_529_and_reserves_nothing(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream(count_raises=True)) as rig:
            status, _ = await rig.call()
            return status, rig.ledger.spend(TENANT, MONTH), rig.upstream.bodies

    assert _run(go()) == (529, (0.0, 0.0), [])


def test_a_fifth_call_at_once_is_a_429(tmp_path):
    hold = asyncio.Event()

    async def go():
        async with Rig(tmp_path, FakeUpstream(hold=hold)) as rig:
            first = [asyncio.create_task(rig.call()) for _ in range(4)]
            while len(rig.upstream.bodies) < 4:
                await asyncio.sleep(0.01)
            fifth = await rig.call()
            hold.set()
            done = await asyncio.gather(*first)
            return fifth[0], [s for s, _ in done]

    fifth, first = _run(go())
    assert fifth == 429 and first == [200, 200, 200, 200]


def test_the_sixty_first_request_in_a_minute_is_a_429(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream(), requests_per_minute=3) as rig:
            statuses = [(await rig.call())[0] for _ in range(4)]
            rig.mono[0] += 61
            statuses.append((await rig.call())[0])
            return statuses

    assert _run(go()) == [200, 200, 200, 429, 200]


def test_the_platform_call_time_is_recorded_even_when_the_call_is_refused(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream()) as rig:
            await rig.call({"model": "claude-opus-5-5", "max_tokens": 10,
                            "messages": [{"role": "user", "content": "hi"}]})
            return rig.ledger.last_platform_call(TENANT)

    assert _run(go()) == NOW


def test_what_goes_upstream_is_the_clamped_body_and_two_headers(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream(), max_tokens_ceiling=4096) as rig:
            await rig.call({"model": SONNET, "max_tokens": 64000,
                            "messages": [{"role": "user", "content": "hi"}]})
            return rig.upstream.bodies[0], rig.upstream.headers[0]

    body, headers = _run(go())
    assert body["max_tokens"] == 4096
    assert headers == {"anthropic-version": "2023-06-01", "content-type": "application/json"}


def test_models_lists_the_allowlist_and_every_other_path_is_a_404(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeUpstream()) as rig:
            models = await rig.call(path="/v1/models", method="GET")
            other = await rig.call(path="/v1/complete")
            return models[0], json.loads(models[1]), other[0]

    status, models, other = _run(go())
    assert status == 200 and [m["id"] for m in models["data"]] == [SONNET, "claude-haiku-4-5"]
    assert other == 404


def test_the_platform_key_replaces_the_tenants_token_upstream():
    seen: list[dict] = []

    async def go():
        async def handler(request):
            seen.append(dict(request.headers))
            return web.json_response({"input_tokens": 12})

        app = web.Application()
        app.router.add_post("/v1/messages/count_tokens", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                upstream = AnthropicUpstream(session, f"http://127.0.0.1:{port}", "sk-ant-platform")
                return await upstream.count_tokens(
                    {"model": SONNET, "messages": [], "max_tokens": 5, "stream": True})
        finally:
            await runner.cleanup()

    assert _run(go()) == 12
    assert seen[0]["x-api-key"] == "sk-ant-platform"
