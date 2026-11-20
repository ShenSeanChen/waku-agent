"""DETERMINISTIC EVAL -- a container's calls to a connector (spec 029, group
B: task B1, and acceptance 2 and 4).

A hosted container holds no Notion token. Its MCP client posts each message
to the metering proxy with the container's own platform token. The proxy
finds the tenant and asks the gateway over gateway.sock, and the gateway
calls Notion with the tokens of that tenant's sign-in.

  container's MCP client --HTTP--> proxy --gateway.sock--> gateway --> vendor
  (waku/, MIT)                     (hosted/, Elastic)      (hosted/, Elastic)

Every piece is the real one: the MIT MCPBridge from waku/, MeteringProxy,
ConnectCalls, the gateway's socket server, McpRelay, Connect and ControlDb.
The vendor is the FakeVendor on loopback, which compresses its answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import urllib.parse

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from gatewaylib import APEX, SLACK_TOKEN_KEY, FakeVendor

from hosted import jsonsock
from hosted.core.connectors import VENDORS
from hosted.core.provision import sync_connectors
from hosted.gateway.connect import Connect, VendorApi
from hosted.gateway.connect_mcp import MAX_ANSWER_BYTES, McpRelay
from hosted.gateway.internal import serve_token_lookup
from hosted.gateway.store import ControlDb
from hosted.proxy.app import MeteringProxy
from hosted.proxy.config import ProxyConfig
from hosted.proxy.connect import CALLS_PER_MINUTE, MAX_MESSAGE_BYTES, ConnectCalls
from hosted.proxy.gateway_client import TokenCache
from hosted.proxy.ledger import Ledger
from waku.tools.mcp_client import MCPBridge

SEARCH = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
          "params": {"name": "notion-search", "arguments": {"query": "roadmap"}}}
CREATE = {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
          "params": {"name": "notion-create-pages", "arguments": {"title": "x"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}}


@pytest.fixture
def notion():
    fake = FakeVendor("notion")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def linear():
    fake = FakeVendor("linear")
    fake.tools = [{"name": "list_issues", "description": "List issues.",
                   "inputSchema": {"type": "object", "properties": {}}},
                  {"name": "create_issue", "description": "Create an issue.",
                   "inputSchema": {"type": "object", "properties": {}}}]
    fake.start()
    yield fake
    fake.stop()


class Rig:
    """The gateway's half and the proxy's half, joined by a real socket."""

    def __init__(self, tmp_path, sock_dir, **fakes: FakeVendor) -> None:
        self.tmp_path, self.sock, self.fakes = tmp_path, sock_dir / "gateway.sock", fakes
        self.now = 1_700_000_000.0
        # The proxy's clock for its per-minute limit. A test moves it by hand.
        self.proxy_clock = 1000.0
        # Every body the container's side was answered with.
        self.container_saw: list[str] = []

    async def __aenter__(self):
        self.store = ControlDb(self.tmp_path / "control.db", now=lambda: self.now)
        # As the gateway makes it (__main__.py): it does not decompress.
        self.session = aiohttp.ClientSession(auto_decompress=False)
        self.client = aiohttp.ClientSession()
        self.vendors = {name: dataclasses.replace(VENDORS[name], url=fake.url + "/mcp")
                        for name, fake in self.fakes.items()}
        self.connect = Connect(key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host=APEX,
                               store=self.store, api=VendorApi(self.session),
                               vendors=self.vendors, now=lambda: self.now)
        self.relay = self.new_relay()
        self.server = await serve_token_lookup(self.sock, self.store, connect=self.relay)
        config = ProxyConfig(
            bind_host="127.0.0.1", port=0, ledger_db=self.tmp_path / "ledger.db",
            gateway_socket=self.sock, proxy_socket=self.tmp_path / "proxy.sock",
            platform_key="sk-ant-platform", free_models=("claude-sonnet-5-5",),
            monthly_cap_usd=1.0, concurrent_calls=4, requests_per_minute=60,
            global_concurrent_calls=16, max_tokens_ceiling=8192,
            max_body_bytes=4 * 1024 * 1024, upstream_base_url="https://api.anthropic.com")
        self.ledger = Ledger(config.ledger_db)
        calls = ConnectCalls(resolve=TokenCache(self.sock).resolve, gateway_socket=self.sock,
                             monotonic=lambda: self.proxy_clock)
        proxy = MeteringProxy(config=config, ledger=self.ledger,
                              resolve=TokenCache(self.sock).resolve, upstream=None,
                              connect=calls.handle, connect_link=calls.link)
        self.runner = web.AppRunner(proxy.build())
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.runner.cleanup()
        self.server.close()
        await self.server.wait_closed()
        await self.session.close()
        await self.client.close()
        self.ledger.close()
        self.store.close()

    def new_relay(self) -> McpRelay:
        return McpRelay(connect=self.connect, session=self.session, apex_host=APEX)

    async def restart_gateway(self) -> None:
        """A gateway restart: the socket is served by a relay that remembers
        no vendor session."""
        self.server.close()
        await self.server.wait_closed()
        self.relay = self.new_relay()
        self.server = await serve_token_lookup(self.sock, self.store, connect=self.relay)

    async def person(self, sub: str, *servers: str) -> tuple[str, str]:
        """A tenant and its container's platform token, signed in to each
        named vendor through the real start and callback."""
        tenant = self.store.create_tenant(sub=sub, email=f"{sub}@example.com",
                                          timezone="UTC")
        for server in servers:
            started = await self.connect.start(tenant.id, server)
            query = dict(urllib.parse.parse_qsl(
                urllib.parse.urlsplit(started.headers["Location"]).query))
            code = self.fakes[server].approve(query)
            landed = await self.connect.callback(make_mocked_request(
                "GET", "/connect/callback?" + urllib.parse.urlencode(
                    {"code": code, "state": query["state"]})), tenant.id)
            assert "connect=connected" in landed.headers["Location"]
        return tenant.id, self.store.issue_token(tenant.id)

    async def post(self, platform_token: str, message, server: str = "notion", *,
                   method: str = "POST", path: str | None = None):
        """What a container's MCP client sends: (status, the JSON body or None)."""
        data = message if isinstance(message, bytes) else json.dumps(message).encode()
        async with self.client.request(
                method, self.base + (path or f"/connect/{server}/mcp"), data=data,
                headers={"Authorization": f"Bearer {platform_token}",
                         "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"}) as reply:
            text = await reply.text()
            self.container_saw.append(text)
            self.container_saw.append(json.dumps(dict(reply.headers)))
            return reply.status, (json.loads(text) if text else None)

    async def bridge(self, home, platform_token: str, monkeypatch, *names: str):
        """The MIT agent's own MCP client, started on the mcp.json that
        provisioning writes for these connectors: (the bridge, its tools)."""
        home.mkdir(exist_ok=True)
        sync_connectors(home, self.base, list(names))
        monkeypatch.setenv("WAKU_PLATFORM_TOKEN", platform_token)
        bridge = MCPBridge(home / "mcp.json")
        return bridge, await asyncio.to_thread(bridge.start)


