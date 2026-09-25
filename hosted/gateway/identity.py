"""Who is asking: a Supabase access token, verified against the project's JWKS.

THE SPEC'S `aud` SENTENCE IS WRONG FOR THIS PROJECT, and writing the verifier
to its letter would refuse every real sign-in. A real token from
upmikpuftlvvpwkvouqr carries:

    aud           https://api.waku.one/mcp      <- a CUSTOM audience
    role          authenticated                 <- what the spec calls `aud`
    iss           https://upmikpuftlvvpwkvouqr.supabase.co/auth/v1
    is_anonymous  False
    amr           otp

So the audience comes from configuration, defaulting to that value, and
`role == "authenticated"` is checked as its own claim. Both are enforced.

ONE AUDIENCE, NEVER A LIST. An audience list containing both the custom value
and "authenticated" would accept a token from any Supabase project that has
not customised its audience, leaving the issuer as the only thing doing work.

ASYMMETRIC ALGORITHMS ONLY. ALGORITHMS holds ES256 and RS256 and no HS*: with
an HMAC algorithm admitted, a JWKS document's own public key bytes become a
valid signing secret, which is the oldest JWT confusion there is.

THE REFETCH IS RATE-LIMITED. An unknown `kid` is a reason to refetch the JWKS,
and `kid` comes from an unauthenticated request, so without a cooldown a
stream of tokens with random kids is a stream of outbound requests the
attacker chose the rate of.

`exp` AND `nbf` ARE CHECKED AGAINST THIS OBJECT'S CLOCK, not PyJWT's. PyJWT
reads time.time() directly and takes no clock, so a verifier built with an
injected clock -- which every other object in hosted/ takes, and which is how
this group's tests make time pass without sleeping -- would have one claim
judged by the fake clock and the expiry judged by the wall clock. `require`
still makes PyJWT refuse a token with no `exp` at all; what moved here is the
comparison, and with the default `now` it is the same comparison PyJWT makes.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable

import jwt

from hosted.ports.identity import Identity

ALGORITHMS = ("ES256", "RS256")
AUTHENTICATED_ROLE = "authenticated"
JWKS_TTL_SECONDS = 3600.0
REFETCH_COOLDOWN_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 5.0
REQUIRED_CLAIMS = ("exp", "iss", "aud", "sub")


class NotSignedIn(ValueError):
    """The token is not one this gateway will act on."""


def fetch_jwks(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


class JwksVerifier:
    """The MVP IdentityVerifier. Synchronous, because the port is: the caller
    runs it with asyncio.to_thread so a JWKS fetch never blocks the loop."""

    def __init__(self, *, jwks_url: str, issuer: str, audience: str,
                 fetch: Callable[[str], dict] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._fetch = fetch or fetch_jwks
        self._now = now
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    def _signing_key(self, kid: str) -> jwt.PyJWK:
        if self._keys is not None and self._now() - self._fetched_at < JWKS_TTL_SECONDS:
            try:
                return self._keys[kid]
            except KeyError:
                pass
        if self._now() - self._last_attempt < REFETCH_COOLDOWN_SECONDS:
            raise NotSignedIn("that key is not one this project publishes")
        self._last_attempt = self._now()
        try:
            document = self._fetch(self._jwks_url)
            keys = jwt.PyJWKSet.from_dict(document)
        except Exception as exc:
            raise NotSignedIn("the signing keys could not be read") from exc
        self._keys = keys
        self._fetched_at = self._now()
        try:
            return keys[kid]
        except KeyError as exc:
            raise NotSignedIn("that key is not one this project publishes") from exc

    def _check_window(self, claims: dict) -> None:
        """`exp` in the future and `nbf`, when present, in the past, both read
        off this object's clock. Anything that is not a number is refused:
        an allowlist of one shape, not a list of shapes to reject."""
        now = self._now()
        expires = claims.get("exp")
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            raise NotSignedIn("that token does not say when it expires")
        if now >= float(expires):
            raise NotSignedIn("that token has expired")
        starts = claims.get("nbf")
        if starts is not None:
            if not isinstance(starts, (int, float)) or isinstance(starts, bool):
                raise NotSignedIn("that token does not say when it starts")
            if now < float(starts):
                raise NotSignedIn("that token is not valid yet")

    def verify(self, access_token: str) -> Identity:
        if not isinstance(access_token, str) or not access_token:
            raise NotSignedIn("no access token")
        try:
            header = jwt.get_unverified_header(access_token)
        except jwt.PyJWTError as exc:
            raise NotSignedIn("that is not a token") from exc
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise NotSignedIn("the token names no signing key")
        if header.get("alg") not in ALGORITHMS:
            # Refused before the key is even looked up: an `alg` outside the
            # allowlist must never reach jwt.decode, whatever it would do
            # with it.
            raise NotSignedIn("that signing algorithm is not accepted here")
        key = self._signing_key(kid)
        try:
            claims = jwt.decode(
                access_token, key.key, algorithms=list(ALGORITHMS),
                audience=self._audience, issuer=self._issuer,
                options={"require": list(REQUIRED_CLAIMS),
                         "verify_exp": False, "verify_nbf": False})
        except jwt.PyJWTError as exc:
            raise NotSignedIn("that token is not valid here") from exc
        self._check_window(claims)
        if claims.get("role") != AUTHENTICATED_ROLE:
            # The claim the spec calls `aud`. A service-role or anon-role
            # token carries the same signature and must not sign anybody in.
            raise NotSignedIn("that token is not an authenticated user's")
        if claims.get("is_anonymous") is True:
            raise NotSignedIn("anonymous sign-in is not accepted here")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise NotSignedIn("the token names no subject")
        email = claims.get("email")
        return Identity(sub=sub, email=email if isinstance(email, str) else "",
                        is_anonymous=False)
