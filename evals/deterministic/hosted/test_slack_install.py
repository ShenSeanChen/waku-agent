"""DETERMINISTIC EVAL -- "Add to Slack" (spec 026 task C1, decision 5).

Ana signs in at agent.waku.one, follows "Add to Slack" and approves in her
workspace. Slack sends her browser back with a code, the gateway exchanges it
for a bot token, and the gateway keeps that token encrypted beside her tenant
with the map from her workspace's team id to her tenant.

The tests drive the real Gateway, ControlDb and SlackApi. Slack is the
FakeSlack on loopback, and the clock is moved by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_BOT_TOKEN,
    SLACK_CLIENT_ID,
    SLACK_CLIENT_SECRET,
    SLACK_SIGNING_SECRET,
    SLACK_TOKEN_KEY,
    FakeSlack,
    FakeSpawner,
    Harness,
    sign_in,
)

from hosted.gateway.config import SLACK_ENV_NAMES, config_from_env
from hosted.gateway.secretbox import SecretBox, Unreadable
from hosted.gateway.slack import STATE_TTL_SECONDS

REDIRECT_URI = f"https://{APEX}/slack/oauth/callback"
COOKIE = "__Host-waku_session"


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


async def _start(harness: Harness, cookie: str | None) -> tuple[int, dict]:
    """GET /slack/install: (status, the Location's query as a dict)."""
    status, headers, _ = await harness.send(
        "GET", "/slack/install", host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None)
    location = headers.get("location", "")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
    return status, {"location": location, **query}


async def _callback(harness: Harness, cookie: str | None, **query) -> str:
    """GET /slack/oauth/callback: where the browser is sent next."""
    target = "/slack/oauth/callback?" + urllib.parse.urlencode(query)
    status, headers, _ = await harness.send(
        "GET", target, host=APEX, cookie=f"{COOKIE}={cookie}" if cookie else None)
    assert status == 302
    return headers["location"]


async def _install(harness: Harness, **claims) -> tuple[str, str, str]:
    """A whole install for one person: (tenant id, apex cookie, where the
    callback sent the browser)."""
    tenant_id, cookie, _code = await sign_in(harness, **claims)
    _, start = await _start(harness, cookie)
    landed = await _callback(harness, cookie, code="code-1", state=start["state"])
    return tenant_id, cookie, landed


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


# --- the start ------------------------------------------------------------

def test_the_start_sends_a_signed_in_person_to_slack_with_a_state(harness):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie)

    status, start = _run(harness, body)
    assert status == 302
    assert start["location"].startswith("https://slack.com/oauth/v2/authorize?")
    assert start["client_id"] == SLACK_CLIENT_ID
    assert start["scope"] == ("app_mentions:read,chat:write,channels:history,"
                              "groups:history,users:read,reactions:write,channels:read,"
                              "groups:read")
    assert start["redirect_uri"] == REDIRECT_URI
    assert len(start["state"]) >= 43
    assert SLACK_CLIENT_SECRET not in start["location"]


def test_the_start_sends_a_person_who_is_not_signed_in_to_the_sign_in_page(harness):
    async def body():
        return await _start(harness, None), await _start(harness, "not-a-session")

    for status, start in _run(harness, body):
        assert status == 302
        assert start["location"] == f"https://{APEX}/login"
        assert "state" not in start


# --- the callback -----------------------------------------------------------

def test_an_approved_install_maps_the_workspace_to_the_tenant(harness, slack):
    tenant_id, _cookie, landed = _run(harness, lambda: _install(harness))

    assert landed == "/setup?slack=connected"
    [exchange] = slack.calls("/oauth.v2.access")
    assert exchange["body"] == {"code": "code-1", "redirect_uri": REDIRECT_URI}
    # Slack takes the client id and secret as HTTP Basic credentials.
    import base64
    basic = base64.b64encode(f"{SLACK_CLIENT_ID}:{SLACK_CLIENT_SECRET}".encode()).decode()
    assert exchange["authorization"] == f"Basic {basic}"

    store = _reopen(harness)
    assert store.slack_tenant("T0ANA") == tenant_id
    install = store.slack_install(tenant_id)
    assert (install.team_id, install.team_name, install.bot_user_id) == (
        "T0ANA", "Ana & Co", "U0BOT")


def _reopen(harness: Harness):
    from hosted.gateway.store import ControlDb

    return ControlDb(harness.config.control_db)


