"""The launcher: who gets a token, who gets a container, and who does not.

A FAKE SPAWNER, NOT A FAKE LAUNCHER. Every test here drives the real
Launcher against a recording spawner and a real ControlDb on a temp file, and
then reads what the spawner was asked for. Nothing asserts that a method was
called; the assertions are on the requests that reached the wire-facing
object, which is the only thing the real spawner would ever see.

asyncio.run around each coroutine: the repo has no pytest-asyncio and two
sockets did not need one (test_internal_api.py). Neither does this.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from gatewaylib import Clock, FakeSpawner, ops

from hosted.core import idle
from hosted.core.tenant import token_hash
from hosted.gateway.launch import (
    DISABLED_MESSAGE,
    InMaintenance,
    Launcher,
    NotActive,
    StartFailed,
)
from hosted.gateway.spawner_client import SpawnerBusy, SpawnerError
from hosted.gateway.store import ControlDb
from hosted.ports.runtime import RunningContainer


@pytest.fixture
def made(tmp_path):
    """A database path, a store, a fake spawner, a fleet on a fake clock and a
    Launcher. The path is yielded too, for the one test that counts rows with
    raw SQL rather than reaching into ControlDb's connection."""
    path = tmp_path / "control.db"
    store = ControlDb(path)
    spawner = FakeSpawner()
    clock = Clock()
    fleet = idle.Fleet(clock, max_running=2)
    launcher = Launcher(store=store, spawner=spawner, fleet=fleet, now=clock)
    yield path, store, spawner, clock, fleet, launcher
    store.close()


