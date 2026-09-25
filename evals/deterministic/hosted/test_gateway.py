"""One origin per tenant -- acceptance 21 -- and the headers on every answer.

E1 wrote the part that does not need a forwarder: the hand-off, the two
cookies, the host check, the headers, and the refusals that run before the
host is even resolved. E3 appended acceptance 14 and 22 below -- everything
from test_a_containers_own_headers_never_reach_the_browser down takes the
`wired` fixture, which is the real ContainerForwarder in front of a fake
container rather than E1's recording stand-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from gatewaylib import (
    SUPABASE_URL,
    FakeContainer,
    FakeSpawner,
    Harness,
    cookie_attributes,
    cookie_value,
    ops,
    send_raw,
    sign,
    sign_in,
    signed_in_on_the_tenant_host,
)

from hosted.core import idle, policy, quota
from hosted.gateway import __main__ as gateway_main
from hosted.gateway.config import REQUIRED_ENV_NAMES
from hosted.gateway.forward import ContainerForwarder

ROOT = Path(__file__).resolve().parents[3]
REPO_ENV_EXAMPLE = ROOT / "hosted" / "deploy" / "gateway.env.example"


def test_the_sign_in_answers_the_enter_url_and_following_it_lands_signed_in(harness):
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        status, headers, _body = await harness.send(
            "GET", f"/auth/enter?code={code}", host=host)
        tenant_cookie = cookie_value(headers, "__Host-waku_tenant")
        forwarded = await harness.send(
            "GET", "/api/data", host=host,
            cookie=f"__Host-waku_tenant={tenant_cookie}")
        await harness.stop()
        return tenant_id, status, headers.get("location"), forwarded

    tenant_id, status, location, forwarded = asyncio.run(run())
    assert (status, location) == (302, "/")
    assert forwarded[0] == 200
    assert forwarded[2] == b"forwarded"
    assert harness.forwarder.calls == [(tenant_id, "/api/data")]


def test_a_hand_off_code_works_once(harness):
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        first = await harness.send("GET", f"/auth/enter?code={code}", host=host)
        second = await harness.send("GET", f"/auth/enter?code={code}", host=host)
        await harness.stop()
        return first, second

    first, second = asyncio.run(run())
    assert cookie_value(first[1], "__Host-waku_tenant") != ""
    assert second[0] == 302
    assert second[1]["location"] == "https://agent.waku.one/login"
    assert cookie_value(second[1], "__Host-waku_tenant") == ""


def test_a_hand_off_code_works_only_on_its_own_tenant_host(harness):
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        other = "zzzzzzzzzzzz.agent.waku.one"
        wrong = await harness.send("GET", f"/auth/enter?code={code}", host=other)
        # And the attempt burned it, so the owner cannot use it either.
        owner = await harness.send("GET", f"/auth/enter?code={code}",
                                   host=f"{tenant_id}.agent.waku.one")
        await harness.stop()
        return wrong, owner

    wrong, owner = asyncio.run(run())
    assert cookie_value(wrong[1], "__Host-waku_tenant") == ""
    assert cookie_value(owner[1], "__Host-waku_tenant") == ""


def test_a_hand_off_code_expires_after_sixty_seconds(harness):
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        harness.clock.t += 61
        late = await harness.send("GET", f"/auth/enter?code={code}",
                                  host=f"{tenant_id}.agent.waku.one")
        await harness.stop()
        return late

    late = asyncio.run(run())
    assert cookie_value(late[1], "__Host-waku_tenant") == ""


def test_both_cookies_carry_exactly_the_attributes_the_host_prefix_requires(harness):
    """The WHOLE attribute set, compared as a set.

    An earlier draft asserted `"Path=/" in line` and `"Domain=" not in line`.
    Both are aiohttp `set_cookie` defaults (`path='/'`, `domain=None`), so
    deleting `path="/"` from `set_session_cookie` left the test green and no
    code change could ever have made `Domain=` appear. Comparing the set fails
    on a dropped `secure`, a dropped `httponly`, a changed `samesite`, an
    added `domain` and a dropped `path` alike.
    """
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        token = sign(harness.private, now=harness.clock.t)
        _s, apex_headers, _b = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one")
        _s2, tenant_headers, _b2 = await harness.send(
            "GET", f"/auth/enter?code={code}", host=f"{tenant_id}.agent.waku.one")
        await harness.stop()
        return (cookie_attributes(apex_headers, "__Host-waku_session"),
                cookie_attributes(tenant_headers, "__Host-waku_tenant"))

    # 2592000 spelled out: thirty days. Built from SESSION_TTL_SECONDS, this
    # assertion held for every value of it -- cutting the constant to 60 left
    # the test green and pinned the cookie's lifetime nowhere.
    expected = {"httponly", "secure", "samesite=lax", "path=/",
                "max-age=2592000"}
    for attributes in asyncio.run(run()):
        assert attributes == expected


def test_logout_deletes_the_apex_cookie_with_the_attributes_it_was_set_with(harness):
    """A __Host- Set-Cookie without Secure is rejected outright, so a deletion
    that drops it leaves the cookie in the jar. The sessions are gone either
    way -- every other test here proves that -- which is exactly why nothing
    but the attribute set can see this one."""
    async def run():
        await harness.start()
        _tenant_id, apex, _code = await sign_in(harness)
        out = await harness.json_post("/auth/logout", {}, host="agent.waku.one",
                                      cookie=f"__Host-waku_session={apex}")
        await harness.stop()
        return out

    attributes = cookie_attributes(asyncio.run(run())[1], "__Host-waku_session")
    assert {"secure", "httponly", "samesite=lax", "path=/"} <= attributes
    assert "max-age=0" in attributes


def test_a_session_is_refused_on_another_tenants_host(harness):
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        _s, headers, _b = await harness.send(
            "GET", f"/auth/enter?code={code}", host=f"{tenant_id}.agent.waku.one")
        cookie = f"__Host-waku_tenant={cookie_value(headers, '__Host-waku_tenant')}"
        elsewhere = await harness.send("GET", "/api/data", cookie=cookie,
                                       host="zzzzzzzzzzzz.agent.waku.one")
        await harness.stop()
        return elsewhere

    elsewhere = asyncio.run(run())
    assert elsewhere[0] == 401
    assert harness.forwarder.calls == []


def test_the_apex_never_forwards_to_a_container(harness):
    async def run():
        await harness.start()
        _tenant_id, apex, _code = await sign_in(harness)
        cookie = f"__Host-waku_session={apex}"
        seen = []
        for target in ("/api/data", "/", "/static/js/main.js", "/api/chat"):
            seen.append(await harness.send("GET", target, host="agent.waku.one",
                                           cookie=cookie))
        await harness.stop()
        return seen

    seen = asyncio.run(run())
    assert [row[0] for row in seen] == [404, 302, 404, 404]
    assert harness.forwarder.calls == []


def test_logout_on_the_apex_ends_the_tenant_host_session_at_once(harness):
    async def run():
        await harness.start()
        tenant_id, apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        _s, headers, _b = await harness.send("GET", f"/auth/enter?code={code}",
                                             host=host)
        cookie = f"__Host-waku_tenant={cookie_value(headers, '__Host-waku_tenant')}"
        before = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        out = await harness.json_post("/auth/logout", {}, host="agent.waku.one",
                                      cookie=f"__Host-waku_session={apex}")
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return before, out, after

    before, out, after = asyncio.run(run())
    assert before[0] == 200
    assert out[0] == 200
    assert after[0] == 401


# 421 as a literal, not as `guards.MISDIRECTED`. Written the second way, the
# test holds for every value of that constant: setting MISDIRECTED = 200 left
# all 646 evals green while a wrong host was answered 200.
@pytest.mark.parametrize("host, expected", [
    ("agent.waku.one", 404),
    ("zzzzzzzzzzzz.agent.waku.one", 401),
    ("not-a-tenant.agent.waku.one", 421),
    ("a.zzzzzzzzzzzz.agent.waku.one", 421),
    ("agent.waku.one.evil.example", 421),
    ("", 421),
    ("localhost", 421),
    ("zzzzzzzzzzzz.agent.waku.one.evil.example", 421),
    ("AGENT.WAKU.ONE", 404),
    ("agent.waku.one:443", 404),
    # The root label's dot, with and without a port. A resolver treats both as
    # the apex, and the port must come off before the dot does.
    ("agent.waku.one.", 404),
    ("agent.waku.one.:443", 404),
    ("zzzzzzzzzzzz.agent.waku.one.:443", 401),
])
def test_only_the_apex_and_a_well_formed_tenant_host_are_served(harness, host, expected):
    async def run():
        await harness.start()
        answer = await harness.send("GET", "/api/data", host=host)
        await harness.stop()
        return answer

    assert asyncio.run(run())[0] == expected


@pytest.mark.parametrize("target", [
    "//api/chat/stream", "/./api/voice", "/api/../api/compare/stream",
    "/api%2fsettings", "/api/ch%61t/stream", "/api\\settings", "/login%2e%2e",
])
def test_a_non_canonical_path_is_refused_before_anything_else(harness, target):
    async def run():
        await harness.start()
        apex = await harness.send("GET", target, host="agent.waku.one")
        tenant = await harness.send("GET", target,
                                    host="zzzzzzzzzzzz.agent.waku.one")
        await harness.stop()
        return apex, tenant

    apex, tenant = asyncio.run(run())
    assert apex[0] == 400
    assert tenant[0] == 400
    # The sentence as a literal. Compared to `policy.BAD_PATH` it held for
    # every value of that constant, "moved" included, and said nothing
    # about what the person reading the 400 is shown.
    assert json.loads(apex[2])["error"] == (
        "That is not a path this dashboard serves.")


@pytest.mark.parametrize("host", ["agent.waku.one", "zzzzzzzzzzzz.agent.waku.one"])
@pytest.mark.parametrize("value", ["script", "Script", "SCRIPT", " script ",
                                   "worker", ""])
def test_a_service_worker_request_is_refused_on_both_hosts(harness, host, value):
    """However the header is spelled, and whatever it says.

    Written as `== "script"`, `Service-Worker: Script` was answered 200 on a
    tenant host while `script` was refused. Case-folding alone would have
    left the shape -- a value nobody listed admitted by default -- so what is
    refused is the header, which is defined for one purpose and which no
    other client has a reason to send.
    """
    async def run():
        await harness.start()
        answer = await harness.send("GET", "/", host=host,
                                    headers={"Service-Worker": value})
        await harness.stop()
        return answer

    assert asyncio.run(run())[0] == 403


@pytest.mark.parametrize("host", ["agent.waku.one", "zzzzzzzzzzzz.agent.waku.one"])
def test_a_request_without_that_header_is_not_refused(harness, host):
    """The other half of the closed set: the refusal is the header, so a
    request that does not carry it must be answered normally. Without this,
    `return True` passes the table above."""
    async def run():
        await harness.start()
        answer = await harness.send("GET", "/", host=host)
        await harness.stop()
        return answer

    # The apex redirects to /login; a tenant host with no session bounces or
    # 401s. Neither is the 403 above, and neither is a refusal of this header.
    assert asyncio.run(run())[0] in (302, 401)


@pytest.mark.parametrize("host", ["agent.waku.one", "zzzzzzzzzzzz.agent.waku.one"])
def test_every_response_refuses_to_be_framed(harness, host):
    async def run():
        await harness.start()
        seen = []
        for method, target in (("GET", "/login"), ("GET", "/"),
                               ("GET", "/api/data"), ("GET", "/nope")):
            seen.append(await harness.send(method, target, host=host))
        await harness.stop()
        return seen

    for _status, headers, _body in asyncio.run(run()):
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cross-origin-resource-policy"] == "same-origin"


def test_clear_site_data_rides_on_login_and_logout_and_on_no_cookie_response(harness):
    async def run():
        await harness.start()
        login = await harness.send("GET", "/login", host="agent.waku.one")
        tenant_id, apex, code = await sign_in(harness)
        signed_in = await harness.json_post(
            "/auth/session", {"access_token": sign(harness.private,
                                                   now=harness.clock.t)},
            host="agent.waku.one")
        entered = await harness.send("GET", f"/auth/enter?code={code}",
                                     host=f"{tenant_id}.agent.waku.one")
        out = await harness.json_post("/auth/logout", {}, host="agent.waku.one",
                                      cookie=f"__Host-waku_session={apex}")
        await harness.stop()
        return login, signed_in, entered, out

    login, signed_in, entered, out = asyncio.run(run())
    assert login[1]["clear-site-data"] == '"cache", "storage"'
    assert out[1]["clear-site-data"] == '"cache", "storage"'
    for response in (signed_in, entered):
        assert "clear-site-data" not in response[1]
    assert cookie_value(signed_in[1], "__Host-waku_session") != ""
    assert cookie_value(entered[1], "__Host-waku_tenant") != ""


def test_a_post_to_a_tenant_host_needs_json_and_its_own_origin(harness):
    """The CSRF pair on a PROXIED post, not only on the apex's own routes.

    An allowlist of one origin: the host the request was sent to. The same
    cookie, the same path and the same body, with an Origin naming any other
    host, never reaches the forwarder.
    """
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        body = json.dumps({"message": "hello"}).encode("utf-8")
        foreign = await harness.json_post("/api/chat", {"message": "hello"},
                                          host=host, cookie=cookie,
                                          origin=f"https://evil.{host}")
        apex_origin = await harness.json_post("/api/chat", {"message": "hello"},
                                              host=host, cookie=cookie,
                                              origin="https://agent.waku.one")
        no_origin = await harness.send("POST", "/api/chat", host=host,
                                       cookie=cookie, body=body,
                                       headers={"Content-Type": "application/json"})
        form = await harness.send(
            "POST", "/api/chat", host=host, cookie=cookie, body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Origin": f"https://{host}"})
        own = await harness.json_post("/api/chat", {"message": "hello"},
                                      host=host, cookie=cookie)
        await harness.stop()
        return foreign, apex_origin, no_origin, form, own

    foreign, apex_origin, no_origin, form, own = asyncio.run(run())
    assert [row[0] for row in (foreign, apex_origin, no_origin, form)] == [415] * 4
    assert own[0] == 200
    assert [path for _tenant, path in harness.forwarder.calls] == ["/api/chat"]


def test_the_body_cap_is_this_gateways_own_and_not_aiohttps_default(harness):
    """4 MiB, set explicitly. aiohttp's own default is 1 MiB, so a body of two
    is the half of this test that a missing `client_max_size` fails, and a
    body of five is the half that a cap removed altogether fails."""
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        two = await harness.json_post(
            "/auth/session", {"access_token": token, "pad": "p" * (2 * 1024 * 1024)},
            host="agent.waku.one")
        five = await harness.json_post(
            "/auth/session", {"access_token": token, "pad": "p" * (5 * 1024 * 1024)},
            host="agent.waku.one")
        await harness.stop()
        return two, five

    two, five = asyncio.run(run())
    assert two[0] == 200
    assert five[0] == 413
    assert json.loads(five[2])["error"] == "That request is too large."
    assert five[1]["x-frame-options"] == "DENY"


def test_a_forwarder_that_raises_still_answers_with_the_security_headers(harness):
    """Acceptance 14 says EVERY response. A handler that raises is answered by
    aiohttp itself, which asks this class nothing -- so the five headers ride
    on on_response_prepare, and this is the response that sees it."""
    async def boom(_request, _tenant):
        raise RuntimeError("the forwarder fell over")

    harness.use_forwarder(boom)

    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        crashed = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return crashed

    status, headers, _body = asyncio.run(run())
    assert status == 500
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_a_session_outlives_the_sixty_second_cache_because_it_is_a_row(harness):
    """The cache is sixty seconds; the session is thirty days.

    Every other test here finishes inside the cache's window, so all of them
    would stay green with `create_session` deleted and the whole session
    living in one process's memory -- until the gateway restarted, or a
    minute passed. Here the clock moves past the cache first, so both hosts
    answer from the store or not at all.
    """
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        _tenant_id, apex, _code = await sign_in(harness)
        harness.clock.t += 61
        late = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        out = await harness.json_post("/auth/logout", {}, host="agent.waku.one",
                                      cookie=f"__Host-waku_session={apex}")
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return late, out, after

    late, out, after = asyncio.run(run())
    assert late[0] == 200
    assert out[0] == 200
    assert after[0] == 401


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_every_method_but_a_navigation_needs_the_csrf_pair(harness, method):
    """The CSRF check is an allowlist of METHODS, not a test for POST.

    Written as `if request.method != "POST"`, a PUT to /api/settings carrying
    the tenant's cookie, `Content-Type: text/plain` and
    `Origin: https://evil.example` was answered 200 and reached the forwarder.
    Nothing upstream takes a PUT today; that sentence is the whole problem,
    because nothing in this file would notice the day it stopped being true.
    """
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        bare = await harness.send(method, "/api/settings", host=host,
                                  cookie=cookie, body=b"{}",
                                  headers={"Content-Type": "text/plain",
                                           "Origin": "https://evil.example"})
        paired = await harness.send(method, "/api/settings", host=host,
                                    cookie=cookie, body=b"{}",
                                    headers={"Content-Type": "application/json",
                                             "Origin": f"https://{host}"})
        await harness.stop()
        return bare, paired

    bare, paired = asyncio.run(run())
    assert bare[0] == 415
    assert paired[0] == 200
    assert [path for _tenant, path in harness.forwarder.calls] == ["/api/settings"]


def test_a_navigation_is_the_only_thing_exempt_from_the_pair(harness):
    """GET and HEAD are the two named exemptions, and they are named because a
    navigation carries neither header. Everything else in the table above is
    checked; these two are what the exemption is for."""
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        got = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        headed = await harness.send("HEAD", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return got, headed

    got, headed = asyncio.run(run())
    assert got[0] == 200
    assert headed[0] == 200
    assert [path for _tenant, path in harness.forwarder.calls] == ["/api/data"] * 2


@pytest.mark.parametrize("extra, why", [
    ([("Sec-Fetch-Site", "cross-site")], "a link from another site"),
    ([("Sec-Fetch-Site", "none")], "typed, pasted or a restored tab"),
    ([("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Site", "cross-site")],
     "two copies, the good one first"),
    ([("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Site", "same-site")],
     "two copies, the other order"),
    ([("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Dest", "image")],
     "an <img> on a page that is same-site"),
    ([("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Dest", "empty")],
     "a fetch from a page that is same-site"),
    ([("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Dest", "document"),
      ("Sec-Fetch-Dest", "image")], "two destinations"),
])
def test_the_hand_off_is_served_only_to_a_same_site_navigation(harness, extra, why):
    """`headers.get` answers the FIRST copy of a repeated header, so a pair is
    refused outright rather than read from either end; `none` is refused
    because the real hand-off is always the apex page's location.assign; and
    a destination that is not a document is a subresource, which must not
    mint a session even from a page that is same-site -- a tenant's container
    serves whatever it likes on a host that shares the registrable domain."""
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        target = f"/auth/enter?code={code}"
        refused = await harness.send("GET", target, host=host, headers=extra)
        # And it did not burn the code: the owner's own navigation still works.
        owner = await harness.send("GET", target, host=host,
                                   headers={"Sec-Fetch-Site": "same-site",
                                            "Sec-Fetch-Dest": "document"})
        await harness.stop()
        return refused, owner

    refused, owner = asyncio.run(run())
    assert refused[0] == 302, why
    assert refused[1]["location"] == "https://agent.waku.one/login"
    assert cookie_value(refused[1], "__Host-waku_tenant") == ""
    assert cookie_value(owner[1], "__Host-waku_tenant") != ""


def test_a_cross_site_hand_off_is_refused(harness):
    """An attacker signs in as themselves, mints a code, and navigates the
    victim's browser to their own tenant host; the victim then works inside
    the attacker's container. The real hand-off is same-site -- the apex and
    a tenant host share agent.waku.one -- so `Sec-Fetch-Site` tells them
    apart. A browser that sends no such header is still served, which is what
    the empty string in HANDOFF_FETCH_SITES is and costs."""
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        target = f"/auth/enter?code={code}"
        elsewhere = await harness.send(
            "GET", target, host=host,
            headers={"Sec-Fetch-Site": "cross-site",
                     "Referer": "https://evil.example/x"})
        # And it did NOT burn the code: the owner's own navigation still works.
        owner = await harness.send("GET", target, host=host,
                                   headers={"Sec-Fetch-Site": "same-site"})
        await harness.stop()
        return elsewhere, owner

    elsewhere, owner = asyncio.run(run())
    assert elsewhere[0] == 302
    assert elsewhere[1]["location"] == "https://agent.waku.one/login"
    assert cookie_value(elsewhere[1], "__Host-waku_tenant") == ""
    assert cookie_value(owner[1], "__Host-waku_tenant") != ""


def test_a_session_is_bound_to_the_host_that_issued_it(harness):
    """The two cookies are two credentials, not one credential with two names.

    Before the scope went into the stored value, the apex cookie's value
    replayed as __Host-waku_tenant was accepted on the tenant host with a
    200. Neither cookie is readable by script, so this was never reachable
    from a browser -- it is the difference between the separation being a
    property of the store and being a naming convention.
    """
    async def run():
        await harness.start()
        tenant_id, apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        _s, headers, _b = await harness.send("GET", f"/auth/enter?code={code}",
                                             host=host)
        tenant_value = cookie_value(headers, "__Host-waku_tenant")
        replayed = await harness.send("GET", "/api/data", host=host,
                                      cookie=f"__Host-waku_tenant={apex}")
        # The reverse: a tenant value at the apex signs nobody out.
        out = await harness.json_post("/auth/logout", {}, host="agent.waku.one",
                                      cookie=f"__Host-waku_session={tenant_value}")
        still_here = await harness.send(
            "GET", "/api/data", host=host,
            cookie=f"__Host-waku_tenant={tenant_value}")
        await harness.stop()
        return replayed, out, still_here

    replayed, out, still_here = asyncio.run(run())
    assert replayed[0] == 401
    assert out[0] == 200
    assert still_here[0] == 200
    assert [path for _tenant, path in harness.forwarder.calls] == ["/api/data"]


def test_a_tenant_host_has_its_own_logout(harness):
    """Clear-Site-Data is per ORIGIN. A logout that only ever rides the apex
    response leaves the tenant origin's cache, its storage and its dead cookie
    in the browser -- and there was no route on that host to carry one."""
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        out = await harness.json_post("/auth/logout", {}, host=host, cookie=cookie)
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return out, after

    out, after = asyncio.run(run())
    assert out[0] == 200
    assert out[1]["clear-site-data"] == '"cache", "storage"'
    assert {"secure", "httponly", "samesite=lax", "path=/", "max-age=0"} <= (
        cookie_attributes(out[1], "__Host-waku_tenant"))
    assert after[0] == 401
    # The container never sees it: the session it ends is the gateway's, and
    # the route has to work when there is no container running at all.
    assert harness.forwarder.calls == []


def test_a_status_flipped_outside_this_process_is_honoured_and_ends_the_session(harness):
    """`disable` is not the only way a status changes: a restore, a second
    gateway or an operator editing the row does it without going through
    `end_sessions`. The status check in _tenant_host is what covers that, and
    until now the disable path's own `end_sessions` covered for it -- delete
    either and all 646 evals stayed green.
    """
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        tenant_id = host.split(".", 1)[0]
        harness.store.set_status(tenant_id, "disabled")
        refused = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        # Re-enabled by the same back door: the cookie from before must still
        # be dead, because the refusal above ended the sessions.
        harness.store.set_status(tenant_id, "active")
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return refused, after

    refused, after = asyncio.run(run())
    assert refused[0] == 401
    assert after[0] == 401
    assert harness.forwarder.calls == []


def test_a_hand_off_into_a_disabled_tenant_is_refused(harness):
    """`redeem` proves only that this gateway issued the code. The tenant can
    be disabled between the sign-in and the hand-off, and without a status
    check here the disable path rests entirely on `end_sessions` burning
    outstanding codes -- one guard, one line, and nothing measuring it."""
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        harness.store.set_status(tenant_id, "disabled")
        landed = await harness.send("GET", f"/auth/enter?code={code}",
                                    host=f"{tenant_id}.agent.waku.one")
        await harness.stop()
        return landed

    landed = asyncio.run(run())
    assert landed[0] == 302
    assert landed[1]["location"] == "https://agent.waku.one/login"
    assert cookie_value(landed[1], "__Host-waku_tenant") == ""


def test_the_login_page_carries_this_deployments_supabase_values(harness):
    """The two @@ markers are substituted by the handler, not by a build step.

    A page served with either marker still in it is a page whose script calls
    createClient("@@SUPABASE_URL@@"), so the markers are asserted gone as well
    as the values asserted present.
    """
    async def run():
        await harness.start()
        page = await harness.send("GET", "/login", host="agent.waku.one")
        await harness.stop()
        return page

    status, headers, body = asyncio.run(run())
    text = body.decode("utf-8")
    assert status == 200
    assert "@@" not in text
    assert 'data-supabase-url="https://upmikpuftlvvpwkvouqr.supabase.co"' in text
    assert 'data-supabase-key="sb_publishable_test"' in text
    # The page's own policy, not the default one-directive fallback.
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "'unsafe-inline'" not in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_auth_static_serves_three_named_files_and_nothing_else(harness):
    """An allowlist of three names, not a directory walk.

    hosted/ is copied whole into the services image, so a walk would serve
    whatever anybody drops beside login.css. The vendored Supabase client is
    checked by digest here as well: the gateway hands that file to every
    visitor, and a swapped copy is the shortest path to every session on the
    apex.
    """
    async def run():
        await harness.start()
        seen = {}
        for name in ("login.css", "login.js", "supabase.js", "supabase-js-2.117.1.js",
                     "login.css/", "nothing.js"):
            seen[name] = await harness.send("GET", f"/auth/static/{name}",
                                            host="agent.waku.one")
        await harness.stop()
        return seen

    seen = asyncio.run(run())
    assert [seen[name][0] for name in ("login.css", "login.js", "supabase.js")] == [200] * 3
    assert seen["login.css"][1]["content-type"] == "text/css; charset=utf-8"
    assert seen["login.js"][1]["content-type"] == "text/javascript; charset=utf-8"
    # The version is a name the page never spells, and the raw filename is not
    # a second way to ask for the same bytes.
    assert seen["supabase-js-2.117.1.js"][0] == 404
    assert seen["login.css/"][0] == 404
    assert seen["nothing.js"][0] == 404
    vendored = (ROOT / "hosted/gateway/static/supabase-js-2.117.1.js").read_bytes()
    assert seen["supabase.js"][2] == vendored
    assert hashlib.sha256(vendored).hexdigest() == (
        "dff1e545f4f35bd42895cd6f46431e56137dd13031e46a9759c446447c11a567")


def test_the_login_page_names_no_waku_static_file():
    """The gateway's pages "use no Waku brand file" (spec). The import
    boundary test reads *.py only, so an HTML src or href pointing into
    waku/ops/static/ is invisible to it."""
    for name in ("hosted/templates/login.html", "hosted/gateway/static/login.css",
                 "hosted/gateway/static/login.js"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "/static/" not in text.replace("/auth/static/", "")
        assert "waku/ops" not in text


def test_the_example_file_and_the_config_agree():
    names = set()
    for raw in REPO_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.add(line.split("=", 1)[0])
    assert names == set(REQUIRED_ENV_NAMES)


# --- acceptance 14: what crosses the boundary, in each direction ---------


def test_a_containers_own_headers_never_reach_the_browser(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        await wired.stop()
        return answer

    status, headers, body = asyncio.run(run())
    assert status == 200
    assert json.loads(body)["path"] == "/api/data"
    # The container sent all four. None of them is here.
    assert headers["__all__"].get("set-cookie", []) == []
    assert "x-secret" not in headers
    assert "location" not in headers
    assert "clear-site-data" not in headers
    # And the gateway's own five are.
    assert headers["cache-control"] == "no-store"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["content-type"] == "application/json"


def test_the_container_never_sees_the_cookie_or_any_other_credential(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        await wired.send("GET", "/api/data", host=host, cookie=cookie,
                         headers={"Authorization": "Bearer secret",
                                  "X-Waku-Background": "1",
                                  "Accept": "application/json",
                                  "User-Agent": "probe/1"})
        await wired.stop()

    asyncio.run(run())
    seen = {name.lower() for name in wired.container.headers[0]}
    # The credentials and the fingerprint the browser sent: gone.
    assert "cookie" not in seen
    assert "authorization" not in seen
    # aiohttp injects these two below the allowlist unless skip_auto_headers
    # names them, which _send does. Measured before the fix: the container saw
    # `User-Agent: Python/3.11 aiohttp/3.14.3` and
    # `Accept-Encoding: gzip, deflate`. Neither is a credential; an allowlist
    # with two names it does not know about is still not an allowlist, and the
    # Accept-Encoding one is what makes __main__'s auto_decompress reason true.
    assert "user-agent" not in seen
    assert "accept-encoding" not in seen
    # What the allowlist admits, and only these plus Host and Content-Length.
    assert "accept" in seen
    assert "x-waku-background" in seen
    assert seen <= {"host", "accept", "content-type", "content-length",
                    "x-waku-background"}


def test_a_stream_passes_through_unbuffered(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        answer = await wired.send("POST", "/api/chat/stream", host=host,
                                  cookie=cookie, body=b'{"message": "hi"}',
                                  headers={"Content-Type": "application/json",
                                           "Origin": f"https://{host}"})
        await wired.stop()
        return answer

    status, headers, body = asyncio.run(run())
    assert status == 200
    assert headers["content-type"] == "text/event-stream"
    assert body == b'data: {"n": 0}\n\ndata: {"n": 1}\n\ndata: {"n": 2}\n\n'
    assert headers["__all__"].get("set-cookie", []) == []


def test_a_tenant_in_maintenance_gets_the_maintenance_answer_and_no_container(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        before = len([r for r in wired.spawner.requests if r["op"] == "start"])
        wired.launcher.mark_maintenance(tenant_id)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        after = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, before, after

    answer, before, after = asyncio.run(run())
    assert answer[0] == 503
    assert json.loads(answer[2])["error"] == idle.MAINTENANCE_MESSAGE
    assert before == after
    assert wired.container.targets == []


# --- acceptance 22: the raw target, and what policy refuses --------------


@pytest.mark.parametrize("target", [
    "/api/events?cursor=42",
    "/api/models?provider=",
    "/api/data",
    "/static/js/main.js",
    "/api/compare/history?limit=3",
    # THE LAST TWO ARE THE ONES THAT DRIVE encoded=True, AND THEY WERE FOUND
    # BY BREAKING IT. Measured on yarl 1.24.2: without encoded=True the five
    # above survive re-encoding unchanged -- the empty `provider=` value
    # included -- so a test of only those cannot fail. What yarl does change
    # is a percent-escape it considers safe to resolve: %2F becomes "/" and
    # %3F becomes "?", so the container is handed a different query from the
    # one the browser sent, and in the %3F case a second "?" that upstream
    # splits on.
    "/api/query?q=%2Fhome%2Fmei",
    "/api/query?q=%3F",
])
def test_the_container_receives_the_raw_path_and_the_raw_query(wired, target):
    """Acceptance 22, and the reason forward._send builds its URL with
    encoded=True: the container gets the target byte for byte."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        await wired.send("GET", target, host=host, cookie=cookie)
        await wired.stop()

    asyncio.run(run())
    assert wired.container.targets == [target]


