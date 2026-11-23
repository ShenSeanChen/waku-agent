"""DETERMINISTIC EVAL -- how a container learns its connectors (spec 029,
tasks B2 and B3, and acceptance 6).

When Ana signs in to Notion, the gateway asks the spawner to provision her
tenant again with the names of her connectors. Provisioning writes a `notion`
server into her container's mcp.json, pointing at the metering proxy. Her
agent reads mcp.json before each run and builds itself again when the list
changed. "connect Notion" in a hosted chat answers with the address on
agent.waku.one that starts the sign-in.

The gateway's side is the real Gateway, Launcher and Connect with a fake
spawner and a fake vendor. The container's side is the MIT code from waku/.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest
from gatewaylib import APEX, FakeSpawner, FakeVendor, Harness, ops, sign_in

from hosted.core import requests
from hosted.core.provision import connector_server, provision, sync_connectors
from hosted.core.tenant import TenantDirs
from hosted.gateway.spawner_client import SpawnerClient
from hosted.spawner import template
from waku.alwayson import approvals
from waku.tools import connectors

PROXY = "http://10.88.0.1:8788"
COOKIE = "__Host-waku_session"
NOTION = {"name": "notion", "url": f"{PROXY}/connect/notion/mcp",
          "auth_env": "WAKU_PLATFORM_TOKEN"}
LINEAR = {"name": "linear", "url": f"{PROXY}/connect/linear/mcp",
          "auth_env": "WAKU_PLATFORM_TOKEN"}
GOOD_ID = "t" + "a" * 11


def _servers(home) -> list[dict]:
    return json.loads((home / "mcp.json").read_text(encoding="utf-8"))["servers"]


# --- provisioning writes the list into mcp.json --------------------------------

def test_a_connector_entry_points_at_the_proxy_and_holds_no_token():
    assert connector_server(PROXY + "/", "notion") == NOTION


def test_the_file_lists_exactly_the_connectors_the_tenant_signed_in_to(tmp_path):
    assert sync_connectors(tmp_path, PROXY, ["notion"]) is True
    assert _servers(tmp_path) == [NOTION]
    assert sync_connectors(tmp_path, PROXY, ["notion"]) is False      # nothing to do
    assert sync_connectors(tmp_path, PROXY, ["linear", "notion"]) is True
    assert _servers(tmp_path) == [NOTION, LINEAR]
    # Acceptance 6: disconnecting takes the server off the container's list.
    assert sync_connectors(tmp_path, PROXY, ["linear"]) is True
    assert _servers(tmp_path) == [LINEAR]
    assert sync_connectors(tmp_path, PROXY, []) is True
    assert _servers(tmp_path) == []


def test_a_tenants_own_servers_are_never_removed_or_replaced(tmp_path):
    own_notion = {"name": "notion", "url": "https://mcp.notion.com/mcp", "oauth": True}
    other = {"name": "fs", "command": "npx"}
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": [other, own_notion]}))
    # Their own `notion` keeps its name: the platform's entry is not added.
    assert sync_connectors(tmp_path, PROXY, ["notion", "linear"]) is True
    assert _servers(tmp_path) == [other, own_notion, LINEAR]
    assert sync_connectors(tmp_path, PROXY, []) is True
    assert _servers(tmp_path) == [other, own_notion]


def test_a_name_that_is_not_on_the_list_is_never_written(tmp_path):
    assert sync_connectors(tmp_path, PROXY, ["slack", "../x", "notion"]) is True
    assert _servers(tmp_path) == [NOTION]


def test_without_a_proxy_a_symlink_or_a_wrong_shape_nothing_is_written(tmp_path):
    assert sync_connectors(tmp_path, "", ["notion"]) is False
    assert not (tmp_path / "mcp.json").exists()
    (tmp_path / "mcp.json").write_text("not json")
    assert sync_connectors(tmp_path, PROXY, ["notion"]) is False
    assert (tmp_path / "mcp.json").read_text(encoding="utf-8") == "not json"
    (tmp_path / "mcp.json").unlink()
    target = tmp_path / "elsewhere.json"
    (tmp_path / "mcp.json").symlink_to(target)
    assert sync_connectors(tmp_path, PROXY, ["notion"]) is False
    assert not target.exists()


def test_provisioning_adds_the_connectors_beside_waku_memory(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("soul")
    dirs = TenantDirs(home=tmp_path / "data", env=tmp_path / "work")
    provision(dirs, soul, connect_base_url=PROXY, connectors=["notion"])
    assert [s["name"] for s in _servers(dirs.home)] == ["waku_memory", "notion"]
    provision(dirs, soul, connect_base_url=PROXY, connectors=[])
    assert [s["name"] for s in _servers(dirs.home)] == ["waku_memory"]


# --- the spawner is told which ----------------------------------------------------

def test_provision_and_start_take_a_list_of_connector_names():
    parsed = requests.parse({"op": "provision", "tenant_id": GOOD_ID, "project_id": 7,
                             "connectors": ["notion", "linear"]})
    assert parsed.connectors == ("notion", "linear")
    assert requests.parse({"op": "provision", "tenant_id": GOOD_ID,
                           "project_id": 7}).connectors == ()


@pytest.mark.parametrize("bad", [["slack"], ["notion", "notion"], "notion", [3],
                                 ["notion/../x"], {"notion": True}, [["notion"]]])
def test_the_spawner_refuses_anything_that_is_not_a_list_of_listed_names(bad):
    with pytest.raises(requests.Invalid, match="connector names"):
        requests.parse({"op": "provision", "tenant_id": GOOD_ID, "project_id": 7,
                        "connectors": bad})


@pytest.mark.parametrize("op", ["stop", "list", "tenants", "task"])
def test_no_other_operation_takes_connectors(op):
    payload = {"op": op, "connectors": ["notion"]}
    if op in ("stop", "task"):
        payload["tenant_id"] = GOOD_ID
    if op == "task":
        payload["task"] = "backup"
    with pytest.raises(requests.Invalid, match="does not take"):
        requests.parse(payload)


def _config(**changes) -> template.SpawnerConfig:
    import dataclasses

    from test_container_template import CONFIG

    return dataclasses.replace(CONFIG, **changes)


def test_the_provision_container_is_told_the_proxy_and_the_names():
    with_proxy = _config(platform_base_url=PROXY, platform_model="m",
                         platform_small_model="m")
    assert template.provision_env(with_proxy, ("notion", "linear")) == [
        f"WAKU_CONNECT_BASE_URL={PROXY}", "WAKU_CONNECTORS=notion,linear"]
    # An empty list is still sent, so the last connector's entry is removed.
    assert template.provision_env(with_proxy) == [
        f"WAKU_CONNECT_BASE_URL={PROXY}", "WAKU_CONNECTORS="]
    # No metering proxy: a connector's entry would point at nothing.
    assert template.provision_env(_config(platform_base_url=""), ("notion",)) == []
    with pytest.raises(ValueError, match="connector names"):
        template.provision_env(with_proxy, ("notion,evil=1",))


def test_the_gateways_client_sends_the_names_only_when_there_are_some(sock_dir):
    from hosted import jsonsock

    async def body():
        seen: list[dict] = []

        async def handler(request: dict) -> dict:
            seen.append(request)
            requests.parse(request)          # the real spawner's own check
            return {"ok": True, "address": "10.88.0.2", "port": 7777}

        server = await jsonsock.serve(sock_dir / "s.sock", handler, mode=0o660)
        client = SpawnerClient(sock_dir / "s.sock")
        try:
            await client.provision(GOOD_ID, 7)
            await client.provision(GOOD_ID, 7, ("notion",))
            await client.start(GOOD_ID, 7, "UTC", "t" * 43,
                               connectors=("notion", "linear"))
        finally:
            server.close()
            await server.wait_closed()
        return seen

    first, second, start = asyncio.run(body())
    assert "connectors" not in first
    assert second["connectors"] == ["notion"]
    assert start["connectors"] == ["notion", "linear"]


# --- the gateway tells the spawner when the list changes --------------------------

@pytest.fixture
def notion():
    fake = FakeVendor("notion")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, notion):
    built = Harness(tmp_path, FakeSpawner())
    built.use_connect(notion=notion)
    return built


def _run(harness: Harness, body):
    async def run():
        await harness.start()

        async def changed(tenant_id: str, server: str) -> None:
            await harness.launcher.reprovision(tenant_id)

        harness.connect.on_change = changed
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


async def _connect(harness: Harness, notion: FakeVendor, cookie: str) -> None:
    _, headers, _ = await harness.send("GET", "/connect/start?server=notion", host=APEX,
                                       cookie=f"{COOKIE}={cookie}")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(headers["location"]).query))
    await harness.send(
        "GET", "/connect/callback?" + urllib.parse.urlencode(
            {"code": notion.approve(query), "state": query["state"]}),
        host=APEX, cookie=f"{COOKIE}={cookie}")


def test_signing_in_writes_the_running_containers_list_without_a_restart(harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)      # the sign-in pre-warms
        before = len(ops(harness.spawner, "provision"))
        await _connect(harness, notion, cookie)
        return tenant_id, ops(harness.spawner, "provision")[before:]

    tenant_id, provisions = _run(harness, body)
    assert [(p["tenant_id"], p["connectors"]) for p in provisions] == [
        (tenant_id, ["notion"])]
    # The container was not stopped or started again for it.
    assert len(ops(harness.spawner, "start")) == 1
    assert ops(harness.spawner, "stop") == []


def test_disconnecting_writes_the_list_without_the_server(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        await _connect(harness, notion, cookie)
        await harness.json_post("/connect/disconnect", {"server": "notion"}, host=APEX,
                                cookie=f"{COOKIE}={cookie}")
        return ops(harness.spawner, "provision")[-1]

    assert _run(harness, body)["connectors"] == []


def test_a_stopped_container_is_not_provisioned_and_its_next_start_carries_the_list(
        harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        await harness.launcher.stop(tenant_id)
        before = len(ops(harness.spawner, "provision"))
        await _connect(harness, notion, cookie)
        untouched = ops(harness.spawner, "provision")[before:]
        await harness.launcher.start(harness.store.tenant_by_id(tenant_id))
        return untouched, ops(harness.spawner, "start")

    untouched, starts = _run(harness, body)
    assert untouched == []
    assert "connectors" not in starts[0]           # before the sign-in: none
    assert starts[-1]["connectors"] == ["notion"]


def test_a_spawner_that_refuses_does_not_undo_the_sign_in(harness, notion):
    from hosted.gateway.spawner_client import SpawnerError

    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        harness.spawner.fail_provision = SpawnerError("busy")
        await _connect(harness, notion, cookie)
        return [row.server for row in harness.store.connections(tenant_id)]

    assert _run(harness, body) == ["notion"]


# --- the container's side: waku/ (MIT) ----------------------------------------------

OFFERED = {"ok": False, "error": "not_listed", "available": ["Notion", "Linear"]}


@pytest.fixture
def hosted_env(monkeypatch):
    from waku.alwayson import senders

    monkeypatch.setenv("WAKU_HOSTED", "1")
    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", PROXY)
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "wkp_test")
    # The platform, asked which connectors it has. No test here reaches PROXY.
    monkeypatch.setattr(senders, "post_json", lambda url, payload, headers: dict(OFFERED))


def test_the_agent_is_built_again_when_provisioning_changes_the_list(tmp_path, hosted_env):
    """AgentRun._build compares connectors.ready() before each run (spec 025),
    and the dashboard's agent does the same on hosted Waku."""
    sync_connectors(tmp_path, PROXY, [])
    before = connectors.ready(tmp_path)
    sync_connectors(tmp_path, PROXY, ["notion"])
    after = connectors.ready(tmp_path)
    assert (before, after) == (frozenset(), frozenset({"notion"}))


