"""DETERMINISTIC EVAL -- treg in every container, through the proxy (spec 004 E1, E2).

Offline: treg is a fake aiohttp server on loopback, the gateway's token lookup
and the person's wallet are fakes. What is pinned is who may call, what
reaches treg, what comes back, and money: each priced tools/call result is
charged once, to the right person, under treg's own call id.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import aiohttp
import pytest
from aiohttp import web

from hosted import jsonsock
from hosted.proxy.app import SIGN_IN_AGAIN, MeteringProxy
from hosted.proxy.config import ProxyConfig
from hosted.proxy.ledger import Ledger
from hosted.proxy.treg import (
    MAX_COST_HEADER,
    NO_CREDITS,
    NO_WALLET,
    TregRelay,
    treg_cost,
)

TOKEN, TENANT = "t" * 43, "k3fq7x2mza4b"
DISABLED_TOKEN = "d" * 43
TREG_TOKEN = "treg_org_" + "s" * 40
MEMORY_KEY = "mem_sk_" + "w" * 43
ENDPOINT = "tikhub.tiktok.user.profile"


def _config(tmp_path) -> ProxyConfig:
    return ProxyConfig(
        bind_host="127.0.0.1", port=0, ledger_db=tmp_path / "ledger.db",
        gateway_socket=tmp_path / "gateway.sock", proxy_socket=tmp_path / "proxy.sock",
        platform_key="sk-ant-platform", free_models=("claude-sonnet-5-5",),
        monthly_cap_usd=1.0, concurrent_calls=4, requests_per_minute=60,
        global_concurrent_calls=16, max_tokens_ceiling=8192, max_body_bytes=4 * 1024 * 1024,
        upstream_base_url="https://api.anthropic.com")


class FakeWallet:
    def __init__(self, plan="free", left=1000, raises=False):
        self.plan, self.left, self.raises = plan, left, raises
        self.charges: list[dict] = []

    async def balance(self, key):
        if self.raises:
            raise OSError("api.waku.one unreachable")
        return self.plan, self.left

    def charge(self, key, *, turn_id, model, usd):
        self.charges.append({"key": key, "turn_id": turn_id, "model": model, "usd": usd})


def call_result(cost=0.004, call_id="call_7f3a", endpoint=ENDPOINT) -> dict:
    """treg's `call` result: the same object as structuredContent and as text."""
    out = {"status": 200, "endpoint_id": endpoint, "body": {"followers": 12}}
    if call_id is not None:
        out["call_id"] = call_id
    if cost is not None:
        out["cost_usd"] = cost
    return {"content": [{"type": "text", "text": json.dumps(out)}],
            "structuredContent": out, "isError": False}


def tools_call(rpc_id=2, name="catalog_call_read", arguments=None) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments if arguments is not None else {
                "endpoint_id": ENDPOINT, "params": {"uniqueId": "tiktok"}}}}