@pytest.mark.parametrize("target, status", [
    ("/api/voice", 403),
    ("/api/reveal/x", 403),
    ("/api/memory-arena", 403),
    ("/api/memory-arena/stores", 403),
    ("/api/judgment-arena", 403),
    ("/api/compare", 403),
    ("/api/compare/clear", 403),
])
def test_a_blocked_route_never_reaches_the_container(wired, target, status):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        answer = await wired.send("GET", target, host=host, cookie=cookie)
        await wired.stop()
        return answer

    answer = asyncio.run(run())
    assert answer[0] == status
    assert wired.container.targets == []


def test_a_blocked_streaming_route_answers_a_done_event_and_not_json(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        answer = await wired.send("POST", "/api/compare/stream", host=host,
                                  cookie=cookie, body=b"{}",
                                  headers={"Content-Type": "application/json",
                                           "Origin": f"https://{host}"})
        await wired.stop()
        return answer

    status, headers, body = asyncio.run(run())
    assert status == 403
    assert headers["content-type"].startswith("text/event-stream")
    frame = json.loads(body.decode("utf-8").removeprefix("data: ").strip())
    assert frame["kind"] == "done"
    # The LITERAL, not policy.ARENA_BLOCKED. This was the only reference to
    # that constant outside policy.py, so comparing to it held for every value
    # the constant could have -- set it to "MUTATED" and all 1679 tests passed.
    # Its five sibling block messages each go red somewhere; this one did not.
    assert frame["error"] == "The arenas are not available on hosted waku."


def test_a_platform_provider_payload_carrying_a_key_is_refused(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        refused = await wired.send(
            "POST", "/api/providers", host=host, cookie=cookie,
            body=json.dumps({"provider": "waku-platform",
                             "key": "sk-ant-stolen"}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Origin": f"https://{host}"})
        allowed = await wired.send(
            "POST", "/api/providers", host=host, cookie=cookie,
            body=json.dumps({"provider": "anthropic", "key": "sk-mine"}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": f"https://{host}"})
        await wired.stop()
        return refused, allowed

    refused, allowed = asyncio.run(run())
    assert refused[0] == 403
    assert allowed[0] == 200
    assert wired.container.targets == ["/api/providers"]


def test_a_filtered_body_is_re_serialised_and_sent_with_its_own_length(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        await wired.send(
            "POST", "/api/settings", host=host, cookie=cookie,
            body=json.dumps({"telemetry": True,
                             "experimental": {"delegation": True}}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": f"https://{host}"})
        await wired.stop()

    asyncio.run(run())
    sent = json.loads(wired.container.bodies[0])
    assert sent == {"telemetry": True}
    assert int(wired.container.headers[0]["Content-Length"]) == len(
        wired.container.bodies[0])
    assert "Transfer-Encoding" not in wired.container.headers[0]
    # IF YOU BREAK THIS ON PURPOSE, IT GOES RED AFTER 121 SECONDS, NOT AT ONCE.
    # Copying the caller's Content-Length hands the container a length longer
    # than the body; it blocks on bytes that never arrive and the request dies
    # at forward's 120-second timeout. That is the failure being refused, and
    # it looks exactly like a hung suite for two minutes first.


# --- turns, background requests and the running cap ----------------------


def test_a_gateway_that_cannot_read_spend_applies_frees_turn_limit(wired):
    """With group D cut, run/proxy/proxy.sock does not exist, so read_spend
    answers None on every call. `platform_call_recent` must read that as
    "treat them as free", not as "no platform call, so promote them to byok's
    120". This is the test the three-branch function exists for."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        seen = []
        for _ in range(31):
            seen.append(await wired.send(
                "POST", "/api/chat", host=host, cookie=cookie, body=b'{"m": "x"}',
                headers={"Content-Type": "application/json",
                         "Origin": f"https://{host}"}))
        await wired.stop()
        return seen

    seen = asyncio.run(run())
    assert [row[0] for row in seen[:30]] == [200] * 30
    assert seen[30][0] == 429
    assert json.loads(seen[30][2])["error"] == quota.TURN_LIMIT_MESSAGE
    assert len(wired.container.targets) == 30


def test_the_turn_limit_on_a_stream_is_a_done_event(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        for _ in range(30):
            await wired.send("POST", "/api/chat", host=host, cookie=cookie,
                             body=b'{"m": "x"}',
                             headers={"Content-Type": "application/json",
                                      "Origin": f"https://{host}"})
        answer = await wired.send("POST", "/api/chat/stream", host=host,
                                  cookie=cookie, body=b'{"m": "x"}',
                                  headers={"Content-Type": "application/json",
                                           "Origin": f"https://{host}"})
        await wired.stop()
        return answer

    status, headers, body = asyncio.run(run())
    assert status == 429
    assert headers["content-type"].startswith("text/event-stream")
    assert quota.TURN_LIMIT_MESSAGE in body.decode("utf-8")


def test_a_background_request_to_a_stopped_container_is_paused_and_starts_nothing(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie,
                                  headers={"X-Waku-Background": "1"})
        starts = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, starts

    answer, starts = asyncio.run(run())
    assert answer[0] == policy.PAUSED_STATUS
    assert json.loads(answer[2]) == policy.PAUSED_BODY
    assert starts == 1              # the sign-in's pre-warm only


def test_a_user_driven_request_to_a_stopped_container_starts_it(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        starts = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, starts

    answer, starts = asyncio.run(run())
    assert answer[0] == 200
    assert starts == 2


def test_a_background_request_never_touches_the_idle_clock(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        wired.clock.t += 14 * 60
        await wired.send("GET", "/api/data", host=host, cookie=cookie,
                         headers={"X-Waku-Background": "1"})
        wired.clock.t += 2 * 60
        stops = wired.fleet.idle_stops()
        await wired.stop()
        return tenant_id, stops

    tenant_id, stops = asyncio.run(run())
    assert stops == [tenant_id]


def test_a_sign_in_at_the_cap_evicts_rather_than_over_committing(wired_one_slot):
    """The running cap binds on EVERY path that starts a container, sign-in
    included.

    It did not. `_sign_in` pre-warms with Launcher.start, `start` consults
    nothing about how many containers are up, and the cap lives in
    Fleet.admit -- so two sign-ins on a one-slot VM left two containers
    running. On a t3.large --max-running is derived from memory and the
    failure mode of exceeding it is the kernel OOM-killing somebody's
    container mid-turn: a running tenant's work destroyed by a stranger
    signing in. The pre-warm now goes through the same admission every
    request uses, and at the cap it evicts.

    THE ASSERTION IS THE FLEET, NOT JUST THE STOP. A `stop` recorded on the
    spawner would still be true of a gateway that stopped the first container
    and then started two anyway; `fleet.running()` is what the cap is about.
    """
    wired = wired_one_slot

    async def run():
        await wired.start()
        first_host, _first_cookie = await signed_in_on_the_tenant_host(
            wired, sub="sub-one", email="one@example.com")
        wired.clock.t += 10
        mark = len(wired.spawner.requests)
        second_host, _second_cookie = await signed_in_on_the_tenant_host(
            wired, sub="sub-two", email="two@example.com")
        during = wired.spawner.requests[mark:]
        running = sorted(wired.fleet.running())
        await wired.stop()
        return (first_host.split(".", 1)[0], second_host.split(".", 1)[0],
                during, running)

    first_id, second_id, during, running = asyncio.run(run())
    assert [r["tenant_id"] for r in during if r["op"] == "stop"] == [first_id]
    assert [r["tenant_id"] for r in during if r["op"] == "start"] == [second_id]
    # One slot, one container. This is the line the finding was about.
    assert running == [second_id]


def test_at_the_cap_the_least_recently_used_container_is_stopped_first(
        wired_two_slots):
    """Two slots and three tenants, so the eviction is a CHOICE.

    With one slot there is exactly one candidate and `min` and `max` over
    last_activity answer the same tenant -- "least recently used" would be
    untested. Here the third sign-in picks between two, and the older one is
    the claim: swap Fleet._evictable's `min` for `max` and this goes red.

    (Before the sign-in pre-warm went through Fleet.admit this test looked
    quite different: it signed three people in on a ONE-slot harness, which
    left all three containers running, and then stopped one by hand so that
    its next REQUEST would evict. The cap now binds at sign-in, so the
    eviction is the third sign-in itself and no container has to be stopped
    by hand to reach it.)
    """
    wired = wired_two_slots

    async def run():
        await wired.start()
        first_host, _c1 = await signed_in_on_the_tenant_host(
            wired, sub="sub-one", email="one@example.com")
        wired.clock.t += 10
        second_host, _c2 = await signed_in_on_the_tenant_host(
            wired, sub="sub-two", email="two@example.com")
        wired.clock.t += 10
        mark = len(wired.spawner.requests)
        third_host, third_cookie = await signed_in_on_the_tenant_host(
            wired, sub="sub-three", email="three@example.com")
        during = wired.spawner.requests[mark:]
        # And the third tenant's dashboard works on the container that start
        # produced, so the eviction was not a container the gateway then
        # forwarded to anyway.
        answer = await wired.send("GET", "/api/data", host=third_host,
                                  cookie=third_cookie)
        running = sorted(wired.fleet.running())
        await wired.stop()
        return ([h.split(".", 1)[0] for h in (first_host, second_host, third_host)],
                during, answer, running)

    (first_id, second_id, third_id), during, answer, running = asyncio.run(run())
    assert answer[0] == 200
    # `== [first_id]` and not `in`: with `in`, an eviction that stopped BOTH
    # of the running containers would still pass.
    assert [r["tenant_id"] for r in during if r["op"] == "stop"] == [first_id]
    assert sorted(running) == sorted([second_id, third_id])


def test_at_the_cap_a_request_evicts_too_and_it_is_still_the_least_recent(
        wired_two_slots):
    """The other entry point into the same eviction: _reach's
    `evict_then_start` arm, reached by a REQUEST rather than a sign-in.

    The first tenant has already been evicted by the third sign-in, so their
    next request finds their own container stopped and the VM full. It must
    evict the older of the two that are up -- the second tenant, whose last
    activity is ten seconds before the third's.
    """
    wired = wired_two_slots

    async def run():
        await wired.start()
        first_host, first_cookie = await signed_in_on_the_tenant_host(
            wired, sub="sub-one", email="one@example.com")
        wired.clock.t += 10
        second_host, _c2 = await signed_in_on_the_tenant_host(
            wired, sub="sub-two", email="two@example.com")
        wired.clock.t += 10
        third_host, _c3 = await signed_in_on_the_tenant_host(
            wired, sub="sub-three", email="three@example.com")
        wired.clock.t += 10
        mark = len(wired.spawner.requests)
        answer = await wired.send("GET", "/api/data", host=first_host,
                                  cookie=first_cookie)
        during = wired.spawner.requests[mark:]
        running = sorted(wired.fleet.running())
        await wired.stop()
        return ([h.split(".", 1)[0] for h in (first_host, second_host, third_host)],
                during, answer, running)

    (first_id, second_id, third_id), during, answer, running = asyncio.run(run())
    assert answer[0] == 200
    assert [r["tenant_id"] for r in during if r["op"] == "stop"] == [second_id]
    assert [r["tenant_id"] for r in during if r["op"] == "start"] == [first_id]
    assert sorted(running) == sorted([first_id, third_id])


@pytest.mark.parametrize("target", ["/api/chat/streamX", "/api/chatX"])
def test_a_suffixed_turn_route_is_still_charged_as_a_turn(wired, target):
    """policy.match is longest-PREFIX and policy.is_turn is exact membership,
    so /api/chat/streamX matches the /api/chat/stream entry and PASSES while
    is_turn says no. Counting from the path would make a one-character suffix
    an uncounted turn. forward.is_a_turn reads the MATCHED ROUTE instead.

    Harmless today only because dashboard.py matches all three turn routes
    with `self.path == ...`, so the container answers 404 -- a future route
    matched with startswith would open it.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        seen = []
        for _ in range(31):
            seen.append(await wired.send(
                "POST", target, host=host, cookie=cookie, body=b'{"m": "x"}',
                headers={"Content-Type": "application/json",
                         "Origin": f"https://{host}"}))
        await wired.stop()
        return seen

    seen = asyncio.run(run())
    assert seen[30][0] == 429
    assert len(wired.container.targets) == 30


# --- the 120-second timeout, and what is exempt from it ------------------


def test_a_slow_ordinary_request_times_out_and_a_slow_turn_does_not(wired, monkeypatch):
    """The 120-second timeout applies to everything that is not a turn or
    another stream, so "a query that never returns cannot hold a container
    awake" (spec). Driven at 50 ms rather than 120 s, through the constant
    hosted/core/idle.py declares and forward.py reads."""
    monkeypatch.setattr(idle, "FORWARD_TIMEOUT_SECONDS", 0.05)

    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        wired.container.delay = 0.4
        ordinary = await wired.send("GET", "/api/query", host=host, cookie=cookie)
        stream = await wired.send("POST", "/api/chat/stream", host=host,
                                  cookie=cookie, body=b'{"m": "x"}',
                                  headers={"Content-Type": "application/json",
                                           "Origin": f"https://{host}"})
        await wired.stop()
        return ordinary, stream

    ordinary, stream = asyncio.run(run())
    assert ordinary[0] == 504
    # The LITERAL, not answers.TOOK_TOO_LONG: comparing the body to the
    # constant that wrote it holds for every value the constant could have,
    # the empty string included. This is the sentence a person reads.
    assert json.loads(ordinary[2])["error"] == "That took too long. Try again."
    assert stream[0] == 200


# --- the refused-connection ladder ---------------------------------------


def test_a_refused_connection_is_re_listed_before_anything_is_started(tmp_path):
    """Spec: a container the gateway believed running that refuses the
    connection is looked up again with `list`; if it is running, the gateway
    retries, and only if it is gone does it start one more time.

    THE STALE ADDRESS ARRIVES THE WAY A REAL ONE WOULD. FakeSpawner.ports is
    consumed in order, so the sign-in's pre-warm records a port nothing is
    listening on and every later answer is the live container's. An earlier
    draft wrote launcher._addresses directly, which is a private write AND a
    test that still passes if `start` stops recording addresses at all.

    THIS IS THE LADDER'S SECOND RUNG: THE CONTAINER IS GONE. The spawner is
    told to forget it before the request, so `list` reports nothing, resync
    drops the address and the gateway starts it once more. The ORDER is the
    assertion -- delete the resync from _deliver and the list disappears from
    the middle of it, leaving ["start"].

    THE FIRST RUNG -- list finds it running at a new address and the retry
    succeeds with NO start -- cannot be driven in this tier, and pretending
    otherwise would be worse than saying so. Launcher.resync only adopts a
    container at the address the tenant's project id derives, which is on
    10.88.0.0/16, and a pytest process can open a socket to loopback and to
    nothing else. It is evals/hosted_docker/'s to hold, where the addresses
    are real.
    """
    container = FakeContainer()
    container.start()
    spawner = FakeSpawner()
    wired = Harness(tmp_path, spawner)
    wired.use_real_forwarding(container)
    # use_real_forwarding sets spawner.port; ports wins, and the second entry
    # repeats for every later start.
    spawner.ports = [1, container.port]     # dead first, then the real one

    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        # Gone: the container the pre-warm recorded is not in the spawner's
        # list any more, which is what a kill for memory looks like from here.
        spawner.running.pop(host.split(".", 1)[0], None)
        mark = len(spawner.requests)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        during = [r["op"] for r in spawner.requests[mark:]]
        await wired.stop()
        return answer, during

    try:
        answer, during = asyncio.run(run())
    finally:
        container.stop()
    assert answer[0] == 200
    assert container.targets == ["/api/data"]
    # The pre-warm was the only start before the request; inside the request,
    # the list comes first and the second start only after it.
    assert during == ["list", "start"]
    assert len(ops(spawner, "start")) == 2


def test_a_refused_connection_on_a_background_request_pauses_rather_than_starts(wired):
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        wired.container.stop()                  # nothing is listening now
        wired.spawner.running.pop(tenant_id, None)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie,
                                  headers={"X-Waku-Background": "1"})
        starts = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, starts

    answer, starts = asyncio.run(run())
    assert answer[0] == policy.PAUSED_STATUS
    assert starts == 1


def test_the_gateway_process_writes_no_hand_off_code_to_its_log(caplog, tmp_path):
    """The hand-off code is the one credential this deployment puts in a URL,
    and aiohttp's default access line is `%r` -- the raw request line, query
    included. E1 left this to E3 in _enter's docstring.

    BOTH RUNNERS ARE DRIVEN, and that is the point. `assert code not in
    caplog.text` on its own passes when nothing logs at all -- a broken
    handler, a filtered logger, a request that never arrived. The default
    runner is the control: it MUST write the code, which proves the mechanism
    is live and this process's root logger is capturing it, so the second
    half is a drop that happened rather than a silence.
    """
    code = "handoffcodeSEKRIT"
    target = f"/auth/enter?code={code}"
    host = f"{'a' * 12}.agent.waku.one"

    async def drive(make_runner):
        harness = Harness(tmp_path, FakeSpawner())
        runner = make_runner(harness.gateway.build())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        answer = await send_raw(runner.addresses[0][1], "GET", target, host)
        await runner.cleanup()
        await harness.stop()
        return answer

    with caplog.at_level(logging.INFO):
        default_answer = asyncio.run(drive(web.AppRunner))
        wrote_by_default = code in caplog.text
        caplog.clear()
        ours_answer = asyncio.run(drive(gateway_main.build_runner))
        wrote_by_ours = code in caplog.text

    # The request really happened on both: an unredeemable code bounces to
    # /login, so a 302 is the proof there was a request line to log.
    assert default_answer[0] == 302
    assert ours_answer[0] == 302
    assert wrote_by_default, "aiohttp stopped writing %r; this test's control is gone"
    assert not wrote_by_ours


def _gateway_env(root) -> dict[str, str]:
    """A config/gateway.env the process can actually start from.

    Every name in config.REQUIRED_ENV_NAMES, because config_from_env refuses a
    missing one -- and a short socket directory, because AF_UNIX caps a path
    at 104 bytes on macOS and pytest spells the test's name into tmp_path.
    The spawner socket is deliberately absent from disk: the startup resync
    must log and carry on.
    """
    return {
        "WAKU_APEX_HOST": "agent.waku.one",
        "WAKU_GATEWAY_BIND": "127.0.0.1",
        "WAKU_GATEWAY_PORT": "0",
        "WAKU_CONTROL_DB": str(root / "control.db"),
        "WAKU_SPAWNER_SOCKET": str(root / "spawner.sock"),
        "WAKU_GATEWAY_SOCKET": str(root / "g.sock"),
        "WAKU_PROXY_SOCKET": str(root / "p.sock"),
        "WAKU_ADMIN_SOCKET": str(root / "a.sock"),
        "WAKU_MAX_RUNNING": "4",
        "WAKU_SUPABASE_URL": SUPABASE_URL,
        "WAKU_SUPABASE_ISSUER": f"{SUPABASE_URL}/auth/v1",
        "WAKU_SUPABASE_JWKS_URL": f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
        "WAKU_SUPABASE_AUDIENCE": "https://api.waku.one/mcp",
        "WAKU_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
        "WAKU_FREE_TURNS_PER_HOUR": "30",
        "WAKU_BYOK_TURNS_PER_HOUR": "120",
    }


def test_the_gateway_process_serves_with_the_access_log_off(
        monkeypatch, caplog, sock_dir):
    """The test above pins build_runner. THIS one pins main(), and the
    difference is the whole finding.

    build_runner has exactly one caller. Leave it correct and change that one
    line back to `web.AppRunner(gateway.build())` and the suite above stays
    green -- measured -- because it drives the helper rather than the process
    that serves. So this runs the real main(), on a real ephemeral port,
    sends the real hand-off URL at it, and reads the log.

    TWO ASSERTIONS AND THEY ARE NOT THE SAME ONE. The kwargs say which runner
    main() built, so the regression is named at the line it happens on; the
    log says what that runner then did with a query string. The control for
    the second -- that aiohttp writes this by default at all -- is the test
    above, which drives both runners and requires the default to write it.
    """
    for name, value in _gateway_env(sock_dir).items():
        monkeypatch.setenv(name, value)
    built: dict = {}

    class Recorder(web.AppRunner):
        def __init__(self, app, **kwargs):
            built["kwargs"] = kwargs
            super().__init__(app, **kwargs)
            built["runner"] = self

    # gateway_main.web IS aiohttp.web, so this patches the module every caller
    # shares -- monkeypatch puts it back, and nothing else runs in between.
    monkeypatch.setattr(gateway_main.web, "AppRunner", Recorder)
    code = "mainlevelSEKRIT"

    async def run():
        task = asyncio.create_task(gateway_main.main())
        for _ in range(500):
            if built.get("runner") is not None and built["runner"].addresses:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            raise AssertionError("main() never bound a port")
        port = built["runner"].addresses[0][1]
        answer = await send_raw(port, "GET", f"/auth/enter?code={code}",
                                f"{'a' * 12}.agent.waku.one")
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.CancelledError, TimeoutError):
            pass
        return answer

    with caplog.at_level(logging.INFO):
        answer = asyncio.run(run())

    assert answer[0] == 302, "the request never reached the gateway"
    assert built["kwargs"].get("access_log", "NOT PASSED") is None
    assert code not in caplog.text


@pytest.mark.parametrize("sent, passed_on", [
    ("/", "/"),
    ("/next", "/next"),
    ("/a?b=c", "/a?b=c"),
    ("//evil.example/x", None),        # protocol-relative: an absolute URL
    ("/\\evil.example/x", None),       # the WHATWG parser reads \ as /
    ("https://evil.example/", None),
    ("http://evil.example/", None),
    ("evil.example", None),
    ("", None),
])
def test_only_a_relative_same_origin_location_is_passed_on(wired, sent, passed_on):
    """A container choosing where the browser goes next is the redirect half
    of acceptance 14. A closed set, and BOTH halves of it are driven: the two
    that must pass through are here so that a guard which simply drops every
    Location would fail, and the five that must not are here so that a guard
    which tests only for "//" would fail on the backslash.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        wired.container.location = sent
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        await wired.stop()
        return answer

    _status, headers, _body = asyncio.run(run())
    assert headers.get("location") == passed_on


def test_a_container_with_a_turn_in_flight_is_not_idle_stopped(wired):
    """_deliver marks a held request with fleet.enter/leave, and
    Fleet.idle_stops only names containers with nothing in flight.

    Without the pair, the once-a-minute sweep stops a container in the middle
    of a twenty-minute turn: the idle clock was last touched when the request
    ARRIVED, so a turn longer than fifteen minutes is stale by the time it
    answers. The second half of the assertion is what keeps this honest --
    the moment the turn finishes, the same sweep does name it.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        wired.container.delay = 0.5
        task = asyncio.create_task(wired.send(
            "POST", "/api/chat/stream", host=host, cookie=cookie,
            body=b'{"m": "x"}',
            headers={"Content-Type": "application/json",
                     "Origin": f"https://{host}"}))
        await asyncio.sleep(0.2)          # the request is at the container
        wired.clock.t += 16 * 60          # older than idle.IDLE_SECONDS
        during = wired.fleet.idle_stops()
        answer = await task
        after = wired.fleet.idle_stops()
        await wired.stop()
        return tenant_id, during, after, answer

    tenant_id, during, after, answer = asyncio.run(run())
    assert answer[0] == 200
    assert during == []                   # in flight: not swept
    assert after == [tenant_id]           # and the moment it finishes, it is


@pytest.mark.parametrize("background, status, starts", [
    (True, policy.PAUSED_STATUS, 1),
    (False, 200, 2),
])
def test_a_container_the_fleet_calls_running_with_no_address_is_paused_or_started(
        wired, background, status, starts):
    """The one case Fleet.admit cannot see: it says RUNNING and the address
    book is empty. That is what a resync leaves behind when it drops an
    address the fleet has not been told about yet, and _reach's last three
    lines are the whole handling of it -- pause a background request, start a
    user-driven one.

    BOTH ROWS ARE HERE BECAUSE ONE OF THEM CANNOT FAIL ALONE. admit() answers
    "paused" for a background request to a STOPPED container long before
    _reach's fall-through is reached, so a test that merely stops the
    container leaves these three lines green whatever they say -- measured:
    deleting the `if background: return self._paused()` from the fall-through
    left test_a_background_request_to_a_stopped_container... passing. The
    fleet is put back to RUNNING through Fleet.set_status, which is public and
    is the same call resync's own adopt/forget pair makes.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)      # forgets the address
        wired.fleet.set_status(tenant_id, idle.RUNNING)   # ... but not this
        headers = {"X-Waku-Background": "1"} if background else None
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie,
                                  headers=headers)
        seen = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, seen

    answer, seen = asyncio.run(run())
    assert answer[0] == status
    assert seen == starts


async def _read_one_chunk(reader) -> bytes | None:
    """One HTTP/1.1 chunk off the wire, or None at the terminating 0-chunk.

    By hand, because the SIZE of each chunk is the evidence: aiohttp emits one
    chunk per StreamResponse.write(), so three chunks is three writes and one
    chunk is one.
    """
    line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
    size = int(line.split(b";")[0] or b"0", 16)
    if size == 0:
        return None
    payload = await asyncio.wait_for(reader.readexactly(size), timeout=5)
    await asyncio.wait_for(reader.readexactly(2), timeout=5)
    return payload


def test_a_stream_reaches_the_browser_before_the_container_has_finished_it(wired):
    """"Unbuffered" is a claim about WHEN, and the frame-for-frame test above
    cannot make it: that one reads the socket to EOF, so a gateway that
    collected the whole body and wrote it once would pass it unchanged.

    TWO PIECES OF EVIDENCE, NEITHER OF THEM A TIMING ASSERTION.

    The chunk sizes. The gateway answers HTTP/1.1 with chunked encoding and
    aiohttp emits one chunk per StreamResponse.write(), so three chunks of
    sixteen bytes is three writes; a gateway that collected upstream.content
    and wrote it once would send one chunk of forty-eight. Replace _send's
    `async for chunk in upstream.content.iter_any()` with a single
    `await upstream.content.read()` and this line is what goes red.

    And what the container had done by then. It holds four tenths of a second
    between frames and sets `finished` after the third, so the only way that
    Event is set when frame one arrives is that the gateway waited for all of
    it.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        wired.container.frame_delay = 0.4
        reader, writer = await asyncio.open_connection("127.0.0.1", wired.port)
        body = b'{"m": "x"}'
        writer.write((
            f"POST /api/chat/stream HTTP/1.1\r\nHost: {host}\r\n"
            f"Connection: close\r\nContent-Type: application/json\r\n"
            f"Origin: https://{host}\r\nCookie: {cookie}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        first = await _read_one_chunk(reader)
        finished_when_frame_one_arrived = wired.container.finished.is_set()
        rest = []
        while (chunk := await _read_one_chunk(reader)) is not None:
            rest.append(chunk)
        writer.close()
        await wired.stop()
        return head, first, rest, finished_when_frame_one_arrived

    head, first, rest, finished = asyncio.run(run())
    assert b"chunked" in head.lower(), "not a chunked answer; the evidence below is gone"
    assert first == b'data: {"n": 0}\n\n'
    assert not finished, "the whole body was collected before anything was sent"
    # Three writes, not one: each frame crossed the gateway on its own.
    assert rest == [b'data: {"n": 1}\n\n', b'data: {"n": 2}\n\n']


def test_a_stopped_container_with_a_stale_address_is_started_rather_than_used(wired):
    """The mirror of the case above: the fleet says STOPPED and the address
    book still holds an address.

    admit() answers "start", and _reach's `start` arm is what turns that into
    a start. Narrow that arm to "evict_then_start" alone and the request falls
    through to the address book instead, forwarding a signed-in person to a
    container the gateway has already given up on -- dead on a good day, and
    after a resync on a shared bridge, somebody else's on a bad one. The start
    COUNT is the claim, because the stale address in this harness happens to
    be a container that still answers 200.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        # Only the status, so the address survives. This is the half of
        # Launcher._forget_running that a resync race can leave behind.
        wired.fleet.set_status(tenant_id, idle.STOPPED)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        starts = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, starts

    answer, starts = asyncio.run(run())
    assert answer[0] == 200
    assert starts == 2          # the pre-warm, and a fresh one for this request


def test_a_forwarder_refuses_a_session_that_would_decompress_a_container(tmp_path):
    """F4. `Content-Encoding` is on RESPONSE_HEADERS on the reasoning that
    nothing arrives compressed, and if something does, the header must travel
    with the body it describes. Both halves rest on the session not
    decompressing, and nothing used to say so: three call sites pass
    `auto_decompress=False` and a fourth is one forgotten keyword away.

    Driven rather than asserted on the constructor's source: a default session
    is built and the forwarder refuses it.
    """
    harness = Harness(tmp_path, FakeSpawner())

    async def run():
        default = aiohttp.ClientSession()
        explicit = aiohttp.ClientSession(auto_decompress=False)
        try:
            with pytest.raises(ValueError, match="auto_decompress=False"):
                ContainerForwarder(
                    launcher=harness.launcher, turns=harness.turns,
                    plans=harness.plans, proxy_socket=harness.config.proxy_socket,
                    session=default, now=harness.clock)
            # And the control: the one the process actually builds is accepted,
            # so this is a refusal of a WRONG session and not of every session.
            ContainerForwarder(
                launcher=harness.launcher, turns=harness.turns,
                plans=harness.plans, proxy_socket=harness.config.proxy_socket,
                session=explicit, now=harness.clock)
        finally:
            await default.close()
            await explicit.close()
            harness.store.close()

    asyncio.run(run())


def test_an_admission_action_forward_py_does_not_handle_is_refused(wired, monkeypatch):
    """F6. `_reach` names five actions and reaches "forward" through the
    fall-through, so a sixth invented in group B would be forwarded -- and for
    a tenant with no address, STARTED, outside whatever cap the new action was
    added to express. Default-deny instead.

    A seventh action is exactly what cannot be driven without inventing one,
    so one is invented: Fleet.admit is replaced with a function answering an
    action forward.py has never heard of.
    """
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        monkeypatch.setattr(wired.fleet, "admit",
                            lambda tenant_id, *, background: idle.Admission("defer"))
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        forwarded = len(wired.container.targets)
        starts = len([r for r in wired.spawner.requests if r["op"] == "start"])
        await wired.stop()
        return answer, forwarded, starts

    answer, forwarded, starts = asyncio.run(run())
    assert answer[0] == 503
    assert json.loads(answer[2])["error"] == "At capacity, try again shortly."
    assert forwarded == 0      # not forwarded ...
    assert starts == 1         # ... and not started: the pre-warm only


def test_a_policy_verdict_forward_py_does_not_handle_is_refused(wired, monkeypatch):
    """F6, the other half. `__call__` names refuse, block and rewrite and
    reaches PASS through the fall-through, so a fifth verdict would be passed
    to the container -- and a verdict is invented precisely when somebody
    wants a request handled differently from PASS."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        monkeypatch.setattr(policy, "decide",
                            lambda method, raw, payload=None:
                            policy.Outcome("quarantine", route="/api/data"))
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        forwarded = len(wired.container.targets)
        await wired.stop()
        return answer, forwarded

    answer, forwarded = asyncio.run(run())
    assert answer[0] == 503
    assert json.loads(answer[2])["error"] == "That request was refused."
    assert forwarded == 0
