"""DETERMINISTIC EVAL -- the setup page, opened signed in from waku.one.

waku.one's Integrations page links to /setup. Its server asks
`POST /v1/setup-link` with the person's Supabase token, and the browser opens
the one-time URL it answers. Covered here:

  - /v1/setup-link refuses a bad token, a GET and a non-JSON body, and mints
    for a good token
  - the code becomes the apex session and lands on /setup, signed in
  - a code works once, expires after 60 s, and is refused from another site
  - a sign-in code and an embed code cannot be spent here
  - a disabled tenant gets no session
"""

from __future__ import annotations

import asyncio
import json
import re

from gatewaylib import APEX, Harness, cookie_attributes, cookie_value, sign

from hosted.gateway import sessions, setup_page

SAME_SITE = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "document"}
LOGIN = f"https://{APEX}/login"


async def _mint(harness: Harness, token: str | None = None, *,
                content_type: str = "application/json", method: str = "POST"):
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await harness.send(method, setup_page.LINK_API_PATH, host=APEX,
                              body=b"{}", headers=headers)


async def _code(harness: Harness) -> str:
    token = sign(harness.private, now=harness.clock.t)
    status, _headers, body = await _mint(harness, token)
    assert status == 200, body
    return json.loads(body)["url"].split("code=", 1)[1]


async def _enter(harness: Harness, code: str, headers=None):
    return await harness.send("GET", f"{setup_page.AUTH_PATH}?code={code}", host=APEX,
                              headers=headers if headers is not None else SAME_SITE)


def test_setup_link_mints_a_one_time_url_for_a_good_token(harness):
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        status, _headers, body = await _mint(harness, token)
        await harness.stop()
        return status, json.loads(body)

    status, answer = asyncio.run(run())
    assert status == 200
    assert set(answer) == {"url", "expires_at"}
    assert re.fullmatch(rf"https://{re.escape(APEX)}/auth/setup\?code=[A-Za-z0-9_-]{{43}}",
                        answer["url"])
    assert answer["expires_at"] == int(harness.clock.t + 60)
    assert harness.forwarder.calls == [], "minting a code starts nothing"


def test_setup_link_refuses_no_token_a_bad_token_a_get_and_plain_text(harness):
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        answers = [(await _mint(harness, None))[0],
                   (await _mint(harness, "not-a-jwt"))[0],
                   (await _mint(harness, token, method="GET"))[0],
                   (await _mint(harness, token, content_type="text/plain"))[0]]
        await harness.stop()
        return answers

    assert asyncio.run(run()) == [401, 401, 405, 415]
    assert len(harness.gateway.setup_codes._codes) == 0


def test_the_code_signs_the_browser_in_and_lands_on_setup(harness):
    async def run():
        await harness.start()
        code = await _code(harness)
        status, headers, _ = await _enter(harness, code)
        value = cookie_value(headers, sessions.APEX_COOKIE)
        _, _, page = await harness.send("GET", "/setup", host=APEX,
                                        cookie=f"{sessions.APEX_COOKIE}={value}")
        await harness.stop()
        return status, headers, page.decode("utf-8")

    status, headers, page = asyncio.run(run())
    assert status == 302 and headers["location"] == "/setup"
    assert cookie_attributes(headers, sessions.APEX_COOKIE) >= {
        "secure", "httponly", "samesite=lax", "path=/"}
    assert "You are signed in." in page


def test_a_code_works_once_and_not_after_sixty_seconds(harness):
    async def run():
        await harness.start()
        code = await _code(harness)
        first = await _enter(harness, code)
        again = await _enter(harness, code)
        late_code = await _code(harness)
        harness.clock.t += 61
        late = await _enter(harness, late_code)
        await harness.stop()
        return first, again, late

    first, again, late = asyncio.run(run())
    assert first[0] == 302 and first[1]["location"] == "/setup"
    assert again[0] == 302 and again[1]["location"] == LOGIN, "once only"
    assert late[0] == 302 and late[1]["location"] == LOGIN
    assert cookie_value(again[1], sessions.APEX_COOKIE) == ""


def test_a_link_from_another_site_or_a_subresource_is_refused(harness):
    async def run():
        await harness.start()
        cross = await _enter(harness, await _code(harness),
                             {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document"})
        image = await _enter(harness, await _code(harness),
                             {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "image"})
        await harness.stop()
        return cross, image

    for status, headers, _ in asyncio.run(run()):
        assert status == 302 and headers["location"] == LOGIN
        assert cookie_value(headers, sessions.APEX_COOKIE) == ""


def test_a_sign_in_code_or_an_embed_code_is_not_a_setup_code(harness):
    async def run():
        await harness.start()
        tenant = (await _code(harness), harness.store.tenant_by_sub("sub-mei"))[1]
        sign_in = harness.gateway.handoffs.issue(tenant.id)
        embedded = harness.gateway.embed_codes.issue(tenant.id)
        answers = [await _enter(harness, sign_in), await _enter(harness, embedded)]
        await harness.stop()
        return answers

    for status, headers, _ in asyncio.run(run()):
        assert status == 302 and headers["location"] == LOGIN


def test_a_disabled_tenant_gets_no_session(harness):
    async def run():
        await harness.start()
        code = await _code(harness)
        harness.store.set_status(harness.store.tenant_by_sub("sub-mei").id, "disabled")
        answer = await _enter(harness, code)
        await harness.stop()
        return answer

    status, headers, _ = asyncio.run(run())
    assert status == 302 and headers["location"] == LOGIN
    assert cookie_value(headers, sessions.APEX_COOKIE) == ""
