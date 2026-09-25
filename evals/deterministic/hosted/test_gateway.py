"""One origin per tenant -- acceptance 21 -- and the headers on every answer.

E3 appends acceptance 14 and 22 to this file. What is here is the part that
does not need a forwarder: the hand-off, the two cookies, the host check, the
headers, and the refusals that run before the host is even resolved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from gatewaylib import (
    cookie_attributes,
    cookie_value,
    sign,
    sign_in,
    signed_in_on_the_tenant_host,
)

from hosted.gateway.config import REQUIRED_ENV_NAMES

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
