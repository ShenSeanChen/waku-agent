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

IDLE IS TWO QUESTIONS SINCE SPEC 026. The fleet answers the first from what
the gateway saw: nothing in flight and no non-background request for the idle
window. A container can still be working with no request open, because an
event or a timer started a run inside it. So the gateway asks an idle
container for its own report, and work_left() below reads it: a container
with a run in progress, an event no run has finished with, or an outgoing
message not yet sent is not idle. The same report carries the container's
earliest timer, which the gateway stores before it stops the container so
that it can start it again at that time. A parked run waiting for the owner's
yes is not work: the yes arrives as an event.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from hosted.core import policy

# The default idle window. config/gateway.env can move it with
# WAKU_IDLE_MINUTES (hosted/gateway/config.py); the Fleet takes the number it
# was built with, so a test or an operator never has to patch this constant.
IDLE_SECONDS = 15 * 60
# How often the gateway's idle loop sweeps (hosted/gateway/sweep.py). The
# spec's "once a minute".
SWEEP_SECONDS = 60
# How long a request waits for the spawner to start a container. It was 15
# seconds, and the first open after `upgrade.sh --now` or an idle stop kept
# showing "taking too long to start" while the start was still on its way:
# the spawner provisions (a container run to completion) and then creates and
# starts the tenant's, and on a cold page cache that passed 15 seconds. The
# wait cancelled the gateway's side of a start the spawner went on to finish,
# and the next open started it all over again. 60 seconds is that sequence's
# worst case with room to spare; spawner_client.START_ASK_TIMEOUT stays above it.
START_TIMEOUT_SECONDS = 60
# After the spawner answers, the container's dashboard is still importing and
# has not bound its port: a connection in that window is refused. The gateway
# waits up to this long for it to accept one before it gives up on the request.
READY_TIMEOUT_SECONDS = 30
# A page navigation that outlasts both waits gets a page that says the
# assistant is starting and reloads itself this often, never a dead end.
STARTING_RELOAD_SECONDS = 3
# Every request forwarded to a container that is not a turn or another stream
# gets this, so a query that never returns cannot hold a container awake.
FORWARD_TIMEOUT_SECONDS = 120
# A session lookup is cached for at most this long. Logout and disable clear
# the tenant's entries at once, so the cache only delays a change made outside
# the gateway process.
SESSION_CACHE_SECONDS = 60
DISK_WARNING_FRACTION = 0.80
# How long past the idle window a container that reports work is kept (spec
# 026). An outgoing message is retried for a little over an hour before it is
# given up on, so two hours covers every honest report; a container that still
# says it is working after that is broken, and it is stopped like any other.
WORK_LIMIT_SECONDS = 2 * 3600
# How long a container on an old image must have gone without a non-background
# request before the gateway replaces it (Launcher.stop_stale). One sweep. A
# request and a scheduler wake both set the idle clock and then take up to
# READY_TIMEOUT_SECONDS plus one hand-over to reach the container, so after
# this long whatever they delivered is in the container's own report.
STALE_QUIET_SECONDS = 60

CAPACITY_MESSAGE = "At capacity, try again shortly."
START_TIMEOUT_MESSAGE = "Your assistant is taking too long to start. Try again."
STARTING_MESSAGE = "Your assistant is starting. This page opens it as soon as it is ready."
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


@dataclass(frozen=True)
class WorkLeft:
    """A container's own report, read. `busy` is True when a run, an event or
    an outgoing message still waits; `wakeup` is when its earliest timer is
    due, in seconds since the epoch, or None when it has no timer."""
    busy: bool
    wakeup: float | None


_WORK_COUNTS = ("events", "outbox", "running")


def work_left(report: object) -> WorkLeft | None:
    """Read what GET /api/alwayson/next answered (spec 026), or None when the
    answer is not the agreed shape.

    A CLOSED SET. The report is {next_timer_at, events, outbox, running}:
    three whole numbers that are not negative, and a time that is either null
    or ISO 8601 with a zone. Anything else is None, and the caller treats None
    as "no report", which stops the container and leaves its stored wakeup
    alone. A container cannot keep itself running or move its wakeup with an
    answer this function would have to guess at.
    """
    if not isinstance(report, dict):
        return None
    counts = [report.get(name) for name in _WORK_COUNTS]
    if not all(type(count) is int and count >= 0 for count in counts):
        return None
    timer = report.get("next_timer_at", "")
    if timer is None:
        return WorkLeft(busy=any(counts), wakeup=None)
    if not isinstance(timer, str):
        return None
    try:
        due = datetime.fromisoformat(timer)
    except ValueError:
        return None
    if due.tzinfo is None:
        return None
    return WorkLeft(busy=any(counts), wakeup=due.timestamp())


