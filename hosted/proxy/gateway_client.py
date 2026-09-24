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

# The cache is keyed on the hash of whatever arrived in the request, so a
# tenant looping bad keys chooses how many keys it has. Every entry expires
# within TOKEN_CACHE_SECONDS, so the pruning below almost always clears the
# whole overflow; the cap is what holds when it does not. Sized well past the
# number of tenants one VM runs, so an honest fleet never reaches it.
TOKEN_CACHE_MAX_ENTRIES = 4096


class TokenCache:
    def __init__(self, socket_path: Path,
                 now: Callable[[], float] = time.monotonic,
                 ttl: float = TOKEN_CACHE_SECONDS,
                 max_entries: int = TOKEN_CACHE_MAX_ENTRIES) -> None:
        self._path = socket_path
        self._now = now
        self._ttl = ttl
        self._max_entries = max_entries
        self._answers: dict[str, tuple[float, tuple[str, str] | None]] = {}

    def __len__(self) -> int:
        """How many answers are held. The proxy is the one service tenant code
        can reach directly, so the size of anything it keeps per request is a
        number worth being able to assert."""
        return len(self._answers)

    def _remember(self, digest: str, resolved: tuple[str, str] | None) -> None:
        now = self._now()
        self._answers.pop(digest, None)         # re-insert, so order is age order
        self._answers[digest] = (now, resolved)
        if len(self._answers) <= self._max_entries:
            return
        for key in [k for k, (at, _) in self._answers.items() if now - at >= self._ttl]:
            del self._answers[key]
        while len(self._answers) > self._max_entries:
            del self._answers[next(iter(self._answers))]

    async def resolve(self, token: str) -> tuple[str, str] | None:
        """(tenant id, status), or None for a token the gateway does not know.

        Raises jsonsock.Unreachable when the gateway cannot answer -- the
        socket is gone, the answer times out, the answer will not parse, or
        the gateway reports that its own lookup failed. The caller sends 529
        overloaded_error on that, rather than a 401 that would read to the SDK,
        and then to the tenant, as a bad key. Only a definite "no such token"
        is None, and only a definite answer is cached.
        """
        digest = token_hash(token)
        cached = self._answers.get(digest)
        if cached is not None and self._now() - cached[0] < self._ttl:
            return cached[1]
        answer = await jsonsock.ask(self._path, {"op": "token", "hash": digest})
        if "error" in answer:
            # The gateway answered, and what it answered is that it could not
            # do the lookup. That is not "this token is unknown".
            raise jsonsock.Unreachable(f"{self._path} refused the lookup: {answer['error']}")
        tenant_id = answer.get("tenant")
        resolved = ((tenant_id, answer.get("status", ""))
                    if isinstance(tenant_id, str) else None)
        self._remember(digest, resolved)
        return resolved
