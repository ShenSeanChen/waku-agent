"""DETERMINISTIC EVAL -- minting a hosted tenant's Waku Memory key (spec 004 A2).

The gateway mints the key with the person's own sign-in token at Waku
Memory's `POST /keys`. These cases run WakuMemoryKeys against a local aiohttp
server standing in for api.waku.one: the URL it derives, the request it sends,
and each answer it must refuse to store.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web

from hosted.gateway.memory_keys import LABEL, WakuMemoryKeys, api_base

KEY = "mem_sk_" + "q" * 43


def test_the_api_base_is_the_audience_without_mcp():
    assert api_base("https://api.waku.one/mcp") == "https://api.waku.one"
    assert api_base("https://api.waku.one/mcp/") == "https://api.waku.one"
    assert api_base("https://api.waku.one") == "https://api.waku.one"


def _mint_against(status: int, body: dict) -> tuple[object, list[dict]]:
    seen: list[dict] = []

    async def keys(request: web.Request) -> web.Response:
        seen.append({"auth": request.headers.get("Authorization"),
                     "body": await request.json(), "path": request.path})
        return web.json_response(body, status=status)

    async def run():
        app = web.Application()
        app.router.add_post("/keys", keys)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                minter = WakuMemoryKeys(session, f"http://127.0.0.1:{port}/mcp")
                return await minter.mint("the-persons-jwt")
        finally:
            await runner.cleanup()

    return asyncio.run(run()), seen


def test_a_201_with_a_real_key_is_minted_with_the_persons_token():
    result, seen = _mint_against(201, {"id": "key-9", "plaintext": KEY, "label": LABEL})
    assert result == (KEY, "key-9")
    assert seen == [{"auth": "Bearer the-persons-jwt", "body": {"label": LABEL},
                     "path": "/keys"}]


@pytest.mark.parametrize("status,body", [
    (401, {"detail": "not signed in"}),
    (201, {"id": "key-9", "plaintext": "not-a-key"}),
    (201, {"id": "key-9", "plaintext": KEY + "\nEVIL=1"}),
    (201, {"plaintext": KEY}),
])
def test_anything_but_a_well_formed_new_key_is_not_minted(status, body):
    result, _ = _mint_against(status, body)
    assert result is None