def test_the_dashboards_agent_follows_the_list_only_on_hosted(tmp_path, monkeypatch):
    from waku.ops import browser_agent

    rebuilt: list[int] = []
    servers = [frozenset()]
    monkeypatch.setattr(browser_agent, "_agent", object())
    monkeypatch.setattr(browser_agent, "_connectors", frozenset())
    monkeypatch.setattr(browser_agent, "_ready_servers", lambda: servers[0])
    monkeypatch.setattr(browser_agent, "rebuild", lambda: rebuilt.append(1))

    servers[0] = frozenset({"notion"})
    browser_agent.follow_connectors()                 # a person's own machine
    assert rebuilt == []
    monkeypatch.setenv("WAKU_HOSTED", "1")
    browser_agent.follow_connectors()
    browser_agent.follow_connectors()                 # unchanged since: no rebuild
    assert rebuilt == [1]


def test_a_platform_connectors_tools_run_without_a_question(tmp_path, hosted_env):
    sync_connectors(tmp_path, PROXY, ["notion"])
    connector = connectors.connector_of(tmp_path, "notion")
    assert connector.kind == "platform"
    for tool in ("notion-search", "notion-create-pages"):
        # A write runs here too: the platform is what refuses it
        # (test_connect_relay.py), because nobody on hosted Waku can be asked.
        assert approvals.mode(f"notion_{tool}", {}, connector=connector,
                              own_name=tool) == approvals.ALLOW


