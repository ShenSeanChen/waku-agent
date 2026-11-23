"""Plans, turn windows, reservations and the dollar cap.

Pure logic on injected values: a clock for the windows, an environment mapping
for the plan values, a Prices record for the arithmetic. Nothing here opens a
database or a socket.

WHICH PLAN APPLIES IS DECIDED FROM PLATFORM-CONTROLLED DATA ONLY. The gateway
counts turns and the proxy's ledger records platform-token calls; a tenant's
own .env has no say. The dollar cap and the concurrency limit bind every
platform-token call with free's values whatever the plan, so a tenant who
copies their platform token into WAKU_API_KEY under another provider still
spends from the same dollar.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

TURN_WINDOW_SECONDS = 3600
RESERVATION_MARGIN = 0.10

TURN_LIMIT_MESSAGE = "Turn limit reached for this hour."
FREE_TIER_MESSAGE = "Free tier used up. Add your own key in Models."
MODEL_NOT_ALLOWED_MESSAGE = "This model is not in the free tier. Add your own key in Models."
SPEND_UNAVAILABLE_MESSAGE = "Spend is unavailable right now."


@dataclass(frozen=True)
class Plan:
    name: str
    turns_per_hour: int
    dollar_cap: float | None
    concurrent_calls: int
    requests_per_minute: int
    max_tokens_ceiling: int


# The defaults. install.sh writes the real values into config/gateway.env
# (turns) and config/proxy.env (money, slots, rate, ceiling), and each service
# reads its own file -- which is why byok's money columns repeat free's: the
# proxy only ever enforces free's, and the byok row never reaches it.
DEFAULT_PLANS: dict[str, Plan] = {
    "free": Plan("free", turns_per_hour=30, dollar_cap=1.00, concurrent_calls=4,
                 requests_per_minute=60, max_tokens_ceiling=4096),
    "byok": Plan("byok", turns_per_hour=120, dollar_cap=None, concurrent_calls=4,
                 requests_per_minute=60, max_tokens_ceiling=4096),
}


@dataclass(frozen=True)
class Prices:
    """Dollars per million tokens, from the proxy's configured per-model table."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


def plans_from_env(env: Mapping[str, str]) -> dict[str, Plan]:
    free = DEFAULT_PLANS["free"]
    byok = DEFAULT_PLANS["byok"]

    def _int(name: str, fallback: int) -> int:
        raw = str(env.get(name, "")).strip()
        return int(raw) if raw else fallback

    def _float(name: str, fallback: float) -> float:
        raw = str(env.get(name, "")).strip()
        return float(raw) if raw else fallback

    money = {
        "dollar_cap": _float("WAKU_FREE_DOLLAR_CAP", free.dollar_cap or 1.00),
        "concurrent_calls": _int("WAKU_FREE_CONCURRENT_CALLS", free.concurrent_calls),
        "requests_per_minute": _int("WAKU_FREE_REQUESTS_PER_MINUTE", free.requests_per_minute),
        "max_tokens_ceiling": _int("WAKU_FREE_MAX_TOKENS", free.max_tokens_ceiling),
    }
    return {
        "free": Plan("free", turns_per_hour=_int("WAKU_FREE_TURNS_PER_HOUR",
                                                 free.turns_per_hour), **money),
        "byok": Plan("byok", turns_per_hour=_int("WAKU_BYOK_TURNS_PER_HOUR",
                                                 byok.turns_per_hour),
                     **{**money, "dollar_cap": None}),
    }


def utc_month(at: float) -> str:
    """Months in ledger.db are UTC months, so one tenant's month does not move
    when they fly somewhere."""
    return dt.datetime.fromtimestamp(at, tz=dt.UTC).strftime("%Y-%m")


def plan_for(plans: Mapping[str, Plan], *, turns_in_last_hour: int,
             platform_call_in_last_hour: bool) -> Plan:
    if turns_in_last_hour >= 1 and not platform_call_in_last_hour:
        return plans["byok"]
    return plans["free"]


def allow_turn(plans: Mapping[str, Plan], *, turns_in_last_hour: int,
               platform_call_in_last_hour: bool) -> tuple[bool, Plan, str]:
    """(allowed, plan, message). `turns_in_last_hour` is the count BEFORE this
    turn, so the 31st free turn arrives with 30 already counted."""
    plan = plan_for(plans, turns_in_last_hour=turns_in_last_hour,
                    platform_call_in_last_hour=platform_call_in_last_hour)
    if turns_in_last_hour >= plan.turns_per_hour:
        return (False, plan, TURN_LIMIT_MESSAGE)
    return (True, plan, "")


def reservation_dollars(*, input_tokens: int, max_tokens: int, prices: Prices) -> float:
    """The call's worst case: the counted input plus 10 percent, plus the whole
    output allowance. Anthropic describes its count as an estimate."""
    counted = input_tokens * (1.0 + RESERVATION_MARGIN)
    return (counted * prices.input_per_mtok + max_tokens * prices.output_per_mtok) / 1_000_000


def settled_dollars(*, input_tokens: int, output_tokens: int,
                    cache_creation_input_tokens: int, cache_read_input_tokens: int,
                    prices: Prices) -> float:
    """All four usage fields. The proxy refuses cache_control today, so the two
    cache fields should be zero -- they are priced anyway, because a field that
    is silently ignored is a field that bills somebody later."""
    return (input_tokens * prices.input_per_mtok
            + output_tokens * prices.output_per_mtok
            + cache_creation_input_tokens * prices.cache_write_per_mtok
            + cache_read_input_tokens * prices.cache_read_per_mtok) / 1_000_000


def at_cap(*, settled: float, reserved: float, cap: float | None) -> bool:
    """Step 5 of the proxy: already at the cap. Costs nothing upstream."""
    return cap is not None and settled + reserved >= cap


def would_pass_cap(*, settled: float, reserved: float, adding: float,
                   cap: float | None) -> bool:
    """Step 7 of the proxy: this reservation would pass it."""
    return cap is not None and settled + reserved + adding > cap


def clamp_max_tokens(asked: int, ceiling: int) -> int:
    return max(1, min(int(asked), int(ceiling)))


class TurnWindow:
    """Turn times per tenant, in the gateway's memory, on an injected clock.

    They live in memory on purpose (spec: "Turn windows and activity times live
    in the gateway's memory"). A restart forgives an hour of turns, which is the
    cheap side of the trade: the alternative is a write to control.db on every
    single turn.
    """

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._turns: dict[str, deque[float]] = {}

    def record(self, tenant_id: str) -> None:
        self._turns.setdefault(tenant_id, deque()).append(self._now())

    def count(self, tenant_id: str) -> int:
        turns = self._turns.get(tenant_id)
        if not turns:
            return 0
        cutoff = self._now() - TURN_WINDOW_SECONDS
        while turns and turns[0] <= cutoff:
            turns.popleft()
        if not turns:
            del self._turns[tenant_id]
            return 0
        return len(turns)

    def forget(self, tenant_id: str) -> None:
        """Disabling or deleting a tenant drops their window with everything
        else the gateway remembers about them."""
        self._turns.pop(tenant_id, None)
