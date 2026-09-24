"""The proxy's side of run/gateway/gateway.sock, with its 10-second cache.

The cache is the whole reason a revoked token stops working at the proxy
within 10 seconds rather than eventually: nothing pushes a revocation, so the
bound on how stale an answer can be IS the bound on how long a disabled
tenant keeps spending.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from hosted import jsonsock
from hosted.core.tenant import token_hash

TOKEN_CACHE_SECONDS = 10.0


class TokenCache:
    def __init__(self, socket_path: Path,
                 now: Callable[[], float] = time.monotonic,
                 ttl: float = TOKEN_CACHE_SECONDS) -> None:
        self._path = socket_path
        self._now = now
        self._ttl = ttl
        self._answers: dict[str, tuple[float, tuple[str, str] | None]] = {}

    async def resolve(self, token: str) -> tuple[str, str] | None:
        """(tenant id, status), or None for an unknown or revoked token.

        Raises jsonsock.Unreachable when the gateway cannot answer and no
        fresh cache entry exists, so the caller can send 529 overloaded_error
        rather than a 401 that would read as a bad key.
        """
        digest = token_hash(token)
        cached = self._answers.get(digest)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        answer = await jsonsock.ask(self._path, {"op": "token", "hash": digest})
        tenant_id = answer.get("tenant")
        resolved = ((tenant_id, answer.get("status", ""))
                    if isinstance(tenant_id, str) else None)
        self._answers[digest] = (self._now(), resolved)
        return resolved