def test_the_same_address_is_an_ordinary_server_on_a_persons_own_machine(
        tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", PROXY)
    sync_connectors(tmp_path, PROXY, ["notion"])
    connector = connectors.connector_of(tmp_path, "notion")
    assert connector.kind == "own"
    assert approvals.mode("notion_notion-create-pages", {}, connector=connector,
                          own_name="notion-create-pages") == approvals.ASK


def test_a_server_elsewhere_is_not_the_platforms_on_hosted(tmp_path, hosted_env):
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "x", "url": "https://evil.example/connect/notion/mcp"},
        {"name": "y", "url": f"{PROXY}.evil.example/connect/notion/mcp"}]}))
    assert connectors.connector_of(tmp_path, "x").kind == "own"
    assert connectors.connector_of(tmp_path, "y").kind == "own"


# --- B3: "connect Notion" in a hosted chat -------------------------------------------

def _connect_tool(tmp_path):
    return next(tool for tool in connectors.make_tools(tmp_path)
                if tool.name == "connect_tool")


def test_connect_notion_in_a_hosted_chat_answers_with_the_link(
        tmp_path, hosted_env, monkeypatch):
    asked: list[tuple] = []

    def post(url, payload, headers):
        asked.append((url, payload, headers))
        return {"ok": True, "title": "Notion", "connected": False,
                "url": f"https://{APEX}/connect/start?server=notion"}

    reply = connectors.hosted_connect("Notion", post=post)
    assert asked == [(f"{PROXY}/connect/link", {"server": "notion"},
                      {"Authorization": "Bearer wkp_test"})]
    assert f"https://{APEX}/connect/start?server=notion" in reply
    assert reply.startswith("Open this link and sign in to Notion.")
    # It needs no yes: nothing is connected by the call, and nobody on hosted
    # Waku could be asked.
    assert approvals.mode("connect_tool", {"name_or_url": "notion"}) == approvals.ALLOW
    assert not (tmp_path / "mcp.json").exists()


