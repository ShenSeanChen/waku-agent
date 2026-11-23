"""DETERMINISTIC EVAL -- an owner adds Waku to Slack for a team space (spec
032 tasks A3 and A7, decision 2).

Ana is an owner of the team space Acme. waku.one sends her browser to the
`install_url` it got from POST /v1/team-homes: /slack/install?home=<ticket>.
Her browser has no session at agent.waku.one. The ticket works once, for 10
minutes. The install page sets a cookie and sends her to Slack. Slack sends
her back with a code, and the callback requires the cookie and the `state`
together. Her browser then lands on Acme's page on waku.one.

A workspace that a person's own install already holds is left as it is until
spec 032 group E.

The tests drive the real Gateway, ControlDb and SlackApi. Slack is the
FakeSlack on loopback, and the clock is moved by hand.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest
from gatewaylib import (
    AGENT_MEMORY_KEY,
    APEX,
    PLATFORM_SECRET,
    SLACK_BOT_TOKEN,
    SLACK_CLIENT_ID,
    SLACK_TOKEN_KEY,
    FakeSlack,
    FakeSpawner,
    Harness,
    cookie_attributes,
    cookie_value,
    sign_in,
)

from hosted.gateway.secretbox import SecretBox
from hosted.gateway.slack import HOME_COOKIE
from hosted.gateway.store import ControlDb
from hosted.gateway.team_homes import TICKET_TTL_SECONDS

ACME = "0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11"
GLOBEX = "7c1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
AGENT = "5f2d8f0a-1c0b-4d67-a2f4-9f1f6f0f3c22"
RETURN_URL = "https://www.waku.one/spaces/acme"
SESSION = "__Host-waku_session"


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.oauth_answer["team"] = {"id": "T0123", "name": "Acme HQ"}
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, slack):
    built = Harness(tmp_path, FakeSpawner())
    built.use_slack(slack)
    built.use_team_homes()
    return built


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


def _reopen(harness: Harness) -> ControlDb:
    return ControlDb(harness.config.control_db)


async def make_home(harness: Harness, space: str = ACME, name: str = "Acme",
                    return_url: str = RETURN_URL) -> tuple[str, str]:
    """waku.one's call 1: (home id, the install_url's path and query)."""
    body = {"space_id": space, "space_name": name, "agent_id": AGENT,
            "agent_name": "Waku", "memory_key": AGENT_MEMORY_KEY,
            "memory_api_url": "", "return_url": return_url}
    status, _, raw = await harness.send(
        "POST", "/v1/team-homes", host=APEX, body=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {PLATFORM_SECRET}",
                 "Content-Type": "application/json"})
    assert status == 201, raw
    answer = json.loads(raw)
    parts = urllib.parse.urlsplit(answer["install_url"])
    return answer["home_id"], f"{parts.path}?{parts.query}"


async def read_home(harness: Harness, space: str = ACME) -> dict:
    """waku.one's call 2."""
    status, _, raw = await harness.send(
        "GET", f"/v1/team-homes/{space}", host=APEX,
        headers={"Authorization": f"Bearer {PLATFORM_SECRET}"})
    assert status == 200, raw
    return json.loads(raw)


async def open_install(harness: Harness, target: str, cookie: str | None = None) -> dict:
    """The browser opens install_url: the status, where it is sent, the
    `state` in that address and the cookie the page set."""
    status, headers, raw = await harness.send("GET", target, host=APEX, cookie=cookie)
    location = headers.get("location", "")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
    return {"status": status, "location": location, "state": query.get("state", ""),
            "body": raw.decode("utf-8", "replace"),
            "client_id": query.get("client_id", ""),
            "cookie": cookie_value(headers, HOME_COOKIE),
            "attributes": cookie_attributes(headers, HOME_COOKIE)}


async def callback(harness: Harness, *, cookie: str | None, **query) -> tuple[int, str]:
    """Slack sends the browser back: (status, where the browser goes next)."""
    target = "/slack/oauth/callback?" + urllib.parse.urlencode(query)
    status, headers, _ = await harness.send("GET", target, host=APEX, cookie=cookie)
    return status, headers.get("location", "")


def home_cookie(value: str) -> str:
    return f"{HOME_COOKIE}={value}"


async def whole_install(harness: Harness, **home) -> tuple[str, tuple[int, str]]:
    home_id, target = await make_home(harness, **home)
    page = await open_install(harness, target)
    return home_id, await callback(harness, cookie=home_cookie(page["cookie"]),
                                   code="code-1", state=page["state"])


# --- the install page -------------------------------------------------------

def test_the_ticket_sends_a_browser_with_no_session_to_slack(harness):
    async def body():
        _, target = await make_home(harness)
        return await open_install(harness, target)

    page = _run(harness, body)
    assert page["status"] == 302
    assert page["location"].startswith("https://slack.com/oauth/v2/authorize?")
    assert page["client_id"] == SLACK_CLIENT_ID
    assert len(page["state"]) >= 43
    assert len(page["cookie"]) >= 43
    assert {"httponly", "secure", "samesite=lax", "path=/",
            "max-age=600"} <= page["attributes"]
    # The cookie's value is not the state: holding one does not give the other.
    assert page["cookie"] != page["state"]
    assert page["cookie"] not in page["location"]