def _run(tmp_path, sock_dir, body, **fakes):
    async def run():
        async with Rig(tmp_path, sock_dir, **fakes) as rig:
            return await body(rig)

    return asyncio.run(run())


def _text(answer: dict) -> str:
    return answer["result"]["content"][0]["text"]


# --- the whole path, with the agent's own MCP client ---------------------------

def test_the_agents_mcp_client_reads_notion_through_the_proxy(
        tmp_path, sock_dir, notion, monkeypatch):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        bridge, tools = await rig.bridge(tmp_path / "home", token, monkeypatch, "notion")
        try:
            answer = await asyncio.to_thread(
                bridge.call, "notion", "notion-search", {"query": "roadmap"})
        finally:
            await asyncio.to_thread(bridge.close)
        return tools, answer, (tmp_path / "home" / "mcp.json").read_text()

    tools, answer, config = _run(tmp_path, sock_dir, body, notion=notion)
    assert [tool.name for tool in tools] == ["notion_notion-search",
                                             "notion_notion-create-pages"]
    assert answer == "notion ran notion-search"
    [call] = notion.mcp_calls("tools/call")
    # Field by field: mcp 2.2.0, which uv.lock pins, also sends an empty
    # `_meta` in a call's params, and mcp 2.3.0, which CI installs, does not.
    params = call["body"]["params"]
    assert (params["name"], params["arguments"]) == ("notion-search", {"query": "roadmap"})
    # The vendor was called with the person's token, and the container's
    # platform token went no further than the proxy.
    assert call["authorization"] == "Bearer notion-access-1"
    # Acceptance 2: no vendor token in the file the container reads.
    assert "notion-access" not in config and "notion-refresh" not in config
    assert json.loads(config)["servers"][0]["auth_env"] == "WAKU_PLATFORM_TOKEN"