def test_a_first_login_creates_the_row_and_provisions_it(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        return await launcher.ensure_tenant(sub="sub-1", email="mei@example.com",
                                            timezone="Asia/Shanghai")

    record, created = asyncio.run(run())
    assert created is True
    assert record.timezone == "Asia/Shanghai"
    assert store.tenant_by_sub("sub-1").id == record.id
    assert ops(spawner, "provision") == [
        {"op": "provision", "tenant_id": record.id, "project_id": record.project_id}]


def test_a_second_login_does_not_reprovision_or_move_the_time_zone(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        first, _ = await launcher.ensure_tenant(
            sub="sub-1", email="mei@example.com", timezone="Asia/Shanghai")
        store.set_timezone(first.id, "Europe/Berlin")
        return await launcher.ensure_tenant(
            sub="sub-1", email="mei@example.com", timezone="UTC")

    record, created = asyncio.run(run())
    assert created is False
    assert record.timezone == "Europe/Berlin"
    assert len(ops(spawner, "provision")) == 1


def test_an_unknown_zone_from_the_browser_is_stored_as_utc(made):
    _path, _store, _spawner, _clock, _fleet, launcher = made

    async def run():
        return await launcher.ensure_tenant(sub="sub-1", email="mei@example.com",
                                            timezone="Middle/Earth")

    record, _created = asyncio.run(run())
    assert record.timezone == "UTC"


def test_a_failed_first_provision_still_leaves_the_row(made):
    _path, store, spawner, _clock, _fleet, launcher = made
    spawner.fail_provision = SpawnerError("no such image")

    async def run():
        return await launcher.ensure_tenant(sub="sub-1", email="mei@example.com",
                                            timezone="UTC")

    record, created = asyncio.run(run())
    assert created is True
    assert store.tenant_by_id(record.id) is not None


def test_a_disabled_tenant_cannot_log_in(made):
    _path, store, _spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="sub-1", email="m@x.com",
                                                 timezone="UTC")
        store.set_status(record.id, "disabled")
        with pytest.raises(NotActive) as caught:
            await launcher.ensure_tenant(sub="sub-1", email="m@x.com", timezone="UTC")
        return str(caught.value)

    assert asyncio.run(run()) == DISABLED_MESSAGE


def test_the_token_the_spawner_was_handed_is_the_one_control_db_holds(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        return record

    record = asyncio.run(run())
    sent = ops(spawner, "start")[0]["token"]
    assert store.tenant_for_token_hash(token_hash(sent)) == (record.id, "active")


def test_a_second_start_revokes_the_first_tenants_token(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        # The fleet says RUNNING, so start() returns the cached container
        # rather than issuing again -- a stop is what makes the next start
        # real, which is also what restart_all does.
        await launcher.stop(record.id)
        await launcher.start(record)
        return record

    record = asyncio.run(run())
    first, second = (r["token"] for r in ops(spawner, "start"))
    assert first != second
    assert store.tenant_for_token_hash(token_hash(first)) is None
    assert store.tenant_for_token_hash(token_hash(second)) == (record.id, "active")


def test_a_disabled_tenant_is_never_issued_a_token_and_never_started(made):
    path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        store.set_status(record.id, "disabled")
        stale = store.tenant_by_id(record.id)
        with pytest.raises(NotActive):
            await launcher.start(stale)
        return record

    record = asyncio.run(run())
    assert ops(spawner, "start") == []
    # Raw SQL, against the path the fixture yielded: "no live token exists"
    # has no public expression on ControlStore, and counting rows is the only
    # way to say it without reaching into ControlDb's own connection.
    with sqlite3.connect(path) as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM proxy_token WHERE tenant_id = ? "
            "AND revoked_at IS NULL", (record.id,)).fetchone()[0]
    assert live == 0


def test_a_tenant_in_maintenance_is_never_issued_a_token_and_never_started(made):
    _path, _store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        launcher.mark_maintenance(record.id)
        with pytest.raises(InMaintenance):
            await launcher.start(record)
        launcher.clear_maintenance(record.id)
        await launcher.start(record)

    asyncio.run(run())
    assert len(ops(spawner, "start")) == 1


def test_a_start_the_spawner_calls_busy_reads_as_maintenance(made):
    _path, _store, spawner, _clock, _fleet, launcher = made
    spawner.fail_start = SpawnerBusy("a task container holds this tenant")

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        with pytest.raises(InMaintenance) as caught:
            await launcher.start(record)
        return str(caught.value)

    assert asyncio.run(run()) == idle.MAINTENANCE_MESSAGE


def test_a_failed_start_leaves_the_tenant_stopped_and_startable(made):
    _path, _store, spawner, _clock, fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        spawner.fail_start = SpawnerError("no such image")
        with pytest.raises(StartFailed) as caught:
            await launcher.start(record)
        message = str(caught.value)
        assert fleet.running_status(record.id) == idle.STOPPED
        assert launcher.address(record.id) is None
        spawner.fail_start = None
        running = await launcher.start(record)
        return message, running

    message, running = asyncio.run(run())
    assert message == idle.START_TIMEOUT_MESSAGE
    assert running.port == 7777


def test_stop_revokes_before_it_asks_the_spawner(made):
    _path, store, spawner, _clock, _fleet, launcher = made
    order: list[str] = []
    original = spawner.stop

    async def watched(tenant_id: str) -> None:
        order.append("spawner-stop")
        await original(tenant_id)

    spawner.stop = watched

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        sent = ops(spawner, "start")[0]["token"]

        def revoked() -> bool:
            return store.tenant_for_token_hash(token_hash(sent)) is None

        async def watcher() -> None:
            # Sampled the moment the spawner is asked: the revoke must already
            # have happened, not be racing it.
            order.append("revoked" if revoked() else "still-live")

        spawner.stop = lambda tid: _both(watched(tid), watcher())
        await launcher.stop(record.id)

    async def _both(first, second):
        await second
        await first

    asyncio.run(run())
    assert order[0] == "revoked"


def test_a_second_request_waits_for_the_start_the_first_one_began(made):
    _path, _store, spawner, _clock, fleet, launcher = made
    spawner.start_delay = 0.05

    async def run():
        record, _ = await launcher.ensure_tenant(sub="s", email="m@x.com",
                                                 timezone="UTC")
        first = asyncio.create_task(launcher.start(record))
        await asyncio.sleep(0)          # let the start reach STARTING
        assert fleet.running_status(record.id) == idle.STARTING
        waited = await launcher.wait_for_start(record.id)
        started = await first
        return waited, started

    waited, started = asyncio.run(run())
    assert waited == started
    assert len(ops(spawner, "start")) == 1


def test_resync_adopts_what_the_spawner_reports_and_forgets_what_it_does_not(made):
    _path, _store, spawner, clock, fleet, launcher = made

    async def run():
        one, _ = await launcher.ensure_tenant(sub="a", email="a@x.com", timezone="UTC")
        two, _ = await launcher.ensure_tenant(sub="b", email="b@x.com", timezone="UTC")
        await launcher.start(one)
        await launcher.start(two)
        # The spawner loses one of them: a kernel OOM kill, say.
        spawner.running.pop(two.id)
        clock.t += 60
        adopted = await launcher.resync()
        return one, two, adopted

    one, two, adopted = asyncio.run(run())
    assert sorted(adopted) == [one.id]
    assert launcher.address(two.id) is None
    assert fleet.running_status(two.id) == idle.STOPPED


def test_resync_at_startup_gives_every_adopted_container_a_fresh_idle_clock(made):
    _path, store, spawner, clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="a", email="a@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        # A new gateway process: same spawner, same store, empty fleet.
        cold_fleet = idle.Fleet(clock, max_running=2)
        cold = Launcher(store=store, spawner=spawner, fleet=cold_fleet, now=clock)
        clock.t += 3600                 # an hour passes with the gateway down
        await cold.resync()
        return record, cold_fleet

    record, cold_fleet = asyncio.run(run())
    assert cold_fleet.running_status(record.id) == idle.RUNNING
    assert cold_fleet.idle_stops() == []


def test_resync_refuses_a_container_at_an_address_its_project_id_does_not_derive(made):
    _path, _store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="a", email="a@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        spawner.running[record.id] = RunningContainer(
            tenant_id=record.id, address="10.88.9.9", port=7777)
        await launcher.resync()
        return record

    record = asyncio.run(run())
    assert launcher.address(record.id) is None


def test_resync_stops_a_container_whose_tenant_is_no_longer_active(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        record, _ = await launcher.ensure_tenant(sub="a", email="a@x.com",
                                                 timezone="UTC")
        await launcher.start(record)
        store.set_status(record.id, "disabled")
        await launcher.resync()
        return record

    record = asyncio.run(run())
    assert launcher.address(record.id) is None
    assert ops(spawner, "stop") == [{"op": "stop", "tenant_id": record.id}]


def test_restart_all_leaves_each_tenant_with_one_container_and_the_newest_token(made):
    _path, store, spawner, _clock, _fleet, launcher = made

    async def run():
        one, _ = await launcher.ensure_tenant(sub="a", email="a@x.com", timezone="UTC")
        two, _ = await launcher.ensure_tenant(sub="b", email="b@x.com", timezone="UTC")
        await launcher.start(one)
        await launcher.start(two)
        return one, two, await launcher.restart_all()

    one, two, restarted = asyncio.run(run())
    assert restarted == sorted([one.id, two.id])
    for record in (one, two):
        tokens = [r["token"] for r in ops(spawner, "start") if r["tenant_id"] == record.id]
        assert len(tokens) == 2
        assert store.tenant_for_token_hash(token_hash(tokens[0])) is None
        assert store.tenant_for_token_hash(token_hash(tokens[1])) == (record.id, "active")
        assert launcher.address(record.id).address == spawner.running[record.id].address