def test_a_ticket_is_used_once(harness, slack):
    async def body():
        home_id, target = await make_home(harness)
        first = await open_install(harness, target)
        second = await open_install(harness, target)
        return home_id, first, second

    home_id, first, second = _run(harness, body)
    assert first["status"] == 302
    assert second["status"] == 410
    # The page says where to go, and offers no link to this host's sign-in.
    assert "waku.one" in second["body"] and "Try again" not in second["body"]
    assert (second["state"], second["cookie"], second["location"]) == ("", "", "")
    assert slack.calls("/oauth.v2.access") == []
    assert _reopen(harness).slack_install(home_id) is None


def test_a_ticket_ends_after_ten_minutes(harness):
    assert TICKET_TTL_SECONDS == 600

    async def body():
        _, early = await make_home(harness)
        harness.clock.t += 599
        in_time = await open_install(harness, early)
        _, late = await make_home(harness)
        harness.clock.t += 600
        return in_time, await open_install(harness, late)

    in_time, too_late = _run(harness, body)
    assert in_time["status"] == 302
    assert too_late["status"] == 410
    assert too_late["state"] == ""


def test_a_new_post_ends_the_tickets_before_it(harness):
    async def body():
        _, old = await make_home(harness)
        _, new = await make_home(harness)
        return await open_install(harness, old), await open_install(harness, new)

    old, new = _run(harness, body)
    assert (old["status"], new["status"]) == (410, 302)


@pytest.mark.parametrize("ticket", ["", "x" * 43, "../../etc"])
def test_a_ticket_nobody_issued_opens_nothing(harness, ticket):
    """A signed-in person with a bad ticket is not sent on as themselves: the
    address asked for a team home."""
    async def body():
        _, cookie, _ = await sign_in(harness)
        target = "/slack/install?" + urllib.parse.urlencode({"home": ticket})
        return (await open_install(harness, target),
                await open_install(harness, target, cookie=f"{SESSION}={cookie}"))

    for page in _run(harness, body):
        assert page["status"] == 410
        assert page["state"] == ""


# --- the callback -----------------------------------------------------------

def test_an_approved_install_maps_the_workspace_to_the_home(harness, slack):
    async def body():
        home_id, landed = await whole_install(harness)
        return home_id, landed, await read_home(harness)

    home_id, (status, location), read = _run(harness, body)
    assert (status, location) == (302, RETURN_URL)
    assert read == {"home_id": home_id,
                    "slack": {"team_id": "T0123", "team_name": "Acme HQ"},
                    "refused": None}
    store = _reopen(harness)
    assert store.slack_tenant("T0123") == home_id
    install = store.slack_install(home_id)
    assert install.bot_user_id == "U0BOT"
    box = SecretBox(bytes.fromhex(SLACK_TOKEN_KEY))
    assert box.open(install.token_sealed, bound_to="slack:T0123") == SLACK_BOT_TOKEN
    assert len(slack.calls("/oauth.v2.access")) == 1
    # Adding the app starts no container.
    assert harness.spawner.requests == []


def test_the_callback_deletes_the_install_pages_cookie(harness):
    async def body():
        _, target = await make_home(harness)
        page = await open_install(harness, target)
        target = "/slack/oauth/callback?" + urllib.parse.urlencode(
            {"code": "code-1", "state": page["state"]})
        _, headers, _ = await harness.send("GET", target, host=APEX,
                                           cookie=home_cookie(page["cookie"]))
        return headers

    headers = _run(harness, body)
    assert cookie_value(headers, HOME_COOKIE) in ("", '""')
    assert "max-age=0" in cookie_attributes(headers, HOME_COOKIE)


def test_a_callback_without_the_cookie_installs_nothing(harness, slack):
    """The state alone is not enough: Slack returns it in an address, and an
    address can be sent to another browser."""
    async def body():
        home_id, target = await make_home(harness)
        page = await open_install(harness, target)
        landed = await callback(harness, cookie=None, code="code-1", state=page["state"])
        # The state went with the refusal: the right cookie is too late.
        again = await callback(harness, cookie=home_cookie(page["cookie"]),
                               code="code-1", state=page["state"])
        return home_id, landed, again, await read_home(harness)

    home_id, landed, again, read = _run(harness, body)
    assert landed == (302, RETURN_URL)
    assert again == (302, "/setup?slack=expired")
    assert slack.calls("/oauth.v2.access") == []
    assert read["slack"] is None
    assert _reopen(harness).slack_install(home_id) is None


def test_a_callback_with_another_browsers_cookie_installs_nothing(harness, slack):
    async def body():
        acme_id, acme = await make_home(harness)
        _, globex = await make_home(harness, space=GLOBEX, name="Globex")
        ana = await open_install(harness, acme)
        mallory = await open_install(harness, globex)
        landed = await callback(harness, cookie=home_cookie(mallory["cookie"]),
                                code="code-1", state=ana["state"])
        return acme_id, landed

    acme_id, landed = _run(harness, body)
    assert landed == (302, RETURN_URL)
    assert slack.calls("/oauth.v2.access") == []
    assert _reopen(harness).slack_installs() == []
    assert _reopen(harness).slack_install(acme_id) is None


