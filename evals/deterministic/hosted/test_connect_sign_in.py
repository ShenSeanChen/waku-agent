"""DETERMINISTIC EVAL -- sign in to a connector on hosted Waku (spec 029,
group A: tasks A1, A2 and A3).

Ana signs in at agent.waku.one and follows "Connect Notion". The gateway
registers itself with Notion once, sends her browser to Notion's sign-in page,
and exchanges the code Notion sends back for her tokens. It keeps them sealed
beside her tenant, renews them, and forgets them when she disconnects.

The tests drive the real Gateway, ControlDb, Connect and VendorApi. The vendor
is the FakeVendor on loopback, which compresses its answers, and the clock is
moved by hand. No test reaches a real vendor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_TOKEN_KEY,
    VENDOR_CLIENT_SECRET,
    FakeSpawner,
    FakeVendor,
    Harness,
    pkce_challenge,
    sign_in,
)

from hosted.core.connectors import VENDORS
from hosted.gateway.config import connect_key_from
from hosted.gateway.connect import (
    STATE_TTL_SECONDS,
    Connect,
    NotConnected,
    ReconnectNeeded,
    VendorApi,
    VendorUnreachable,
    on_origin,
)
from hosted.gateway.store import ControlDb

REDIRECT_URI = f"https://{APEX}/connect/callback"
COOKIE = "__Host-waku_session"


@pytest.fixture
def notion():
    fake = FakeVendor("notion")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def linear():
    fake = FakeVendor("linear")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, notion, linear):
    built = Harness(tmp_path, FakeSpawner())
    built.use_connect(notion=notion, linear=linear)
    return built


def _run(harness: Harness, body):
    """Run `body` against the started gateway. The store is opened again
    afterwards, so a test can read what the gateway left in control.db."""
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    try:
        return asyncio.run(run())
    finally:
        harness.store = ControlDb(harness.config.control_db, now=harness.clock)
        if harness.connect is not None:
            harness.connect = Connect(
                key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host=APEX, store=harness.store,
                api=None, vendors=harness.vendors(), now=harness.clock)


async def _start(harness: Harness, cookie: str | None, server: str = "notion") -> dict:
    """GET /connect/start: the status, the Location, and its query."""
    status, headers, body = await harness.send(
        "GET", f"/connect/start?server={server}", host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None)
    location = headers.get("location", "")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
    return {"status": status, "location": location, "body": body, **query}


async def _callback(harness: Harness, cookie: str | None, **query) -> str:
    """GET /connect/callback: where the browser is sent next."""
    status, headers, _ = await harness.send(
        "GET", "/connect/callback?" + urllib.parse.urlencode(query), host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None)
    assert status == 302
    return headers["location"]


async def _connect(harness: Harness, fake: FakeVendor, server: str = "notion",
                   **claims) -> tuple[str, str, str]:
    """A whole sign-in for one person: (tenant id, apex cookie, where the
    callback sent the browser)."""
    tenant_id, cookie, _code = await sign_in(harness, **claims)
    start = await _start(harness, cookie, server)
    landed = await _callback(harness, cookie, code=fake.approve(start), state=start["state"])
    return tenant_id, cookie, landed


def _raw(harness: Harness) -> bytes:
    """Every byte control.db holds on disk, its write-ahead log included."""
    folder = harness.config.control_db.parent
    return b"".join(path.read_bytes() for path in folder.glob("control.db*"))


# --- A1: the registration ---------------------------------------------------

def test_the_gateway_registers_once_with_the_one_redirect_address(harness, notion):
    async def body():
        _, ana, _ = await sign_in(harness)
        _, bob, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        return await _start(harness, ana), await _start(harness, bob)

    first, second = _run(harness, body)
    [registration] = notion.calls("/register")
    assert registration["body"]["redirect_uris"] == [REDIRECT_URI]
    assert registration["body"]["grant_types"] == ["authorization_code", "refresh_token"]
    assert registration["body"]["token_endpoint_auth_method"] == "client_secret_post"
    # Every tenant signs in as the same client.
    assert first["client_id"] == second["client_id"] == "client-1"


def test_the_registration_is_kept_across_a_gateway_restart(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        await _start(harness, cookie)
        # A second Connect over the same control.db is the gateway after a restart.
        restarted = Connect(key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host=APEX,
                            store=harness.store, api=VendorApi(harness.session),
                            vendors=harness.vendors(), now=harness.clock)
        return await restarted.start("t" + "a" * 11, "notion")

    response = _run(harness, body)
    assert "client_id=client-1" in response.headers["Location"]
    assert len(notion.calls("/register")) == 1


def test_another_apex_host_registers_again(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        await _start(harness, cookie)
        moved = Connect(key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host="agent.example.org",
                        store=harness.store, api=VendorApi(harness.session),
                        vendors=harness.vendors(), now=harness.clock)
        return await moved.start("t" + "a" * 11, "notion")

    response = _run(harness, body)
    # The redirect address is part of a registration, so the old one is no use.
    assert [call["body"]["redirect_uris"] for call in notion.calls("/register")] == [
        [REDIRECT_URI], ["https://agent.example.org/connect/callback"]]
    assert "client_id=client-2" in response.headers["Location"]


def test_control_db_holds_no_client_secret_and_no_token_in_the_clear(harness, notion):
    async def body():
        await _connect(harness, notion)
        return _raw(harness)

    raw = _run(harness, body)
    assert harness.store.connect_client("notion") is not None
    for secret in (VENDOR_CLIENT_SECRET, "notion-access-1", "notion-refresh-1"):
        assert secret.encode() not in raw


@pytest.mark.parametrize("changes", [
    {"authorization_endpoint": "https://evil.example/authorize"},
    {"token_endpoint": "https://evil.example/token"},
    {"registration_endpoint": "https://evil.example/register"},
    {"token_endpoint": "http://user:pw@127.0.0.1/token"},
    {"code_challenge_methods_supported": ["plain"]},
])
def test_a_sign_in_address_on_another_origin_stops_the_sign_in(harness, notion, changes):
    notion.metadata_changes = changes

    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie)

    start = _run(harness, body)
    assert (start["status"], start["location"]) == (
        302, "/setup?connect=failed&server=notion")
    assert notion.calls("/register") == []
    assert harness.store.connect_client("notion") is None


def test_a_sign_in_server_on_another_origin_is_never_asked(harness, notion):
    notion.resource_changes = {"authorization_servers": ["https://evil.example"]}

    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie)

    assert _run(harness, body)["location"] == "/setup?connect=failed&server=notion"
    assert notion.calls("/.well-known/oauth-authorization-server") == []


def test_an_address_counts_as_the_vendors_only_on_its_exact_origin():
    notion = VENDORS["notion"]
    assert on_origin("https://mcp.notion.com/authorize", notion)
    for other in ("http://mcp.notion.com/authorize", "https://notion.com/authorize",
                  "https://mcp.notion.com.evil.example/authorize",
                  "https://mcp.notion.com:8443/authorize",
                  "https://mcp.notion.com@evil.example/authorize", "", None):
        assert not on_origin(other, notion)


# --- A2: the start and the callback -----------------------------------------

def test_the_start_sends_a_signed_in_person_to_the_vendor_with_a_state(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie)

    start = _run(harness, body)
    assert start["status"] == 302
    assert start["location"].startswith(f"{notion.url}/authorize?")
    assert start["response_type"] == "code"
    assert start["redirect_uri"] == REDIRECT_URI
    assert start["code_challenge_method"] == "S256"
    assert len(start["code_challenge"]) == 43
    assert len(start["state"]) >= 43
    assert start["scope"] == "default"
    assert start["resource"] == f"{notion.url}/mcp"
    assert VENDOR_CLIENT_SECRET not in start["location"]


def test_linear_is_asked_for_read_alone(harness, linear):
    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie, "linear")

    assert _run(harness, body)["scope"] == "read"


def test_the_start_sends_a_person_who_is_not_signed_in_to_the_sign_in_page(harness, notion):
    async def body():
        return await _start(harness, None), await _start(harness, "not-a-session")

    for start in _run(harness, body):
        assert (start["status"], start["location"]) == (302, f"https://{APEX}/login")
    assert notion.requests == []


@pytest.mark.parametrize("server", ["github", "", "https://evil.example/mcp", "Notion"])
def test_a_server_that_is_not_on_the_list_cannot_be_started(harness, notion, server):
    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie, urllib.parse.quote(server, safe=""))

    start = _run(harness, body)
    assert start["status"] == 404
    assert notion.requests == []


def test_an_approved_sign_in_keeps_the_persons_tokens(harness, notion):
    async def body():
        tenant_id, _, landed = await _connect(harness, notion)
        return tenant_id, landed, await harness.connect.access_token(tenant_id, "notion")

    tenant_id, landed, token = _run(harness, body)
    assert landed == "/setup?connect=connected&server=notion"
    assert token == "notion-access-1"
    [exchange] = notion.calls("/token")
    assert exchange["body"]["grant_type"] == "authorization_code"
    assert exchange["body"]["redirect_uri"] == REDIRECT_URI
    assert exchange["body"]["client_secret"] == VENDOR_CLIENT_SECRET
    assert exchange["body"]["resource"] == f"{notion.url}/mcp"
    assert harness.connect.states(tenant_id) == {"notion": "connected", "linear": "todo"}


def test_the_vendors_compressed_answers_are_opened(harness, notion):
    """The gateway's session does not decompress, and the fake compresses for
    a client that takes gzip, as Slack did on 2026-11-18."""
    _run(harness, lambda: _connect(harness, notion))
    assert all("gzip" in call["headers"]["accept-encoding"]
               for call in notion.calls("/token") + notion.calls("/register"))


def test_the_code_is_exchanged_with_the_verifier_of_its_own_start(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        await _callback(harness, cookie, code=notion.approve(start), state=start["state"])
        return start

    start = _run(harness, body)
    [exchange] = notion.calls("/token")
    assert pkce_challenge(exchange["body"]["code_verifier"]) == start["code_challenge"]


def test_a_state_works_once(harness, notion):
    async def body():
        _, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        first = await _callback(harness, cookie, code=notion.approve(start),
                                state=start["state"])
        second = await _callback(harness, cookie, code=notion.approve(start),
                                 state=start["state"])
        return first, second

    first, second = _run(harness, body)
    assert first == "/setup?connect=connected&server=notion"
    assert second == "/setup?connect=expired&server="
    assert len(notion.calls("/token")) == 1


def test_a_state_ends_after_ten_minutes(harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        harness.clock.t += STATE_TTL_SECONDS
        return tenant_id, await _callback(harness, cookie, code=notion.approve(start),
                                          state=start["state"])

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=expired&server="
    assert harness.store.connections(tenant_id) == []


def test_a_sign_in_cannot_be_finished_in_another_persons_browser(harness, notion):
    """Mallory starts a sign-in, approves it with her own Notion, and gets Ana
    to open the callback. Ana's agent must not get Mallory's Notion."""
    async def body():
        ana, ana_cookie, _ = await sign_in(harness)
        _, mallory_cookie, _ = await sign_in(harness, sub="sub-mal", email="m@example.com")
        start = await _start(harness, mallory_cookie)
        code = notion.approve(start)
        in_anas = await _callback(harness, ana_cookie, code=code, state=start["state"])
        signed_out = await _callback(harness, None, code=code, state=start["state"])
        return ana, in_anas, signed_out

    ana, in_anas, signed_out = _run(harness, body)
    assert in_anas == "/setup?connect=expired&server=notion"
    # The state went with the first try, whoever made it.
    assert signed_out == "/setup?connect=expired&server="
    assert notion.calls("/token") == []
    assert harness.store.connections(ana) == []


def test_a_cancelled_sign_in_stores_nothing(harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        return tenant_id, await _callback(harness, cookie, error="access_denied",
                                          state=start["state"])

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=denied&server=notion"
    assert harness.store.connections(tenant_id) == []


def test_a_callback_that_names_another_issuer_is_refused(harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        return tenant_id, await _callback(
            harness, cookie, code=notion.approve(start), state=start["state"],
            iss="https://evil.example")

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=failed&server=notion"
    assert notion.calls("/token") == []
    assert harness.store.connections(tenant_id) == []


@pytest.mark.parametrize("answer", [
    {"error": "invalid_grant"},
    {"access_token": "t", "token_type": "mac"},
    {"access_token": "", "token_type": "Bearer"},
])
def test_an_answer_that_is_not_a_bearer_token_stores_nothing(harness, notion, answer):
    notion.token_answer = answer

    async def body():
        return await _connect(harness, notion)

    tenant_id, _, landed = _run(harness, body)
    assert landed == "/setup?connect=failed&server=notion"
    assert harness.store.connections(tenant_id) == []


def test_no_log_line_holds_a_token_a_code_a_secret_or_a_verifier(harness, notion, caplog):
    async def body():
        _, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        code = notion.approve(start)
        await _callback(harness, cookie, code=code, state=start["state"])
        # A failure too: the vendor is gone for the next sign-in.
        notion.stop()
        again = await _start(harness, cookie, "notion")
        await _callback(harness, cookie, code="code-x", state=again.get("state", ""))
        return code, start["state"]

    with caplog.at_level(logging.DEBUG):
        code, state = _run(harness, body)
    verifier = notion.calls("/token")[0]["body"]["code_verifier"]
    # The gateway's own lines. The harness leaves aiohttp's access log on,
    # and the real gateway turns it off for this reason (__main__.py).
    logged = "\n".join(record.getMessage() for record in caplog.records
                       if record.name.startswith("hosted"))
    assert "connected notion" in logged
    for secret in ("notion-access-1", "notion-refresh-1", VENDOR_CLIENT_SECRET, code,
                   state, verifier, "127.0.0.1"):
        assert secret not in logged


# --- A3: sealed per tenant and server, renewal, disconnect ---------------------

def test_a_token_row_moved_to_another_tenant_or_server_does_not_open(harness, notion, linear):
    async def body():
        ana, _, _ = await _connect(harness, notion)
        bob, _, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        sealed = harness.store.connection(ana, "notion").token_sealed
        harness.store.set_connection(bob, "notion", token_sealed=sealed)
        harness.store.set_connection(ana, "linear", token_sealed=sealed)
        outcomes = []
        for tenant, server in ((bob, "notion"), (ana, "linear")):
            with pytest.raises(ReconnectNeeded):
                await harness.connect.access_token(tenant, server)
            outcomes.append(harness.store.connection(tenant, server).needs_reconnect)
        return outcomes, await harness.connect.access_token(ana, "notion")

    outcomes, anas = _run(harness, body)
    assert outcomes == [True, True]
    assert anas == "notion-access-1"


def test_a_second_tenant_has_no_token_of_the_first(harness, notion):
    async def body():
        await _connect(harness, notion)
        bob, _, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        with pytest.raises(NotConnected):
            await harness.connect.access_token(bob, "notion")
        return harness.connect.states(bob)

    assert _run(harness, body) == {"notion": "todo", "linear": "todo"}


def test_a_token_about_to_end_is_renewed_once_and_the_new_pair_is_kept(harness, notion):
    async def body():
        tenant_id, _, _ = await _connect(harness, notion)
        harness.clock.t += 3600 - 30      # inside the last minute of the hour
        # Three calls find it ending at once. A refresh token works once.
        tokens = await asyncio.gather(*[
            harness.connect.access_token(tenant_id, "notion") for _ in range(3)])
        harness.clock.t += 3600
        later = await harness.connect.access_token(tenant_id, "notion")
        return tokens, later, _raw(harness)

    tokens, later, raw = _run(harness, body)
    assert tokens == ["notion-access-2"] * 3
    # The second renewal used the refresh token the first one returned.
    assert later == "notion-access-3"
    renewals = [call["body"] for call in notion.calls("/token")
                if call["body"]["grant_type"] == "refresh_token"]
    assert [sent["refresh_token"] for sent in renewals] == [
        "notion-refresh-1", "notion-refresh-2"]
    assert b"notion-access-3" not in raw and b"notion-refresh-3" not in raw


def test_a_token_that_is_not_ending_is_not_renewed(harness, notion):
    async def body():
        tenant_id, _, _ = await _connect(harness, notion)
        harness.clock.t += 3600 - 61
        return await harness.connect.access_token(tenant_id, "notion")

    assert _run(harness, body) == "notion-access-1"
    assert len(notion.calls("/token")) == 1


def test_a_token_the_vendor_refused_is_renewed_whatever_its_end(harness, notion):
    async def body():
        tenant_id, _, _ = await _connect(harness, notion)
        first = await harness.connect.access_token(tenant_id, "notion",
                                                   refused="notion-access-1")
        # A second call that was refused the same old token gets the new one
        # without spending the refresh token again.
        second = await harness.connect.access_token(tenant_id, "notion",
                                                    refused="notion-access-1")
        return first, second

    assert _run(harness, body) == ("notion-access-2", "notion-access-2")
    assert len(notion.calls("/token")) == 2


def test_a_vendor_that_keeps_its_refresh_token_is_renewed_with_it_again(harness, notion):
    async def body():
        tenant_id, _, _ = await _connect(harness, notion)
        notion.send_refresh_token = False
        notion.refresh_tokens.add("notion-refresh-1")
        harness.clock.t += 3600
        await harness.connect.access_token(tenant_id, "notion")
        notion.refresh_tokens.add("notion-refresh-1")   # this vendor does not rotate
        harness.clock.t += 3600
        return await harness.connect.access_token(tenant_id, "notion")

    assert _run(harness, body) == "notion-access-3"


def test_a_refresh_token_the_vendor_ended_asks_the_person_to_connect_again(harness, notion):
    async def body():
        tenant_id, cookie, _ = await _connect(harness, notion)
        notion.refresh_tokens.clear()      # the person removed Waku at the vendor
        harness.clock.t += 3600
        with pytest.raises(ReconnectNeeded):
            await harness.connect.access_token(tenant_id, "notion")
        _, _, page = await harness.send("GET", "/setup", host=APEX,
                                        cookie=f"{COOKIE}={cookie}")
        return tenant_id, page.decode("utf-8")

    tenant_id, page = _run(harness, body)
    assert harness.connect.states(tenant_id)["notion"] == "reconnect"
    assert "Reconnect Notion" in page


def test_an_unreachable_vendor_does_not_end_a_sign_in(harness, notion):
    async def body():
        tenant_id, _, _ = await _connect(harness, notion)
        harness.clock.t += 3600
        notion.stop()
        with pytest.raises(VendorUnreachable):
            await harness.connect.access_token(tenant_id, "notion")
        return harness.connect.states(tenant_id)["notion"]

    # The row is as it was, so the next call tries again.
    assert _run(harness, body) == "connected"


def test_a_sign_in_again_clears_the_reconnect_mark(harness, notion):
    async def body():
        tenant_id, cookie, _ = await _connect(harness, notion)
        harness.store.set_connection_reconnect(tenant_id, "notion", True)
        start = await _start(harness, cookie)
        await _callback(harness, cookie, code=notion.approve(start), state=start["state"])
        return tenant_id, await harness.connect.access_token(tenant_id, "notion")

    tenant_id, token = _run(harness, body)
    assert token == "notion-access-2"
    assert harness.connect.states(tenant_id)["notion"] == "connected"


async def _disconnect(harness: Harness, cookie: str | None, payload, **kwargs):
    status, _, body = await harness.json_post(
        "/connect/disconnect", payload, host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None, **kwargs)
    return status, json.loads(body)


def test_disconnecting_removes_the_sealed_tokens_of_that_person_and_server(
        harness, notion, linear):
    async def body():
        ana, ana_cookie, _ = await _connect(harness, notion)
        start = await _start(harness, ana_cookie, "linear")
        await _callback(harness, ana_cookie, code=linear.approve(start), state=start["state"])
        bob, _, _ = await _connect(harness, notion, sub="sub-bob", email="bob@example.com")
        answer = await _disconnect(harness, ana_cookie, {"server": "notion"})
        again = await _disconnect(harness, ana_cookie, {"server": "notion"})
        return ana, bob, answer, again

    ana, bob, answer, again = _run(harness, body)
    assert answer == (200, {"ok": True, "disconnected": True})
    assert again == (200, {"ok": True, "disconnected": False})
    assert harness.store.connection(ana, "notion") is None
    assert harness.connect.states(ana) == {"notion": "todo", "linear": "connected"}
    assert harness.connect.states(bob)["notion"] == "connected"


def test_disconnecting_needs_the_persons_own_session_and_a_listed_server(harness, notion):
    async def body():
        tenant_id, cookie, _ = await _connect(harness, notion)
        return (tenant_id,
                await _disconnect(harness, None, {"server": "notion"}),
                await _disconnect(harness, cookie, {"server": "github"}),
                await _disconnect(harness, cookie, ["notion"]),
                # Another site's page cannot send it: the Origin is not ours.
                await harness.json_post("/connect/disconnect", {"server": "notion"},
                                        host=APEX, cookie=f"{COOKIE}={cookie}",
                                        origin="https://evil.example"),
                # And a link cannot: the route is a POST.
                await harness.send("GET", "/connect/disconnect?server=notion", host=APEX,
                                   cookie=f"{COOKIE}={cookie}"))

    tenant_id, signed_out, unlisted, not_object, cross_site, by_link = _run(harness, body)
    assert signed_out[0] == 401
    assert unlisted[0] == not_object[0] == 400
    assert cross_site[0] == 415
    assert by_link[0] == 404
    assert harness.connect.states(tenant_id)["notion"] == "connected"


def test_disconnecting_ends_a_sign_in_under_way(harness, notion):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        await _disconnect(harness, cookie, {"server": "notion"})
        landed = await _callback(harness, cookie, code=notion.approve(start),
                                 state=start["state"])
        return tenant_id, landed

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=expired&server="
    assert harness.store.connections(tenant_id) == []


def test_deleting_a_tenant_deletes_its_tokens(harness, notion):
    async def body():
        ana, _, _ = await _connect(harness, notion)
        bob, _, _ = await _connect(harness, notion, sub="sub-bob", email="bob@example.com")
        harness.store.delete_tenant(ana)
        return ana, bob

    ana, bob = _run(harness, body)
    reopened = ControlDb(harness.config.control_db)
    assert reopened.connections(ana) == []
    assert [row.server for row in reopened.connections(bob)] == ["notion"]
    # The registration is the gateway's, not a tenant's.
    assert reopened.connect_client("notion") is not None
    reopened.close()


# --- the setup page and the configuration -------------------------------------

async def _page(harness: Harness, cookie: str | None, query: str = "") -> str:
    status, _, body = await harness.send(
        "GET", "/setup" + query, host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None)
    assert status == 200
    return body.decode("utf-8")


def test_the_setup_page_offers_each_connector_to_a_signed_in_person(harness, notion):
    async def body():
        before = await _page(harness, None)
        _, cookie, _ = await _connect(harness, notion)
        return before, await _page(harness, cookie, "?connect=connected&server=notion")

    before, after = _run(harness, body)
    assert 'href="/connect/start?server=notion"' not in before
    assert 'data-connector="notion" data-state="done"' in after
    assert 'data-connector="linear" data-state="todo"' in after
    assert 'href="/connect/start?server=linear"' in after
    assert "Notion is connected." in after


@pytest.mark.parametrize("query", [
    "?connect=connected&server=github",
    "?connect=%3Cscript%3E&server=notion",
    "?connect=connected&server=%3Cscript%3Ealert(1)%3C/script%3E",
])
def test_the_setup_page_shows_nothing_from_a_query_it_does_not_know(harness, query):
    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie, query)

    page = _run(harness, body)
    assert 'id="notice"' not in page
    assert "<script" not in page


def test_without_a_key_the_connector_routes_answer_503(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())     # no use_connect: no key

    async def body():
        _, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        callback = await harness.send("GET", "/connect/callback?state=s", host=APEX)
        disconnect = await _disconnect(harness, cookie, {"server": "notion"})
        return start, callback[0], disconnect, await _page(harness, cookie)

    start, callback, disconnect, page = _run(harness, body)
    assert start["status"] == callback == disconnect[0] == 503
    assert b"WAKU_SLACK_TOKEN_KEY" in start["body"]
    assert "data-connector" not in page


def test_connectors_need_the_sealing_key_and_no_other_value():
    key, problem = connect_key_from({"WAKU_SLACK_TOKEN_KEY": "0f" * 32})
    assert (key, problem) == (bytes.fromhex("0f" * 32), "")
    for env in ({}, {"WAKU_SLACK_TOKEN_KEY": ""}):
        assert connect_key_from(env) == (
            None, "config/gateway.env is missing WAKU_SLACK_TOKEN_KEY")
    key, problem = connect_key_from({"WAKU_SLACK_TOKEN_KEY": "not-a-key-zzzz"})
    assert key is None
    # The refusal names the line and never quotes the value.
    assert "WAKU_SLACK_TOKEN_KEY" in problem and "zzzz" not in problem
