"""The only writer of the fleet: tenant rows, proxy tokens, containers.

THREE THINGS LIVE HERE AND NOWHERE ELSE, and each has a reason that is a bug
somebody would otherwise ship:

  the token issue     ControlDb.issue_token REVOKES the tenant's previous
                      token in the same transaction. Two call sites means the
                      second one silently 401s a container the first one
                      started, and the tenant sees their free tier stop
                      working with nothing in any log to say why.
  the address book    the gateway forwards to an address it remembers. A
                      second dict of addresses is a second answer to "where is
                      this tenant", and the stale one is the one that gets
                      used, because the fresh one belongs to whichever handler
                      just ran.
  the maintenance mark  it has to be checked before a start and set before a
                      task, and the spec's rule is that no container starts
                      while it is set. A mark one handler sets and another
                      forgets to read is not a mark.

WHAT THIS MODULE IS NOT. It makes no admission decision: `Fleet.admit` does
that, in hosted/core/idle.py, on an injected clock, and group B tested it
there. `Launcher` calls `Fleet` and the spawner; it does not re-derive what
they already decide. The one place it looks like it decides something is
`resync`, which refuses to adopt a container at an address no project id
derives -- and that is a refusal, not a decision.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from hosted import log
from hosted.core import idle
from hosted.core import tenant as tenant_core
from hosted.gateway.spawner_client import SpawnerBusy, SpawnerClient, SpawnerError
from hosted.ports.control import ControlStore, Tenant
from hosted.ports.runtime import RunningContainer

_LOG = log.get(__name__)

DISABLED_MESSAGE = "This account is disabled."


class NotActive(RuntimeError):
    """The tenant's status is not `active`. Never issue them a token."""


class InMaintenance(RuntimeError):
    """A backup, restore, archive or inspect holds this tenant."""


class StartFailed(RuntimeError):
    """The container did not start. The caller sends the spec's sentence."""


