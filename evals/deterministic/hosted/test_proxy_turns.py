"""DETERMINISTIC EVAL -- what one chat turn was charged (waku-agent spec 011 B1).

A tenant container's `waku-platform` client sends `X-Waku-Turn` on every
model call and asks `GET /v1/turns/<turn_id>/charges` at the end of the turn,
so the receipt under the reply shows the exact model charge and the credits
Waku Memory took, which is the drop the person sees on waku.one.

Spec 011 acceptance 9 and 10, the proxy's half:

  9   the header never reaches Anthropic; the route sums only that turn's
      settled calls; another tenant's turn and a turn never seen answer 404.
  10  `credits` is the sum of what Waku Memory answered as `charged` for the
      turn's model calls and for the treg calls the relay charged during it;
      one charge still pending or failed makes it null, never short.

Offline: Anthropic, treg, the token lookup and the wallet are fakes on
loopback or in memory, as in test_proxy.py and test_proxy_treg.py.
"""

from __future__ import annotations

import asyncio
import json
import math

import aiohttp
from aiohttp import web

from hosted.ports.upstream import UpstreamResponse
from hosted.proxy.app import MeteringProxy
from hosted.proxy.config import ProxyConfig
from hosted.proxy.ledger import Ledger
from hosted.proxy.treg import TregRelay
from hosted.proxy.turns import KEEP_SECONDS, TurnCharges

TOKEN, TENANT = "t" * 43, "k3fq7x2mza4b"
OTHER_TOKEN, OTHER = "o" * 43, "zq8w2v1nb3xc"
MEMORY_KEY = "mem_sk_" + "w" * 43
SONNET = "claude-sonnet-5-5"
USAGE = {"input_tokens": 1000, "output_tokens": 200}       # $0.004 at $2/$10
CALL_USD = 0.004
ENDPOINT = "tomba.email.find"


def credits_for(usd: float) -> int:
    """Waku Memory's own rule (waku_api/agent_usage.py): 25,000 a dollar, rounded up."""
    return math.ceil(usd * 25_000)


