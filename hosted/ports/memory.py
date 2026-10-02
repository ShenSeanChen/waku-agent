"""Minting a person's Waku Memory API key (spec 004).

A port, like IdentityVerifier, so the gateway's evals use a fake and never
reach the network.
"""

from __future__ import annotations

from typing import Protocol


class MemoryKeys(Protocol):
    async def mint(self, access_token: str) -> tuple[str, str] | None:
        """(plaintext key, key id) minted with the person's own sign-in token,
        or None when Waku Memory did not issue one. May raise on a network
        failure; the caller treats a raise and a None the same way."""
        ...