def test_the_hosted_tool_itself_asks_the_platform_and_writes_nothing(
        tmp_path, hosted_env, monkeypatch):
    from waku.alwayson import senders

    monkeypatch.setattr(senders, "post_json", lambda url, payload, headers: {
        "ok": True, "title": "Linear", "connected": True,
        "url": f"https://{APEX}/connect/start?server=linear"})
    reply = _connect_tool(tmp_path).fn(name_or_url="linear")
    assert reply == ("Linear is already connected. To sign in to it again, open:\n"
                     f"https://{APEX}/connect/start?server=linear")
    assert not (tmp_path / "mcp.json").exists()


def test_a_server_hosted_waku_does_not_offer_is_refused_in_words(tmp_path, hosted_env):
    def post(url, payload, headers):
        return {"ok": False, "error": "not_listed", "available": ["Notion", "Linear"]}

    reply = connectors.hosted_connect("https://mcp.example.com/mcp", post=post)
    assert reply == ("Not connected: on hosted Waku the connectors are Notion and Linear. "
                     "Another server cannot be added here.")


def test_a_platform_that_does_not_answer_is_a_sentence(tmp_path, hosted_env):
    from waku.alwayson.senders import SendFailed

    def post(url, payload, headers):
        raise SendFailed("HTTP 503")

    assert connectors.hosted_connect("notion", post=post).startswith("Not connected:")


def test_on_a_persons_own_machine_connect_tool_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    assert approvals.mode("connect_tool", {"name_or_url": "notion"}) == approvals.ASK
    assert approvals.mode("disconnect_tool", {"name": "notion"}) == approvals.ASK
    assert "needs the owner's yes" in _connect_tool(tmp_path).fn(name_or_url="notion")


# --- the model is told what this server can connect, and nothing else ---------------
#
# 2026-11-19, Slack: the hosted agent offered "shall I connect GitHub?", was
# told yes, and then answered that hosted Waku has only Notion and Linear. It
# had read the starter list of a person's own machine in connect_tool's
# description.

