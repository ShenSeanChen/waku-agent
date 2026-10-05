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

# A SESSION IS BOUND TO THE HOST THAT ISSUED IT, in the value that is stored
# rather than in a column. What the browser holds is the secret; what the
# store and the cache are keyed on is the secret with its scope in front, so
# the apex's cookie value replayed as __Host-waku_tenant hashes to a row that
# does not exist. Before this, the two cookies were one credential with two
# names: the separation was a naming convention, and any future path that
# leaked an apex value handed over a tenant session with it.
#
# In the value and not in a new column because hosted/gateway/store.py is
# group B's, `create_session` hashes whatever it is given, and a scope that
# lives in the string is a scope no caller can forget to pass -- there is no
# unscoped overload to reach for.
APEX_SCOPE = "apex:"
TENANT_SCOPE = "tenant:"


def apex_key(value: str) -> str:
    return APEX_SCOPE + value


def tenant_key(tenant_id: str, value: str) -> str:
    return f"{TENANT_SCOPE}{tenant_id}:{value}"


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
        """A single-use code, held IN PLAINTEXT, unlike a session value.

        WHY THE ASYMMETRY WITH SessionCache, which stores token_hash(value).
        A session value is a live credential for its whole TTL, so a heap dump
        of the gateway must not be a set of live cookies. A hand-off code is
        live for sixty seconds and dies on first read, whichever way the
        redeem goes -- and the gateway has to be able to answer "which tenant
        is this code for", which a hash of the code alone cannot do without
        storing the mapping this dict already is. Hashing it would buy a
        sixty-second window against an attacker who can already read the
        process's memory, which is an attacker who can read the sessions the
        code is about to become.

        SWEPT ON ISSUE. Entries used to leave only through redeem or
        forget_tenant, so a code nobody followed sat here for the life of the
        process -- one per abandoned sign-in, for ever. The sweep is bounded
        by the number of codes issued in the last sixty seconds, which is the
        sign-in rate, so it costs nothing and needs no timer of its own.
        """
        self._sweep()
        code = new_secret()
        self._codes[code] = (tenant_id, self._now() + self._ttl)
        return code

    def _sweep(self) -> None:
        now = self._now()
        for code in [c for c, (_, expires) in self._codes.items() if expires <= now]:
            self._codes.pop(code, None)

    def redeem(self, code: str, tenant_id: str) -> bool:
        found = self._codes.pop(code, None)
        if found is None:
            return False
        owner, expires = found
        return owner == tenant_id and self._now() < expires

    def forget_tenant(self, tenant_id: str) -> None:
        for code in [c for c, (t, _) in self._codes.items() if t == tenant_id]:
            self._codes.pop(code, None)
