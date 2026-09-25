"""Cookies, the session cache and the single-use hand-off codes.

TWO COOKIES, BOTH __Host-. The prefix is a browser-enforced contract: a
cookie named __Host-* is only accepted with Secure, Path=/ and NO Domain, so
it is host-only and a subdomain can neither set it nor read it. That is what
keeps one tenant's compromised page from writing the apex's session cookie,
and it costs one naming convention.

THE CACHE HOLDS HITS, NEVER MISSES. A miss cached for sixty seconds would make
a fresh sign-in on the same value look signed-out, and caching "no" buys
nothing: the store lookup on a miss is a single indexed SELECT.

A HAND-OFF CODE IS POPPED BEFORE IT IS CHECKED. Single use means single use,
including a use that turns out to be for the wrong host. Burning a code on a
wrong-host attempt is deliberate: it makes guessing one-shot, and the code is
43 characters of secrets.token_urlsafe, delivered to one browser over TLS, so
there is nobody to deny service to but the holder.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from aiohttp import web

from hosted.core import idle
from hosted.core.tenant import token_hash

APEX_COOKIE = "__Host-waku_session"
TENANT_COOKIE = "__Host-waku_tenant"
HANDOFF_TTL_SECONDS = 60.0


def new_secret() -> str:
    """43 characters of the URL-safe base64 alphabet: a session value or a
    hand-off code. The same generator hosted/core/tenant.new_proxy_token
    uses, for the same reason."""
    return secrets.token_urlsafe(32)


def set_session_cookie(response: web.StreamResponse, name: str, value: str, *,
                       max_age: int) -> None:
    """Secure, HttpOnly, SameSite=Lax, Path=/, and NO domain -- the four the
    __Host- prefix requires and the browser checks."""
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True,
                        httponly=True, samesite="Lax")


def clear_session_cookie(response: web.StreamResponse, name: str) -> None:
    """The deletion carries the SAME attributes the cookie was set with.

    A __Host- cookie cannot be set OR cleared without Secure: a Set-Cookie
    whose name starts with __Host- and which lacks Secure, Path=/ or carries a
    Domain is rejected by the browser outright. Measured on aiohttp 3.14.1:

        del_cookie(name, path="/")
        -> __Host-waku_session=""; expires=Thu, 01 Jan 1970 ...; Max-Age=0; Path=/
        del_cookie(name, path="/", secure=True, httponly=True, samesite="Lax")
        -> __Host-waku_session=""; expires=Thu, 01 Jan 1970 ...; HttpOnly;
           Max-Age=0; Path=/; SameSite=Lax; Secure

    The first is silently ignored and the cookie survives logout in the jar.
    The session rows are gone either way, so this was never an auth bypass --
    it is the one place the docstring above ("the four the __Host- prefix
    requires and the browser checks") would have been untrue of the code.
    """
    response.del_cookie(name, path="/", secure=True, httponly=True,
                        samesite="Lax")


class SessionCache:
    def __init__(self, now: Callable[[], float],
                 ttl: float = idle.SESSION_CACHE_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        # digest -> (tenant id, expiry). The digest and not the plaintext, so
        # a heap dump of the gateway is not a set of live cookies.
        self._entries: dict[str, tuple[str, float]] = {}

    def get(self, value: str) -> str | None:
        found = self._entries.get(token_hash(value))
        if found is None:
            return None
        tenant_id, expires = found
        if self._now() >= expires:
            self._entries.pop(token_hash(value), None)
            return None
        return tenant_id

    def put(self, value: str, tenant_id: str) -> None:
        self._entries[token_hash(value)] = (tenant_id, self._now() + self._ttl)

    def forget_tenant(self, tenant_id: str) -> None:
        """Logout and disable clear the tenant's entries at once, so the cache
        only ever delays a change made outside this process."""
        for digest in [d for d, (t, _) in self._entries.items() if t == tenant_id]:
            self._entries.pop(digest, None)

    def forget_value(self, value: str) -> None:
        self._entries.pop(token_hash(value), None)


class HandoffCodes:
    def __init__(self, now: Callable[[], float],
                 ttl: float = HANDOFF_TTL_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        self._codes: dict[str, tuple[str, float]] = {}

    def issue(self, tenant_id: str) -> str:
        code = new_secret()
        self._codes[code] = (tenant_id, self._now() + self._ttl)
        return code

    def redeem(self, code: str, tenant_id: str) -> bool:
        found = self._codes.pop(code, None)
        if found is None:
            return False
        owner, expires = found
        return owner == tenant_id and self._now() < expires

    def forget_tenant(self, tenant_id: str) -> None:
        for code in [c for c, (t, _) in self._codes.items() if t == tenant_id]:
            self._codes.pop(code, None)