class FakeTreg:
    """treg.to/mcp/v2/ on loopback. `answer` builds each response; every
    request it receives is recorded with its headers and body."""

    def __init__(self, answer=None):
        self.seen: list[dict] = []
        self.answer = answer or self.json_answer()

    @staticmethod
    def json_answer(result=None, status=200, headers=None):
        async def answer(request, body):
            message = json.loads(body) if body else {}
            payload = {"jsonrpc": "2.0", "id": message.get("id"),
                       "result": result if result is not None else call_result()}
            return web.json_response(payload, status=status, headers=headers or {})
        return answer

    async def handler(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.seen.append({"method": request.method, "path": request.path,
                          "headers": dict(request.headers), "body": body})
        return await self.answer(request, body)


class Rig:
    def __init__(self, tmp_path, treg: FakeTreg | None = None, *, wallet=None,
                 key=MEMORY_KEY, relay_on=True, resolve=None):
        self.tmp_path = tmp_path
        self.treg = treg or FakeTreg()
        self.wallet = wallet if wallet is not None else FakeWallet()
        self.key = key
        self.relay_on = relay_on
        self.resolve = resolve

    async def __aenter__(self):
        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/mcp/v2/", self.treg.handler)
        self.upstream_runner = web.AppRunner(upstream_app)
        await self.upstream_runner.setup()
        site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await site.start()
        upstream_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp/v2/"

        async def default_resolve(token):
            if token == TOKEN:
                return TENANT, "active"
            if token == DISABLED_TOKEN:
                return "zzzzzzzzzzzz", "disabled"
            return None

        self.session = aiohttp.ClientSession(auto_decompress=False)
        relay = TregRelay(session=self.session, token=TREG_TOKEN, max_call_usd=0.5,
                          resolve=self.resolve or default_resolve,
                          memory_key=lambda token: self.key, wallet=self.wallet,
                          upstream_url=upstream_url)
        config = _config(self.tmp_path)
        proxy = MeteringProxy(config=config, ledger=Ledger(config.ledger_db),
                              resolve=self.resolve or default_resolve, upstream=None,
                              treg=relay.handle if self.relay_on else None)
        self.runner = web.AppRunner(proxy.build())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.client = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.session.close()
        await self.runner.cleanup()
        await self.upstream_runner.cleanup()

    async def post(self, body, *, token=TOKEN, headers=None, method="POST"):
        sent = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", **(headers or {})}
        async with self.client.request(method, self.base + "/treg/mcp/",
                                       data=json.dumps(body) if body is not None else None,
                                       headers=sent) as response:
            return response.status, dict(response.headers), await response.read()


def _run(coro):
    return asyncio.run(coro)


# --- E3: off without a token -------------------------------------------------


def test_the_relay_is_a_404_when_the_proxy_has_no_treg_token(tmp_path):
    async def go():
        async with Rig(tmp_path, relay_on=False) as rig:
            return (await rig.post(tools_call()))[0], rig.treg.seen

    status, seen = _run(go())
    assert status == 404 and seen == []


def test_a_relay_cannot_be_built_without_a_token():
    with pytest.raises(ValueError, match="WAKU_TREG_TOKEN"):
        TregRelay(session=None, token="", max_call_usd=0.5, resolve=None,
                  memory_key=lambda t: "", wallet=None)


# --- E1: who may call ----------------------------------------------------------


def test_unknown_disabled_missing_and_unreachable_tokens_never_reach_treg(tmp_path):
    async def unreachable(token):
        raise jsonsock.Unreachable("gateway.sock is gone")

    async def go():
        async with Rig(tmp_path) as rig:
            unknown = await rig.post(tools_call(), token="x" * 43)
            disabled = await rig.post(tools_call(), token=DISABLED_TOKEN)
            async with rig.client.post(rig.base + "/treg/mcp/", data="{}") as response:
                missing = response.status
            # The model path's header is not this path's credential.
            async with rig.client.post(rig.base + "/treg/mcp/", data="{}",
                                       headers={"x-api-key": TOKEN}) as response:
                api_key_header = response.status
            seen = list(rig.treg.seen)
        async with Rig(tmp_path, resolve=unreachable) as rig:
            down = (await rig.post(tools_call()))[0]
        return unknown, disabled, missing, api_key_header, down, seen

    unknown, disabled, missing, api_key_header, down, seen = _run(go())
    assert unknown[0] == 401 and json.loads(unknown[2])["error"]["message"] == SIGN_IN_AGAIN
    assert disabled[0] == 401
    assert missing == 401 and api_key_header == 401
    assert down == 503
    assert seen == []


def test_a_tenant_with_no_waku_memory_key_is_refused_because_nothing_could_be_charged(tmp_path):
    async def go():
        async with Rig(tmp_path, key="") as rig:
            return await rig.post(tools_call()), rig.treg.seen

    (status, _, body), seen = _run(go())
    assert status == 403 and json.loads(body)["error"]["message"] == NO_WALLET and seen == []


def test_a_free_person_at_zero_credits_never_reaches_treg(tmp_path):
    async def go():
        async with Rig(tmp_path, wallet=FakeWallet(plan="free", left=0)) as rig:
            return await rig.post(tools_call()), rig.treg.seen

    (status, _, body), seen = _run(go())
    assert status == 403 and json.loads(body)["error"]["message"] == NO_CREDITS and seen == []


def test_a_pro_person_below_zero_and_an_unreachable_wallet_are_both_served(tmp_path):
    async def go(wallet):
        async with Rig(tmp_path, wallet=wallet) as rig:
            return (await rig.post(tools_call()))[0]

    assert _run(go(FakeWallet(plan="pro", left=-50))) == 200
    assert _run(go(FakeWallet(raises=True))) == 200


# --- E1: what reaches treg -----------------------------------------------------


def test_only_the_four_mcp_headers_go_upstream_with_our_token_tag_and_ceiling(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            await rig.post(tools_call(), headers={
                "Mcp-Session-Id": "sess-1", "Mcp-Protocol-Version": "2025-06-18",
                "Cookie": "waku_session=abc", "X-Treg-Token": "smuggled",
                "X-Treg-Meta": "customer=someone-else", MAX_COST_HEADER: "100",
                "X-Treg-Org": "another-team", "Idempotency-Key": "k"})
            return rig.treg.seen[0]

    seen = _run(go())
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["authorization"] == f"Bearer {TREG_TOKEN}"
    assert headers["x-treg-meta"] == f"customer={TENANT}"
    assert headers["x-treg-route-max-cost"] == "0.5"
    assert headers["mcp-session-id"] == "sess-1"
    assert headers["mcp-protocol-version"] == "2025-06-18"
    assert headers["accept"] == "application/json, text/event-stream"
    assert headers["content-type"] == "application/json"
    for refused in ("cookie", "x-treg-token", "x-treg-org", "idempotency-key"):
        assert refused not in headers, refused
    # The tenant's platform token reaches treg nowhere, header or body.
    assert TOKEN not in json.dumps(seen["headers"]) and TOKEN.encode() not in seen["body"]


def test_every_catalog_call_carries_our_ceiling_in_its_own_headers(tmp_path):
    """treg's MCP server relays a call's `headers` argument to /catalog/call
    and not the transport's ceiling header, so the ceiling is written there,
    over whatever the model asked for."""
    arguments = {"endpoint_id": ENDPOINT, "headers": {"x-treg-route-max-cost": "50",
                                                      "login-customer-id": "123"}}

    async def go():
        async with Rig(tmp_path) as rig:
            await rig.post(tools_call(arguments=arguments))
            await rig.post(tools_call(rpc_id=3, name="catalog_call_write",
                                      arguments={"endpoint_id": ENDPOINT}))
            await rig.post(tools_call(rpc_id=4, name="catalog_search",
                                      arguments={"query": "backlinks"}))
            return [json.loads(s["body"])["params"]["arguments"] for s in rig.treg.seen]

    read, write, search = _run(go())
    assert read["headers"] == {"login-customer-id": "123", MAX_COST_HEADER: "0.5"}
    assert write["headers"] == {MAX_COST_HEADER: "0.5"}
    assert "headers" not in search


def test_a_tool_outside_the_catalog_surface_is_refused_before_treg(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            balance = await rig.post(tools_call(name="balance", arguments={}))
            own_tool = await rig.post(tools_call(rpc_id=9, name="call",
                                                 arguments={"endpoint_id": "stripe"}))
            batch = await rig.post([tools_call(), tools_call(rpc_id=5, name="my_tools")])
            async with rig.client.post(rig.base + "/treg/mcp/", data=b"{nope",
                                       headers={"Authorization": f"Bearer {TOKEN}"}) as response:
                not_json = response.status, await response.json()
            return balance, own_tool, batch, not_json, rig.treg.seen

    balance, own_tool, batch, not_json, seen = _run(go())
    assert balance[0] == 200 and json.loads(balance[2])["error"]["code"] == -32601
    assert json.loads(own_tool[2])["id"] == 9 and "error" in json.loads(own_tool[2])
    assert batch[0] == 400
    assert not_json[0] == 200 and not_json[1]["error"]["code"] == -32700
    assert seen == []


def test_get_and_delete_are_relayed_for_the_session_and_other_methods_are_not(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeTreg(answer=lambda r, b: _empty(r))) as rig:
            got = await rig.post(None, method="GET", headers={"Mcp-Session-Id": "sess-1"})
            deleted = await rig.post(None, method="DELETE",
                                     headers={"Mcp-Session-Id": "sess-1"})
            put = await rig.post(None, method="PUT")
            return got, deleted, put, [(s["method"], s["headers"].get("Mcp-Session-Id"))
                                       for s in rig.treg.seen]

    got, deleted, put, seen = _run(go())
    assert got[0] == 202 and deleted[0] == 202
    assert got[1]["Mcp-Session-Id"] == "sess-1"
    assert put[0] == 405
    assert seen == [("GET", "sess-1"), ("DELETE", "sess-1")]


async def _empty(request):
    return web.Response(status=202, headers={"Mcp-Session-Id": "sess-1"})


# --- E1: what comes back, and what never does ---------------------------------


def test_nothing_but_the_returned_headers_comes_back_and_no_token_is_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    leaky = FakeTreg.json_answer(headers={
        "Mcp-Session-Id": "sess-2", "X-Treg-Echo": TREG_TOKEN, "Set-Cookie": "treg=1",
        "X-Treg-Call-Id": "call_7f3a"})

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=leaky)) as rig:
            return await rig.post(tools_call())

    status, headers, body = _run(go())
    assert TREG_TOKEN.encode() not in body
    assert status == 200 and headers["Mcp-Session-Id"] == "sess-2"
    assert "X-Treg-Echo" not in headers and "Set-Cookie" not in headers
    assert TREG_TOKEN not in json.dumps(headers)
    assert TREG_TOKEN not in caplog.text and TOKEN not in caplog.text
    assert TREG_TOKEN not in repr(ProxyConfig(**{**dataclasses.asdict(_config(tmp_path)),
                                                "treg_token": TREG_TOKEN}))


def test_a_402_above_the_ceiling_passes_through_and_is_not_charged(tmp_path):
    refusal = {"detail": {"error": "route_max_cost", "estimated_cost_micro": 900000}}

    async def answer(request, body):
        return web.json_response(refusal, status=402)

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=answer)) as rig:
            return await rig.post(tools_call()), rig.wallet.charges

    (status, _, body), charges = _run(go())
    assert status == 402 and json.loads(body) == refusal and charges == []


