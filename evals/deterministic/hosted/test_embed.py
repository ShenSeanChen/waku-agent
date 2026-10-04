"""DETERMINISTIC EVAL -- the agent's own chat, embedded in waku.one (spec 008).

waku.one's server asks `POST /v1/embed` for a one-time URL, puts it in an
iframe, and the frame trades the code for an embed session that reaches the
chat and nothing else. Every acceptance bullet of the spec that is the
gateway's is here:

  - /v1/embed refuses a bad token and mints for a good one
  - a code works once, expires after 60 s, and only for its tenant
  - the embed page and /auth/embed carry frame-ancestors with exactly the
    allowlist and no X-Frame-Options; every other response still denies
    framing
  - an embed-only session gets 403 on a non-chat route
  - the cookie carries Secure; HttpOnly; SameSite=None; Partitioned

The page's half (the chat column, the report card, postMessage) is in
evals/deterministic/test_embed_chat.py.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import re
import shutil
import subprocess

import pytest
from gatewaylib import (
    APEX,
    FakeSpawner,
    Harness,
    cookie_attributes,
    sign,
    signed_in_on_the_tenant_host,
)

from hosted.gateway import embed
from hosted.gateway.config import (
    DEFAULT_EMBED_ORIGINS,
    MAY_BE_ABSENT,
    REQUIRED_ENV_NAMES,
    config_from_env,
    embed_origins_from,
)

DEFAULT_ANCESTORS = "frame-ancestors https://www.waku.one https://waku.one https://dev.waku.one"
IN_A_FRAME = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "iframe"}


async def _mint(harness: Harness, token: str | None = None, *,
                content_type: str = "application/json", method: str = "POST"):
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await harness.send(method, embed.EMBED_API_PATH, host=APEX,
                              body=b"{}", headers=headers)


async def _minted(harness: Harness, **claims) -> tuple[str, str, int]:
    """(tenant host, code, expires_at) from a real POST /v1/embed."""
    token = sign(harness.private, now=harness.clock.t, **claims)
    status, _headers, body = await _mint(harness, token)
    assert status == 200, body
    answer = json.loads(body)
    url = answer["url"]
    host = url.split("//", 1)[1].split("/", 1)[0]
    return host, url.split("code=", 1)[1], answer["expires_at"]


async def _enter(harness: Harness, host: str, code: str, headers=None):
    return await harness.send("GET", f"{embed.AUTH_PATH}?code={code}", host=host,
                              headers=headers if headers is not None else IN_A_FRAME)


async def _embedded(harness: Harness, **claims) -> tuple[str, str]:
    """(tenant host, embed cookie header) after a real mint and redeem."""
    host, code, _ = await _minted(harness, **claims)
    status, headers, _ = await _enter(harness, host, code)
    assert status == 302
    return host, f"{embed.EMBED_COOKIE}={cookie_value(headers, embed.EMBED_COOKIE)}"


def cookie_value(headers: dict, name: str) -> str:
    """gatewaylib.cookie_value, without http.cookies: Python 3.11's parser does
    not know `Partitioned` and drops the whole line, so it reads the embed
    cookie as absent. The value is everything up to the first `;`."""
    for line in headers["__all__"].get("set-cookie", []):
        if line.startswith(name + "="):
            return line.split(";", 1)[0][len(name) + 1:]
    return ""


def _denies_framing(headers: dict) -> bool:
    return (headers.get("x-frame-options") == "DENY"
            and "frame-ancestors 'none'" in headers.get("content-security-policy", ""))


def _framable_by(headers: dict, ancestors: str = DEFAULT_ANCESTORS) -> bool:
    policies = headers["__all__"].get("content-security-policy", [])
    return ("x-frame-options" not in headers and len(policies) == 1
            and policies[0].split("; ")[-1] == ancestors
            and policies[0].count("frame-ancestors") == 1)


# --- POST /v1/embed ---------------------------------------------------------


def test_v1_embed_mints_a_one_time_url_for_a_good_token(harness):
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        status, headers, body = await _mint(harness, token)
        tenant = harness.store.tenant_by_sub("sub-mei")
        await harness.stop()
        return status, headers, json.loads(body), tenant

    status, headers, answer, tenant = asyncio.run(run())
    assert status == 200
    assert set(answer) == {"url", "expires_at"}
    assert re.fullmatch(
        rf"https://{tenant.id}\.{re.escape(APEX)}/auth/embed\?code=[A-Za-z0-9_-]{{43}}",
        answer["url"])
    assert answer["expires_at"] == int(harness.clock.t + 60)
    assert _denies_framing(headers), "the bearer answer itself is not a framable page"
    assert harness.forwarder.calls == [], "minting a code starts nothing"


def test_v1_embed_refuses_no_token_a_bad_token_and_an_expired_one(harness):
    async def run():
        await harness.start()
        none = await _mint(harness, None)
        bad = await _mint(harness, "not-a-jwt")
        expired = await _mint(harness, sign(harness.private, now=harness.clock.t - 7200))
        await harness.stop()
        return none[0], bad[0], expired[0]

    assert asyncio.run(run()) == (401, 401, 401)
    assert len(harness.gateway.embed_codes._codes) == 0


def test_v1_embed_is_post_and_json_only(harness):
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        get = await _mint(harness, token, method="GET")
        plain = await _mint(harness, token, content_type="text/plain")
        await harness.stop()
        return get[0], plain[0]

    assert asyncio.run(run()) == (405, 415)


def test_v1_embed_refuses_a_disabled_tenant(harness):
    async def run():
        await harness.start()
        await _minted(harness)
        harness.store.set_status(harness.store.tenant_by_sub("sub-mei").id, "disabled")
        status = (await _mint(harness, sign(harness.private, now=harness.clock.t)))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) == 403


# --- GET /auth/embed: once, for 60 seconds, for one tenant --------------------


def test_a_code_becomes_the_embed_cookie_and_a_redirect_to_the_chat(harness):
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        answer = await _enter(harness, host, code)
        await harness.stop()
        return answer

    status, headers, _ = asyncio.run(run())
    assert status == 302 and headers["location"] == "/embed/chat"
    assert len(cookie_value(headers, embed.EMBED_COOKIE)) == 43
    assert cookie_attributes(headers, embed.EMBED_COOKIE) == {
        "max-age=43200", "path=/", "secure", "httponly", "samesite=none", "partitioned"}
    assert cookie_value(headers, "__Host-waku_tenant") == "", \
        "the embed must not hand out the dashboard's own cookie"
    assert _framable_by(headers)


def test_a_code_works_once(harness):
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        first = await _enter(harness, host, code)
        second = await _enter(harness, host, code)
        await harness.stop()
        return first, second

    first, second = asyncio.run(run())
    assert first[0] == 302
    assert second[0] == 401 and cookie_value(second[1], embed.EMBED_COOKIE) == ""
    assert b"session-expired" in second[2]


def test_a_code_expires_after_sixty_seconds(harness):
    async def run():
        await harness.start()
        start = harness.clock.t
        host, early, expires_at = await _minted(harness)
        _, late, _ = await _minted(harness)
        harness.clock.t = start + 59.9
        in_time = await _enter(harness, host, early)
        harness.clock.t = start + 60
        too_late = await _enter(harness, host, late)
        await harness.stop()
        return expires_at - start, in_time[0], too_late[0]

    assert asyncio.run(run()) == (60, 302, 401)


def test_a_code_works_only_on_its_own_tenants_host(harness):
    """And trying it elsewhere burns it: single use means single use."""
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness, sub="sub-mei")
        other, _, _ = await _minted(harness, sub="sub-kai", email="kai@example.com")
        wrong = await _enter(harness, other, code)
        right_after = await _enter(harness, host, code)
        await harness.stop()
        return wrong[0], right_after[0]

    assert asyncio.run(run()) == (401, 401)


def test_a_sign_in_code_is_not_an_embed_code(harness):
    from gatewaylib import sign_in

    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        status = (await _enter(harness, f"{tenant_id}.{APEX}", code))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) == 401


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"},
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document"},
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "iframe"},
    {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "image"},
    [("Sec-Fetch-Dest", "iframe"), ("Sec-Fetch-Dest", "document")],
], ids=["typed", "top-level-link", "own-pages", "subresource", "duplicate"])
def test_the_redeem_must_be_an_iframe_navigation(harness, headers):
    """Top-level, a code minted on the attacker's own account would sign the
    victim's browser into the attacker's chat on the tenant host's own site."""
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        refused = await _enter(harness, host, code, headers=headers)
        await harness.stop()
        return refused

    status, answer, _ = asyncio.run(run())
    assert status == 401 and cookie_value(answer, embed.EMBED_COOKIE) == ""