def test_nothing_the_container_is_answered_holds_a_vendor_token_or_session(
        tmp_path, sock_dir, notion):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        for message in (INITIALIZE, LIST, SEARCH, CREATE):
            await rig.post(token, message)
        await rig.post(token, {"server": "notion"}, path="/connect/link")
        return "\n".join(rig.container_saw)

    saw = _run(tmp_path, sock_dir, body, notion=notion)
    assert "notion ran notion-search" in saw
    for secret in ("notion-access", "notion-refresh", "notion-session", "client-1",
                   notion.url):
        assert secret not in saw
    assert all(platform not in call["authorization"]
               for call in notion.calls("/mcp") for platform in ("wkp_",))


# --- a write is not run ---------------------------------------------------------

def test_a_tool_that_writes_is_refused_with_a_sentence_and_never_reaches_the_vendor(
        tmp_path, sock_dir, notion, monkeypatch):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        bridge, _ = await rig.bridge(tmp_path / "home", token, monkeypatch, "notion")
        try:
            through_the_agent = await asyncio.to_thread(
                bridge.call, "notion", "notion-create-pages", {"title": "x"})
        finally:
            await asyncio.to_thread(bridge.close)
        return through_the_agent, await rig.post(token, CREATE)

    through_the_agent, (status, raw) = _run(tmp_path, sock_dir, body, notion=notion)
    sentence = ("Not run: 'notion-create-pages' changes something in Notion, and a change "
                "needs your yes first. Saying yes is not built on hosted Waku yet. Reading "
                "from Notion works.")
    assert through_the_agent == sentence
    assert (status, _text(raw), raw["result"]["isError"]) == (200, sentence, True)
    assert notion.mcp_calls("tools/call") == []


def test_linears_write_verbs_are_refused_and_its_reads_run(tmp_path, sock_dir, linear):
    async def body(rig):
        _, token = await rig.person("ana", "linear")
        read = {**SEARCH, "params": {"name": "list_issues", "arguments": {}}}
        write = {**SEARCH, "params": {"name": "create_issue", "arguments": {}}}
        return (await rig.post(token, read, "linear"),
                await rig.post(token, write, "linear"))

    (_, read), (_, write) = _run(tmp_path, sock_dir, body, linear=linear)
    assert _text(read) == "linear ran list_issues"
    assert write["result"]["isError"] is True
    assert [c["body"]["params"]["name"] for c in linear.mcp_calls("tools/call")] == [
        "list_issues"]


@pytest.mark.parametrize("method", ["resources/list", "prompts/get", "sampling/createMessage",
                                    "completion/complete", "tools/call/extra"])
def test_a_method_outside_the_closed_set_is_refused_here(tmp_path, sock_dir, notion, method):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return await rig.post(token, {"jsonrpc": "2.0", "id": 3, "method": method})

    status, answer = _run(tmp_path, sock_dir, body, notion=notion)
    assert status == 200
    assert answer["error"]["code"] == -32601
    assert notion.calls("/mcp") == []


# --- acceptance 4: whose connection it is ----------------------------------------

def test_a_second_tenant_cannot_use_the_first_tenants_connection(
        tmp_path, sock_dir, notion, monkeypatch):
    async def body(rig):
        _, ana = await rig.person("ana", "notion")
        _, bob = await rig.person("bob")
        before = len(notion.calls("/mcp"))
        answers = [await rig.post(bob, message) for message in (INITIALIZE, LIST, SEARCH)]
        # Bob's container also writes notion into its own mcp.json by hand.
        bridge, tools = await rig.bridge(tmp_path / "bob", bob, monkeypatch, "notion")
        await asyncio.to_thread(bridge.close)
        return answers, tools, len(notion.calls("/mcp")) - before, await rig.post(ana, SEARCH)

    answers, tools, reached_notion, (_, anas) = _run(tmp_path, sock_dir, body, notion=notion)
    for status, answer in answers:
        assert status == 200
        assert answer["error"]["message"] == (
            f"Notion is not connected. Connect it at https://{APEX}/setup.")
    assert tools == []
    assert reached_notion == 0
    assert _text(anas) == "notion ran notion-search"