class Launcher:
    def __init__(self, *, store: ControlStore, spawner: SpawnerClient,
                 fleet: idle.Fleet, now: Callable[[], float] = time.time) -> None:
        self._store = store
        self._spawner = spawner
        self._fleet = fleet
        # E2's own logic never reads this: every clock decision it makes is
        # a delegate's (Fleet.adopt, Fleet.set_status, ControlDb's own
        # `now`). It is stored anyway because the brief's signature takes
        # it, and E1 and E3 build on this class and inject the same fake
        # clock every other object in this group's tests takes -- a
        # Launcher built with `time.time` in one test and a fake clock via a
        # sibling object would be two clocks disagreeing about "now" inside
        # one wired gateway.
        self._now = now
        self._addresses: dict[str, RunningContainer] = {}
        self._maintenance: set[str] = set()
        # One start at a time per tenant, and one waiter-visible event per
        # start. DockerRuntime.start holds its own per-tenant lock as well;
        # this one is here because the gateway must not send the spawner two
        # starts it will serialise anyway while a browser waits on both.
        self._starts: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def fleet(self) -> idle.Fleet:
        return self._fleet

    def address(self, tenant_id: str) -> RunningContainer | None:
        return self._addresses.get(tenant_id)

    def in_maintenance(self, tenant_id: str) -> bool:
        return tenant_id in self._maintenance

    def mark_maintenance(self, tenant_id: str) -> None:
        self._maintenance.add(tenant_id)
        _LOG.info("maintenance on tenant=%s", tenant_id)

    def clear_maintenance(self, tenant_id: str) -> None:
        self._maintenance.discard(tenant_id)
        _LOG.info("maintenance off tenant=%s", tenant_id)

    def require_active(self, tenant: Tenant) -> None:
        if tenant.status != "active":
            raise NotActive(DISABLED_MESSAGE)

    async def ensure_tenant(self, *, sub: str, email: str,
                            timezone: str) -> tuple[Tenant, bool]:
        """The tenant behind a verified identity, creating the row on first
        login. Returns (tenant, created).

        THE TIME ZONE IS WRITTEN ON FIRST LOGIN AND NEVER AGAIN. The spec:
        "records the browser's time zone; later logins leave the time zone
        alone, because /account owns it." A tenant who set Asia/Shanghai on
        /account and then signs in from a laptop still on UTC must not be
        moved back by the browser.

        PROVISIONING IS CALLED ONLY FOR A ROW THIS CALL CREATED, and a failure
        of it is not fatal. DockerRuntime.start provisions before every start
        ("it only creates what is missing, so a failed first provision is
        repaired on the next start"), so the worst case of a provision that
        raises here is that the tenant's first request does the work instead.
        The row is what must not be lost -- it holds the project id, and a
        project id is never reissued.
        """
        found = self._store.tenant_by_sub(sub)
        if found is not None:
            self.require_active(found)
            return (found, False)
        record = self._store.create_tenant(
            sub=sub, email=email, timezone=tenant_core.normalise_timezone(timezone))
        _LOG.info("created tenant=%s project=%s zone=%s",
                  record.id, record.project_id, record.timezone)
        try:
            await self._spawner.provision(record.id, record.project_id)
        except (SpawnerError, OSError) as exc:
            _LOG.warning("first provision of tenant=%s failed (%s); the next "
                         "start provisions again", record.id, exc)
        return (record, True)

    async def start(self, tenant: Tenant) -> RunningContainer:
        """Issue a fresh token and ask the spawner for a container.

        THE THREE REFUSALS COME BEFORE THE TOKEN. A token is a live credential
        against the platform's own key, so a disabled tenant, a deleted tenant
        and a tenant under maintenance must not be issued one even if the
        start that follows would have failed anyway. The order is: status,
        maintenance, then issue.

        MAINTENANCE IS CHECKED TWICE: once here, before a second caller waits
        on the lock at all, so the common case fails fast without contending
        for it; and again just inside the lock, because a mark can land while
        a second caller is queued on `await lock.acquire()` -- the first
        check ran before the mark existed, and without the second one this
        caller would resume holding the lock with a now-stale answer and
        issue a token for a tenant a task, backup, restore or archive
        already owns. The spawner's own `busy` answer is still the real
        backstop (the mark is enforced there so it survives a gateway
        restart), so what the second check narrows is a token issue, not a
        container start reaching a held tenant.

        A FAILED START DOES NOT REVOKE THE TOKEN IT ISSUED, and that is
        deliberate. `start` can fail after the spawner has already created the
        container -- a timeout on the socket read, a gateway restart mid-call
        -- and revoking then would 401 the model calls of a container that is
        up and serving its tenant. The next successful start issues a new
        token, which revokes this one in the same transaction, and `stop`
        revokes unconditionally. The window is one container's lifetime, the
        token is bound to one tenant, and the alternative breaks a working
        tenant to tidy a row.
        """
        self.require_active(tenant)
        if self.in_maintenance(tenant.id):
            raise InMaintenance(idle.MAINTENANCE_MESSAGE)
        lock = self._locks.setdefault(tenant.id, asyncio.Lock())
        async with lock:
            if self.in_maintenance(tenant.id):
                raise InMaintenance(idle.MAINTENANCE_MESSAGE)
            running = self._addresses.get(tenant.id)
            if running is not None and self._fleet.running_status(tenant.id) == idle.RUNNING:
                return running
            return await self._start_locked(tenant)

    async def _start_locked(self, tenant: Tenant) -> RunningContainer:
        event = asyncio.Event()
        self._starts[tenant.id] = event
        self._fleet.set_status(tenant.id, idle.STARTING)
        token = self._store.issue_token(tenant.id)
        try:
            running = await asyncio.wait_for(
                self._spawner.start(tenant.id, tenant.project_id,
                                    tenant.timezone, token),
                idle.START_TIMEOUT_SECONDS)
        except SpawnerBusy as exc:
            self._forget_running(tenant.id)
            raise InMaintenance(idle.MAINTENANCE_MESSAGE) from exc
        except (TimeoutError, SpawnerError, OSError) as exc:
            self._forget_running(tenant.id)
            _LOG.warning("start of tenant=%s failed: %s", tenant.id, exc)
            raise StartFailed(idle.START_TIMEOUT_MESSAGE) from exc
        finally:
            self._starts.pop(tenant.id, None)
            event.set()
        self._addresses[tenant.id] = running
        self._fleet.set_status(tenant.id, idle.RUNNING)
        _LOG.info("tenant=%s running at %s:%s token=%s",
                  tenant.id, running.address, running.port, log.redact(token))
        return running

    async def wait_for_start(self, tenant_id: str) -> RunningContainer | None:
        """Wait for a start another request began. None when it did not
        finish in the budget or finished without producing a container."""
        event = self._starts.get(tenant_id)
        if event is None:
            return self._addresses.get(tenant_id)
        try:
            await asyncio.wait_for(event.wait(), idle.START_TIMEOUT_SECONDS)
        except TimeoutError:
            return None
        return self._addresses.get(tenant_id)

    async def stop(self, tenant_id: str) -> None:
        """Stop the container and revoke the token that was in it.

        Revoke FIRST. Between the revoke and the spawner's answer the
        container is still up and can still call the proxy; after the revoke
        it cannot. Doing it the other way round leaves a window whose length
        is a Docker stop timeout, and the spec's sentence -- "Stopping the
        container, disabling the tenant or deleting the tenant revokes the
        token" -- is about the token, not about the order two things happened
        to be written in.
        """
        self._store.revoke_tokens(tenant_id)
        self._forget_running(tenant_id)
        try:
            await self._spawner.stop(tenant_id)
        except (SpawnerError, OSError) as exc:
            # The token is already revoked and the address is already
            # forgotten, so the tenant is safe either way. A container that
            # survives this is picked up by the next resync.
            #
            # UNDECIDED, FOR E3: "picked up" is not "fixed". If the container
            # really did survive, the next resync finds it active at the
            # address its project id derives and re-adopts it (RUNNING), and
            # `start`'s fast path then hands that same container straight
            # back to every request -- with no token, because nothing on
            # this path re-issues one. The dashboard loads and every model
            # call 401s, with no self-healing until an idle stop or an
            # operator `restart-all`. E2 does not decide which of the two
            # fixes is right -- `resync` refusing to adopt a tenant with no
            # live token, or a failed stop here marking the tenant for a
            # forced restart -- because E3 owns the refused-connection
            # ladder this would slot into. This paragraph is that decision
            # waiting to be made on purpose rather than found by an operator.
            _LOG.warning("stop of tenant=%s failed: %s", tenant_id, exc)

    def _forget_running(self, tenant_id: str) -> None:
        self._addresses.pop(tenant_id, None)
        self._fleet.set_status(tenant_id, idle.STOPPED)

    async def spawner_task(self, tenant_id: str, task: str,
                           project_id: int = 0) -> dict:
        """The admin path's one door to the spawner's five tasks. It exists so
        that admin.py does not reach into a private attribute, and it
        deliberately takes the task name from admin.py's five-entry table
        rather than from anything on the wire."""
        return await self._spawner.task(tenant_id, task, project_id)

    async def resync(self) -> dict[str, RunningContainer]:
        """Ask the spawner what is actually running and believe that.

        Called at gateway startup (spec: "When the gateway starts, it asks the
        spawner to `list` running tenant containers and treats each as active
        from that moment") and again whenever a container the gateway believed
        running refuses a connection.

        ONE FUNCTION FOR BOTH, because Fleet.adopt already distinguishes them:
        it sets the idle clock only when the fleet has none, so a cold start
        gets fresh timers and a mid-life re-list leaves a running clock alone.
        A second function differing only in that would be two places to get it
        wrong.

        THE ADDRESS IS CHECKED AGAINST THE PROJECT ID. DockerRuntime.list
        already refuses an address outside the tenant subnet, and this is the
        stronger check it says it cannot make: here the tenant row is
        available, so the address can be required to be the one THAT tenant's
        project id derives. With C3 deferred there is no firewall on the
        bridge, so a container somebody else placed there is not a hypothetical
        -- and a container at a tenant's address that is not that tenant's
        container is an unauthenticated dashboard the gateway would forward a
        signed-in person to.
        """
        containers = await self._spawner.list()
        fresh: dict[str, RunningContainer] = {}
        for container in containers:
            record = self._store.tenant_by_id(container.tenant_id)
            if record is None:
                _LOG.warning("spawner lists tenant=%s, which control.db does "
                             "not know; not adopting it", container.tenant_id)
                continue
            if record.status != "active":
                _LOG.warning("spawner lists tenant=%s whose status is %s; "
                             "stopping it", record.id, record.status)
                await self.stop(record.id)
                continue
            expected = tenant_core.address_for_project(record.project_id)
            if container.address != expected:
                _LOG.warning("tenant=%s is at %s, not the %s its project id "
                             "derives; not adopting it",
                             record.id, container.address, expected)
                continue
            fresh[record.id] = container
        for tenant_id in list(self._addresses):
            if tenant_id not in fresh:
                self._forget_running(tenant_id)
        for tenant_id, container in fresh.items():
            self._addresses[tenant_id] = container
            self._fleet.adopt(tenant_id)
        return dict(self._addresses)

    async def restart_all(self) -> list[str]:
        """upgrade.sh --now: every running tenant gets a new container from
        the new image, a fresh token and a recorded address.

        The list is taken from the SPAWNER, not from the fleet, because the
        point of the call is that the images changed under a gateway that may
        itself have just restarted.
        """
        restarted: list[str] = []
        for tenant_id in sorted(await self.resync()):
            record = self._store.tenant_by_id(tenant_id)
            if record is None or record.status != "active":
                continue
            await self.stop(tenant_id)
            try:
                await self.start(record)
            except (NotActive, InMaintenance, StartFailed) as exc:
                _LOG.warning("restart of tenant=%s failed: %s", tenant_id, exc)
                continue
            restarted.append(tenant_id)
        return restarted
