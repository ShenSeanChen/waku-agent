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

from hosted.core import policy
from hosted.gateway import answers, guards
from hosted.gateway.config import REQUIRED_ENV_NAMES
from hosted.gateway.store import SESSION_TTL_SECONDS

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

    expected = {"httponly", "secure", "samesite=lax", "path=/",
                f"max-age={int(SESSION_TTL_SECONDS)}"}
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


@pytest.mark.parametrize("host, expected", [
    ("agent.waku.one", 404),
    ("zzzzzzzzzzzz.agent.waku.one", 401),
    ("not-a-tenant.agent.waku.one", guards.MISDIRECTED),
    ("a.zzzzzzzzzzzz.agent.waku.one", guards.MISDIRECTED),
    ("agent.waku.one.evil.example", guards.MISDIRECTED),
    ("", guards.MISDIRECTED),
    ("localhost", guards.MISDIRECTED),
    ("AGENT.WAKU.ONE", 404),
    ("agent.waku.one:443", 404),
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
    assert json.loads(apex[2])["error"] == policy.BAD_PATH


@pytest.mark.parametrize("host", ["agent.waku.one", "zzzzzzzzzzzz.agent.waku.one"])
def test_a_service_worker_request_is_refused_on_both_hosts(harness, host):
    async def run():
        await harness.start()
        answer = await harness.send("GET", "/", host=host,
                                    headers={"Service-Worker": "script"})
        await harness.stop()
        return answer

    assert asyncio.run(run())[0] == 403


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
    assert json.loads(five[2])["error"] == answers.TOO_LARGE
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
