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
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from hosted import log
from hosted.core import policy, quota
from hosted.gateway import answers, guards, sessions
from hosted.gateway.config import GatewayConfig
from hosted.gateway.identity import NotSignedIn
from hosted.gateway.launch import InMaintenance, Launcher, NotActive, StartFailed
from hosted.gateway.store import SESSION_TTL_SECONDS
from hosted.ports.control import ControlStore, Tenant
from hosted.ports.identity import IdentityVerifier

_LOG = log.get(__name__)

Forward = Callable[[web.Request, Tenant], Awaitable[web.StreamResponse]]

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"

# The ONLY files /auth/static/ serves. An allowlist and not a directory walk:
# STATIC sits inside hosted/, which the services image copies whole, so a
# directory walk would serve whatever anybody ever drops in there.
#
# supabase-js-2.117.1.js is the un-minified UMD build of @supabase/supabase-js
# 2.117.1, taken from cdn.jsdelivr.net on 2026-09-24. 217945 bytes, sha256
# dff1e545f4f35bd42895cd6f46431e56137dd13031e46a9759c446447c11a567.
STATIC_FILES: dict[str, tuple[str, str]] = {
    "supabase.js": ("supabase-js-2.117.1.js", "text/javascript"),
    "login.css": ("login.css", "text/css"),
    "login.js": ("login.js", "text/javascript"),
}

CLEAR_SITE_DATA = '"cache", "storage"'
SIGN_IN_REFUSED = "That sign-in did not work. Ask for a new link."

# aiohttp's own default is 1 MiB. Set explicitly, and equal to the proxy's
# body cap in the spec's proxy step 1, so hosted waku has ONE body limit
# rather than a library default nobody chose.
MAX_BODY_BYTES = 4 * 1024 * 1024


async def _harden_on_prepare(_request: web.Request,
                             response: web.StreamResponse) -> None:
    """The five security headers on every response this process writes,
    including the ones aiohttp writes on its own. harden() is idempotent, so
    a response that already went through it is unchanged."""
    answers.harden(response)


def _sentence_for(status: int) -> str:
    """aiohttp's own refusals, in the gateway's words. A branch and not a
    format string: every sentence the gateway sends is a named constant."""
    if status == 413:
        return answers.TOO_LARGE
    if status == 400:
        return answers.BAD_REQUEST
    return answers.NOT_FOUND if status == 404 else answers.REFUSED


