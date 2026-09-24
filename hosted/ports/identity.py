"""Who is asking. Supabase JWKS now; any JWT issuer later (design 4.5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    is_anonymous: bool = False


class IdentityVerifier(Protocol):
    def verify(self, access_token: str) -> Identity:
        """Return the identity, or raise. Group E's JwksVerifier checks the
        signature against the project's JWKS (asymmetric keys only), iss, aud
        and exp, and refuses a token whose is_anonymous claim is true."""
        ...
