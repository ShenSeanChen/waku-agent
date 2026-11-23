"""DETERMINISTIC EVAL -- the proxy's view of a person's credits (spec 004 D).

WakuMemoryWallet against a local stand-in for Waku Memory's /agent-usage: it
sends the person's own key, caches the balance for a minute, retries a charge
that failed with a 5xx (idempotent on turn_id upstream), and gives up on a 4xx.
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from hosted.proxy import wallet as wallet_module
from hosted.proxy.wallet import WakuMemoryWallet

KEY = "mem_sk_" + "w" * 43


def _serve(handlers: dict, test):
    async def run():
        app = web.Application()
        for (method, path), handler in handlers.items():
            app.router.add_route(method, path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                clock = [0.0]
                wallet = WakuMemoryWallet(session, f"http://127.0.0.1:{port}",
                                          now=lambda: clock[0])
                return await test(wallet, clock)
        finally:
            await runner.cleanup()

    return asyncio.run(run())


def test_the_balance_is_read_with_the_persons_key_and_cached_a_minute():
    seen: list[str] = []

    async def balance(request):
        seen.append(request.headers["Authorization"])
        return web.json_response({"plan": "free", "credits_left": 420})

    async def test(wallet, clock):
        first = await wallet.balance(KEY)
        second = await wallet.balance(KEY)
        clock[0] += 61
        third = await wallet.balance(KEY)
        return first, second, third

    answers = _serve({("GET", "/agent-usage/balance"): balance}, test)
    assert answers == (("free", 420),) * 3
    assert seen == [f"Bearer {KEY}"] * 2


def test_a_charge_is_retried_after_a_5xx_and_not_after_a_4xx(monkeypatch):
    monkeypatch.setattr(wallet_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    reports: list[dict] = []
    answers = {"turn-a": [500, 200], "turn-b": [422, 200]}

    async def usage(request):
        report = await request.json()
        reports.append(report)
        return web.json_response({}, status=answers[report["turn_id"]].pop(0))

    async def test(wallet, clock):
        wallet.charge(KEY, turn_id="turn-a", model="claude-sonnet-5-5", usd=0.0123456789)
        wallet.charge(KEY, turn_id="turn-b", model="claude-haiku-4-5", usd=0.001)
        while wallet._pending:
            await asyncio.sleep(0.01)

    _serve({("POST", "/agent-usage"): usage}, test)
    assert [r["turn_id"] for r in reports].count("turn-a") == 2
    assert [r["turn_id"] for r in reports].count("turn-b") == 1
    assert reports[0]["usd"] == 0.012346
