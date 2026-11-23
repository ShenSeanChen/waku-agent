"""The agent's chat inside waku.one: the embed session and what it may reach.

Spec 008. waku.one frames ONE page of the tenant host, /embed/chat, which the
container serves as the dashboard's chat column alone. Getting there takes
three steps, and each has its own constant here:

    POST https://<apex>/v1/embed            (bearer, server to server)
        -> {"url": "https://<tenant>.<apex>/auth/embed?code=<code>",
            "expires_at": <unix seconds>}
    GET  https://<tenant>.<apex>/auth/embed?code=<code>[&theme=light|dark]
                                                          (inside the iframe)
        -> Set-Cookie: __Host-waku_embed=...; Secure; HttpOnly;
                       SameSite=None; Partitioned
        -> 302 /embed/chat[?theme=light|dark]
    GET  /embed/chat, its /static/ files, and the chat's own calls.

WHY A THIRD COOKIE, AND NOT THE TENANT HOST'S. __Host-waku_tenant is
SameSite=Lax and a first-party cookie; a frame on another site does not get
it, and loosening it would hand the whole dashboard to every cross-site
request. The embed cookie is SameSite=None so the frame can carry it, and
Partitioned so a browser that blocks third-party cookies still keeps it -- in
a jar keyed to the top-level site, so it only exists inside frames on the
sites that framed it. What it can reach is EMBED_ROUTES and nothing else.

WHY THE CODE LIVES IN MEMORY, like the sign-in hand-off codes in sessions.py.
It is valid for sixty seconds and dies on first use. A gateway restart losing
the outstanding ones costs waku.one one more POST /v1/embed; writing them to
control.db would buy nothing for that window and leave a table of credentials
to sweep. It is a SEPARATE HandoffCodes from the sign-in one, so a sign-in code
cannot be redeemed here and an embed code cannot open the full dashboard.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json

from aiohttp import web

from hosted.gateway import answers

EMBED_API_PATH = "/v1/embed"
AUTH_PATH = "/auth/embed"
PAGE_PATH = "/embed/chat"

EMBED_COOKIE = "__Host-waku_embed"
EMBED_SESSION_SECONDS = 12 * 3600
EMBED_CODE_SECONDS = 60.0
EMBED_SCOPE = "embed:"

# The console's theme, carried from /auth/embed?theme= to /embed/chat?theme=
# so the frame's first paint matches the page around it (waku-memory spec 040
# T). Exactly these two values; anything else, or none, redirects to the page
# with no query, and the chat falls back to its own stored choice.
EMBED_THEMES = ("light", "dark")


def page_location(theme: str) -> str:
    """Where the redeemed code goes: the chat page, with the theme when it is
    one of EMBED_THEMES. Nothing else from the request reaches the Location."""
    return f"{PAGE_PATH}?theme={theme}" if theme in EMBED_THEMES else PAGE_PATH


# The frame's "Dashboard" button (waku/ops/static/js/embed.js). A POST with
# the session the frame already has answers {"url": "/auth/enter?code=..."}:
# a SIGN-IN hand-off code, from the same HandoffCodes the apex's sign-in uses
# (60 seconds, single use, bound to this tenant), which the frame opens in a
# new tab. The embed cookie itself still opens nothing but the chat: the full
# session is only ever made by GET /auth/enter, a top-level document
# navigation with all of _enter's checks, in a tab that is first-party. The
# code reaches only the page that asked for it -- the route needs the CSRF
# pair (JSON and an Origin equal to this host), and the answer carries no
# CORS header -- so it is something the tenant's own page can do, and the
# tenant's own page is already the dashboard's origin.
DASHBOARD_PATH = "/auth/dashboard"

EMBED_POST_ONLY = "Ask for an embedded chat with a POST."
EMBED_REFUSED = "The embedded chat cannot open that."

# Spec 008 E: what a request carrying ONLY the embed cookie may reach, as
# (method, exact path). The chat column calls exactly these: the page, the
# turn, the conversation list and its actions (GET reads, POST new/switch),
# and the read-only state the header draws (?action=state, a GET on the same
# route). An allowlist, so a dashboard route added tomorrow is refused here
# until somebody decides the chat needs it.
EMBED_ROUTES = frozenset({
    ("GET", PAGE_PATH),
    ("POST", "/api/chat/stream"),
    ("GET", "/api/session"),
    ("POST", "/api/session"),
})
# Its static files, by prefix. The dashboard's own /static/ is public code and
# styles, identical in every container.
EMBED_STATIC_PREFIX = "/static/"
# The one write outside the chat's own routes: the model picker in its header,
# which switches the model with POST /api/providers. Only a body that names a
# model and nothing else passes -- no key, no endpoint, no enable/disable --
# and the forwarder's own /api/providers filter still runs after this.
PROVIDERS_PATH = "/api/providers"
MODEL_SWITCH_FIELDS = frozenset({"provider", "model", "small_model"})

# Header the gateway sets on the forwarded GET /embed/chat, never copied from
# the browser: the allowlist the page is served with, so its script posts only
# to an origin the gateway also lets frame it (spec 008 F). The key under which
# the gateway leaves the origins on the request for the forwarder to read.
ORIGINS_HEADER = "X-Waku-Embed-Origins"
REQUEST_ORIGINS_KEY = web.RequestKey("waku_embed_origins", tuple)


def embed_key(tenant_id: str, value: str) -> str:
    """The stored form of an embed session, scoped like sessions.tenant_key so
    the same secret presented as __Host-waku_tenant hashes to no row."""
    return f"{EMBED_SCOPE}{tenant_id}:{value}"


def set_embed_cookie(response: web.StreamResponse, value: str) -> None:
    """Secure; HttpOnly; SameSite=None; Partitioned, Path=/ and no Domain.

    WRITTEN BY HAND, NOT WITH set_cookie. aiohttp 3.14 passes `partitioned`
    to the standard library's Morsel, and Python 3.11's http.cookies does not
    know the attribute: measured, set_cookie(..., partitioned=True) raises
    CookieError("Invalid attribute 'partitioned'"). The value is
    secrets.token_urlsafe, whose alphabet needs no quoting, so the line is
    exactly what a browser expects.
    """
    response.headers.add(
        "Set-Cookie",
        f"{EMBED_COOKIE}={value}; Max-Age={EMBED_SESSION_SECONDS}; Path=/; "
        "Secure; HttpOnly; SameSite=None; Partitioned")


def route_refusal(method: str, path: str, body: bytes) -> str:
    """Empty when an embed-only session may make this request, the reason when
    it may not. `path` is the request path without its query, after the
    gateway's path hygiene has already run."""
    if (method, path) in EMBED_ROUTES:
        return ""
    if method == "GET" and path.startswith(EMBED_STATIC_PREFIX):
        return ""
    if method == "POST" and path == PROVIDERS_PATH:
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            return "not json"
        if (isinstance(payload, dict) and payload
                and set(payload) <= MODEL_SWITCH_FIELDS
                and all(isinstance(v, str) for v in payload.values())):
            return ""
        return "not a model switch"
    return "not a chat route"


