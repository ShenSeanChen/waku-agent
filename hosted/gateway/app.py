"""The gateway's HTTP surface: one catch-all, two hosts, one forwarder.

ONE CATCH-ALL ROUTE, AND THE ROUTER IS NOT USED. aiohttp matches on the
DECODED path, so a handler registered for "/api/chat/stream" also answers
"/api/ch%61t/stream" -- and hosted/core/policy.path_refusal, whose whole job
is to refuse a path carrying a percent sign before anything matches it, would
never run. Measured on aiohttp 3.14.1: with a single add_route("*",
"/{tail:.*}"), request.raw_path is byte-for-byte what was on the wire, for
"//api/chat/stream", "/api/ch%61t/stream", "/api/../x" and "/api%2fsettings"
alike. So there is one route, and this module does the matching.

THE FORWARDER IS A CONSTRUCTOR ARGUMENT, NOT A PORT. hosted/ports/__init__.py
is explicit that there are four seams and a fifth Protocol means somebody is
abstracting over a choice made once. `forward` is a plain callable with
exactly one implementation (hosted/gateway/forward.py, task E3), and it is
REQUIRED: there is no default, so a Gateway cannot be built without one, and
until E3 lands there is no hosted/gateway/__main__.py to build one from.

WHAT RUNS BEFORE THE HOST IS EVEN LOOKED AT. Path hygiene and the
Service-Worker refusal run on every request to either host. The spec puts
path hygiene inside route policy, which is a tenant-host concern; running it
first is a strengthening, and it is deliberate: the apex's routes are matched
by exact string, and "/%6cogin" matching nothing and answering 404 is a
worse answer than 400 for the same reason "/api/ch%61t/stream" is.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from hosted import log
from hosted.core import policy, quota
from hosted.gateway import answers, embed, guards, sessions
from hosted.gateway.config import GatewayConfig
from hosted.gateway.identity import NotSignedIn
from hosted.gateway.launch import (
    DISABLED_MESSAGE,
    InMaintenance,
    Launcher,
    NotActive,
    StartFailed,
)
from hosted.gateway.store import SESSION_TTL_SECONDS
from hosted.ports.control import ControlStore, Tenant
from hosted.ports.identity import Identity, IdentityVerifier
from hosted.ports.memory import MemoryKeys

_LOG = log.get(__name__)

Forward = Callable[[web.Request, Tenant], Awaitable[web.StreamResponse]]

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"

# The ONLY files /auth/static/ serves. An allowlist and not a directory walk:
# STATIC sits inside hosted/, which the services image copies whole, so a
# directory walk would serve whatever anybody ever drops in there.
#
# A key may contain a slash, and the VALUE is what reaches the filesystem, so
# a nested file costs an entry here and nothing else. No request string is
# ever joined to STATIC, which is what keeps `..` a 404 rather than a
# traversal that has to be defended against.
#
# design/ and fonts/ are copies of the Waku design system, byte-identical to
# waku/ops/static/. They are copies rather than a shared directory because
# the services image's build context refuses waku/ outright
# (services.Dockerfile.dockerignore) -- that refusal is the hosted/waku
# boundary, and it is worth more than the duplication.
# evals/deterministic/hosted/test_login_page.py fails if the two ever differ.
#
# supabase-js-2.117.1.js is GONE as of 2026-09-27: 217945 bytes, thrown away
# by this page's own Clear-Site-Data and refetched on every sign-in, to make
# one call login.js now makes with fetch. See that file.
STATIC_FILES: dict[str, tuple[str, str]] = {
    "login.css": ("login.css", "text/css"),
    "login.js": ("login.js", "text/javascript"),
    "waku-mark.svg": ("waku-mark.svg", "image/svg+xml"),
    "design/tokens.css": ("design/tokens.css", "text/css"),
    "design/type.css": ("design/type.css", "text/css"),
    "design/fonts.css": ("design/fonts.css", "text/css"),
    "design/controls.css": ("design/controls.css", "text/css"),
    "fonts/InstrumentSans-var.woff2": ("fonts/InstrumentSans-var.woff2", "font/woff2"),
    "fonts/JetBrainsMono-var.woff2": ("fonts/JetBrainsMono-var.woff2", "font/woff2"),
    "fonts/PlayfairDisplaySC-400.woff2": ("fonts/PlayfairDisplaySC-400.woff2", "font/woff2"),
}

# A woff2 is bytes. Sending `font/woff2; charset=utf-8` says it is text in a
# character set, which is false, and aiohttp will not let charset be set on
# some binary types at all.
TEXT_STATIC_TYPES = frozenset({"text/css", "text/javascript", "image/svg+xml"})

# "storage" and NOT "cache", since 2026-09-28.
#
# The spec (line 509) writes both. "storage" is the half that protects a
# person: it clears localStorage, sessionStorage and IndexedDB, so nothing
# Supabase or this page wrote survives for the next person at this browser.
# "cache" clears the HTTP cache for this origin -- which on the apex holds
# four stylesheets, three fonts, the Waku mark and a 2.5 KB script, all of
# them public, identical for every visitor, and none of them a credential.
#
# It cost 113591 bytes on EVERY sign-in, measured against the live
# deployment: ten requests, zero cache hits, 88 KB of it incompressible
# woff2, on every visit forever. That is the whole of what "cache" bought
# and the whole of what it cost.
#
# Cookies are untouched either way, and deliberately: the spec notes that
# this header "carries no cookies", because the session cookie is cleared by
# the logout that sends this, not by the browser.
# --- the public static files, and the ONE place they are allowed to cache ---
#
# WHY THESE AND NOTHING ELSE. answers.harden sends `Cache-Control: no-store`
# on every response, which is right for what the spec's acceptance 14 names:
# "every CONTAINER response carries Cache-Control: no-store". A tenant's
# dashboard data is theirs and must never sit in a shared cache.
#
# /auth/static/ is the opposite kind of thing. Every byte is public, identical
# for every visitor, and carries no credential: four stylesheets, three fonts,
# the Waku mark and the sign-in script. `no-store` on those bought nothing and
# cost 113591 bytes per sign-in.
#
# REVALIDATION AND NOT A LIFETIME, on purpose. `no-cache` means "you may keep
# it, ask me before using it". The browser sends the tag it holds and an
# unchanged file answers 304 with no body -- so a repeat visit costs round
# trips instead of bytes, and a deploy is picked up IMMEDIATELY. A max-age
# would be faster and would serve the previous release's stylesheet to
# somebody signing in after a deploy, which on the page that holds a
# credential is not a trade worth a few hundred milliseconds.
STATIC_CACHE_CONTROL = "no-cache"

# The file's bytes, so the tag changes exactly when the file does. Computed
# per request and not at import: the files are small, and a cache keyed on a
# process's startup would serve a stale tag for the life of a container that
# outlived a `docker cp`. Weak would be wrong -- these are byte-identical
# copies checked by an eval, and a strong tag is what they are.
def _etag(filename: str, body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _cacheable(response: web.Response, tag: str) -> web.Response:
    """harden(), then the two headers that let this one file be kept.

    The order matters and is the whole of this function: harden sets
    Cache-Control: no-store, so a caller that hardened afterwards would put it
    back and silently undo the caching. One place does both, in one order.
    """
    answers.harden(response)
    response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
    response.headers["ETag"] = tag
    return response


CLEAR_SITE_DATA = '"storage"'
SIGN_IN_REFUSED = "That sign-in did not work. Ask for a new link."
# Spec 004 B: the chat API every gateway calls, and the container route it
# becomes. The container's own path, so the forwarder's policy, turn quota and
# wake-up apply exactly as they do to a turn typed into the dashboard.
CHAT_API_PATH = "/v1/chat"
CHAT_STREAM_PATH = "/api/chat/stream"
CHAT_API_POST_ONLY = "Send the message as a POST."
# Spec 007 D: chat history over the same front door. Three routes, one
# container route: the dashboard's own /api/session, which already lists,
# reads, starts and switches conversations for the chat dock. The two reads go
# as GET with the action in the query; the two writes go as the POST body the
# caller sent, after the gateway has read it and checked the action.
CONVERSATIONS_API_PATH = "/v1/conversations"
SESSION_PATH = "/api/session"
CONVERSATION_ACTIONS = ("new", "switch")
# What a conversation id looks like: the dashboard's s-YYYYMMDD-HHMMSS, a
# channel's name (terminal, voice, whatsapp), telegram-<chat>. A leading
# letter or digit, so the dock's "__all__" timeline is not a conversation.
CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
CONVERSATIONS_METHODS = "Read conversations with GET; start or switch one with a POST."
CONVERSATION_ACTION_REFUSED = ('Send {"action": "new"} or '
                               '{"action": "switch", "id": "<conversation id>"}.')
NO_SUCH_CONVERSATION = "That is not a conversation id."
# Spec 004 A7: how often a tenant's Waku Memory key is checked for revocation.
MEMORY_KEY_CHECK_SECONDS = 3600

# aiohttp's own default is 1 MiB. Set explicitly, and equal to the proxy's
# body cap in the spec's proxy step 1, so hosted waku has ONE body limit
# rather than a library default nobody chose.
MAX_BODY_BYTES = 4 * 1024 * 1024


async def _harden_on_prepare(_request: web.Request,
                             response: web.StreamResponse) -> None:
    """The five security headers on every response this process writes,
    including the ones aiohttp writes on its own.

    THIS RUNS AFTER THE HANDLER, which is the whole reason harden() defaults
    Cache-Control rather than imposing it. A response that set its own -- the
    public static files, which may be kept and revalidated -- keeps it; every
    other response gets no-store here even if its handler never thought about
    it. Before that, this line silently undid the caching and the only symptom
    was a slow sign-in page."""
    answers.harden(response)


def _sentence_for(status: int) -> str:
    """aiohttp's own refusals, in the gateway's words. A branch and not a
    format string: every sentence the gateway sends is a named constant."""
    if status == 413:
        return answers.TOO_LARGE
    if status == 400:
        return answers.BAD_REQUEST
    return answers.NOT_FOUND if status == 404 else answers.REFUSED


def _conversation_action_refusal(body: bytes) -> web.Response | None:
    """None when a POST /v1/conversations body is one the container should
    get, the refusal when it is not. Only the two writes: the reads have their
    own GET routes, and an action the front door does not name is not passed
    through to find out what the container makes of it."""
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
    if not isinstance(payload, dict):
        return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
    action = payload.get("action")
    if action not in CONVERSATION_ACTIONS:
        return answers.json_error(400, CONVERSATION_ACTION_REFUSED)
    if action == "switch" and not (isinstance(payload.get("id"), str)
                                   and CONVERSATION_ID.fullmatch(payload["id"])):
        return answers.json_error(400, CONVERSATION_ACTION_REFUSED)
    return None


class Gateway:
    def __init__(self, *, config: GatewayConfig, store: ControlStore,
                 launcher: Launcher, verifier: IdentityVerifier,
                 forward: Forward,
                 turns: quota.TurnWindow | None = None,
                 plans: dict[str, quota.Plan] | None = None,
                 memory_keys: MemoryKeys | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self._config = config
        self._memory_keys = memory_keys
        # tenant id -> when its Waku Memory key was last minted or checked (A7).
        self._key_checked: dict[str, float] = {}
        self._store = store
        self._launcher = launcher
        self._verifier = verifier
        self._forward = forward
        self._now = now
        self._sessions = sessions.SessionCache(now)
        self._handoffs = sessions.HandoffCodes(now)
        # Spec 008 C: the embedded chat's one-time codes, a separate set so a
        # sign-in code cannot be redeemed as an embed code or the other way.
        self._embed_codes = sessions.HandoffCodes(now, ttl=embed.EMBED_CODE_SECONDS)
        # ONE TurnWindow per process, shared with E3's forwarder, which is
        # built BEFORE the Gateway because the Gateway requires a forwarder.
        # Two windows would mean the forwarder counts turns and `disable`
        # forgets a different object's -- a limit that never resets for
        # somebody. The defaults keep every test that does not care about turn
        # limits on the documented values.
        self._turns = turns if turns is not None else quota.TurnWindow(now)
        self._plans = plans if plans is not None else quota.DEFAULT_PLANS

    # --- what the other modules read ------------------------------------

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def store(self) -> ControlStore:
        return self._store

    @property
    def launcher(self) -> Launcher:
        return self._launcher

    @property
    def sessions(self) -> sessions.SessionCache:
        return self._sessions

    @property
    def handoffs(self) -> sessions.HandoffCodes:
        return self._handoffs

    @property
    def embed_codes(self) -> sessions.HandoffCodes:
        return self._embed_codes

    @property
    def turns(self) -> quota.TurnWindow:
        return self._turns

    @property
    def plans(self) -> dict[str, quota.Plan]:
        return self._plans

    def end_sessions(self, tenant_id: str) -> None:
        """Every session of the tenant, on both hosts, plus both in-memory
        caches. Logout and disable both go through here so neither can forget
        half of it.

        SO LOGOUT IS TENANT-WIDE, AND THAT IS A CHOICE. Signing out in one
        browser signs the same person out of every browser and every device,
        because a tenant IS a person here -- one Supabase identity, one
        container, one home directory. Fail-closed: "log me out" on a shared
        machine ends the session that is on the phone too. The alternative,
        deleting only the row the cookie names, would leave `disable` needing
        a second code path to end all of them, and that is the path that must
        not be forgotten.
        """
        self._store.delete_sessions(tenant_id)
        self._sessions.forget_tenant(tenant_id)
        self._handoffs.forget_tenant(tenant_id)
        self._embed_codes.forget_tenant(tenant_id)

    def build(self) -> web.Application:
        """One catch-all route, one explicit body cap, and the five security
        headers on responses aiohttp writes without asking this class.

        `harden` is a function, so it only reaches responses THIS code builds.
        Measured on an app of exactly this shape: a handler that raises answers
        a bare 500 and a body over `client_max_size` answers a bare 413, both
        carrying `Server: Python/3.11 aiohttp/3.14.1` and none of the five
        headers -- which would make acceptance 14's "every response, apex and
        tenant host, carries frame-ancestors 'none' and X-Frame-Options: DENY"
        false for exactly the responses no test drives. `on_response_prepare`
        fires for all three (verified: 500, 413 and 200), so it is where the
        five go.

        `_shape_errors` is the other half: without it aiohttp's own 413 is a
        plain-text body the dashboard has no branch for. With it, every refusal
        the browser can see is the JSON-or-HTML shape group A's page reads.

        `client_max_size` is set explicitly at 4 MiB. aiohttp's default is
        1 MiB, so leaving it out makes a library default into hosted waku's
        request-body limit by accident. 4 MiB is the one body cap the spec does
        name -- the proxy's, in its step 1 -- so the two numbers are the same
        number rather than two guesses.
        """
        app = web.Application(client_max_size=MAX_BODY_BYTES,
                              middlewares=[self._shape_errors])
        app.on_response_prepare.append(_harden_on_prepare)
        app.router.add_route("*", "/{tail:.*}", self.dispatch)
        return app

    @web.middleware
    async def _shape_errors(self, request: web.Request,
                            handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as exc:
            # aiohttp raises these itself: 413 from client_max_size, 400 from
            # a malformed body. Re-shaped so the page can read them.
            return answers.refusal(request, exc.status, _sentence_for(exc.status))

    # --- the front door -------------------------------------------------

    async def dispatch(self, request: web.Request) -> web.StreamResponse:
        bad_path = policy.path_refusal(request.raw_path)
        if bad_path:
            return answers.refusal(request, 400, bad_path)
        if guards.is_service_worker(request):
            return answers.refusal(request, 403, answers.SERVICE_WORKER)
        host = guards.normalise_host(request.headers.get("Host"))
        if not host:
            return answers.refusal(request, guards.MISDIRECTED, answers.WRONG_HOST)
        if host == self._config.apex_host:
            # Before the CSRF pair, on purpose: see _chat_api. The history
            # routes are bearer routes on the same terms.
            path = policy.split_path(request.raw_path)[0]
            if path == CHAT_API_PATH:
                return await self._chat_api(request)
            if path == embed.EMBED_API_PATH:
                return await self._embed_api(request)
            if (path == CONVERSATIONS_API_PATH
                    or path.startswith(CONVERSATIONS_API_PATH + "/")):
                return await self._conversations_api(request, path)
        if guards.csrf_refusal(request, host):
            return answers.refusal(request, guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
        if host == self._config.apex_host:
            return await self._apex(request)
        label = guards.tenant_label(host, self._config.apex_host)
        if label is None:
            return answers.refusal(request, guards.MISDIRECTED, answers.WRONG_HOST)
        return await self._tenant_host(request, label)

    # --- the apex -------------------------------------------------------

    async def _apex(self, request: web.Request) -> web.StreamResponse:
        path, _query = policy.split_path(request.raw_path)
        if request.method == "GET" and path == "/login":
            return self._login_page()
        if request.method == "GET" and path.startswith("/auth/static/"):
            return self._static(request, path[len("/auth/static/"):])
        if request.method == "POST" and path == "/auth/session":
            return await self._sign_in(request)
        if request.method == "POST" and path == "/auth/logout":
            return self._sign_out(request)
        if request.method == "GET" and path == "/":
            return answers.redirect("/login")
        return answers.refusal(request, 404, answers.NOT_FOUND)

    def _login_page(self) -> web.Response:
        """The sign-in page, with its own strict policy and Clear-Site-Data.

        The two Supabase values reach the script as data attributes on <body>,
        so script-src is 'self' with no 'unsafe-inline' anywhere: a page whose
        only job is to hold a credential for a moment should not be a page
        that can run a string.
        """
        template = (TEMPLATES / "login.html").read_text(encoding="utf-8")
        # ESCAPED ON THE WAY INTO THE HTML, raw on the way into the header.
        # Both values are operator-written in config/gateway.env, so this is
        # not attacker-controlled -- but they land in a <body> attribute, and
        # a quote in either would break out of the attribute rather than be
        # read as data. The CSP below is a HEADER and not HTML, so it takes
        # the value as written: html.escape there would turn an ampersand in a
        # URL into &amp; and silently change the policy.
        body = (template
                .replace("@@SUPABASE_URL@@", html.escape(self._config.supabase_url))
                .replace("@@SUPABASE_KEY@@",
                         html.escape(self._config.supabase_publishable_key)))
        policy_header = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            f"connect-src 'self' {self._config.supabase_url}; img-src 'self'; "
            # font-src, because the page now serves the design system's own
            # faces from this origin. Without it they are refused and the
            # page silently falls back to the system stack.
            "font-src 'self'; "
            "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        response = web.Response(text=body, content_type="text/html", charset="utf-8")
        response.headers["Content-Security-Policy"] = policy_header
        response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA
        return answers.harden(response)

    def _static(self, request: web.Request, name: str) -> web.Response:
        found = STATIC_FILES.get(name)
        if found is None:
            return answers.json_error(404, answers.NOT_FOUND)
        filename, content_type = found
        # charset explicitly on TEXT: without it a classic script or
        # stylesheet is decoded in the encoding the BROWSER picks, and this
        # page's copy is the one a mojibake bug report would be about. A font
        # is not text and does not get one.
        charset = "utf-8" if content_type in TEXT_STATIC_TYPES else None
        body = (STATIC / filename).read_bytes()
        tag = _etag(filename, body)
        # A conditional request costs one round trip and no body. The browser
        # asks with the tag it holds; an unchanged file answers 304 and sends
        # nothing.
        if request.headers.get("If-None-Match") == tag:
            return _cacheable(web.Response(status=304), tag)
        return _cacheable(web.Response(
            body=body, content_type=content_type, charset=charset), tag)

    async def _sign_in(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
        if not isinstance(payload, dict):
            return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
        token = payload.get("access_token")
        try:
            # to_thread, because JwksVerifier is synchronous by the port's
            # shape and a JWKS refetch is a blocking HTTP call.
            identity = await asyncio.to_thread(self._verifier.verify, token)
        except NotSignedIn as exc:
            _LOG.info("sign-in refused: %s", exc)
            return answers.json_error(401, SIGN_IN_REFUSED)
        try:
            tenant, _created = await self._launcher.ensure_tenant(
                sub=identity.sub, email=identity.email,
                timezone=str(payload.get("timezone", "")))
        except NotActive as exc:
            return answers.json_error(403, str(exc))
        await self._ensure_memory_key(tenant.id, token)
        value = sessions.new_secret()
        self._remember(sessions.apex_key(value), tenant.id)
        code = self._handoffs.issue(tenant.id)
        # Pre-warm: the container boots while the tenant's page loads. A
        # failure here is not a failed sign-in -- the first request on the
        # tenant host starts it again -- so it is logged and passed over.
        #
        # `prewarm`, NOT `start`. The running cap lives in Fleet.admit, which
        # every request path consults and which `start` does not; this call
        # site is the one that used to skip it, so N sign-ins left N
        # containers running whatever --max-running said. At the cap the
        # pre-warm now evicts the least recently active container instead of
        # over-committing the VM's memory. See Launcher.prewarm.
        try:
            await self._launcher.prewarm(tenant)
        except (NotActive, InMaintenance, StartFailed) as exc:
            _LOG.info("pre-warm of tenant=%s did not start it: %s", tenant.id, exc)
        enter = (f"https://{tenant.id}.{self._config.apex_host}"
                 f"/auth/enter?code={code}")
        response = answers.json_ok({"enter": enter})
        sessions.set_session_cookie(response, sessions.APEX_COOKIE, value,
                                    max_age=int(SESSION_TTL_SECONDS))
        return response

    async def _chat_api(self, request: web.Request) -> web.StreamResponse:
        """`POST /v1/chat`: one person's message to their own Waku Agent (spec 004 B).

        The front door every gateway calls -- the waku.one Waku Agent tab
        first, Slack later -- server to server, with the person's Supabase
        token as `Authorization: Bearer`. It finds or creates their tenant,
        mints their Waku Memory key if they have none, and hands the request
        to the forwarder as the container's own `/api/chat/stream`, so the
        turn quota, the wake-up and the header allowlist are the ones every
        dashboard turn already goes through. The forwarder never passes
        Authorization on: the container does not see the person's token.

        WHY THE ORIGIN CHECK DOES NOT APPLY. CSRF needs a credential the
        browser attaches on its own. This route ignores cookies and reads only
        the Authorization header, which a browser never attaches by itself, so
        a forged cross-site request arrives with no credential at all. A
        server-to-server caller sends no Origin, and requiring one would refuse
        every legitimate call. The JSON half stays.
        """
        if request.method != "POST":
            return answers.json_error(405, CHAT_API_POST_ONLY)
        if request.content_type != guards.JSON_CONTENT_TYPE:
            return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
        signed_in = await self._bearer(request)
        if isinstance(signed_in, web.Response):
            return signed_in
        tenant = await self._bearer_tenant(*signed_in)
        if isinstance(tenant, web.Response):
            return tenant
        return await self._forward(request.clone(rel_url=CHAT_STREAM_PATH), tenant)

    async def _embed_api(self, request: web.Request) -> web.Response:
        """`POST /v1/embed`: a one-time way into the person's own chat, for
        waku.one to put in an iframe (spec 008 C).

            -> {"url": "https://<tenant>.<apex>/auth/embed?code=<code>",
                "expires_at": <unix seconds>}

        Bearer-checked exactly like /v1/chat, through the same two helpers, so
        the tenant is found or created and its Waku Memory key minted here
        too. The Supabase token never goes into the URL: a URL lands in
        history, logs and Referer headers, and a sixty-second, single-use code
        bound to one tenant is worth nothing once the frame has used it.

        Nothing is started here. The frame's first request does that, through
        the forwarder, exactly as the first request after a sign-in does.
        """
        if request.method != "POST":
            return answers.json_error(405, embed.EMBED_POST_ONLY)
        if request.content_type != guards.JSON_CONTENT_TYPE:
            return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
        signed_in = await self._bearer(request)
        if isinstance(signed_in, web.Response):
            return signed_in
        tenant = await self._bearer_tenant(*signed_in)
        if isinstance(tenant, web.Response):
            return tenant
        code = self._embed_codes.issue(tenant.id)
        url = (f"https://{tenant.id}.{self._config.apex_host}"
               f"{embed.AUTH_PATH}?code={code}")
        return answers.json_ok({"url": url,
                                "expires_at": int(self._now() + embed.EMBED_CODE_SECONDS)})

    async def _conversations_api(self, request: web.Request,
                                 path: str) -> web.StreamResponse:
        """Chat history over the front door (spec 007 D).

            GET  /v1/conversations        -> GET  /api/session?action=list
            GET  /v1/conversations/<id>   -> GET  /api/session?action=history&id=<id>
            POST /v1/conversations        -> POST /api/session, the body as sent

        Everything /v1/chat does, in the same order and through the same two
        helpers: the bearer token, the tenant (found or created), the Waku
        Memory key, then the forwarder, which wakes the container exactly as a
        turn would. The container keeps the one copy of the history, in its
        own chat_log; the gateway only checks the person and forwards. The
        forwarder never passes Authorization on.

        THE POST BODY IS READ HERE, FROM THE CLONE. aiohttp refuses to clone a
        request whose body has been read, but a clone keeps what IT read: the
        forwarder's own read() of the same clone answers the same bytes. So
        the gateway checks the action on the object it then hands over, and
        the container receives exactly the bytes that were checked.
        """
        rest = path[len(CONVERSATIONS_API_PATH):]
        if rest:
            conversation = rest[1:]
            if not CONVERSATION_ID.fullmatch(conversation):
                return answers.json_error(404, NO_SUCH_CONVERSATION)
            if request.method != "GET":
                return answers.json_error(405, CONVERSATIONS_METHODS)
            target = f"{SESSION_PATH}?action=history&id={conversation}"
        elif request.method == "GET":
            target = f"{SESSION_PATH}?action=list"
        elif request.method == "POST":
            if request.content_type != guards.JSON_CONTENT_TYPE:
                return answers.json_error(guards.UNSUPPORTED_MEDIA, answers.NOT_JSON)
            target = SESSION_PATH
        else:
            return answers.json_error(405, CONVERSATIONS_METHODS)
        signed_in = await self._bearer(request)
        if isinstance(signed_in, web.Response):
            return signed_in
        forwarded = request.clone(rel_url=target)
        if request.method == "POST":
            refused = _conversation_action_refusal(await forwarded.read())
            if refused is not None:
                return refused
        tenant = await self._bearer_tenant(*signed_in)
        if isinstance(tenant, web.Response):
            return tenant
        return await self._forward(forwarded, tenant)

    async def _bearer(self, request: web.Request) -> tuple[Identity, str] | web.Response:
        """The person behind `Authorization: Bearer`, verified exactly as at
        sign-in, or the 401 every bearer route answers."""
        values = request.headers.getall("Authorization", ())
        scheme, _, token = (values[0] if len(values) == 1 else "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return answers.json_error(401, SIGN_IN_REFUSED)
        try:
            identity = await asyncio.to_thread(self._verifier.verify, token)
        except NotSignedIn as exc:
            _LOG.info("bearer route refused: %s", exc)
            return answers.json_error(401, SIGN_IN_REFUSED)
        return identity, token

    async def _bearer_tenant(self, identity: Identity, token: str) -> Tenant | web.Response:
        """Their tenant, found or created, with its Waku Memory key in place,
        or the 403 a disabled one gets."""
        try:
            tenant, _created = await self._launcher.ensure_tenant(
                sub=identity.sub, email=identity.email, timezone="")
        except NotActive as exc:
            return answers.json_error(403, str(exc))
        if tenant.status != "active":
            return answers.json_error(403, DISABLED_MESSAGE)
        await self._ensure_memory_key(tenant.id, token)
        return tenant

    async def _ensure_memory_key(self, tenant_id: str, access_token: str) -> None:
        """Mint the tenant's Waku Memory key the first time they sign in (spec 004).

        Before the pre-warm, so the container that boots during this sign-in
        already has it. Never fails the sign-in: a person who cannot reach Waku
        Memory today can still use their agent, and the next sign-in tries again.
        """
        if self._memory_keys is None:
            return
        if self._store.memory_key(tenant_id):
            await self._check_memory_key(tenant_id, access_token)
            return
        await self._mint_memory_key(tenant_id, access_token)

    async def _check_memory_key(self, tenant_id: str, access_token: str) -> None:
        """Replace the key if the person revoked it on waku.one (spec 004 A7).

        At most once an hour per tenant: a sign-in and every /v1/chat call pass
        through here, and Waku Memory need not be asked about each one. Only a
        definite "revoked" replaces the key; an unreachable Waku Memory is not
        evidence of anything. The running container still holds the old key,
        so it is stopped, and the next request starts it on the new one.
        """
        now = self._now()
        if now - self._key_checked.get(tenant_id, now) < MEMORY_KEY_CHECK_SECONDS:
            return
        self._key_checked[tenant_id] = now
        try:
            live = await self._memory_keys.is_live(
                access_token, self._store.memory_key_id(tenant_id))
        except Exception as exc:
            _LOG.info("tenant=%s: Waku Memory key check failed: %s", tenant_id, exc)
            return
        if live is not False:
            return
        _LOG.info("tenant=%s: Waku Memory key was revoked; minting a new one", tenant_id)
        if await self._mint_memory_key(tenant_id, access_token):
            try:
                await self._launcher.stop(tenant_id)
            except Exception as exc:
                _LOG.info("tenant=%s: stop after key replacement failed: %s", tenant_id, exc)

    async def _mint_memory_key(self, tenant_id: str, access_token: str) -> bool:
        self._key_checked[tenant_id] = self._now()
        try:
            minted = await self._memory_keys.mint(access_token)
        except Exception as exc:  # network, timeout, a malformed answer
            _LOG.info("tenant=%s: Waku Memory key not minted: %s", tenant_id, exc)
            return False
        if minted is None:
            _LOG.info("tenant=%s: Waku Memory issued no key", tenant_id)
            return False
        key, key_id = minted
        self._store.set_memory_key(tenant_id, key=key, key_id=key_id)
        _LOG.info("tenant=%s: Waku Memory key %s minted", tenant_id, key_id)
        return True

    def _sign_out(self, request: web.Request) -> web.Response:
        value = request.cookies.get(sessions.APEX_COOKIE, "")
        tenant_id = self._resolve(sessions.apex_key(value)) if value else None
        if tenant_id is not None:
            self.end_sessions(tenant_id)
            _LOG.info("signed out tenant=%s", tenant_id)
        return self._signed_out_response(sessions.APEX_COOKIE)

    def _signed_out_response(self, cookie_name: str) -> web.Response:
        """The same answer on both hosts: the cookie deleted with the
        attributes it was set with, and this origin's cache and storage
        cleared. Clear-Site-Data is per ORIGIN, so a logout that only ever
        rides the apex response leaves the tenant origin's storage and its
        now-dead cookie sitting in the browser."""
        response = answers.json_ok({"ok": True})
        response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA
        sessions.clear_session_cookie(response, cookie_name)
        return response

    # --- a tenant host --------------------------------------------------

    async def _tenant_host(self, request: web.Request,
                           label: str) -> web.StreamResponse:
        path, _query = policy.split_path(request.raw_path)
        if request.method == "GET" and path == "/auth/enter":
            return self._enter(request, label)
        if request.method == "GET" and path == embed.AUTH_PATH:
            return self._enter_embed(request, label)
        if request.method == "GET" and path.startswith("/auth/static/"):
            # The sign-in page's public stylesheets and faces, on this host
            # too: the gateway's own error page (answers.html_error) links
            # them, and CORP same-origin keeps a tenant host from loading
            # the apex's copies.
            return self._static(request, path[len("/auth/static/"):])
        value = request.cookies.get(sessions.TENANT_COOKIE, "")
        tenant_id = (self._resolve(sessions.tenant_key(label, value))
                     if value else None)
        if request.method == "POST" and path == "/auth/logout":
            # A logout route on THIS origin, so Clear-Site-Data reaches it.
            # The gateway answers it rather than forwarding it: the stock
            # dashboard has no session to end, and this path must work even
            # when the container is not running.
            if tenant_id is not None:
                self.end_sessions(tenant_id)
                _LOG.info("signed out tenant=%s on its own host", tenant_id)
            return self._signed_out_response(sessions.TENANT_COOKIE)
        embed_only = False
        if tenant_id != label:
            # Not signed in here, or signed in as somebody else. Either way the
            # embed cookie is the only other way in, and it opens the chat
            # alone (spec 008 E). The dashboard's own cookie, when it is
            # valid, wins: it can already reach everything this one can.
            tenant_id = self._embed_session(request, label)
            embed_only = tenant_id is not None
        if tenant_id != label:
            # The same answer for no session and somebody else's: a session
            # that names another tenant tells the holder nothing about whether
            # this tenant exists.
            return self._no_session(request, path)
        tenant = self._store.tenant_by_id(tenant_id)
        if tenant is None or tenant.status != "active":
            if tenant is not None:
                self.end_sessions(tenant.id)
            return self._no_session(request, path)
        if request.method == "POST" and path == embed.DASHBOARD_PATH:
            return self._dashboard_handoff(tenant)
        if embed_only:
            body = await request.read() if request.method == "POST" else b""
            refused = embed.route_refusal(request.method, path, body)
            if refused and guards.is_top_level_page(request):
                # A tab, not the frame: the partitioned embed cookie reached
                # it because both have top-level site waku.one (see
                # guards.is_top_level_page). It is no session here. The tab
                # gets what a signed-out visitor gets, the sign-in page.
                _LOG.info("embed cookie ignored on a top-level page for "
                          "tenant=%s: %s", tenant.id, path)
                return answers.redirect(f"https://{self._config.apex_host}/login")
            if refused:
                # From inside the frame (its fetches, or a navigation of the
                # frame itself): 403 and not 401, because a 401 tells
                # waku.one the session ended and it would re-mint and reload
                # the chat, which would ask again.
                _LOG.info("embed session for tenant=%s refused %s %s: %s",
                          tenant.id, request.method, path, refused)
                return answers.refusal(request, 403, embed.EMBED_REFUSED)
        if request.method == "GET" and path == embed.PAGE_PATH:
            return await self._embed_page(request, tenant)
        return await self._forward(request, tenant)

    def _embed_session(self, request: web.Request, label: str) -> str | None:
        value = request.cookies.get(embed.EMBED_COOKIE, "")
        return self._resolve(embed.embed_key(label, value)) if value else None

    async def _embed_page(self, request: web.Request,
                          tenant: Tenant) -> web.StreamResponse:
        """GET /embed/chat: the container's page, framable by the allowlist.

        The origins ride on the REQUEST for the forwarder, which sends them to
        the container as X-Waku-Embed-Origins and marks the container's answer
        framable before its headers go out -- a streamed response cannot have
        a header changed after prepare(). A refusal the forwarder writes
        itself (paused, maintenance, at capacity) comes back unprepared and
        is marked here, so the frame shows the sentence rather than the
        browser's own "refused to connect".
        """
        request[embed.REQUEST_ORIGINS_KEY] = self._config.embed_origins
        response = await self._forward(request, tenant)
        if not response.prepared:
            answers.allow_framing(response, self._config.embed_origins)
        return response

    def _enter_embed(self, request: web.Request, label: str) -> web.Response:
        """The embed's hand-off: a code from /v1/embed becomes an embed session.

        _enter's three checks, with the fetch-metadata rule for a frame (see
        guards.embed_refusal), and two differences that are the point of it:
        the cookie is the embed one (12 hours, SameSite=None, Partitioned) and
        the redirect goes to the chat page, not the dashboard, carrying the
        console's `theme` when it is exactly light or dark. Every answer,
        including a refusal, is framable by the allowlist: a refusal is the
        expired page, which tells waku.one to ask for a new code.

        The same warning as _enter: `?code=` must never reach an access log.
        """
        origins = self._config.embed_origins
        refused = guards.embed_refusal(request)
        if refused:
            _LOG.info("embed hand-off on %s refused: %s", label, refused)
            return embed.expired_page(origins)
        if not self._embed_codes.redeem(request.query.get("code", ""), label):
            return embed.expired_page(origins)
        tenant = self._store.tenant_by_id(label)
        if tenant is None or tenant.status != "active":
            return embed.expired_page(origins)
        value = sessions.new_secret()
        self._remember(embed.embed_key(label, value), label,
                       ttl=embed.EMBED_SESSION_SECONDS)
        response = answers.allow_framing(
            web.Response(status=302, headers={
                "Location": embed.page_location(request.query.get("theme", ""))}),
            origins)
        embed.set_embed_cookie(response, value)
        return answers.harden(response)

    def _dashboard_handoff(self, tenant: Tenant) -> web.Response:
        """POST /auth/dashboard: a sign-in hand-off code for the frame's
        "Dashboard" button (embed.DASHBOARD_PATH says why this is safe).

        The code is the apex's kind, from the same HandoffCodes, so it is
        redeemed by _enter and nothing else: 60 seconds, once, this tenant
        only, and only as a document navigation that is not cross-site.
        """
        code = self._handoffs.issue(tenant.id)
        return answers.json_ok({"url": f"/auth/enter?code={code}"})

    def _enter(self, request: web.Request, label: str) -> web.Response:
        """The hand-off: a code from the apex becomes this host's session.

        The one route on a tenant host served without a session, because it is
        how the session is made (spec, "Host check").

        THREE THINGS ARE CHECKED AND THE ANSWER IS THE SAME FOR ALL THREE.
        The navigation must not be cross-site, or an attacker mints a code on
        their own account and walks somebody else's browser into their
        container. The code must redeem. And the tenant it names must exist
        and be active -- `redeem` proves only that this gateway issued the
        code, and a tenant can be disabled between the sign-in and the
        hand-off, so without this the disable path rests entirely on
        `end_sessions` burning outstanding codes.

        FOR E3: do not turn on aiohttp's access log without filtering this
        route. Its default line would write `?code=<43 characters>` into the
        operator's log file, where it is a live hand-off for sixty seconds.
        """
        if guards.handoff_refusal(request):
            return answers.redirect(f"https://{self._config.apex_host}/login")
        if not self._handoffs.redeem(request.query.get("code", ""), label):
            return answers.redirect(f"https://{self._config.apex_host}/login")
        tenant = self._store.tenant_by_id(label)
        if tenant is None or tenant.status != "active":
            return answers.redirect(f"https://{self._config.apex_host}/login")
        value = sessions.new_secret()
        self._remember(sessions.tenant_key(label, value), label)
        response = answers.redirect("/")
        sessions.set_session_cookie(response, sessions.TENANT_COOKIE, value,
                                    max_age=int(SESSION_TTL_SECONDS))
        return response

    def _no_session(self, request: web.Request, path: str = "") -> web.Response:
        if request.method == "GET" and path == embed.PAGE_PATH:
            # Inside a frame the sign-in page is a blank box (it carries
            # frame-ancestors 'none'), so the chat page says it signed out and
            # tells waku.one, which asks /v1/embed for a new code.
            return embed.expired_page(self._config.embed_origins)
        if answers.wants_html(request):
            return answers.redirect(f"https://{self._config.apex_host}/login")
        return answers.json_error(401, answers.NO_SESSION)

    def _remember(self, key: str, tenant_id: str, *,
                  ttl: float = SESSION_TTL_SECONDS) -> None:
        """One writer for a session: the row and the cache, on the SCOPED key.

        The row is what survives this process; the cache is what keeps a
        dashboard's burst of requests off SQLite. A caller that wrote one and
        forgot the other would be a session that works for sixty seconds, or
        one that works only after a restart.
        """
        self._store.create_session(tenant_id=tenant_id, value=key,
                                   expires_at=self._now() + ttl)
        self._sessions.put(key, tenant_id)

    def _resolve(self, key: str) -> str | None:
        cached = self._sessions.get(key)
        if cached is not None:
            return cached
        tenant_id = self._store.session_tenant(key, self._now())
        if tenant_id is not None:
            self._sessions.put(key, tenant_id)
        return tenant_id
