"""What one chat turn was charged, for the turn receipt (waku-agent spec 011 B1).

A tenant container's `waku-platform` client sends `X-Waku-Turn: <turn_id>`
on every model call. The proxy never forwards it (admission.forward_headers
is an allowlist), and keeps, per tenant and turn, for one hour in memory:

  model_usd  the settled dollars of that turn's model calls
  calls      how many model calls settled
  credits    what Waku Memory answered as `charged` for each of those calls,
             and for the treg calls the relay charged during the turn

`GET /v1/turns/<turn_id>/charges`, with the container's own platform token,
answers `{"model_usd", "calls", "credits"}` for that tenant's turn and 404
for any other. `credits` is null while a charge is still pending or when one
failed, never short.

The relay learns the turn from the latest `X-Waku-Turn` that tenant sent. A
container runs one turn at a time, and every turn calls the model before any
tool, so the latest turn is the one the treg call belongs to.

Nothing here is stored on disk: a restart forgets the last hour, and the
receipt then keeps the agent's own estimate.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

KEEP_SECONDS = 3600.0
# How long the route waits for a turn's last charge, which the wallet sends in
# the background right after the call settles.
WAIT_SECONDS = 3.0
TURN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def turn_id(value: str | None) -> str:
    """The header's turn id, or "" when it is missing or not id-shaped."""
    return value if value and TURN_ID.fullmatch(value) else ""


@dataclass
class _Turn:
    at: float
    model_usd: float = 0.0
    calls: int = 0
    # one entry per charge sent to Waku Memory: a future that answers the
    # credits it took, or None for a charge that could not be sent
    charges: list[asyncio.Future | None] = field(default_factory=list)


class TurnCharges:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._turns: dict[tuple[str, str], _Turn] = {}
        self._latest: dict[str, str] = {}

    def _prune(self) -> None:
        cutoff = self._now() - KEEP_SECONDS
        for key in [k for k, t in self._turns.items() if t.at < cutoff]:
            del self._turns[key]
            if self._latest.get(key[0]) == key[1]:
                del self._latest[key[0]]

    def seen(self, tenant: str, turn: str) -> None:
        """A model call named this turn: it is now the tenant's latest."""
        if not turn:
            return
        self._prune()
        self._turns.setdefault((tenant, turn), _Turn(at=self._now())).at = self._now()
        self._latest[tenant] = turn

    def model_call(self, tenant: str, turn: str, usd: float,
                   charge: asyncio.Future | None) -> None:
        """One settled model call. `charge` is the wallet's pending charge, or
        None when none was sent although the call cost something."""
        record = self._turns.get((tenant, turn)) if turn else None
        if record is None:
            return
        record.model_usd += usd
        record.calls += 1
        if usd > 0:
            record.charges.append(charge)

    def tool_call(self, tenant: str, charge: asyncio.Future | None) -> None:
        """One treg call the relay charged, counted to the tenant's latest turn."""
        turn = self._latest.get(tenant)
        record = self._turns.get((tenant, turn)) if turn else None
        if record is not None:
            record.charges.append(charge)

    async def answer(self, tenant: str, turn: str,
                     wait: float = WAIT_SECONDS) -> dict | None:
        """The route's body for one turn, or None for a turn this tenant never
        sent (another tenant's included)."""
        self._prune()
        record = self._turns.get((tenant, turn))
        if record is None:
            return None
        pending = [c for c in record.charges if c is not None and not c.done()]
        if pending:
            await asyncio.wait(pending, timeout=wait)
        return {"model_usd": round(record.model_usd, 6), "calls": record.calls,
                "credits": _credits(record.charges)}


def _credits(charges: list[asyncio.Future | None]) -> int | None:
    total = 0
    for charge in charges:
        if charge is None or not charge.done() or charge.cancelled() or charge.exception():
            return None
        taken = charge.result()
        if not isinstance(taken, int) or isinstance(taken, bool):
            return None
        total += taken
    return total
