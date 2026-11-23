"""DETERMINISTIC EVAL -- what a tenant's dashboard is told about Slack
(waku-agent spec 030).

`GET /hosted/slack` on a tenant's own host answers the facts the Slack card
on the Connections page shows: whether this server has a Slack app, whether
this tenant connected a workspace, the workspace's name, and the two apex
addresses the card links to. The gateway answers it itself. It reads
`slack_install` and changes nothing, and no token is in the answer.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_BOT_TOKEN,
    FakeSlack,
    FakeSpawner,
    Harness,
    cookie_value,
    sign_in,
)

APEX_COOKIE = "__Host-waku_session"
TENANT_COOKIE = "__Host-waku_tenant"
PATH = "/hosted/slack"


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, slack):
    built = Harness(tmp_path, FakeSpawner())
    built.use_slack(slack)
    return built


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


async def _enter(harness: Harness, **claims) -> tuple[str, str, str]:
    """(tenant id, apex cookie, the tenant host's cookie header)."""
    tenant, apex_cookie, code = await sign_in(harness, **claims)
    _, headers, _ = await harness.send("GET", f"/auth/enter?code={code}",
                                       host=f"{tenant}.{APEX}")
    return tenant, apex_cookie, f"{TENANT_COOKIE}={cookie_value(headers, TENANT_COOKIE)}"


async def _connect(harness: Harness, apex_cookie: str) -> None:
    _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                       cookie=f"{APEX_COOKIE}={apex_cookie}")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["location"]).query)["state"][0]
    status, _, _ = await harness.send(
        "GET", f"/slack/oauth/callback?code=c&state={state}", host=APEX,
        cookie=f"{APEX_COOKIE}={apex_cookie}")
    assert status == 302


async def _ask(harness: Harness, tenant: str, cookie: str | None, method: str = "GET"):
    status, headers, body = await harness.send(method, PATH, host=f"{tenant}.{APEX}",
                                               cookie=cookie)
    return status, headers, body


def test_a_person_with_no_workspace_is_told_where_to_add_one(harness):
    async def body():
        tenant, _, cookie = await _enter(harness)
        return await _ask(harness, tenant, cookie)

    status, headers, raw = _run(harness, body)

    assert status == 200 and headers["content-type"].startswith("application/json")
    assert json.loads(raw) == {
        "available": True, "connected": False, "needs_reconnect": False, "team_name": "",
        "install_url": f"https://{APEX}/slack/install",
        "setup_url": f"https://{APEX}/setup",
    }


def test_a_connected_workspace_is_named_and_no_token_is_in_the_answer(harness):
    async def body():
        tenant, apex_cookie, cookie = await _enter(harness)
        await _connect(harness, apex_cookie)
        return await _ask(harness, tenant, cookie)

    status, _, raw = _run(harness, body)
    answer = json.loads(raw)

    assert status == 200
    assert (answer["connected"], answer["team_name"]) == (True, "Ana & Co")
    assert answer["needs_reconnect"] is False
    assert set(answer) == {"available", "connected", "needs_reconnect", "team_name",
                           "install_url", "setup_url"}
    assert SLACK_BOT_TOKEN.encode() not in raw


def test_a_workspace_whose_token_cannot_be_opened_is_not_connected(harness):
    async def body():
        tenant, apex_cookie, cookie = await _enter(harness)
        await _connect(harness, apex_cookie)
        install = harness.store.slack_install(tenant)
        harness.store.set_slack_reconnect(install.team_id, True)
        return await _ask(harness, tenant, cookie)

    _, _, raw = _run(harness, body)
    answer = json.loads(raw)

    assert (answer["connected"], answer["needs_reconnect"]) == (False, True)
    assert answer["team_name"] == "Ana & Co"


def test_one_persons_workspace_is_not_told_to_another(harness):
    async def body():
        first, apex_cookie, _ = await _enter(harness)
        await _connect(harness, apex_cookie)
        second, _, cookie = await _enter(harness, sub="another-person",
                                         email="other@example.com")
        assert first != second
        mine = await _ask(harness, second, cookie)
        theirs = await _ask(harness, first, cookie)
        return mine, theirs

    (status, _, raw), (other_status, _, other_raw) = _run(harness, body)

    assert status == 200 and json.loads(raw)["connected"] is False
    assert other_status != 200 and b"Ana" not in other_raw


def test_nobody_is_told_anything_without_a_session(harness):
    async def body():
        tenant, _, _ = await _enter(harness)
        return await _ask(harness, tenant, None)

    status, _, raw = _run(harness, body)

    assert status != 200 and b"install_url" not in raw


def test_a_server_with_no_slack_app_says_so(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())

    async def body():
        tenant, _, cookie = await _enter(harness)
        return await _ask(harness, tenant, cookie)

    status, _, raw = _run(harness, body)

    assert status == 200
    assert json.loads(raw) == {"available": False, "connected": False,
                               "needs_reconnect": False, "team_name": "",
                               "install_url": "", "setup_url": ""}


def test_the_gateway_answers_it_and_the_container_is_not_asked(harness):
    async def body():
        tenant, _, cookie = await _enter(harness)
        before = len(harness.forwarder.calls)
        await _ask(harness, tenant, cookie)
        return before, len(harness.forwarder.calls)

    before, after = _run(harness, body)

    assert before == after, "a stopped container is not started to answer this"


def test_only_a_get_is_answered_here(harness):
    async def body():
        tenant, _, cookie = await _enter(harness)
        before = len(harness.forwarder.calls)
        status, _, raw = await harness.send(
            "POST", PATH, host=f"{tenant}.{APEX}", cookie=cookie, body=b"{}",
            headers={"Content-Type": "application/json", "Origin": f"https://{tenant}.{APEX}"})
        return status, raw, len(harness.forwarder.calls) - before

    status, raw, forwarded = _run(harness, body)

    assert b"install_url" not in raw
    assert (status, forwarded) in ((405, 0), (403, 0))