def test_a_frame_on_another_site_may_redeem(harness):
    """A developer's http://localhost:3000 frames from another site."""
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        status = (await _enter(harness, host, code,
                               headers={"Sec-Fetch-Site": "cross-site",
                                        "Sec-Fetch-Dest": "iframe"}))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) == 302


# --- framing: the allowlist on two paths, DENY everywhere else ----------------


def test_framing_is_allowed_on_the_embed_page_and_auth_embed_and_nowhere_else(harness):
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        entered = await _enter(harness, host, code)
        cookie = f"{embed.EMBED_COOKIE}={cookie_value(entered[1], embed.EMBED_COOKIE)}"
        refused_code = await _enter(harness, host, "not-a-code")
        page = await harness.send("GET", "/embed/chat", host=host, cookie=cookie)
        signed_out_page = await harness.send("GET", "/embed/chat", host=host)
        chat = await harness.json_post("/api/chat/stream", {"message": "hi"},
                                       host=host, cookie=cookie)
        static = await harness.send("GET", "/static/style.css", host=host, cookie=cookie)
        refused = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        dashboard = await harness.send("GET", "/", host=host, cookie=cookie)
        login = await harness.send("GET", "/login", host=APEX)
        await harness.stop()
        return {"enter": entered, "refused code": refused_code, "page": page,
                "signed-out page": signed_out_page, "chat": chat, "static": static,
                "403": refused, "dashboard": dashboard, "login": login}

    answers = asyncio.run(run())
    for name in ("enter", "refused code", "page", "signed-out page"):
        assert _framable_by(answers[name][1]), name
    for name in ("chat", "static", "403", "dashboard", "login"):
        assert _denies_framing(answers[name][1]), name