def _told(tmp_path, offered=lambda: ("Notion", "Linear")) -> dict:
    """What each connector tool tells the model: its description and the
    words under its arguments."""
    return {tool.name: tool.description + " " + json.dumps(tool.input_schema)
            for tool in connectors.make_tools(tmp_path, offered=offered)}


def test_a_hosted_agent_is_told_only_the_connectors_this_server_offers(
        tmp_path, hosted_env):
    told = _told(tmp_path)
    for name in ("connector_status", "connect_tool"):
        assert "On this server Notion and Linear can be connected." in told[name]
        assert "No other service can be connected here yet." in told[name]
    everything = " ".join(told.values()).lower()
    # No name from the starter list that the platform did not give, and no
    # way in that hosted Waku does not have.
    assert "github" not in everything and "starter" not in everything
    assert "https" not in everything and "writes" not in everything
    assert "do not offer it" in told["connect_tool"]


def test_the_names_are_the_platforms_and_not_a_list_in_this_file(tmp_path, hosted_env):
    told = _told(tmp_path, offered=lambda: ("Linear",))
    assert "On this server Linear can be connected." in told["connect_tool"]
    assert "notion" not in " ".join(told.values()).lower()
    assert "e.g. linear" in told["connect_tool"]


def test_the_tools_ask_the_platform_once_when_they_are_built(tmp_path, hosted_env,
                                                             monkeypatch):
    from waku.alwayson import senders

    asked: list[tuple] = []

    def post(url, payload, headers):
        asked.append((url, payload, headers))
        return {"ok": False, "error": "not_listed", "available": ["Linear"]}

    monkeypatch.setattr(senders, "post_json", post)
    told = {tool.name: tool.description for tool in connectors.make_tools(tmp_path)}
    assert asked == [(f"{PROXY}/connect/link", {"server": ""},
                      {"Authorization": "Bearer wkp_test"})]
    assert "On this server Linear can be connected." in told["connect_tool"]


@pytest.mark.parametrize("answer", [
    None,                                               # the platform did not answer
    {"ok": False, "error": "connectors_not_configured"},
    {"ok": False, "error": "not_listed", "available": "Notion"},
    {"ok": True, "title": "", "url": "https://x.example/"},
])
def test_a_platform_that_names_no_list_means_no_offer(tmp_path, hosted_env, monkeypatch,
                                                      answer):
    from waku.alwayson import senders

    def post(url, payload, headers):
        if answer is None:
            raise senders.SendFailed("HTTP 503")
        return answer

    monkeypatch.setattr(senders, "post_json", post)
    assert connectors.hosted_offered() is None
    told = " ".join(tool.description for tool in connectors.make_tools(tmp_path)).lower()
    assert "do not offer to connect one" in told
    for name in ("notion", "linear", "github"):
        assert name not in told


def test_a_platform_with_no_connector_says_so(tmp_path, hosted_env):
    told = _told(tmp_path, offered=lambda: ())
    assert "No connector can be connected on this server yet." in told["connect_tool"]


def test_connector_status_on_hosted_ends_with_what_can_be_connected(tmp_path, hosted_env):
    """The sentence a hosted agent used to read here named the starter list:
    "A starter name connects one: linear, notion, github"."""
    status = next(tool for tool in connectors.make_tools(tmp_path)
                  if tool.name == "connector_status").fn
    offer = ("On this server Notion and Linear can be connected. No other service can "
             "be connected here yet.")
    assert status() == "No connector is connected yet.\n" + offer
    sync_connectors(tmp_path, PROXY, ["notion"])
    assert status() == "Notion (notion): connected.\n" + offer


def test_on_a_persons_own_machine_the_tools_say_what_they_said(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_HOSTED", raising=False)

    def never():
        raise AssertionError("a person's own machine asks no platform")

    tools = {tool.name: tool for tool in connectors.make_tools(tmp_path, offered=never)}
    assert "starter list (linear, notion, github)" in tools["connect_tool"].description
    assert "the server's https address" in tools["connect_tool"].description
    assert set(tools["connect_tool"].input_schema["properties"]) == {"name_or_url", "writes"}
    assert tools["connector_status"].description.startswith(
        "Whether each connected service (Notion, Linear, any MCP server) can be used")
    assert tools["connector_status"].fn() == (
        "No connector is added yet. A starter name connects one: linear, notion, github")