class Gateway:
    def __init__(self, *, config: GatewayConfig, store: ControlStore,
                 launcher: Launcher, verifier: IdentityVerifier,
                 forward: Forward,
                 turns: quota.TurnWindow | None = None,
                 plans: dict[str, quota.Plan] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self._config = config
        self._store = store
        self._launcher = launcher
        self._verifier = verifier
        self._forward = forward
        self._now = now
        self._sessions = sessions.SessionCache(now)
        self._handoffs = sessions.HandoffCodes(now)
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
    def turns(self) -> quota.TurnWindow:
        return self._turns

    @property
    def plans(self) -> dict[str, quota.Plan]:
        return self._plans

    def end_sessions(self, tenant_id: str) -> None:
        """Every session of the tenant, on both hosts, plus both in-memory
        caches. Logout and disable both go through here so neither can forget
        half of it."""
        self._store.delete_sessions(tenant_id)
        self._sessions.forget_tenant(tenant_id)
        self._handoffs.forget_tenant(tenant_id)

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
            return self._static(path[len("/auth/static/"):])
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
        body = (template
                .replace("@@SUPABASE_URL@@", self._config.supabase_url)
                .replace("@@SUPABASE_KEY@@", self._config.supabase_publishable_key))
        policy_header = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            f"connect-src 'self' {self._config.supabase_url}; img-src 'self'; "
            "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        response = web.Response(text=body, content_type="text/html", charset="utf-8")
        response.headers["Content-Security-Policy"] = policy_header
        response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA
        return answers.harden(response)

    def _static(self, name: str) -> web.Response:
        found = STATIC_FILES.get(name)
        if found is None:
            return answers.json_error(404, answers.NOT_FOUND)
        filename, content_type = found
        # charset explicitly: without it a classic script or stylesheet is
        # decoded in the encoding the BROWSER picks, and the vendored client
        # is 218 KB of UTF-8 nobody re-reads after a mojibake bug report.
        return answers.harden(web.Response(
            body=(STATIC / filename).read_bytes(), content_type=content_type,
            charset="utf-8"))

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
        value = sessions.new_secret()
        self._store.create_session(tenant_id=tenant.id, value=value,
                                   expires_at=self._now() + SESSION_TTL_SECONDS)
        self._sessions.put(value, tenant.id)
        code = self._handoffs.issue(tenant.id)
        # Pre-warm: the container boots while the tenant's page loads. A
        # failure here is not a failed sign-in -- the first request on the
        # tenant host starts it again -- so it is logged and passed over.
        try:
            await self._launcher.start(tenant)
        except (NotActive, InMaintenance, StartFailed) as exc:
            _LOG.info("pre-warm of tenant=%s did not start it: %s", tenant.id, exc)
        enter = (f"https://{tenant.id}.{self._config.apex_host}"
                 f"/auth/enter?code={code}")
        response = answers.json_ok({"enter": enter})
        sessions.set_session_cookie(response, sessions.APEX_COOKIE, value,
                                    max_age=int(SESSION_TTL_SECONDS))
        return response

    def _sign_out(self, request: web.Request) -> web.Response:
        value = request.cookies.get(sessions.APEX_COOKIE, "")
        tenant_id = self._resolve_session(value) if value else None
        if tenant_id is not None:
            self.end_sessions(tenant_id)
            _LOG.info("signed out tenant=%s", tenant_id)
        response = answers.json_ok({"ok": True})
        response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA
        sessions.clear_session_cookie(response, sessions.APEX_COOKIE)
        return response

    # --- a tenant host --------------------------------------------------

    async def _tenant_host(self, request: web.Request,
                           label: str) -> web.StreamResponse:
        path, _query = policy.split_path(request.raw_path)
        if request.method == "GET" and path == "/auth/enter":
            return self._enter(request, label)
        value = request.cookies.get(sessions.TENANT_COOKIE, "")
        tenant_id = self._resolve_session(value) if value else None
        if tenant_id != label:
            # Not signed in here, or signed in as somebody else. The same
            # answer for both: a session that names another tenant tells the
            # holder nothing about whether this tenant exists.
            return self._no_session(request)
        tenant = self._store.tenant_by_id(tenant_id)
        if tenant is None or tenant.status != "active":
            if tenant is not None:
                self.end_sessions(tenant.id)
            return self._no_session(request)
        return await self._forward(request, tenant)

    def _enter(self, request: web.Request, label: str) -> web.Response:
        """The hand-off: a code from the apex becomes this host's session.

        The one route on a tenant host served without a session, because it is
        how the session is made (spec, "Host check").
        """
        if not self._handoffs.redeem(request.query.get("code", ""), label):
            return answers.redirect(f"https://{self._config.apex_host}/login")
        value = sessions.new_secret()
        self._store.create_session(tenant_id=label, value=value,
                                   expires_at=self._now() + SESSION_TTL_SECONDS)
        self._sessions.put(value, label)
        response = answers.redirect("/")
        sessions.set_session_cookie(response, sessions.TENANT_COOKIE, value,
                                    max_age=int(SESSION_TTL_SECONDS))
        return response

    def _no_session(self, request: web.Request) -> web.Response:
        if answers.wants_html(request):
            return answers.redirect(f"https://{self._config.apex_host}/login")
        return answers.json_error(401, answers.NO_SESSION)

    def _resolve_session(self, value: str) -> str | None:
        cached = self._sessions.get(value)
        if cached is not None:
            return cached
        tenant_id = self._store.session_tenant(value, self._now())
        if tenant_id is not None:
            self._sessions.put(value, tenant_id)
        return tenant_id