def test_a_5xx_is_relayed_and_never_charged_even_if_it_names_a_cost(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeTreg(answer=FakeTreg.json_answer(status=503))) as rig:
            return (await rig.post(tools_call()))[0], rig.wallet.charges

    assert _run(go()) == (503, [])


def test_treg_refusing_our_token_is_not_relayed_as_the_tenants_401(tmp_path, caplog):
    """A 401 from treg is about WAKU_TREG_TOKEN. Relayed as-is, the tenant's
    MCP client would read it as their own platform token being bad."""
    answer = FakeTreg.json_answer(status=401, headers={
        "WWW-Authenticate": 'Bearer resource_metadata="https://treg.to/.well-known/x"'})

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=answer)) as rig:
            return await rig.post(tools_call()), rig.wallet.charges

    (status, headers, _), charges = _run(go())
    assert status == 502 and "WWW-Authenticate" not in headers and charges == []
    assert "WAKU_TREG_TOKEN" in caplog.text and TREG_TOKEN not in caplog.text


# --- E2: charged once ----------------------------------------------------------


def test_a_priced_call_is_charged_once_under_treg_call_id(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            first = await rig.post(tools_call())
            second = await rig.post(tools_call())       # the same answer, replayed
            return first, second, rig.wallet.charges

    first, second, charges = _run(go())
    assert first[0] == second[0] == 200
    assert json.loads(first[2])["result"] == call_result()   # relayed unchanged
    # structuredContent and the text block name the same cost: one charge each.
    assert len(charges) == 2
    assert charges[0] == {"key": MEMORY_KEY, "turn_id": "treg:call_7f3a",
                          "model": f"treg:{ENDPOINT}", "usd": 0.004}
    # Waku Memory is idempotent on turn_id, so the replay is charged nothing.
    assert charges[1]["turn_id"] == charges[0]["turn_id"]


def test_without_a_call_id_the_turn_id_is_a_stable_hash(tmp_path):
    answer = FakeTreg.json_answer(result=call_result(call_id=None))

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=answer)) as rig:
            await rig.post(tools_call())
            await rig.post(tools_call())
            return rig.wallet.charges

    charges = _run(go())
    assert len(charges) == 2 and charges[0]["turn_id"] == charges[1]["turn_id"]
    assert charges[0]["turn_id"].startswith("treg:sha256:")