# --- the page a frame gets when there is no session to show ---------------
#
# /embed/chat without a session, and /auth/embed with a code that does not
# redeem, answer this rather than a redirect to /login: the sign-in page
# carries frame-ancestors 'none', so inside the frame it would be a blank box
# and waku.one would never learn why. This page is framable by the same
# allowlist and tells the page around it, with the same rule the chat uses:
# post only to the referrer's origin, and only when that origin is on the list
# this page was served with.
_EXPIRED_SCRIPT = (
    "(function(){var b=document.body,"
    "a=(b.getAttribute('data-embed-origins')||'').split(' '),o='';"
    "try{o=new URL(document.referrer).origin}catch(e){}"
    "if(window.parent!==window&&o&&a.indexOf(o)>=0)"
    "{window.parent.postMessage({source:'waku-agent',type:'session-expired'},o)}})();")
_EXPIRED_SCRIPT_HASH = "sha256-" + base64.b64encode(
    hashlib.sha256(_EXPIRED_SCRIPT.encode("utf-8")).digest()).decode("ascii")
EXPIRED_MESSAGE = "This chat has signed out. Reopen it to sign in again."
EXPIRED_STATUS = 401


def expired_page(origins: tuple[str, ...]) -> web.Response:
    """A sentence, the one script above, and a policy that runs nothing else.

    The script is inline and allowed by its own hash, so the policy names no
    source at all for scripts. The origins reach it as a data attribute,
    escaped; they are already validated origins (config.embed_origins_from).
    """
    body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<title>waku</title></head>"
            f"<body data-embed-origins=\"{html.escape(' '.join(origins))}\">"
            f"<p>{html.escape(EXPIRED_MESSAGE)}</p>"
            f"<script>{_EXPIRED_SCRIPT}</script></body></html>")
    response = web.Response(status=EXPIRED_STATUS, text=body,
                            content_type="text/html", charset="utf-8")
    answers.allow_framing(
        response, origins,
        policy=f"default-src 'none'; script-src '{_EXPIRED_SCRIPT_HASH}'; "
               "base-uri 'none'; form-action 'none'")
    return answers.harden(response)