class FakeWallet:
    """Waku Memory's /agent-usage. `answer` decides each charge's future:
    "ok" answers the credits, "pending" never answers, "failed" answers None."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.charges: list[dict] = []

    async def balance(self, key):
        return "pro", 500_000

    def charge(self, key, *, turn_id, model, usd):
        self.charges.append({"model": model, "usd": usd})
        future = asyncio.get_running_loop().create_future()
        answer = self.answers.pop(0) if self.answers else "ok"
        if answer == "ok":
            future.set_result(credits_for(usd))
        elif answer == "failed":
            future.set_result(None)
        return future


class FakeUpstream:
    def __init__(self):
        self.headers: list[dict] = []

    async def count_tokens(self, body):
        return 1000

    async def messages(self, body, headers):
        self.headers.append(headers)

        async def chunks():
            yield json.dumps({"type": "message", "content": [], "usage": USAGE}).encode()

        return UpstreamResponse(status=200, content_type="application/json", chunks=chunks())


async def _treg(request: web.Request) -> web.Response:
    message = json.loads(await request.read())
    result = {"status": 200, "endpoint_id": ENDPOINT, "call_id": "call_1", "cost_usd": 0.01}
    return web.json_response({"jsonrpc": "2.0", "id": message.get("id"), "result": result})


class Rig:
    def __init__(self, tmp_path, wallet: FakeWallet, *, wait: float = 3.0):
        self.tmp_path, self.wallet, self.upstream = tmp_path, wallet, FakeUpstream()
        self.turns = TurnCharges()
        self.wait = wait

    async def __aenter__(self):
        treg_app = web.Application()
        treg_app.router.add_route("POST", "/mcp/v2/", _treg)
        self.treg_runner = web.AppRunner(treg_app)
        await self.treg_runner.setup()
        site = web.TCPSite(self.treg_runner, "127.0.0.1", 0)
        await site.start()
        treg_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp/v2/"

        async def resolve(token):
            return {TOKEN: (TENANT, "active"), OTHER_TOKEN: (OTHER, "active")}.get(token)

        config = ProxyConfig(
            bind_host="127.0.0.1", port=0, ledger_db=self.tmp_path / "ledger.db",
            gateway_socket=self.tmp_path / "g.sock", proxy_socket=self.tmp_path / "p.sock",
            platform_key="sk-ant-platform", free_models=(SONNET,), monthly_cap_usd=1.0,
            concurrent_calls=4, requests_per_minute=60, global_concurrent_calls=16,
            max_tokens_ceiling=8192, max_body_bytes=4 * 1024 * 1024,
            upstream_base_url="https://api.anthropic.com")
        self.session = aiohttp.ClientSession(auto_decompress=False)
        relay = TregRelay(session=self.session, token="treg_org_" + "s" * 40, max_call_usd=0.5,
                          resolve=resolve, memory_key=lambda token: MEMORY_KEY,
                          wallet=self.wallet, upstream_url=treg_url, turns=self.turns)
        proxy = MeteringProxy(config=config, ledger=Ledger(config.ledger_db), resolve=resolve,
                              upstream=self.upstream, memory_key=lambda token: MEMORY_KEY,
                              wallet=self.wallet, treg=relay.handle, turns=self.turns)
        original = self.turns.answer
        self.turns.answer = lambda tenant, turn: original(tenant, turn, wait=self.wait)
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
        await self.treg_runner.cleanup()

    async def model_call(self, turn: str | None, token: str = TOKEN) -> int:
        headers = {"x-api-key": token, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        if turn is not None:
            headers["X-Waku-Turn"] = turn
        body = {"model": SONNET, "max_tokens": 1000,
                "messages": [{"role": "user", "content": "hi"}]}
        async with self.client.post(self.base + "/v1/messages", data=json.dumps(body),
                                    headers=headers) as response:
            await response.read()
            return response.status

    async def treg_call(self) -> int:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "catalog_call_read",
                           "arguments": {"endpoint_id": ENDPOINT, "params": {}}}}
        async with self.client.post(self.base + "/treg/mcp/", data=json.dumps(body), headers={
                "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}) as response:
            await response.read()
            return response.status

    async def charges(self, turn: str, token: str = TOKEN) -> tuple[int, dict]:
        async with self.client.get(f"{self.base}/v1/turns/{turn}/charges",
                                   headers={"x-api-key": token}) as response:
            return response.status, await response.json(content_type=None)


def _run(coro):
    return asyncio.run(coro)


# --- 9 ----------------------------------------------------------------------------


def test_the_turn_header_never_reaches_anthropic(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeWallet()) as rig:
            assert await rig.model_call("t_7f3a") == 200
            return rig.upstream.headers

    headers = _run(go())
    assert headers and all("x-waku-turn" not in {k.lower() for k in h} for h in headers)


def test_the_route_sums_only_that_turns_calls(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeWallet()) as rig:
            for turn in ("t_a", "t_a", "t_b", None):
                assert await rig.model_call(turn) == 200
            return await rig.charges("t_a"), await rig.charges("t_b")

    (status_a, a), (status_b, b) = _run(go())
    assert status_a == status_b == 200
    assert a == {"model_usd": 2 * CALL_USD, "calls": 2, "credits": 2 * credits_for(CALL_USD)}
    assert b == {"model_usd": CALL_USD, "calls": 1, "credits": credits_for(CALL_USD)}


def test_another_tenants_turn_and_an_unknown_turn_answer_404(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeWallet()) as rig:
            await rig.model_call("t_mine")
            return (await rig.charges("t_mine", token=OTHER_TOKEN),
                    await rig.charges("t_never_seen"),
                    await rig.charges("t_mine", token="x" * 43),
                    await rig.charges("t_mine"))

    other, unseen, bad_token, mine = _run(go())
    assert other[0] == 404 and other[1]["error"]["type"] == "not_found_error"
    assert unseen[0] == 404
    assert bad_token[0] == 401
    assert mine[0] == 200


# --- 10 ---------------------------------------------------------------------------


def test_credits_add_the_turns_model_calls_and_its_treg_calls(tmp_path):
    """The treg call comes after the turn's first model call, as in every
    turn, so the relay counts it to that turn."""
    async def go():
        async with Rig(tmp_path, FakeWallet()) as rig:
            await rig.model_call("t_research")
            assert await rig.treg_call() == 200
            await rig.model_call("t_research")
            return await rig.charges("t_research"), rig.wallet.charges

    (status, answer), charged = _run(go())
    assert status == 200 and [c["model"] for c in charged] == [SONNET, f"treg:{ENDPOINT}", SONNET]
    assert answer == {"model_usd": 2 * CALL_USD, "calls": 2,
                      "credits": 2 * credits_for(CALL_USD) + credits_for(0.01)}


def test_a_pending_or_failed_charge_makes_credits_null_not_short(tmp_path):
    async def go(answers):
        async with Rig(tmp_path, FakeWallet(answers), wait=0.05) as rig:
            await rig.model_call("t_x")
            await rig.model_call("t_x")
            return await rig.charges("t_x")

    for answers in (["ok", "pending"], ["failed", "ok"]):
        status, answer = _run(go(answers))
        assert status == 200 and answer["calls"] == 2 and answer["credits"] is None


def test_a_call_with_no_wallet_to_charge_makes_credits_null():
    turns = TurnCharges()
    turns.seen(TENANT, "t_y")
    turns.model_call(TENANT, "t_y", CALL_USD, None)
    assert _run(turns.answer(TENANT, "t_y"))["credits"] is None


def test_a_turn_is_kept_an_hour():
    clock = [0.0]
    turns = TurnCharges(now=lambda: clock[0])
    turns.seen(TENANT, "t_old")
    clock[0] += KEEP_SECONDS + 1
    assert _run(turns.answer(TENANT, "t_old")) is None
    turns.tool_call(TENANT, None)   # no turn left to count it to: nothing happens


def test_a_header_that_is_not_a_turn_id_is_ignored(tmp_path):
    async def go():
        async with Rig(tmp_path, FakeWallet()) as rig:
            assert await rig.model_call("t a/../b") == 200
            return rig.turns._turns

    assert _run(go()) == {}


def test_the_wallets_charge_answers_the_credits_waku_memory_took():
    """WakuMemoryWallet against a stand-in for POST /agent-usage: `charged` is
    the answer; a duplicate (an earlier attempt already charged) and a refusal
    are unknown."""
    from hosted.proxy.wallet import WakuMemoryWallet

    answers = {"a": (200, {"charged": 100, "duplicate": False, "credits_left": 9}),
               "b": (200, {"charged": 0, "duplicate": True, "credits_left": 9}),
               "c": (422, {"detail": "no"})}

    async def usage(request):
        status, body = answers[(await request.json())["turn_id"]]
        return web.json_response(body, status=status)

    async def go():
        app = web.Application()
        app.router.add_route("POST", "/agent-usage", usage)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                port = site._server.sockets[0].getsockname()[1]
                wallet = WakuMemoryWallet(session, f"http://127.0.0.1:{port}")
                tasks = [wallet.charge(MEMORY_KEY, turn_id=t, model=SONNET, usd=CALL_USD)
                         for t in "abc"]
                return await asyncio.gather(*tasks)
        finally:
            await runner.cleanup()

    assert _run(go()) == [100, None, None]