def test_a_call_names_no_tenant_and_a_token_nobody_holds_is_refused(
        tmp_path, sock_dir, notion):
    async def body(rig):
        tenant_id, token = await rig.person("ana", "notion")
        rig.store.set_status(tenant_id, "active")
        return (await rig.post("wkp_" + "x" * 43, SEARCH),
                await rig.post("", SEARCH),
                await rig.post(token, {**SEARCH, "tenant": "someone-else"}))

    unknown, missing, (status, extra_field) = _run(tmp_path, sock_dir, body, notion=notion)
    assert unknown == (401, {"ok": False, "error": "not_signed_in"})
    assert missing == (401, {"ok": False, "error": "not_signed_in"})
    # A field the message is not supposed to have changes nothing about who.
    assert (status, _text(extra_field)) == (200, "notion ran notion-search")


def test_a_disabled_tenants_container_is_refused(tmp_path, sock_dir, notion):
    async def body(rig):
        tenant_id, token = await rig.person("ana", "notion")
        rig.store.set_status(tenant_id, "disabled")
        return await rig.post(token, SEARCH)

    assert _run(tmp_path, sock_dir, body, notion=notion)[0] == 401
    assert notion.mcp_calls("tools/call") == []


# --- the door itself ---------------------------------------------------------------

def test_only_a_listed_connector_and_only_a_post_of_one_message(tmp_path, sock_dir, notion):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return (await rig.post(token, SEARCH, "slack"),
                await rig.post(token, SEARCH, "notion%2F..%2Fslack"),
                await rig.post(token, SEARCH, method="GET"),
                await rig.post(token, SEARCH, method="DELETE"),
                await rig.post(token, [SEARCH, LIST]),
                await rig.post(token, b"not json"),
                await rig.post(token, {**SEARCH, "pad": "x" * MAX_MESSAGE_BYTES}),
                await rig.post(token, {"id": 1, "method": "tools/list"}),
                await rig.post(token, {"jsonrpc": "2.0", "method": "tools/list"}))

    (unlisted, traversal, get, delete, batch, garbage, too_long, no_version,
     no_id) = _run(tmp_path, sock_dir, body, notion=notion)
    assert unlisted[0] == traversal[0] == 404
    assert get[0] == delete[0] == 405
    for refused in (batch, garbage, too_long, no_version, no_id):
        assert refused == (400, {"ok": False, "error": "bad_request"})
    assert notion.mcp_calls("tools/call") == []


def test_a_notification_is_taken_with_a_202_and_no_body(tmp_path, sock_dir, notion):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return await rig.post(token, {"jsonrpc": "2.0",
                                      "method": "notifications/initialized"})

    assert _run(tmp_path, sock_dir, body, notion=notion) == (202, None)
    assert notion.calls("/mcp") == []


def test_a_tenant_may_call_one_connector_sixty_times_a_minute(
        tmp_path, sock_dir, notion, linear):
    async def body(rig):
        _, ana = await rig.person("ana", "notion", "linear")
        _, bob = await rig.person("bob", "notion")
        taken = [(await rig.post(ana, LIST))[0] for _ in range(CALLS_PER_MINUTE)]
        over = await rig.post(ana, LIST)
        other_server = await rig.post(ana, LIST, "linear")
        other_tenant = await rig.post(bob, LIST)
        rig.proxy_clock += 60.0
        return taken, over, other_server[0], other_tenant[0], (await rig.post(ana, LIST))[0]

    taken, over, other_server, other_tenant, later = _run(
        tmp_path, sock_dir, body, notion=notion, linear=linear)
    assert CALLS_PER_MINUTE == 60
    assert taken == [200] * 60
    assert over[0] == 429
    assert over[1]["error"]["message"].startswith("Too many calls")
    assert (other_server, other_tenant, later) == (200, 200, 200)


# --- the vendor's answer, in each shape -----------------------------------------------