def test_a_state_is_used_once(harness, slack):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        _, start = await _start(harness, cookie)
        first = await _callback(harness, cookie, code="code-1", state=start["state"])
        second = await _callback(harness, cookie, code="code-2", state=start["state"])
        return first, second

    first, second = _run(harness, body)
    assert first == "/setup?slack=connected"
    assert second == "/setup?slack=expired"
    assert len(slack.calls("/oauth.v2.access")) == 1


def test_a_state_expires_after_ten_minutes(harness, slack):
    assert STATE_TTL_SECONDS == 600

    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        _, early = await _start(harness, cookie)
        harness.clock.t += 599
        in_time = await _callback(harness, cookie, code="c", state=early["state"])
        _, late = await _start(harness, cookie)
        harness.clock.t += 600
        too_late = await _callback(harness, cookie, code="c", state=late["state"])
        return in_time, too_late

    in_time, too_late = _run(harness, body)
    assert in_time == "/setup?slack=connected"
    assert too_late == "/setup?slack=expired"
    assert len(slack.calls("/oauth.v2.access")) == 1


def test_a_state_only_works_in_the_browser_of_the_person_it_was_made_for(harness, slack):
    """Bob cannot finish Ana's install, and a browser with no session cannot
    either: the state names Ana's tenant, and the callback's own session must
    name the same one."""
    async def body():
        ana, ana_cookie, _ = await sign_in(harness)
        _bob, bob_cookie, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        _, for_bob = await _start(harness, ana_cookie)
        as_bob = await _callback(harness, bob_cookie, code="c", state=for_bob["state"])
        _, for_nobody = await _start(harness, ana_cookie)
        as_nobody = await _callback(harness, None, code="c", state=for_nobody["state"])
        return ana, as_bob, as_nobody

    ana, as_bob, as_nobody = _run(harness, body)
    assert as_bob == as_nobody == "/setup?slack=expired"
    assert slack.calls("/oauth.v2.access") == []
    assert _reopen(harness).slack_install(ana) is None


def test_a_state_the_gateway_never_issued_is_refused(harness, slack):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return (await _callback(harness, cookie, code="c", state="made-up"),
                await _callback(harness, cookie, code="c"))

    assert _run(harness, body) == ("/setup?slack=expired", "/setup?slack=expired")
    assert slack.calls("/oauth.v2.access") == []


def test_a_new_start_ends_the_persons_earlier_state(harness, slack):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        _, first = await _start(harness, cookie)
        _, second = await _start(harness, cookie)
        assert first["state"] != second["state"]
        return (await _callback(harness, cookie, code="c", state=first["state"]),
                await _callback(harness, cookie, code="c", state=second["state"]))

    assert _run(harness, body) == ("/setup?slack=expired", "/setup?slack=connected")


def test_a_person_who_says_no_in_slack_installs_nothing(harness, slack):
    async def body():
        tenant, cookie, _ = await sign_in(harness)
        _, start = await _start(harness, cookie)
        denied = await _callback(harness, cookie, error="access_denied",
                                 state=start["state"])
        again = await _callback(harness, cookie, code="c", state=start["state"])
        return tenant, denied, again

    tenant, denied, again = _run(harness, body)
    assert denied == "/setup?slack=denied"
    assert again == "/setup?slack=expired"     # the state went with the refusal
    assert slack.calls("/oauth.v2.access") == []
    assert _reopen(harness).slack_install(tenant) is None


@pytest.mark.parametrize("answer", [
    {"ok": False, "error": "invalid_code"},
    {"ok": True, "access_token": SLACK_BOT_TOKEN, "token_type": "user",
     "team": {"id": "T0ANA", "name": "Ana & Co"}},
    {"ok": True, "access_token": SLACK_BOT_TOKEN, "token_type": "bot", "team": None,
     "is_enterprise_install": True, "enterprise": {"id": "E0ORG"}},
    {"ok": True, "access_token": SLACK_BOT_TOKEN, "token_type": "bot",
     "team": {"id": "T0ANA", "name": "Ana & Co"}, "is_enterprise_install": True},
    {"ok": True, "access_token": "", "token_type": "bot",
     "team": {"id": "T0ANA", "name": "Ana & Co"}},
    {"ok": True, "access_token": SLACK_BOT_TOKEN, "token_type": "bot",
     "team": {"id": "not a team id", "name": "x"}},
])
def test_an_answer_that_is_not_one_workspaces_bot_token_installs_nothing(harness, slack,
                                                                         answer):
    slack.oauth_answer = answer
    tenant_id, _cookie, landed = _run(harness, lambda: _install(harness))
    assert landed == "/setup?slack=failed"
    assert _reopen(harness).slack_install(tenant_id) is None


