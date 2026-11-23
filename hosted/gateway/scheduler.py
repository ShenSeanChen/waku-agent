"""The gateway's scheduler: spec 026, task A3.

A tenant's container stops after the idle window, and its worker and its
timer loop stop with it. The gateway keeps running, so this loop does the two
things a stopped container cannot do for itself.

WHAT IT DOES, EVERY TICK_SECONDS, for each tenant that has an event waiting
in tenant_events or a row in tenant_wakeups whose time has come:

  1. Start the tenant's container, unless it is running. Launcher.prewarm
     asks Fleet.admit, so a wake obeys the running cap like a request does.
  2. Hand the tenant's waiting events to the container (POST /api/events) and
     mark them `handed` when the container answers 200.
  3. Delete the wakeup once the container listens. The container's own timer
     loop fires a timer that came due while it was stopped, so the gateway
     only has to get the container running.

WHAT IT NEVER DOES. It starts no run and calls no model. An event it could
not hand over stays `pending`, and the next tick offers it again. The
container stores an event under UNIQUE(source, external_id), so an event
offered twice is one run.

A KICK IS A TICK NOW (spec 026 C2). A Slack mention that waited for the next
tick would wait up to 30 seconds before its container even started, so the
handler that stored it calls `kick`. The container a kick starts is usually
not listening yet when the tick ends, so a kick ticks again every
KICK_RETRY_SECONDS, at most KICK_TRIES times, until no event is waiting. Ticks
never overlap: a kick and the loop take the same lock.

A TICK THAT RAISES DOES NOT END THE LOOP, and one tenant's failure does not
hold up another's: tenants are served side by side, and each failure is one
line in the log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from hosted import log
from hosted.gateway.container_api import ContainerApi
from hosted.gateway.launch import InMaintenance, Launcher, NotActive, StartFailed
from hosted.ports.control import ControlStore

_LOG = log.get(__name__)

TICK_SECONDS = 30
# The most events one request carries. The rest go with the next tick, oldest
# first, so a tenant with a long queue cannot build one very large request.
HAND_OVER_BATCH = 50
# After a kick: how long between tries, and how many. Twenty seconds in all,
# which is longer than a container takes to listen and shorter than a tick.
KICK_RETRY_SECONDS = 2.0
KICK_TRIES = 10


class Scheduler:
    def __init__(self, *, store: ControlStore, launcher: Launcher,
                 containers: ContainerApi, now: Callable[[], float] = time.time,
                 interval: float = TICK_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._store = store
        self._launcher = launcher
        self._containers = containers
        self._now = now
        self._interval = interval
        self._sleep = sleep
        self._ticking = asyncio.Lock()
        self._kick: asyncio.Task | None = None
        self._kick_again = False

    def kick(self) -> None:
        """Tick now, from inside the event loop, without waiting for it. One
        kick runs at a time; a kick that arrives while one runs makes that
        one start its tries again, so the new event gets the full count."""
        if self._kick is not None and not self._kick.done():
            self._kick_again = True
            return
        self._kick = asyncio.get_running_loop().create_task(self._kicked())

    async def _kicked(self) -> None:
        tries = 0
        while tries < KICK_TRIES:
            if self._kick_again:
                self._kick_again, tries = False, 0
            tries += 1
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 -- see the module docstring
                _LOG.exception("a kicked scheduler tick failed")
            if not self._kick_again and not self._store.tenants_with_pending_events():
                return
            await self._sleep(KICK_RETRY_SECONDS)

    async def settled(self) -> None:
        """Wait for the kick under way, if there is one. For shutdown and for
        a test that needs to see what a kick did."""
        while self._kick is not None and not self._kick.done():
            await asyncio.wait([self._kick])

    async def tick(self) -> None:
        """One pass over every tenant with an event waiting or a wakeup due."""
        async with self._ticking:
            await self._tick()

    async def _tick(self) -> None:
        due = set(self._store.due_wakeups(self._now()))
        waiting = self._store.tenants_with_pending_events()
        tenant_ids = waiting + sorted(due - set(waiting))
        results = await asyncio.gather(
            *(self._serve(tenant_id, wakeup_due=tenant_id in due)
              for tenant_id in tenant_ids), return_exceptions=True)
        for tenant_id, result in zip(tenant_ids, results, strict=True):
            if isinstance(result, Exception):
                _LOG.error("the scheduler failed for tenant=%s: %r", tenant_id, result)

    async def _serve(self, tenant_id: str, *, wakeup_due: bool) -> None:
        tenant = self._store.tenant_by_id(tenant_id)
        if tenant is None:
            return
        try:
            # prewarm refuses a tenant who is not active before it asks the
            # fleet for a slot, so a disabled tenant's container never
            # starts. Its rows stay as they are.
            running = await self._launcher.prewarm(tenant)
        except (NotActive, InMaintenance, StartFailed) as exc:
            _LOG.info("not waking tenant=%s: %s", tenant_id, exc)
            return
        if running is None:
            # At the cap with nothing to evict, or a start already under way.
            return
        if wakeup_due and await self._containers.listening(running):
            self._store.clear_wakeup(tenant_id)
            _LOG.info("woke tenant=%s for a timer", tenant_id)
        events = self._store.pending_events(tenant_id)[:HAND_OVER_BATCH]
        if events and await self._containers.hand_over(running, events):
            self._store.mark_events_handed(tenant_id, [event.id for event in events])
            _LOG.info("handed %s events to tenant=%s", len(events), tenant_id)

    async def run(self) -> None:
        """Tick every interval until cancelled."""
        while True:
            await self._sleep(self._interval)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 -- see the module docstring
                _LOG.exception("scheduler tick failed; the next one runs in %ss",
                               self._interval)