class Fleet:
    """Every container the gateway believes exists, and the decisions about it."""

    def __init__(self, now: Callable[[], float], max_running: int,
                 idle_seconds: float = IDLE_SECONDS) -> None:
        self._now = now
        self._max_running = max_running
        self._idle_seconds = idle_seconds
        self._states: dict[str, ContainerState] = {}

    def _state(self, tenant_id: str) -> ContainerState:
        return self._states.setdefault(tenant_id, ContainerState(tenant_id))

    def set_status(self, tenant_id: str, status: str) -> None:
        """Record a transition the gateway made. Touches no clock."""
        self._state(tenant_id).status = status

    def running_status(self, tenant_id: str) -> str:
        """The status the gateway believes, without creating a state for a
        tenant nobody has asked about. `_state` is a setdefault, so a bare
        read through it would grow the fleet by one entry per probe."""
        state = self._states.get(tenant_id)
        return state.status if state is not None else STOPPED

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
        non-background request for the idle window (fifteen minutes unless
        WAKU_IDLE_MINUTES says otherwise)."""
        return sorted(s.tenant_id for s in self._states.values() if self._idle(s))

    def is_idle(self, tenant_id: str) -> bool:
        """The same question as idle_stops, for one tenant, NOW.

        The idle loop asks this again immediately before each stop, because
        it stops containers one at a time and every stop awaits the spawner.
        A request that arrived for the second tenant on the list while the
        first was being stopped has touched the clock or entered the fleet,
        and the answer the list gave a moment ago is no longer true.
        """
        state = self._states.get(tenant_id)
        return state is not None and self._idle(state)

    def quiet_for(self, tenant_id: str, seconds: float) -> bool:
        """Whether a running container has nothing in flight and has had no
        non-background request for `seconds`. is_idle() is this question with
        the idle window. Launcher.stop_stale asks it with STALE_QUIET_SECONDS,
        to find the gap between two turns."""
        state = self._states.get(tenant_id)
        return state is not None and self._quiet(state, seconds)

    def seconds_idle(self, tenant_id: str) -> float:
        """How long since the last non-background request, for the log line
        the idle loop writes. 0.0 for a tenant the fleet has no state for."""
        state = self._states.get(tenant_id)
        return self._now() - state.last_activity if state is not None else 0.0

    def worked_too_long(self, tenant_id: str) -> bool:
        """Whether a container that says it still has work has said so for
        WORK_LIMIT_SECONDS past the idle window. False for a tenant the fleet
        has no state for."""
        state = self._states.get(tenant_id)
        if state is None:
            return False
        limit = self._idle_seconds + WORK_LIMIT_SECONDS
        return self._now() - state.last_activity >= limit

    def _idle(self, state: ContainerState) -> bool:
        return self._quiet(state, self._idle_seconds)

    def _quiet(self, state: ContainerState, seconds: float) -> bool:
        cutoff = self._now() - seconds
        return (state.status == RUNNING and state.in_flight == 0
                and state.last_activity <= cutoff)

    def _evictable(self) -> str | None:
        candidates = [s for s in self._states.values()
                      if s.status == RUNNING and s.in_flight == 0]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (s.last_activity, s.tenant_id)).tenant_id

    def release_start(self, tenant_id: str) -> None:
        """Give back a slot admit() reserved for a start that never happened.

        ONLY while the fleet still says STARTING. A start that got as far as a
        container has already moved the status on, and a tenant whose
        container is RUNNING must not be recorded as stopped because some
        other caller was refused.
        """
        state = self._states.get(tenant_id)
        if state is not None and state.status == STARTING:
            state.status = STOPPED

    def admit(self, tenant_id: str, *, background: bool) -> Admission:
        """The decision AND the reservation, in one synchronous call.

        THIS METHOD TAKES THE SLOT IT GRANTS. It used to only answer, and the
        caller then suspended -- on a unix socket to the spawner, and on the
        `stop` of an evicted container before that -- while the fleet still
        said a slot was free. Measured at max_running=1: two interleaved
        POST /auth/session produced two containers, and so did two interleaved
        requests for different tenants through the real forwarder. The cap is
        derived from the VM's memory, so exceeding it is the kernel
        OOM-killing a stranger's container mid-turn; a cap that a second
        caller can walk through is not a cap.

        So both start arms mark the tenant STARTING, which running() already
        counts. After this call the arithmetic is settled and nothing a caller
        does while suspended can change it.

        The evict arm ALSO marks the evicted tenant STOPPED, and that half is
        belt and braces rather than load-bearing today -- said here because it
        was measured, not guessed. Launcher.stop calls _forget_running
        (status STOPPED) before its first await, so through the gateway the
        victim is already out of running() by the time any second caller can
        be scheduled: deleting this line leaves both interleaved gateway tests
        green. It is kept because admit's answer should be self-consistent on
        its own -- a caller that admitted and then yielded before stopping,
        or a future stop() that awaited the spawner first, would otherwise
        find the same victim RUNNING and evict it twice. That property is
        pinned at this level instead, by
        test_admit_settles_the_cap_without_any_help_from_the_caller.

        WHO GIVES THE SLOT BACK. Every path out of Launcher.start either
        reaches a container (RUNNING) or calls _forget_running/release_start
        (STOPPED). A reservation that leaked would not over-commit the VM --
        it would shrink it by one container for the life of the process --
        but it would be just as wrong, so it is closed rather than argued
        about.
        """
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
            self.set_status(tenant_id, STARTING)
            return Admission("start")
        evict = self._evictable()
        if evict is None:
            return Admission("at_capacity", message=CAPACITY_MESSAGE)
        self.touch(tenant_id)
        # The slot changes hands HERE. The caller still has to ask the spawner
        # to stop the evicted container, and Launcher.stop marks it STOPPED
        # again on the way -- but by then the decision has already been made
        # on a fleet that cannot offer the same slot twice.
        self.set_status(evict, STOPPED)
        self.set_status(tenant_id, STARTING)
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
