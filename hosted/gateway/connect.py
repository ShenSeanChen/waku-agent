"""Sign in to a connector on hosted Waku (spec 029, group A).

Ana signs in at agent.waku.one and follows "Connect Notion". She approves on
Notion's own page, Notion sends her browser back, and from then on the gateway
holds her Notion tokens. Three things happen here:

  register  the gateway registers itself with the vendor once (dynamic client
            registration), with the one address a browser may come back to,
            https://<apex>/connect/callback, and keeps the registration.
  sign in   GET /connect/start sends a signed-in person to the vendor, and
            GET /connect/callback exchanges the code the vendor sends back
            for that person's tokens. They are stored sealed (secretbox.py),
            bound to the tenant and the vendor.
  token     Connect.access_token hands the gateway's own MCP relay a token
            that works, renewing it with the refresh token when it is about
            to end. disconnect deletes the row.

THE GATEWAY IS THE OAUTH CLIENT, AND NO TOKEN LEAVES IT (decision 1). A
container never receives a vendor token: not in its environment, not on its
disk, and not in any answer. A container that is broken into holds nothing
that works at Notion.

THE STATE TIES A SIGN-IN TO A PERSON, as it does for "Add to Slack"
(slack.py). A state is random, bound to the signed-in tenant and to one
vendor, good for ten minutes and for one use, and the callback also requires
the browser's own session to name the same tenant. The code is exchanged with
a PKCE verifier that never leaves this process, so a code read off a redirect
is worth nothing to anyone else.

THE VENDOR'S ANSWERS ARE NOT TRUSTED WITH AN ADDRESS. Discovery returns the
addresses of the vendor's sign-in page, its registration and its token
endpoint. Each has to be on the origin of the vendor's own MCP address
(hosted/core/connectors.py), or the sign-in does not start: the gateway never
calls, and never sends a person to, an address a response chose.

NOTHING HERE LOGS OR RETURNS A TOKEN, A CODE, A CLIENT SECRET OR A VERIFIER.
A log line carries a tenant id, a vendor's name and a short error code.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import aiohttp
from aiohttp import web

from hosted import log
from hosted.core.connectors import VENDORS, Vendor
from hosted.gateway import answers, sessions
from hosted.gateway.secretbox import SecretBox, Unreadable
from hosted.ports.control import ControlStore

_LOG = log.get(__name__)

START_PATH = "/connect/start"
CALLBACK_PATH = "/connect/callback"
DISCONNECT_PATH = "/connect/disconnect"
SETUP_PATH = "/setup"
STATE_TTL_SECONDS = 600
# One request to the vendor. A person's browser is waiting behind it.
TIMEOUT_SECONDS = 10
# The largest answer read from a vendor's sign-in server. Each is a small
# JSON object, so anything longer is not one.
MAX_ANSWER_BYTES = 256 * 1024
# A token is renewed this long before the vendor says it ends, so a call that
# starts now does not arrive with a token that ended on the way.
RENEW_EARLY_SECONDS = 60
# What the gateway calls itself on the vendor's consent page.
CLIENT_NAME = "Waku"

NOT_CONFIGURED = "Connectors are not set up on this server"
NO_SUCH_CONNECTOR = "That is not a connector Waku can connect."
DISCONNECT_BODY = 'Send {"server": "<name>"} as a POST.'

# Where the callback sends the browser, as /setup?connect=<outcome>&server=
# <name>. A closed set: the setup page shows a sentence for each and ignores
# anything else.
CONNECTED, DENIED, EXPIRED, FAILED = "connected", "denied", "expired", "failed"

# The errors with which a vendor says a refresh token will never work again.
GRANT_ENDED = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})

_GZIP_MAGIC = b"\x1f\x8b"
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


def not_configured(problem: str) -> web.Response:
    """What every connector route answers when the gateway has no key to seal
    a token with. `problem` names the missing name and never a value."""
    return answers.json_error(503, f"{NOT_CONFIGURED}: {problem}.")


def client_binding(server: str) -> str:
    """What the sealed registration with one vendor is bound to."""
    return f"connect-client:{server}"


def token_binding(tenant_id: str, server: str) -> str:
    """What one person's sealed tokens are bound to, so a row's value does not
    open under another tenant or another vendor."""
    return f"connect:{tenant_id}:{server}"


class VendorUnreachable(RuntimeError):
    """The vendor did not answer with a JSON object. The message holds no
    token and no address."""


class NotConnected(RuntimeError):
    """This tenant has no sign-in to this vendor."""


class ReconnectNeeded(RuntimeError):
    """The stored tokens cannot be used any more. The person signs in again."""


def _error_code(answer: Mapping) -> str:
    """The vendor's own error code, or "" when the answer carries none that
    looks like one. Only this shape is ever logged."""
    code = answer.get("error")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urllib.parse.urlsplit(url)
        return (parts.scheme, parts.hostname or "", parts.port) if parts.hostname else None
    except ValueError:
        return None


def on_origin(url: object, vendor: Vendor) -> bool:
    """Whether `url` is an address on the vendor's own origin: the same
    scheme, host and port as its MCP address, with no credential in it."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.username or parts.password or parts.fragment:
        return False
    return _origin(url) is not None and _origin(url) == _origin(vendor.url)