def test_a_persons_session_does_not_finish_a_homes_install(harness, slack):
    """Ben is signed in at agent.waku.one. His session is not the cookie the
    install page set, so it finishes nothing, for the home or for Ben."""
    async def body():
        ben, ben_cookie, _ = await sign_in(harness, sub="sub-ben", email="ben@acme.com")
        home_id, target = await make_home(harness)
        page = await open_install(harness, target)
        landed = await callback(harness, cookie=f"{SESSION}={ben_cookie}",
                                code="code-1", state=page["state"])
        return ben, home_id, landed

    ben, home_id, landed = _run(harness, body)
    assert landed == (302, RETURN_URL)
    assert slack.calls("/oauth.v2.access") == []
    store = _reopen(harness)
    assert store.slack_install(home_id) is None
    assert store.slack_install(ben) is None


def test_a_homes_cookie_does_not_finish_a_persons_install(harness, slack):
    async def body():
        ben, ben_cookie, _ = await sign_in(harness, sub="sub-ben", email="ben@acme.com")
        _, target = await make_home(harness)
        page = await open_install(harness, target)
        _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                           cookie=f"{SESSION}={ben_cookie}")
        state = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(headers["location"]).query))["state"]
        landed = await callback(harness, cookie=home_cookie(page["cookie"]),
                                code="code-1", state=state)
        return ben, landed

    ben, landed = _run(harness, body)
    assert landed == (302, "/setup?slack=expired")
    assert slack.calls("/oauth.v2.access") == []
    assert _reopen(harness).slack_install(ben) is None


def test_a_state_finishes_one_install(harness, slack):
    async def body():
        _, target = await make_home(harness)
        page = await open_install(harness, target)
        cookie = home_cookie(page["cookie"])
        first = await callback(harness, cookie=cookie, code="code-1", state=page["state"])
        second = await callback(harness, cookie=cookie, code="code-2", state=page["state"])
        return first, second

    first, second = _run(harness, body)
    assert first == (302, RETURN_URL)
    assert second == (302, "/setup?slack=expired")
    assert len(slack.calls("/oauth.v2.access")) == 1


def test_a_cancelled_install_goes_back_to_the_space_and_installs_nothing(harness, slack):
    async def body():
        _, target = await make_home(harness)
        page = await open_install(harness, target)
        landed = await callback(harness, cookie=home_cookie(page["cookie"]),
                                error="access_denied", state=page["state"])
        return landed, await read_home(harness)

    landed, read = _run(harness, body)
    assert landed == (302, RETURN_URL)
    assert slack.calls("/oauth.v2.access") == []
    assert read["slack"] is None


def test_a_code_slack_refuses_goes_back_to_the_space_and_installs_nothing(harness, slack):
    slack.oauth_answer = {"ok": False, "error": "invalid_code"}

    async def body():
        _, landed = await whole_install(harness)
        return landed, await read_home(harness)

    landed, read = _run(harness, body)
    assert landed == (302, RETURN_URL)
    assert read["slack"] is None


def test_the_browser_goes_to_the_return_address_the_space_sent(harness):
    other = "https://dev.waku.one/s/acme?tab=agent"

    async def body():
        return await whole_install(harness, return_url=other)

    _, landed = _run(harness, body)
    assert landed == (302, other)


def test_the_install_page_answers_503_while_slack_is_off(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())
    harness.use_team_homes()

    async def body():
        _, target = await make_home(harness)
        return await open_install(harness, target)

    assert _run(harness, body)["status"] == 503


# --- A7: a workspace a person's install holds ---------------------------------

def test_a_workspace_a_person_holds_is_left_as_it_is(harness, slack):
    """Until spec 032 group E. Sean added the app to the AutoManus workspace
    from his own tenant, and reminders set in its channels are timers on his
    tenant's disk. An owner who approves that workspace for a team space
    changes nothing, and waku.one keeps reading `slack: null`."""
    async def body():
        sean, cookie, _ = await sign_in(harness, sub="sub-sean", email="sean@example.com")
        _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                           cookie=f"{SESSION}={cookie}")
        state = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(headers["location"]).query))["state"]
        mine = await callback(harness, cookie=f"{SESSION}={cookie}", code="code-0",
                              state=state)
        before = harness.store.slack_install(sean)
        home_id, landed = await whole_install(harness)
        return sean, mine, before, home_id, landed, await read_home(harness)

    sean, mine, before, home_id, landed, read = _run(harness, body)
    assert mine == (302, "/setup?slack=connected")
    assert landed == (302, RETURN_URL)
    assert read == {"home_id": home_id, "slack": None, "refused": None}
    store = _reopen(harness)
    assert store.slack_tenant("T0123") == sean
    assert store.slack_install(sean) == before
    assert store.slack_install(sean).token_sealed == before.token_sealed
    assert store.slack_install(home_id) is None
    assert [install.tenant_id for install in store.slack_installs()] == [sean]