def test_the_allowlist_comes_from_the_config(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())
    harness.config = dataclasses.replace(
        harness.config, embed_origins=("https://dev.waku.one", "http://localhost:3000"))
    harness.gateway = harness._build_gateway()

    async def run():
        await harness.start()
        host, cookie = await _embedded(harness)
        page = await harness.send("GET", "/embed/chat", host=host, cookie=cookie)
        await harness.stop()
        return page

    _, headers, _ = asyncio.run(run())
    assert _framable_by(headers, "frame-ancestors https://dev.waku.one http://localhost:3000")


def test_the_signed_out_page_tells_the_parent_with_a_hashed_script_only(harness):
    async def run():
        await harness.start()
        answer = await harness.send("GET", "/embed/chat", host=f"aaaaaaaaaaaa.{APEX}")
        await harness.stop()
        return answer

    status, headers, body = asyncio.run(run())
    assert status == 401
    policy = headers["content-security-policy"]
    script = re.search(rb"<script>(.*?)</script>", body, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in policy
    assert "default-src 'none'" in policy and "unsafe-inline" not in policy
    assert b"'*'" not in script and b'"*"' not in script
    assert b'data-embed-origins="https://www.waku.one https://waku.one https://dev.waku.one"' in body


# --- what an embed-only session reaches -----------------------------------------


@pytest.mark.parametrize(("method", "target", "payload"), [
    ("GET", "/embed/chat", None),
    ("GET", "/embed/chat?theme=dark", None),
    ("GET", "/static/js/render.js", None),
    ("POST", "/api/chat/stream", {"message": "hi"}),
    ("GET", "/api/session?action=state", None),
    ("POST", "/api/session", {"action": "new"}),
    ("POST", "/api/providers", {"provider": "anthropic", "model": "claude-sonnet-5"}),
])
def test_an_embed_session_reaches_the_chat(harness, method, target, payload):
    async def run():
        await harness.start()
        host, cookie = await _embedded(harness)
        if payload is None:
            answer = await harness.send(method, target, host=host, cookie=cookie)
        else:
            answer = await harness.json_post(target, payload, host=host, cookie=cookie)
        await harness.stop()
        return answer

    status, _, body = asyncio.run(run())
    assert status == 200 and body == b"forwarded"
    assert [path for _, path in harness.forwarder.calls] == [target]


@pytest.mark.parametrize(("method", "target", "payload"), [
    ("GET", "/", None),
    ("GET", "/api/data", None),
    ("GET", "/api/events?cursor=1", None),
    ("GET", "/api/models", None),
    ("GET", "/embed/chatty", None),
    ("POST", "/api/settings", {"graph_workflows": True}),
    ("POST", "/api/memory", {"action": "delete", "id": 1}),
    ("POST", "/api/query", {"sql": "SELECT 1"}),
    ("POST", "/api/chat", {"message": "hi"}),
    ("POST", "/api/graph/stream", {"workflow": "gather"}),
    ("POST", "/api/connections", {"key": "tavily", "values": {}}),
    ("POST", "/api/providers", {"provider": "anthropic", "key": "sk-x"}),
    ("POST", "/api/providers", {"provider": "anthropic", "disabled": True}),
    ("POST", "/api/providers", {}),
])
def test_an_embed_session_gets_403_everywhere_else(harness, method, target, payload):
    async def run():
        await harness.start()
        host, cookie = await _embedded(harness)
        if payload is None:
            answer = await harness.send(method, target, host=host, cookie=cookie)
        else:
            answer = await harness.json_post(target, payload, host=host, cookie=cookie)
        await harness.stop()
        return answer

    status, _, body = asyncio.run(run())
    assert status == 403
    assert embed.EMBED_REFUSED.encode() in body
    assert harness.forwarder.calls == []


def test_the_dashboards_own_cookie_is_unaffected(harness):
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        data = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        page = await harness.send("GET", "/embed/chat", host=host, cookie=cookie)
        await harness.stop()
        return data, page

    data, page = asyncio.run(run())
    assert data[0] == 200 and _denies_framing(data[1])
    assert page[0] == 200 and _framable_by(page[1])


def test_an_embed_cookie_is_only_an_embed_cookie(harness):
    """Scoped like the other two: replayed as the tenant cookie, or on another
    tenant's host, it is no session at all."""
    async def run():
        await harness.start()
        host, cookie = await _embedded(harness, sub="sub-mei")
        other, _ = await _embedded(harness, sub="sub-kai", email="kai@example.com")
        value = cookie.split("=", 1)[1]
        as_tenant = await harness.send("GET", "/api/data", host=host,
                                       cookie=f"__Host-waku_tenant={value}")
        elsewhere = await harness.json_post("/api/chat/stream", {"message": "hi"},
                                            host=other, cookie=cookie)
        await harness.stop()
        return as_tenant[0], elsewhere[0]

    assert asyncio.run(run()) == (401, 401)
    assert harness.forwarder.calls == []


def test_the_embed_session_lasts_twelve_hours(harness):
    async def run():
        await harness.start()
        host, cookie = await _embedded(harness)
        harness.clock.t += 12 * 3600 - 60
        harness.gateway.sessions.forget_tenant(host.split(".")[0])
        before = await harness.send("GET", "/api/session?action=state", host=host, cookie=cookie)
        harness.clock.t += 120
        harness.gateway.sessions.forget_tenant(host.split(".")[0])
        after = await harness.send("GET", "/api/session?action=state", host=host, cookie=cookie)
        await harness.stop()
        return before[0], after[0]

    assert asyncio.run(run()) == (200, 401)


def test_signing_out_ends_the_embed_session_and_its_codes(harness):
    async def run():
        await harness.start()
        host, cookie = await _embedded(harness)
        _, code, _ = await _minted(harness)
        tenant_id = host.split(".")[0]
        harness.gateway.end_sessions(tenant_id)
        chat = await harness.json_post("/api/chat/stream", {"message": "hi"},
                                       host=host, cookie=cookie)
        late = await _enter(harness, host, code)
        await harness.stop()
        return chat[0], late[0]

    assert asyncio.run(run()) == (401, 401)


# --- through the real forwarder ---------------------------------------------------


def test_the_container_is_told_the_allowlist_and_its_page_is_framable(wired):
    async def run():
        await wired.start()
        host, cookie = await _embedded(wired)
        page = await wired.send("GET", "/embed/chat", host=host, cookie=cookie)
        page_headers = {k.lower(): v for k, v in wired.container.headers[-1].items()}
        forged = await wired.send("GET", "/static/style.css", host=host, cookie=cookie,
                                  headers={"X-Waku-Embed-Origins": "https://evil.example"})
        forged_headers = {k.lower(): v for k, v in wired.container.headers[-1].items()}
        await wired.stop()
        return page, page_headers, forged, forged_headers

    page, page_headers, forged, forged_headers = asyncio.run(run())
    assert page[0] == 200 and _framable_by(page[1])
    assert page_headers["x-waku-embed-origins"] == " ".join(DEFAULT_EMBED_ORIGINS)
    assert _denies_framing(forged[1])
    assert "x-waku-embed-origins" not in forged_headers, \
        "a browser's own copy of the header must never reach the container"


# --- config: optional, validated -------------------------------------------------


def _example_env() -> dict[str, str]:
    from gatewaylib import ISSUER, JWKS_URL, SUPABASE_URL

    return {"WAKU_APEX_HOST": APEX, "WAKU_GATEWAY_BIND": "127.0.0.1",
            "WAKU_GATEWAY_PORT": "8787", "WAKU_CONTROL_DB": "/x/control.db",
            "WAKU_SPAWNER_SOCKET": "/x/s.sock", "WAKU_GATEWAY_SOCKET": "/x/g.sock",
            "WAKU_PROXY_SOCKET": "/x/p.sock", "WAKU_ADMIN_SOCKET": "/x/a.sock",
            "WAKU_MAX_RUNNING": "4", "WAKU_SUPABASE_URL": SUPABASE_URL,
            "WAKU_SUPABASE_ISSUER": ISSUER, "WAKU_SUPABASE_JWKS_URL": JWKS_URL,
            "WAKU_SUPABASE_AUDIENCE": "https://api.waku.one/mcp",
            "WAKU_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_x",
            "WAKU_FREE_TURNS_PER_HOUR": "30", "WAKU_BYOK_TURNS_PER_HOUR": "120"}


def test_a_gateway_env_from_before_spec_008_still_starts():
    """upgrade.sh never rewrites config/: an existing gateway.env has no
    WAKU_EMBED_ORIGINS line, and the gateway must start on the default."""
    env = _example_env()
    assert set(env) == set(REQUIRED_ENV_NAMES) - {"WAKU_EMBED_ORIGINS"}
    assert MAY_BE_ABSENT == {"WAKU_EMBED_ORIGINS"}
    assert config_from_env(env).embed_origins == DEFAULT_EMBED_ORIGINS
    assert config_from_env(env | {"WAKU_EMBED_ORIGINS": ""}).embed_origins == DEFAULT_EMBED_ORIGINS
    with pytest.raises(ValueError, match="WAKU_SUPABASE_URL"):
        config_from_env({k: v for k, v in env.items() if k != "WAKU_SUPABASE_URL"})


def test_the_allowlist_holds_origins_and_nothing_else():
    assert embed_origins_from("https://www.waku.one  http://localhost:3000") == (
        "https://www.waku.one", "http://localhost:3000")
    for bad in ("*", "https://*.waku.one", "https://waku.one/app", "'self'",
                "https://waku.one;script-src", "http://evil.example", "HTTPS://WAKU.ONE",
                "javascript:alert(1)"):
        with pytest.raises(ValueError, match="WAKU_EMBED_ORIGINS"):
            embed_origins_from(f"https://www.waku.one {bad}")


# --- the signed-out page's script, run --------------------------------------------


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize(("referrer", "framed", "posted"), [
    ("https://dev.waku.one/agent", True, [("session-expired", "https://dev.waku.one")]),
    ("https://evil.example/", True, []),
    ("", True, []),
    ("https://dev.waku.one/agent", False, []),
])
def test_the_signed_out_page_posts_only_to_an_allowlisted_parent(referrer, framed, posted):
    page = embed.expired_page(DEFAULT_EMBED_ORIGINS).text
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    origins = re.search(r'data-embed-origins="([^"]*)"', page).group(1)
    harness = f"""
const posts = [];
const parent = {{postMessage: (m, o) => posts.push([m.type, o, m.source])}};
const window = {{}};
window.parent = {json.dumps(framed)} ? parent : window;
const document = {{referrer: {json.dumps(referrer)},
                  body: {{getAttribute: () => {json.dumps(origins)}}}}};
{script}
console.log(JSON.stringify(posts));
"""
    out = subprocess.run([shutil.which("node"), "-e", harness],  # noqa: S603
                         capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout)
    assert [(t, o) for t, o, _ in got] == posted
    assert all(source == "waku-agent" for _, _, source in got)


# --- spec 040 T (waku-memory): the console's theme rides the redirect ---------------


@pytest.mark.parametrize(("query", "location"), [
    ("&theme=dark", "/embed/chat?theme=dark"),
    ("&theme=light", "/embed/chat?theme=light"),
    ("", "/embed/chat"),
    ("&theme=system", "/embed/chat"),
    ("&theme=DARK", "/embed/chat"),
    ("&theme=dark%0d%0aSet-Cookie:x=1", "/embed/chat"),
    ("&theme=https://evil.example", "/embed/chat"),
], ids=["dark", "light", "none", "system", "upper-case", "header-injection", "url"])
def test_the_redirect_carries_only_exactly_light_or_dark(harness, query, location):
    async def run():
        await harness.start()
        host, code, _ = await _minted(harness)
        answer = await harness.send("GET", f"{embed.AUTH_PATH}?code={code}{query}",
                                    host=host, headers=IN_A_FRAME)
        await harness.stop()
        return answer

    status, headers, _ = asyncio.run(run())
    assert status == 302 and headers["location"] == location