async def read_capped(reply: aiohttp.ClientResponse, cap: int) -> bytes:
    """The whole body, or ValueError when it is longer than `cap` bytes."""
    raw = bytearray()
    async for chunk in reply.content.iter_any():
        raw.extend(chunk)
        if len(raw) > cap:
            raise ValueError("the answer is too long")
    return bytes(raw)


@dataclass(frozen=True)
class Client:
    """The gateway's registration with one vendor, opened. Never logged: the
    repr leaves the secret out."""
    client_id: str
    client_secret: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str

    def __repr__(self) -> str:
        return f"Client(client_id={self.client_id!r})"

    def sealed_by(self, box: SecretBox, server: str) -> bytes:
        return box.seal(json.dumps(self.__dict__), bound_to=client_binding(server))


def _client_from(text: str, vendor: Vendor) -> Client | None:
    """A stored registration, or None when it is not one this code wrote for
    this vendor's origin."""
    try:
        data = json.loads(text)
        client = Client(**data)
    except (ValueError, TypeError):
        return None
    if not all(isinstance(value, str) for value in data.values()) or not client.client_id:
        return None
    if not (on_origin(client.authorization_endpoint, vendor)
            and on_origin(client.token_endpoint, vendor)):
        return None
    return client


class VendorApi:
    """The four requests the gateway makes to a vendor's sign-in server."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)

    async def _ask(self, what: str, method: str, url: str, **kwargs) -> dict:
        try:
            # The gateway's one session does not decompress (__main__.py), so
            # this asks for gzip or nothing and opens gzip itself, as
            # slack.SlackApi does. Redirects are not followed: an address on
            # the vendor's origin must not lead to one that is not.
            headers = {**kwargs.pop("headers", {}), "Accept": "application/json",
                       "Accept-Encoding": "gzip, identity"}
            async with self._session.request(
                    method, url, timeout=self._timeout, headers=headers,
                    allow_redirects=False, **kwargs) as reply:
                raw = await read_capped(reply, MAX_ANSWER_BYTES)
            if raw[:2] == _GZIP_MAGIC:
                raw = gzip.decompress(raw)
            answer = json.loads(raw.decode("utf-8"))
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, EOFError) as exc:
            # The exception's own text is left out: aiohttp can quote a
            # request's address and headers, and the body holds a code.
            raise VendorUnreachable(f"{what} failed ({type(exc).__name__})") from None
        if not isinstance(answer, dict):
            raise VendorUnreachable(f"{what} answered something that is not an object")
        return answer

    async def discover(self, vendor: Vendor) -> dict:
        """The vendor's sign-in addresses, each checked to be on its origin:
        {issuer, authorization_endpoint, token_endpoint, registration_endpoint}.

        Two documents, as the MCP specification lays them out: the MCP server
        names its authorization server (RFC 9728), and that server names its
        endpoints (RFC 8414)."""
        scheme, host, port = _origin(vendor.url)
        base = f"{scheme}://{host}" + (f":{port}" if port else "")
        resource = await self._ask("discovery", "GET",
                                   f"{base}/.well-known/oauth-protected-resource")
        servers = resource.get("authorization_servers")
        issuer = servers[0] if isinstance(servers, list) and servers else base
        if not on_origin(issuer, vendor):
            raise VendorUnreachable("discovery named a sign-in server on another origin")
        found = await self._ask(
            "discovery", "GET",
            f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server")
        names = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
        if not all(on_origin(found.get(name), vendor) for name in names):
            raise VendorUnreachable("discovery named an address on another origin")
        methods = found.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            raise VendorUnreachable("the sign-in server does not offer PKCE with S256")
        return {"issuer": issuer.rstrip("/"), **{name: found[name] for name in names}}

    async def register(self, endpoints: dict, redirect_uri: str) -> dict:
        """Register this gateway as a client (RFC 7591), with one redirect
        address and nothing a person typed."""
        return await self._ask("registration", "POST", endpoints["registration_endpoint"], json={
            "client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post"})

    async def _token(self, what: str, client: Client, fields: dict) -> dict:
        data = {**fields, "client_id": client.client_id}
        if client.client_secret:
            data["client_secret"] = client.client_secret
        return await self._ask(what, "POST", client.token_endpoint, data=data)

    async def exchange(self, client: Client, *, code: str, verifier: str,
                       redirect_uri: str, resource: str) -> dict:
        return await self._token("the code exchange", client, {
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "redirect_uri": redirect_uri, "resource": resource})

    async def refresh(self, client: Client, *, refresh_token: str, resource: str) -> dict:
        return await self._token("the renewal", client, {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "resource": resource})


@dataclass(frozen=True)
class _SignIn:
    tenant_id: str
    server: str
    verifier: str
    expires: float


class SignIns:
    """The `state` of each sign-in under way, in memory, with its PKCE
    verifier.

    A state is popped before it is checked, so it is used once whichever way
    the check goes. A tenant has one state per vendor: a new start ends the
    earlier one, which also bounds this dict by tenants times vendors. A
    gateway restart forgets them all, and the person starts again.
    """

    def __init__(self, now: Callable[[], float], ttl: float = STATE_TTL_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        self._states: dict[str, _SignIn] = {}

    def issue(self, tenant_id: str, server: str) -> tuple[str, str]:
        """(state, the PKCE challenge for the vendor's sign-in page)."""
        now = self._now()
        for state in [s for s, found in self._states.items()
                      if (found.tenant_id, found.server) == (tenant_id, server)
                      or found.expires <= now]:
            self._states.pop(state, None)
        state, verifier = sessions.new_secret(), sessions.new_secret()
        self._states[state] = _SignIn(tenant_id, server, verifier, now + self._ttl)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return state, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def forget(self, tenant_id: str, server: str) -> None:
        """End this person's sign-in to one vendor, if one is under way."""
        for state in [s for s, found in self._states.items()
                      if (found.tenant_id, found.server) == (tenant_id, server)]:
            self._states.pop(state, None)

    def redeem(self, state: str) -> _SignIn | None:
        """The sign-in this state was issued for, once, or None."""
        found = self._states.pop(state, None)
        if found is None or self._now() >= found.expires:
            return None
        return found


def _tokens_from(answer: dict, now: float, keep_refresh: str = "") -> dict | None:
    """What is stored from a token answer, or None when the answer is not a
    bearer token. `keep_refresh` is the refresh token in use: a vendor that
    does not send a new one means the old one still stands."""
    access = answer.get("access_token")
    if (not isinstance(access, str) or not access
            or str(answer.get("token_type") or "").lower() != "bearer"):
        return None
    refresh = answer.get("refresh_token")
    ends = answer.get("expires_in")
    return {"access_token": access,
            "refresh_token": refresh if isinstance(refresh, str) and refresh else keep_refresh,
            # 0 when the vendor names no end: the token is used until the
            # vendor refuses it.
            "expires_at": now + ends if isinstance(ends, (int, float))
            and not isinstance(ends, bool) and ends > 0 else 0}


class Connect:
    """Every tenant's sign-ins to the vendors on the list."""

    def __init__(self, *, key: bytes, apex_host: str, store: ControlStore, api: VendorApi,
                 vendors: Mapping[str, Vendor] = VENDORS,
                 now: Callable[[], float] = time.time) -> None:
        self._box = SecretBox(key)
        self._apex = apex_host
        self._store = store
        self._api = api
        self._vendors = dict(vendors)
        self._now = now
        self._sign_ins = SignIns(now)
        # One registration at a time per vendor, and one renewal at a time
        # per person and vendor: two calls that both find a token ending must
        # not both spend the refresh token, which a vendor may accept once.
        self._registering: dict[str, asyncio.Lock] = {}
        self._renewing: dict[tuple[str, str], asyncio.Lock] = {}
        # Spec 029 B2: called with (tenant id, server) after a person signed
        # in to a vendor or disconnected one, so the container's list of
        # servers can follow. The gateway's __main__ sets it.
        self.on_change: Callable[[str, str], Awaitable[None]] | None = None

    async def _changed(self, tenant_id: str, server: str) -> None:
        """Tell whoever listens. A failure there is logged and never undoes
        the sign-in: the tokens are kept, and the container's next start
        reads the list again."""
        if self.on_change is None:
            return
        try:
            await self.on_change(tenant_id, server)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            _LOG.warning("tenant=%s: the container was not told that %s changed (%s)",
                         tenant_id, server, type(exc).__name__)

    @property
    def vendors(self) -> dict[str, Vendor]:
        return dict(self._vendors)

    @property
    def redirect_uri(self) -> str:
        """The one address a vendor may send a browser back to. It is part of
        the registration, and the vendor refuses any other."""
        return f"https://{self._apex}{CALLBACK_PATH}"

    def vendor(self, server: object) -> Vendor | None:
        return self._vendors.get(server) if isinstance(server, str) else None

    # --- the registration (A1) --------------------------------------------

    async def _client(self, vendor: Vendor) -> Client:
        """The gateway's registration with this vendor: the one kept in
        control.db, or a new one when there is none, when it was made for
        another redirect address, or when the key does not open it."""
        lock = self._registering.setdefault(vendor.name, asyncio.Lock())
        async with lock:
            kept = self._store.connect_client(vendor.name)
            if kept is not None and kept.redirect_uri == self.redirect_uri:
                try:
                    client = _client_from(
                        self._box.open(kept.client_sealed,
                                       bound_to=client_binding(vendor.name)), vendor)
                except Unreadable:
                    client = None
                if client is not None:
                    return client
            endpoints = await self._api.discover(vendor)
            answer = await self._api.register(endpoints, self.redirect_uri)
            client_id, secret = answer.get("client_id"), answer.get("client_secret")
            if not isinstance(client_id, str) or not client_id:
                raise VendorUnreachable(
                    f"registration refused ({_error_code(answer) or 'no client id'})")
            client = Client(
                client_id=client_id,
                client_secret=secret if isinstance(secret, str) else "",
                issuer=endpoints["issuer"],
                authorization_endpoint=endpoints["authorization_endpoint"],
                token_endpoint=endpoints["token_endpoint"])
            self._store.set_connect_client(
                vendor.name, redirect_uri=self.redirect_uri,
                client_sealed=client.sealed_by(self._box, vendor.name))
            _LOG.info("registered with %s for %s", vendor.name, self.redirect_uri)
            return client

    # --- the sign-in (A2) ---------------------------------------------------

    async def start(self, tenant_id: str, server: str) -> web.Response:
        """Send a signed-in person's browser to the vendor's sign-in page."""
        vendor = self.vendor(server)
        if vendor is None:
            return answers.json_error(404, NO_SUCH_CONNECTOR)
        try:
            client = await self._client(vendor)
        except VendorUnreachable as exc:
            _LOG.warning("tenant=%s: sign-in to %s could not start: %s",
                         tenant_id, vendor.name, exc)
            return self._back(FAILED, vendor.name)
        state, challenge = self._sign_ins.issue(tenant_id, vendor.name)
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": client.client_id,
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "scope": vendor.scope, "resource": vendor.url})
        joiner = "&" if "?" in client.authorization_endpoint else "?"
        return answers.redirect(f"{client.authorization_endpoint}{joiner}{query}")

    async def callback(self, request: web.Request,
                       session_tenant: str | None) -> web.Response:
        """The vendor sent the browser back. `session_tenant` is the active
        tenant the browser's own apex session names, or None.

        Like /auth/enter, this route's query holds a code, so it must never
        reach an access log.
        """
        found = self._sign_ins.redeem(request.query.get("state", ""))
        server = found.server if found is not None else ""
        if request.query.get("error"):
            # The person pressed Cancel. The state went with it, above.
            return self._back(DENIED, server)
        if found is None or found.tenant_id != session_tenant:
            _LOG.info("connector callback refused: no state for this browser's session")
            return self._back(EXPIRED, server)
        vendor = self._vendors[found.server]
        try:
            client = await self._client(vendor)
            # A vendor that says who it is in the callback is checked: an
            # answer from another sign-in server is not this sign-in's.
            issuer = request.query.get("iss")
            if issuer is not None and issuer.rstrip("/") != client.issuer:
                _LOG.info("tenant=%s: %s callback named another issuer",
                          found.tenant_id, vendor.name)
                return self._back(FAILED, vendor.name)
            answer = await self._api.exchange(
                client, code=request.query.get("code", ""), verifier=found.verifier,
                redirect_uri=self.redirect_uri, resource=vendor.url)
        except VendorUnreachable as exc:
            _LOG.warning("tenant=%s: sign-in to %s failed: %s",
                         found.tenant_id, vendor.name, exc)
            return self._back(FAILED, vendor.name)
        tokens = _tokens_from(answer, self._now())
        if tokens is None:
            _LOG.info("tenant=%s: sign-in to %s refused: %s", found.tenant_id, vendor.name,
                      _error_code(answer) or "not a bearer token")
            return self._back(FAILED, vendor.name)
        self._keep(found.tenant_id, vendor.name, tokens)
        _LOG.info("tenant=%s: connected %s", found.tenant_id, vendor.name)
        await self._changed(found.tenant_id, vendor.name)
        return self._back(CONNECTED, vendor.name)

    @staticmethod
    def _back(outcome: str, server: str) -> web.Response:
        query = urllib.parse.urlencode({"connect": outcome, "server": server})
        return answers.redirect(f"{SETUP_PATH}?{query}")

    # --- the tokens (A3) ----------------------------------------------------

    def _keep(self, tenant_id: str, server: str, tokens: dict) -> None:
        self._store.set_connection(
            tenant_id, server,
            token_sealed=self._box.seal(json.dumps(tokens),
                                        bound_to=token_binding(tenant_id, server)))

    def _stored(self, tenant_id: str, server: str) -> dict:
        """This person's tokens for one vendor, opened."""
        row = self._store.connection(tenant_id, server)
        if row is None:
            raise NotConnected(server)
        if row.needs_reconnect:
            raise ReconnectNeeded(server)
        try:
            tokens = json.loads(self._box.open(row.token_sealed,
                                               bound_to=token_binding(tenant_id, server)))
        except (Unreadable, ValueError):
            _LOG.error("tenant=%s: the stored tokens for %s do not open with "
                       "WAKU_SLACK_TOKEN_KEY", tenant_id, server)
            self._store.set_connection_reconnect(tenant_id, server, True)
            raise ReconnectNeeded(server) from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
            self._store.set_connection_reconnect(tenant_id, server, True)
            raise ReconnectNeeded(server)
        return tokens

    async def access_token(self, tenant_id: str, server: str, *, refused: str = "") -> str:
        """A token for this person's calls to one vendor, renewed first when
        it is about to end.

        `refused` is a token the vendor just answered 401 for: it is renewed
        whatever its stated end, unless another call already replaced it.

        Raises NotConnected, ReconnectNeeded or VendorUnreachable. The token
        is for the gateway's own request to the vendor and goes nowhere else.
        """
        vendor = self._vendors.get(server)
        if vendor is None:
            raise NotConnected(server)
        lock = self._renewing.setdefault((tenant_id, server), asyncio.Lock())
        async with lock:
            tokens = self._stored(tenant_id, server)
            ends = tokens.get("expires_at") or 0
            stale = tokens["access_token"] == refused or (
                ends and self._now() >= ends - RENEW_EARLY_SECONDS)
            if not stale:
                return tokens["access_token"]
            if not tokens.get("refresh_token"):
                if tokens["access_token"] != refused:
                    return tokens["access_token"]   # the vendor decides when it ends
                self._store.set_connection_reconnect(tenant_id, server, True)
                raise ReconnectNeeded(server)
            client = await self._client(vendor)
            answer = await self._api.refresh(
                client, refresh_token=tokens["refresh_token"], resource=vendor.url)
            renewed = _tokens_from(answer, self._now(), keep_refresh=tokens["refresh_token"])
            if renewed is None:
                code = _error_code(answer)
                _LOG.info("tenant=%s: %s did not renew the token: %s", tenant_id, server,
                          code or "not a bearer token")
                if code in GRANT_ENDED:
                    # The person removed Waku at the vendor, or the refresh
                    # token ended. The setup page now says to connect again.
                    self._store.set_connection_reconnect(tenant_id, server, True)
                    raise ReconnectNeeded(server)
                raise VendorUnreachable(f"the renewal was refused ({code or 'no code'})")
            self._keep(tenant_id, server, renewed)
            return renewed["access_token"]

    async def disconnect(self, tenant_id: str, server: str) -> bool:
        """Forget this person's sign-in to one vendor. True when there was
        one. The row is deleted, so no copy of either token is left here."""
        if server not in self._vendors:
            return False
        self._sign_ins.forget(tenant_id, server)
        removed = self._store.delete_connection(tenant_id, server)
        if removed:
            _LOG.info("tenant=%s: disconnected %s", tenant_id, server)
            await self._changed(tenant_id, server)
        return removed

    def states(self, tenant_id: str | None) -> dict[str, str]:
        """Each vendor's state for one person, for the setup page:
        `connected`, `reconnect` or `todo`."""
        rows = ({row.server: row for row in self._store.connections(tenant_id)}
                if tenant_id else {})
        return {name: ("todo" if name not in rows
                       else "reconnect" if rows[name].needs_reconnect else "connected")
                for name in self._vendors}