@pytest.mark.parametrize("sse", [False, True])
def test_a_compressed_answer_is_read_as_json_and_as_an_event_stream(
        tmp_path, sock_dir, notion, sse):
    notion.sse = sse
    notion.tool_text["notion-search"] = "Roadmap 2027 — 路线图"

    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return await rig.post(token, SEARCH), await rig.post(token, LIST)

    (status, answer), (_, listed) = _run(tmp_path, sock_dir, body, notion=notion)
    assert (status, answer["id"]) == (200, 7)
    assert _text(answer) == "Roadmap 2027 — 路线图"
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "notion-search", "notion-create-pages"]
    # The gateway asked for gzip, and the fake compressed every answer.
    assert all("gzip" in call["headers"]["accept-encoding"] for call in notion.calls("/mcp"))


def test_an_answer_too_large_to_read_says_so(tmp_path, sock_dir, notion):
    notion.tool_text["notion-search"] = "x" * (MAX_ANSWER_BYTES + 10)

    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return await rig.post(token, SEARCH)

    status, answer = _run(tmp_path, sock_dir, body, notion=notion)
    assert status == 200
    assert answer["error"]["message"].startswith("Notion's answer was too large")


def test_a_large_answer_under_the_limit_crosses_the_socket(tmp_path, sock_dir, notion):
    # Longer than jsonsock's ordinary line, which is 64 KB.
    notion.tool_text["notion-search"] = "y" * (jsonsock.MAX_LINE * 4)

    async def body(rig):
        _, token = await rig.person("ana", "notion")
        return await rig.post(token, SEARCH)

    status, answer = _run(tmp_path, sock_dir, body, notion=notion)
    assert (status, len(_text(answer))) == (200, jsonsock.MAX_LINE * 4)


# --- the vendor's session and the person's token ------------------------------------------

def test_the_gateway_keeps_the_vendors_session_and_opens_one_per_person(
        tmp_path, sock_dir, notion):
    async def body(rig):
        _, ana = await rig.person("ana", "notion")
        _, bob = await rig.person("bob", "notion")
        for token in (ana, bob, ana, bob):
            await rig.post(token, SEARCH)

    _run(tmp_path, sock_dir, body, notion=notion)
    assert notion.opened == 2
    sessions = {call["authorization"]: call["headers"]["mcp-session-id"]
                for call in notion.mcp_calls("tools/call")}
    assert sessions == {"Bearer notion-access-1": "notion-session-1",
                        "Bearer notion-access-2": "notion-session-2"}


def test_a_session_the_vendor_forgot_is_opened_again_and_the_call_runs_once(
        tmp_path, sock_dir, notion):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        await rig.post(token, SEARCH)
        notion.sessions.clear()                 # the vendor redeployed
        return await rig.post(token, SEARCH)

    status, answer = _run(tmp_path, sock_dir, body, notion=notion)
    assert (status, _text(answer)) == (200, "notion ran notion-search")
    assert notion.opened == 2


def test_a_gateway_restart_does_not_break_a_running_containers_calls(
        tmp_path, sock_dir, notion, monkeypatch):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        bridge, _ = await rig.bridge(tmp_path / "home", token, monkeypatch, "notion")
        try:
            await rig.restart_gateway()
            return await asyncio.to_thread(bridge.call, "notion", "notion-search", {})
        finally:
            await asyncio.to_thread(bridge.close)

    assert _run(tmp_path, sock_dir, body, notion=notion) == "notion ran notion-search"


def test_a_token_the_vendor_refuses_is_renewed_and_the_call_sent_again(
        tmp_path, sock_dir, notion):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        await rig.post(token, SEARCH)
        notion.access_tokens.clear()            # the vendor ended it early
        return await rig.post(token, SEARCH)

    status, answer = _run(tmp_path, sock_dir, body, notion=notion)
    assert (status, _text(answer)) == (200, "notion ran notion-search")
    assert notion.mcp_calls("tools/call")[-1]["authorization"] == "Bearer notion-access-2"


