"""What to do with a request, given what the fleet is doing -- the spec's
"the gateway, per request" flowchart, minus the HTTP.

Pure logic on an injected clock. Group E wraps this; group C makes the starts
and stops real.

WHO REFRESHES THE IDLE CLOCK, AND WHY IT IS NOT set_status(). There are three
ways a container comes to be RUNNING, and they are not the same event:

  a real request       admit() returns "start" or "evict_then_start", and the
                       request that caused it IS activity -- admit() touches
                       on both branches. Missing this is not cosmetic: the
                       container is started with a last_activity from before
                       the idle stop, so the next sweep, within sixty seconds,
                       stops the container the tenant just waited for.
  a gateway restart    adopt(), for a container the spawner's `list` reports.
                       The spec: "treats each as active from that moment, so
                       the idle timer starts fresh and no container is
                       orphaned." It refreshes ONLY when the fleet has no
                       clock for that tenant, because adopt() is also called
                       on a re-list mid-life (a container that refused a
                       connection), and resetting the clock there would keep a
                       broken container alive forever.
  anything else        touch() first. E2 starts a container on first login,
                       before any request reaches admit(); a login is a user
                       action, so E2 calls touch() and then set_status().

set_status() itself records a transition and nothing more. It used to refresh
the clock "if last_activity == 0.0", which is true exactly once in a
container's life, so every restart after the first was stopped a minute later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hosted.core import policy

IDLE_SECONDS = 15 * 60
START_TIMEOUT_SECONDS = 15
# Every request forwarded to a container that is not a turn or another stream
# gets this, so a query that never returns cannot hold a container awake.
FORWARD_TIMEOUT_SECONDS = 120
# A session lookup is cached for at most this long. Logout and disable clear
# the tenant's entries at once, so the cache only delays a change made outside
# the gateway process.
SESSION_CACHE_SECONDS = 60
DISK_WARNING_FRACTION = 0.80

CAPACITY_MESSAGE = "At capacity, try again shortly."
START_TIMEOUT_MESSAGE = "Your assistant is taking too long to start. Try again."
MAINTENANCE_MESSAGE = "Your assistant is under maintenance. Try again in a few minutes."

RUNNING = "running"
STARTING = "starting"
STOPPED = "stopped"


@dataclass
class ContainerState:
    tenant_id: str
    status: str = STOPPED
    # The last NON-BACKGROUND request, or the moment the gateway adopted this
    # container. 0.0 means no request has ever been seen and the gateway
    # cannot account for the container -- see the module docstring.
    last_activity: float = 0.0
    in_flight: int = 0


@dataclass(frozen=True)
class Admission:
    action: str               # forward | wait | start | evict_then_start | paused | at_capacity
    evict: str | None = None
    message: str = ""


class Fleet:
    """Every container the gateway believes exists, and the decisions about it."""

    def __init__(self, now: Callable[[], float], max_running: int) -> None:
        self._now = now
        self._max_running = max_running
        self._states: dict[str, ContainerState] = {}

    def _state(self, tenant_id: str) -> ContainerState:
        return self._states.setdefault(tenant_id, ContainerState(tenant_id))

    def set_status(self, tenant_id: str, status: str) -> None:
        """Record a transition the gateway made. Touches no clock."""
        self._state(tenant_id).status = status

    def adopt(self, tenant_id: str) -> None:
        """A container the spawner's `list` reports as running.

        Sets the idle clock only when the fleet has none, so a gateway
        restart starts every timer fresh while a mid-life re-list leaves a
        running clock alone.
        """
        state = self._state(tenant_id)
        state.status = RUNNING
        if state.last_activity == 0.0:
            state.last_activity = self._now()

    def touch(self, tenant_id: str) -> None:
        """A non-background request. Only these count as activity."""
        self._state(tenant_id).last_activity = self._now()

    def enter(self, tenant_id: str) -> None:
        self._state(tenant_id).in_flight += 1

    def leave(self, tenant_id: str) -> None:
        state = self._state(tenant_id)
        state.in_flight = max(0, state.in_flight - 1)

    def forget(self, tenant_id: str) -> None:
        self._states.pop(tenant_id, None)

    def running(self) -> list[str]:
        return [s.tenant_id for s in self._states.values() if s.status in (RUNNING, STARTING)]

    def idle_stops(self) -> list[str]:
        """Once a minute: every container with nothing in flight and no
        non-background request for fifteen minutes."""
        cutoff = self._now() - IDLE_SECONDS
        return sorted(s.tenant_id for s in self._states.values()
                      if s.status == RUNNING and s.in_flight == 0 and s.last_activity <= cutoff)

    def _evictable(self) -> str | None:
        candidates = [s for s in self._states.values()
                      if s.status == RUNNING and s.in_flight == 0]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (s.last_activity, s.tenant_id)).tenant_id

    def admit(self, tenant_id: str, *, background: bool) -> Admission:
        state = self._state(tenant_id)
        if state.status == RUNNING:
            if not background:
                self.touch(tenant_id)
            return Admission("forward")
        if state.status == STARTING:
            return Admission("wait")
        if background:
            # A stopped container stays stopped. The page reads this body and
            # stops its own timers until the next user action.
            return Admission("paused", message=policy.PAUSED_BODY["error"])
        # Past here the container is stopped and a real request caused this.
        # The start is activity: without these touches the container is
        # started carrying a last_activity from before the idle stop, and the
        # next sweep stops it sixty seconds later.
        if len(self.running()) < self._max_running:
            self.touch(tenant_id)
            return Admission("start")
        evict = self._evictable()
        if evict is None:
            return Admission("at_capacity", message=CAPACITY_MESSAGE)
        self.touch(tenant_id)
        return Admission("evict_then_start", evict=evict)


def disk_warning(used_bytes: int, total_bytes: int) -> str:
    """The idle loop logs this. An operator finds out before a tenant does."""
    if total_bytes <= 0:
        return ""
    fraction = used_bytes / total_bytes
    if fraction < DISK_WARNING_FRACTION:
        return ""
    return (f"warning: /srv/waku is {fraction * 100:.0f}% full. "
            "Tenant writes start failing when it fills.")