def test_a_workspace_stays_with_the_tenant_that_connected_it_first(harness, slack):
    async def body():
        ana, _, first = await _install(harness)
        bob, _, second = await _install(harness, sub="sub-bob", email="bob@example.com")
        return ana, bob, first, second

    ana, bob, first, second = _run(harness, body)
    assert (first, second) == ("/setup?slack=connected", "/setup?slack=taken")
    store = _reopen(harness)
    assert store.slack_tenant("T0ANA") == ana
    assert store.slack_install(bob) is None


def test_a_tenant_has_one_workspace_and_a_second_install_replaces_it(harness, slack):
    async def body():
        ana, cookie, _ = await _install(harness)
        slack.oauth_answer = {**slack.oauth_answer, "access_token": "xoxb-fake-second",
                              "team": {"id": "T0TWO", "name": "Second"}}
        _, start = await _start(harness, cookie)
        landed = await _callback(harness, cookie, code="c", state=start["state"])
        return ana, landed

    ana, landed = _run(harness, body)
    assert landed == "/setup?slack=connected"
    store = _reopen(harness)
    assert store.slack_install(ana).team_id == "T0TWO"
    assert store.slack_tenant("T0ANA") is None


def test_deleting_a_tenant_removes_its_workspace(harness, slack):
    tenant_id, _cookie, _ = _run(harness, lambda: _install(harness))
    store = _reopen(harness)
    store.delete_tenant(tenant_id)
    assert store.slack_tenant("T0ANA") is None


# --- the token at rest -----------------------------------------------------

def test_the_bot_token_cannot_be_read_in_the_database_file(harness, slack, caplog):
    """The acceptance of C1. Every file SQLite keeps for control.db is read as
    bytes, and none holds the token. The stored value opens with the key, so
    the search is not passing because nothing was stored."""
    seen: list[bytes] = []

    async def body():
        tenant, _cookie, landed = await _install(harness)
        seen.append(landed.encode())
        return tenant

    with caplog.at_level(logging.DEBUG):
        tenant_id = _run(harness, body)

    token = SLACK_BOT_TOKEN.encode()
    directory = harness.config.control_db.parent
    files = sorted(directory.glob("control.db*"))
    assert harness.config.control_db in files
    for path in files:
        assert token not in path.read_bytes(), path.name
    # A dump of every column of every table, the way an operator would look.
    conn = sqlite3.connect(harness.config.control_db)
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert SLACK_BOT_TOKEN not in dump

    install = _reopen(harness).slack_install(tenant_id)
    box = SecretBox.from_hex(SLACK_TOKEN_KEY)
    assert box.open(install.token_sealed, bound_to="slack:T0ANA") == SLACK_BOT_TOKEN

    # Nor in what the browser was sent, nor in the log.
    assert all(token not in part for part in seen)
    assert SLACK_BOT_TOKEN not in caplog.text
    assert SLACK_CLIENT_SECRET not in caplog.text


def test_a_sealed_token_opens_only_with_its_key_and_for_its_workspace():
    box = SecretBox.from_hex(SLACK_TOKEN_KEY)
    sealed = box.seal(SLACK_BOT_TOKEN, bound_to="slack:T0ANA")
    assert SLACK_BOT_TOKEN.encode() not in sealed
    assert box.open(sealed, bound_to="slack:T0ANA") == SLACK_BOT_TOKEN
    # Sealed twice, the bytes differ: the nonce is new each time.
    assert box.seal(SLACK_BOT_TOKEN, bound_to="slack:T0ANA") != sealed

    with pytest.raises(Unreadable):
        SecretBox.from_hex("1e" * 32).open(sealed, bound_to="slack:T0ANA")
    with pytest.raises(Unreadable):
        box.open(sealed, bound_to="slack:T0BOB")     # another workspace's row
    with pytest.raises(Unreadable):
        box.open(sealed[:-1] + bytes([sealed[-1] ^ 1]), bound_to="slack:T0ANA")
    with pytest.raises(Unreadable):
        box.open(b"", bound_to="slack:T0ANA")


@pytest.mark.parametrize("key", ["", "0f" * 31, "0f" * 33, "zz" * 32, "0f" * 31 + "0"])
def test_a_key_that_is_not_32_bytes_of_hex_is_refused_without_being_quoted(key):
    with pytest.raises(ValueError) as refused:
        SecretBox.from_hex(key)
    assert "64 hexadecimal characters" in str(refused.value)
    if key:
        assert key not in str(refused.value)


