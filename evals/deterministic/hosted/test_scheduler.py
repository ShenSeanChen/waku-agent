"""The gateway's scheduler -- spec 026 task A3, acceptance 1, 2 and 3.

Every 30 seconds the scheduler starts each tenant that has an event waiting
or a wakeup that is due, and hands the tenant's events to its container. The
tests drive the real Scheduler, Launcher, Fleet, ControlDb and ContainerApi.
The spawner is the FakeSpawner, the clock is moved by hand, and the container
is a small HTTP server on loopback. One test uses the stock waku dashboard as
the container, so the request the gateway sends is read by the code that will
read it on the VM.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import ThreadingHTTPServer
from typing import Self

import aiohttp
import pytest
from gatewaylib import AlwaysOnContainer, Clock, FakeSpawner, ops

from hosted.core import idle
from hosted.gateway.container_api import ContainerApi
from hosted.gateway.launch import Launcher
from hosted.gateway.scheduler import TICK_SECONDS, Scheduler
from hosted.gateway.spawner_client import SpawnerError
from hosted.gateway.store import ControlDb

FRIDAY_0900 = 1_700_300_000.0
EVENT = {"source": "slack", "external_id": "Ev0001", "session_id": "slack:C1:171.5",
         "payload": {"text": "remind the channel on Friday at 09:00"}}


class Rig:
    """Everything one test drives, built inside its event loop."""

    def __init__(self, tmp_path, *, port: int, max_running: int = 4) -> None:
        self.clock = Clock()
        self.store = ControlDb(tmp_path / "control.db", now=self.clock)
        self.spawner = FakeSpawner()
        self.spawner.address, self.spawner.port = "127.0.0.1", port
        self.fleet = idle.Fleet(self.clock, max_running=max_running)
        self.launcher = Launcher(store=self.store, spawner=self.spawner, fleet=self.fleet,
                                 now=self.clock)
        self.session: aiohttp.ClientSession | None = None
        self.scheduler: Scheduler | None = None

    async def __aenter__(self) -> Self:
        self.session = aiohttp.ClientSession()
        self.scheduler = Scheduler(store=self.store, launcher=self.launcher,
                                   containers=ContainerApi(self.session), now=self.clock)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.session.close()
        self.store.close()

    def tenant(self, sub: str = "sub-ana"):
        return self.store.create_tenant(sub=sub, email=f"{sub}@example.com", timezone="UTC")

    def add(self, tenant_id: str, **changes) -> bool:
        return self.store.add_event(tenant_id, **{**EVENT, **changes})


@pytest.fixture
def container():
    fake = AlwaysOnContainer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def short_ready_wait(monkeypatch):
    """A container that is not listening is given up on at once, so no test
    waits thirty seconds for a port nothing will bind."""
    monkeypatch.setattr(idle, "READY_TIMEOUT_SECONDS", 0)


def test_a_sleeping_tenant_is_started_once_and_handed_its_event(tmp_path, container):
    """Acceptance 1."""
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            assert rig.fleet.running_status(ana.id) == idle.STOPPED
            await rig.scheduler.tick()
            await rig.scheduler.tick()
            return rig, ana, rig.store.pending_events(ana.id)

    rig, ana, pending = asyncio.run(run())
    assert [r["tenant_id"] for r in ops(rig.spawner, "start")] == [ana.id]
    assert container.handed == [[EVENT]]
    assert pending == []
    assert rig.fleet.running_status(ana.id) == idle.RUNNING


def test_a_sleeping_tenants_redelivered_event_is_handed_over_once(tmp_path, container):
    """Acceptance 2: the same Slack event id twice is one row and one
    hand-over, whether the second copy arrives before the hand-over or after."""
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            assert rig.add(ana.id) is True
            assert rig.add(ana.id) is False
            await rig.scheduler.tick()
            assert rig.add(ana.id) is False
            await rig.scheduler.tick()

    asyncio.run(run())
    assert container.handed == [[EVENT]]


def test_a_sleeping_tenants_events_arrive_in_order_in_one_request(tmp_path, container):
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            for name in ("Ev1", "Ev2", "Ev3"):
                rig.add(ana.id, external_id=name)
            await rig.scheduler.tick()

    asyncio.run(run())
    assert [[e["external_id"] for e in batch] for batch in container.handed] == [
        ["Ev1", "Ev2", "Ev3"]]


def test_an_event_is_handed_only_when_the_container_answered_200(tmp_path, container):
    container.events_status = 500

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            await rig.scheduler.tick()
            after_refusal = [e.status for e in rig.store.pending_events(ana.id)]
            container.events_status = 200
            await rig.scheduler.tick()
            return rig, after_refusal, rig.store.pending_events(ana.id)

    rig, after_refusal, after_ok = asyncio.run(run())
    assert after_refusal == ["pending"]
    assert after_ok == []
    # The container was offered the event twice and started once.
    assert container.handed == [[EVENT], [EVENT]]
    assert len(ops(rig.spawner, "start")) == 1


def test_an_event_is_not_handed_to_a_container_that_is_not_listening(tmp_path, container):
    """The spawner answers before the dashboard has bound its port. The row
    stays pending and the next tick offers it again."""
    port = container.port
    container.stop()

    async def run():
        async with Rig(tmp_path, port=port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            await rig.scheduler.tick()
            before = [e.status for e in rig.store.pending_events(ana.id)]
            container.start(port)
            await rig.scheduler.tick()
            return before, rig.store.pending_events(ana.id)

    before, after = asyncio.run(run())
    assert before == ["pending"]
    assert after == []
    assert container.handed == [[EVENT]]


def test_a_handed_event_for_one_tenant_never_reaches_another(tmp_path, container):
    other = AlwaysOnContainer()
    other.start()

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana, ben = rig.tenant(), rig.tenant("sub-ben")
            rig.add(ana.id, external_id="for-ana")
            await rig.scheduler.tick()
            rig.spawner.port = other.port          # Ben's container
            rig.add(ben.id, external_id="for-ben")
            await rig.scheduler.tick()

    try:
        asyncio.run(run())
    finally:
        other.stop()
    assert [[e["external_id"] for e in b] for b in container.handed] == [["for-ana"]]
    assert [[e["external_id"] for e in b] for b in other.handed] == [["for-ben"]]


def test_a_timer_wakes_a_sleeping_tenant_at_its_time_and_not_before(tmp_path, container):
    """Acceptance 3: the container reported a timer for Friday 09:00 and was
    stopped. The spawner is asked to start it when the clock reaches it."""
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.store.set_wakeup(ana.id, FRIDAY_0900)
            rig.clock.t = FRIDAY_0900 - 1
            await rig.scheduler.tick()
            early = len(ops(rig.spawner, "start"))
            rig.clock.t = FRIDAY_0900
            await rig.scheduler.tick()
            await rig.scheduler.tick()
            return rig, ana, early, rig.store.wakeup(ana.id)

    rig, ana, early, wakeup = asyncio.run(run())
    assert early == 0
    assert [r["tenant_id"] for r in ops(rig.spawner, "start")] == [ana.id]
    # The container's own timer loop fires the timer. Nothing is handed over.
    assert container.handed == []
    assert wakeup is None


def test_a_timer_wakeup_is_kept_until_the_container_listens(tmp_path, container):
    """A start that produced no listening dashboard has fired no timer, so the
    wakeup must still be there for the next tick."""
    port = container.port
    container.stop()

    async def run():
        async with Rig(tmp_path, port=port) as rig:
            ana = rig.tenant()
            rig.store.set_wakeup(ana.id, FRIDAY_0900)
            rig.clock.t = FRIDAY_0900
            await rig.scheduler.tick()
            kept = rig.store.wakeup(ana.id)
            container.start(port)
            await rig.scheduler.tick()
            return kept, rig.store.wakeup(ana.id)

    assert asyncio.run(run()) == (FRIDAY_0900, None)


def test_a_timer_for_a_tenant_that_is_already_running_starts_nothing(tmp_path, container):
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            assert rig.fleet.admit(ana.id, background=False).action == "start"
            await rig.launcher.start(ana)
            rig.store.set_wakeup(ana.id, FRIDAY_0900)
            rig.clock.t = FRIDAY_0900
            await rig.scheduler.tick()
            return rig, rig.store.wakeup(ana.id)

    rig, wakeup = asyncio.run(run())
    assert len(ops(rig.spawner, "start")) == 1
    assert wakeup is None


@pytest.mark.parametrize("status", ["disabled", "deleted"])
def test_a_sleeping_tenant_that_is_not_active_is_not_started(tmp_path, container, status):
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            rig.store.set_wakeup(ana.id, FRIDAY_0900)
            rig.store.set_status(ana.id, status)
            rig.clock.t = FRIDAY_0900
            await rig.scheduler.tick()
            return rig, [e.status for e in rig.store.pending_events(ana.id)]

    rig, statuses = asyncio.run(run())
    assert ops(rig.spawner, "start") == []
    assert container.handed == []
    assert statuses == ["pending"]


def test_a_sleeping_tenant_under_maintenance_is_not_started(tmp_path, container):
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            rig.launcher.mark_maintenance(ana.id)
            await rig.scheduler.tick()
            held = (len(ops(rig.spawner, "start")), len(rig.store.pending_events(ana.id)))
            rig.launcher.clear_maintenance(ana.id)
            await rig.scheduler.tick()
            return held, rig.store.pending_events(ana.id)

    held, after = asyncio.run(run())
    assert held == (0, 1)
    assert after == []


def test_a_sleeping_tenant_waits_when_every_slot_is_busy(tmp_path, container):
    """The scheduler starts a container through Fleet.admit, like a request.
    On a VM with one slot whose container is mid-turn, the event waits."""
    async def run():
        async with Rig(tmp_path, port=container.port, max_running=1) as rig:
            ana, ben = rig.tenant(), rig.tenant("sub-ben")
            assert rig.fleet.admit(ben.id, background=False).action == "start"
            await rig.launcher.start(ben)
            rig.fleet.enter(ben.id)                 # Ben is mid-turn
            rig.add(ana.id)
            await rig.scheduler.tick()
            waiting = [r["tenant_id"] for r in ops(rig.spawner, "start")]
            rig.fleet.leave(ben.id)
            await rig.scheduler.tick()
            return rig, ana, ben, waiting

    rig, ana, ben, waiting = asyncio.run(run())
    assert waiting == [ben.id]
    assert [r["tenant_id"] for r in ops(rig.spawner, "start")] == [ben.id, ana.id]
    assert [r["tenant_id"] for r in ops(rig.spawner, "stop")] == [ben.id]
    assert len(rig.fleet.running()) == 1


def test_a_start_that_fails_leaves_the_event_for_the_next_tick(tmp_path, container):
    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            rig.spawner.fail_start = SpawnerError("no such image")
            await rig.scheduler.tick()
            failed = len(rig.store.pending_events(ana.id))
            rig.spawner.fail_start = None
            await rig.scheduler.tick()
            return failed, rig.store.pending_events(ana.id)

    assert asyncio.run(run()) == (1, [])


def test_the_loop_ticks_every_thirty_seconds_and_survives_a_tick_that_raises(
        tmp_path, container):
    assert TICK_SECONDS == 30
    slept: list[float] = []

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            real_pending = rig.store.tenants_with_pending_events
            calls = {"n": 0}

            def flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("control.db was busy")
                return real_pending()

            rig.store.tenants_with_pending_events = flaky

            async def sleep(seconds):
                slept.append(seconds)
                if len(slept) == 3:
                    raise asyncio.CancelledError

            loop = Scheduler(store=rig.store, launcher=rig.launcher,
                             containers=ContainerApi(rig.session), now=rig.clock, sleep=sleep)
            with pytest.raises(asyncio.CancelledError):
                await loop.run()
            return rig.store.pending_events(ana.id)

    assert asyncio.run(run()) == []
    assert slept == [30, 30, 30]


def test_a_sleeping_tenants_event_reaches_the_stock_dashboards_inbox(
        tmp_path, monkeypatch):
    """Acceptance 1 across the licence line. The container here is the real
    waku dashboard handler (MIT) with WAKU_HOSTED=1, so the request the
    gateway (Elastic License 2.0) sends is parsed by the code that parses it
    on the VM, and the report it reads back is the one that code writes."""
    from waku.config import Settings
    from waku.db import connect
    from waku.ops import dashboard

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.setenv("WAKU_HOSTED", "1")
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    Settings(home=home).ensure_home()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async def run():
        async with Rig(tmp_path, port=httpd.server_port) as rig:
            ana = rig.tenant()
            rig.add(ana.id)
            await rig.scheduler.tick()
            report = await ContainerApi(rig.session).next_work(rig.launcher.address(ana.id))
            return rig.store.pending_events(ana.id), report

    try:
        pending, report = asyncio.run(run())
    finally:
        httpd.shutdown()
        httpd.server_close()
    conn = connect(home)
    rows = [tuple(row) for row in conn.execute(
        "SELECT source, external_id, session_id, payload, status FROM events")]
    conn.close()
    assert rows == [("slack", "Ev0001", "slack:C1:171.5",
                     '{"text": "remind the channel on Friday at 09:00"}', "pending")]
    assert pending == []
    assert report == {"next_timer_at": None, "events": 1, "outbox": 0, "running": 0}


# --- a kick: a tick now, for an event that just arrived (spec 026 C2) ------

def _kicked_rig(rig, sleep) -> Scheduler:
    return Scheduler(store=rig.store, launcher=rig.launcher,
                     containers=ContainerApi(rig.session), now=rig.clock, sleep=sleep)


def test_a_kick_hands_a_new_event_over_without_waiting_for_the_loop(tmp_path, container):
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            scheduler = _kicked_rig(rig, sleep)
            ana = rig.tenant()
            rig.add(ana.id)
            scheduler.kick()
            await scheduler.settled()
            return rig.store.pending_events(ana.id)

    assert asyncio.run(run()) == []
    assert container.handed == [[EVENT]]
    assert slept == []      # handed on the first try, so nothing was waited for


def test_a_kick_tries_again_every_two_seconds_until_the_container_takes_the_event(
        tmp_path, container):
    """The container a kick starts is not ready when the first tick ends.
    Here it refuses the first two hand-overs and takes the third."""
    container.events_status = 503
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            container.events_status = 200

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            scheduler = _kicked_rig(rig, sleep)
            ana = rig.tenant()
            rig.add(ana.id)
            scheduler.kick()
            await scheduler.settled()
            return rig, ana, rig.store.pending_events(ana.id)

    rig, ana, pending = asyncio.run(run())
    assert slept == [2.0, 2.0]
    assert pending == []
    assert len(container.handed) == 3
    assert [r["tenant_id"] for r in ops(rig.spawner, "start")] == [ana.id]


def test_a_kick_gives_up_after_ten_tries_and_leaves_the_event_for_the_loop(tmp_path,
                                                                           container):
    container.events_status = 503
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            scheduler = _kicked_rig(rig, sleep)
            ana = rig.tenant()
            rig.add(ana.id)
            scheduler.kick()
            await scheduler.settled()
            return [event.external_id for event in rig.store.pending_events(ana.id)]

    assert asyncio.run(run()) == ["Ev0001"]
    assert len(container.handed) == 10
    assert slept == [2.0] * 10


def test_a_second_kick_joins_the_one_under_way_and_no_two_ticks_overlap(tmp_path,
                                                                        container):
    """Two mentions arrive together. One kick runs, the second event gets its
    own full count of tries, and the tenant's container is started once."""
    container.events_status = 503
    slept: list[float] = []
    rigs: list[Rig] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 4:
            # A second event, and its kick, in the middle of the first's tries.
            rig = rigs[0]
            rig.add(rig.ana.id, external_id="Ev0002")
            rig.kicker.kick()

    async def run():
        async with Rig(tmp_path, port=container.port) as rig:
            rigs.append(rig)
            rig.kicker = _kicked_rig(rig, sleep)
            rig.ana = rig.tenant()
            rig.add(rig.ana.id)
            rig.kicker.kick()
            rig.kicker.kick()
            await rig.kicker.settled()
            return rig

    rig = asyncio.run(run())
    # Four tries, then the count starts again: ten more.
    assert len(slept) == 14
    assert len(container.handed) == 14
    assert len(ops(rig.spawner, "start")) == 1