def test_a_sign_in_the_vendor_ended_tells_the_person_to_connect_again(
        tmp_path, sock_dir, notion):
    async def body(rig):
        tenant_id, token = await rig.person("ana", "notion")
        notion.access_tokens.clear()
        notion.refresh_tokens.clear()           # the person removed Waku at Notion
        answer = await rig.post(token, SEARCH)
        return answer, rig.store.connection(tenant_id, "notion").needs_reconnect

    (status, answer), marked = _run(tmp_path, sock_dir, body, notion=notion)
    assert status == 200
    assert answer["error"]["message"] == (
        f"Waku lost its sign-in to Notion. Connect it again at https://{APEX}/setup.")
    assert marked is True


def test_an_unreachable_vendor_is_a_sentence_and_no_log_line_holds_a_token(
        tmp_path, sock_dir, notion, caplog):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        await rig.post(token, SEARCH)
        await rig.post(token, CREATE)
        notion.stop()
        return token, await rig.post(token, SEARCH)

    with caplog.at_level(logging.DEBUG):
        token, (status, answer) = _run(tmp_path, sock_dir, body, notion=notion)
    assert status == 200
    assert answer["error"]["message"] == "Notion did not answer. Try again in a moment."
    logged = "\n".join(record.getMessage() for record in caplog.records
                       if record.name.startswith("hosted"))
    assert "to notion failed" in logged
    for secret in ("notion-access-1", "notion-refresh-1", token, "roadmap", "127.0.0.1"):
        assert secret not in logged


# --- B3: where a person starts the sign-in ---------------------------------------------

def test_a_container_learns_which_connectors_the_platform_offers(
        tmp_path, sock_dir, notion, linear, monkeypatch):
    """The MIT agent asks the real proxy and gateway, as it does when it
    builds its tools in a hosted container."""
    from waku.tools import connectors

    async def body(rig):
        _, token = await rig.person("ana")
        monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", rig.base)
        monkeypatch.setenv("WAKU_PLATFORM_TOKEN", token)
        return await asyncio.to_thread(connectors.hosted_offered)

    assert _run(tmp_path, sock_dir, body, notion=notion, linear=linear) == (
        "Notion", "Linear")
    # A platform with one connector says one.
    (tmp_path / "second").mkdir()
    assert _run(tmp_path / "second", sock_dir, body, linear=linear) == ("Linear",)


def test_the_link_is_the_start_address_on_the_apex(tmp_path, sock_dir, notion, linear):
    async def body(rig):
        _, token = await rig.person("ana", "notion")
        ask = lambda sent: rig.post(token, sent, path="/connect/link")   # noqa: E731
        return (await ask({"server": "notion"}), await ask({"server": "linear"}),
                await ask({"server": "github"}), await ask({"server": 3}),
                await ask({"server": "notion", "tenant": "x"}),
                await rig.post("wkp_nobody", {"server": "notion"}, path="/connect/link"))

    notion_link, linear_link, unlisted, not_text, extra, stranger = _run(
        tmp_path, sock_dir, body, notion=notion, linear=linear)
    assert notion_link == (200, {
        "ok": True, "title": "Notion", "connected": True,
        "url": f"https://{APEX}/connect/start?server=notion"})
    assert linear_link[1]["connected"] is False
    assert linear_link[1]["url"] == f"https://{APEX}/connect/start?server=linear"
    assert unlisted == (200, {"ok": False, "error": "not_listed",
                              "available": ["Notion", "Linear"]})
    assert not_text[0] == extra[0] == 400
    assert stranger[0] == 401


def test_with_connectors_off_the_gateway_says_so(tmp_path, sock_dir):
    async def body():
        store = ControlDb(tmp_path / "control.db")
        server = await serve_token_lookup(sock_dir / "gateway.sock", store)
        try:
            return (await jsonsock.ask(sock_dir / "gateway.sock", {
                        "op": "connect_mcp", "tenant": "t", "server": "notion",
                        "message": SEARCH}),
                    await jsonsock.ask(sock_dir / "gateway.sock", {
                        "op": "connect_link", "tenant": "t", "server": "notion"}),
                    await jsonsock.ask(sock_dir / "gateway.sock", {
                        "op": "connect_mcp", "tenant": "t", "token": "x"}))
        finally:
            server.close()
            await server.wait_closed()
            store.close()

    mcp, link, extra_field = asyncio.run(body())
    assert mcp == link == {"ok": False, "error": "connectors_not_configured"}
    assert extra_field == {"error": "connect_mcp does not take ['token']"}