# --- the configuration ----------------------------------------------------

def _env(**slack) -> dict[str, str]:
    env = {"WAKU_APEX_HOST": APEX, "WAKU_GATEWAY_BIND": "127.0.0.1",
           "WAKU_GATEWAY_PORT": "8787", "WAKU_CONTROL_DB": "/x/control.db",
           "WAKU_SPAWNER_SOCKET": "/x/s.sock", "WAKU_GATEWAY_SOCKET": "/x/g.sock",
           "WAKU_PROXY_SOCKET": "/x/p.sock", "WAKU_ADMIN_SOCKET": "/x/a.sock",
           "WAKU_MAX_RUNNING": "4", "WAKU_SUPABASE_URL": "https://x.supabase.co",
           "WAKU_SUPABASE_ISSUER": "https://x.supabase.co/auth/v1",
           "WAKU_SUPABASE_JWKS_URL": "https://x.supabase.co/auth/v1/.well-known/jwks.json",
           "WAKU_SUPABASE_AUDIENCE": "https://api.waku.one/mcp",
           "WAKU_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_x",
           "WAKU_FREE_TURNS_PER_HOUR": "30", "WAKU_BYOK_TURNS_PER_HOUR": "120"}
    return env | slack


_FULL = {"WAKU_SLACK_CLIENT_ID": SLACK_CLIENT_ID,
         "WAKU_SLACK_CLIENT_SECRET": SLACK_CLIENT_SECRET,
         "WAKU_SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
         "WAKU_SLACK_TOKEN_KEY": SLACK_TOKEN_KEY}


def test_the_four_slack_names_are_the_ones_the_gateway_reads():
    assert SLACK_ENV_NAMES == tuple(_FULL)


def test_all_four_values_turn_slack_on():
    config = config_from_env(_env(**_FULL))
    assert config.slack_problem == ""
    assert config.slack.client_id == SLACK_CLIENT_ID
    assert config.slack.client_secret == SLACK_CLIENT_SECRET
    assert config.slack.signing_secret == SLACK_SIGNING_SECRET
    assert config.slack.token_key == bytes.fromhex(SLACK_TOKEN_KEY)


def test_the_settings_do_not_print_their_secrets():
    config = config_from_env(_env(**_FULL))
    for text in (repr(config), repr(config.slack), str(config.slack)):
        assert SLACK_CLIENT_SECRET not in text
        assert SLACK_SIGNING_SECRET not in text
        assert SLACK_TOKEN_KEY not in text
        assert repr(bytes.fromhex(SLACK_TOKEN_KEY)) not in text


@pytest.mark.parametrize("absent", list(_FULL))
def test_a_missing_name_turns_slack_off_and_is_named(absent):
    """The gateway still starts: a gateway.env from before spec 026 has none
    of the four, and an upgrade must not take every tenant down."""
    for value in ({}, {absent: ""}, {absent: "   "}):
        present = {k: v for k, v in _FULL.items() if k != absent} | value
        config = config_from_env(_env(**present))
        assert config.slack is None
        assert absent in config.slack_problem
        for other in set(_FULL) - {absent}:
            assert other not in config.slack_problem


def test_a_gateway_env_with_no_slack_line_names_all_four():
    config = config_from_env(_env())
    assert config.slack is None
    assert all(name in config.slack_problem for name in _FULL)


def test_a_malformed_key_turns_slack_off_and_is_named_but_not_quoted():
    bad = "not-a-key-" + "0" * 20
    config = config_from_env(_env(**(_FULL | {"WAKU_SLACK_TOKEN_KEY": bad})))
    assert config.slack is None
    assert "WAKU_SLACK_TOKEN_KEY" in config.slack_problem
    assert bad not in config.slack_problem


def test_without_its_configuration_every_slack_route_refuses_and_says_what_is_missing(
        tmp_path):
    """Neither route half-works: the install start and the callback answer
    503 with the missing names. test_slack_events.py has the events route."""
    harness = Harness(tmp_path, FakeSpawner())      # no use_slack: Slack is off

    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        signed = f"{COOKIE}={cookie}"
        return [
            await harness.send("GET", "/slack/install", host=APEX, cookie=signed),
            await harness.send("GET", "/slack/oauth/callback?code=c&state=s", host=APEX,
                               cookie=signed),
        ]

    for status, _headers, raw in _run(harness, body):
        assert status == 503
        message = json.loads(raw)["error"]
        for name in SLACK_ENV_NAMES:
            assert name in message