def test_an_unpriced_call_and_a_cost_in_answer_to_anything_else_charge_nothing(tmp_path):
    async def go(result, body):
        async with Rig(tmp_path, FakeTreg(answer=FakeTreg.json_answer(result=result))) as rig:
            await rig.post(body)
            return rig.wallet.charges

    # A team's own key, or a free endpoint: no cost_usd.
    assert _run(go(call_result(cost=None), tools_call())) == []
    # A tools/list answer that happens to carry cost_usd is not a call.
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert _run(go(call_result(), listing)) == []


def test_the_cost_header_is_charged_when_the_body_names_none(tmp_path):
    answer = FakeTreg.json_answer(result=call_result(cost=None), headers={
        "X-Treg-Cost-Micro": "2500", "X-Treg-Call-Id": "call_hdr"})

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=answer)) as rig:
            await rig.post(tools_call())
            return rig.wallet.charges

    assert _run(go()) == [{"key": MEMORY_KEY, "turn_id": "treg:call_hdr",
                           "model": "treg", "usd": 0.0025}]


def test_an_sse_answer_streams_through_and_its_split_result_is_charged(tmp_path):
    """The first event reaches the tenant while treg is still holding the
    second, and the result event, split across three writes, is charged."""
    release = asyncio.Event()
    result_frame = ("event: message\ndata: " + json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": call_result(cost=0.01)}) + "\n\n").encode()

    async def answer(request, body):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'event: message\ndata: {"jsonrpc":"2.0","method":'
                             b'"notifications/progress","params":{}}\n\n')
        await release.wait()
        third = len(result_frame) // 3
        for part in (result_frame[:third], result_frame[third:2 * third],
                     result_frame[2 * third:]):
            await response.write(part)
            await asyncio.sleep(0)
        await response.write_eof()
        return response

    async def go():
        async with Rig(tmp_path, FakeTreg(answer=answer)) as rig:
            async with rig.client.post(
                    rig.base + "/treg/mcp/", data=json.dumps(tools_call()),
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "Accept": "application/json, text/event-stream"}) as response:
                first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
                release.set()
                rest = await response.read()
            return response.headers["Content-Type"], first, rest, rig.wallet.charges

    content_type, first, rest, charges = _run(go())
    assert content_type.startswith("text/event-stream")
    assert b"notifications/progress" in first
    assert rest == result_frame
    assert [(c["turn_id"], c["usd"]) for c in charges] == [("treg:call_7f3a", 0.01)]


def test_treg_cost_reads_text_json_structured_content_and_ignores_nonsense():
    assert treg_cost(call_result(cost=0.2)) == (0.2, "call_7f3a", ENDPOINT)
    text_only = {"content": [{"type": "text", "text": json.dumps({"cost_usd": 0.3})}]}
    assert treg_cost(text_only) == (0.3, None, None)
    for bad in (None, [], {"cost_usd": True}, {"cost_usd": "0.3"}, {"cost_usd": -1},
                {"cost_usd": float("nan")}, {"content": [{"type": "text", "text": "{no"}]}):
        assert treg_cost(bad) is None, bad
