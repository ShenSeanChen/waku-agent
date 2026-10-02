"""The person's Waku Memory credits, from the proxy's side (spec 004 D).

Two calls, both made with the PERSON'S OWN Waku Memory key, so they can only
ever read or charge that person (waku-memory spec 040 D1):

  balance  GET  <api>/agent-usage/balance, cached 60 seconds per key. A Free
           person at zero is refused before their call reaches Anthropic.
  charge   POST <api>/agent-usage after a call settles, in the background,
           retried; idempotent on turn_id, so a retry never bills twice.

Neither ever blocks or fails a model call. An unreachable Waku Memory leaves
the $1 cap as the only limit, which is where the free tier started.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import aiohttp

from hosted import log

_LOG = log.get(__name__)
BALANCE_CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 10
RETRY_DELAYS = (1.0, 4.0, 15.0)


class WakuMemoryWallet:
    def __init__(self, session: aiohttp.ClientSession, api_url: str,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._session = session
        self._api = api_url.rstrip("/")
        self._now = now
        self._balances: dict[str, tuple[float, tuple[str, int]]] = {}
        self._pending: set[asyncio.Task] = set()

    async def balance(self, key: str) -> tuple[str, int] | None:
        cached = self._balances.get(key)
        if cached is not None and self._now() - cached[0] < BALANCE_CACHE_SECONDS:
            return cached[1]
        async with self._session.get(
                f"{self._api}/agent-usage/balance",
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as response:
            if response.status != 200:
                return None
            body = await response.json()
        plan, left = body.get("plan"), body.get("credits_left")
        if not isinstance(plan, str) or not isinstance(left, int):
            return None
        self._balances[key] = (self._now(), (plan, left))
        return plan, left

    def charge(self, key: str, *, turn_id: str, model: str, usd: float) -> None:
        task = asyncio.get_running_loop().create_task(
            self._charge(key, {"turn_id": turn_id, "model": model, "usd": round(usd, 6)}))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _charge(self, key: str, report: dict) -> None:
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._session.post(
                        f"{self._api}/agent-usage", json=report,
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as response:
                    if response.status == 200:
                        self._balances.pop(key, None)   # the next read sees the charge
                        return
                    if response.status < 500:
                        _LOG.warning("agent usage %s refused: %s", report["turn_id"],
                                     response.status)
                        return
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOG.info("agent usage %s attempt %s failed: %s",
                          report["turn_id"], attempt + 1, exc)
        _LOG.warning("agent usage %s not charged after retries", report["turn_id"])
