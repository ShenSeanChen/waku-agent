"""The once-a-minute idle loop -- spec 001 task E4, the idle half.

test_idle.py pins Fleet's decision on a fake clock. This file pins the caller
that decision never had: hosted/gateway/sweep.py's IdleLoop and
Launcher.stop_idle, driven against the real Launcher, a real ControlDb and the
FakeSpawner every other gateway test uses. The assertions are on what reached
the spawner, which is the only thing a real VM would see.

No test sleeps for a minute. The loop takes its `sleep` and the fleet takes
its clock, so a test moves time by hand.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from gatewaylib import Clock, FakeSpawner, ops, signed_in_on_the_tenant_host

from hosted.core import idle
from hosted.gateway.config import config_from_env, idle_seconds_from
from hosted.gateway.launch import Launcher
from hosted.gateway.store import ControlDb
from hosted.gateway.sweep import IdleLoop

QUIET_DISK = (100, 10, 90)          # shutil.disk_usage's (total, used, free)


def _quiet_disk(_path):
    return QUIET_DISK


class CountingFleet(idle.Fleet):
    """A real Fleet that counts how often it is asked for idle_stops."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.asked = 0

    def idle_stops(self) -> list[str]:
        self.asked += 1
        return super().idle_stops()


@pytest.fixture
def made(tmp_path):
    store = ControlDb(tmp_path / "control.db")
    spawner = FakeSpawner()
    clock = Clock()
    fleet = CountingFleet(clock, max_running=4)
    launcher = Launcher(store=store, spawner=spawner, fleet=fleet, now=clock)
    yield store, spawner, clock, fleet, launcher
    store.close()


async def _started(store, launcher, sub: str):
    """A tenant whose container is running, started the way a request starts
    it: Fleet.admit first, so the start is activity, then Launcher.start."""
    record = store.create_tenant(sub=sub, email=f"{sub}@example.com", timezone="UTC")
    assert launcher.fleet.admit(record.id, background=False).action == "start"
    await launcher.start(record)
    return record


def _loop(launcher, clock, tmp_path, *, sweeps: int, disk_usage=_quiet_disk):
    """An IdleLoop whose `sleep` moves the clock a minute and ends the loop
    after `sweeps` sweeps, the way the gateway's shutdown cancels it."""
    count = {"n": 0}

    async def sleep(seconds):
        if count["n"] == sweeps:
            raise asyncio.CancelledError
        count["n"] += 1
        clock.t += seconds

    return IdleLoop(launcher, disk_path=tmp_path, sleep=sleep, disk_usage=disk_usage)


async def _run_until_cancelled(loop: IdleLoop) -> None:
    with pytest.raises(asyncio.CancelledError):
        await loop.run()


# --- the loop calls the decision, once a minute ------------------------------

def test_the_loop_asks_the_fleet_once_a_minute(made, tmp_path):
    """The gap this task closes: idle_stops() had evals and no caller."""
    store, _spawner, clock, fleet, launcher = made

    async def run():
        await _started(store, launcher, "sub-1")
        loop = _loop(launcher, clock, tmp_path, sweeps=3)
        await _run_until_cancelled(loop)

    asyncio.run(run())
    assert fleet.asked == 3
    assert idle.SWEEP_SECONDS == 60


def test_an_idle_tenant_is_stopped_and_a_fresh_one_is_not(made, tmp_path):
    store, spawner, clock, _fleet, launcher = made

    async def run():
        old = await _started(store, launcher, "sub-old")
        clock.t += idle.IDLE_SECONDS - 5 * 60      # ten minutes idle so far
        new = await _started(store, launcher, "sub-new")
        # Six sweeps: the old tenant crosses fifteen minutes during them, the
        # new one is at six minutes when the loop ends.
        await _run_until_cancelled(_loop(launcher, clock, tmp_path, sweeps=6))
        return old.id, new.id

    old, new = asyncio.run(run())
    assert ops(spawner, "stop") == [{"op": "stop", "tenant_id": old}]
    assert launcher.fleet.running() == [new]
    assert launcher.address(old) is None


def test_a_tenant_with_a_request_open_is_never_stopped(made, tmp_path):
    """A turn can run longer than the window. Fleet.enter is what the
    forwarder calls for every forwarded request, streamed turns included."""
    store, spawner, clock, fleet, launcher = made

    async def run():
        busy = await _started(store, launcher, "sub-busy")
        fleet.enter(busy.id)
        clock.t += idle.IDLE_SECONDS * 3
        stopped_while_busy = await launcher.stop_idle()
        fleet.leave(busy.id)
        stopped_after = await launcher.stop_idle()
        return busy.id, stopped_while_busy, stopped_after

    busy, during, after = asyncio.run(run())
    assert during == []
    assert after == [busy]
    assert ops(spawner, "stop") == [{"op": "stop", "tenant_id": busy}]


def test_a_stop_revokes_the_token_the_container_held(made):
    """An idle stop is Launcher.stop, so the token goes with the container."""
    store, _spawner, clock, _fleet, launcher = made

    async def run():
        record = await _started(store, launcher, "sub-1")
        assert store.has_live_token(record.id)
        clock.t += idle.IDLE_SECONDS + 1
        await launcher.stop_idle()
        return record.id

    tenant_id = asyncio.run(run())
    assert not store.has_live_token(tenant_id)


def test_a_request_during_another_tenants_stop_saves_its_own_container(made):
    """THE RACE THE SECOND QUESTION CLOSES. Two tenants are idle; the loop
    stops them one at a time, and each stop awaits the spawner. A message
    from the second tenant arriving while the first is being stopped must
    keep the second container, though the list said to stop it."""
    store, spawner, clock, fleet, launcher = made
    spawner.stop_delay = 0.05

    async def run():
        first = await _started(store, launcher, "sub-a")
        second = await _started(store, launcher, "sub-b")
        clock.t += idle.IDLE_SECONDS + 1
        a, b = sorted([first.id, second.id])
        sweep = asyncio.create_task(launcher.stop_idle())
        await asyncio.sleep(0.01)                 # the first stop is in flight
        assert fleet.admit(b, background=False).action == "forward"
        stopped = await sweep
        return a, b, stopped

    a, b, stopped = asyncio.run(run())
    assert stopped == [a]
    assert [r["tenant_id"] for r in ops(spawner, "stop")] == [a]
    assert b in launcher.fleet.running()


def test_a_sweep_that_raises_does_not_end_the_loop(made, tmp_path, caplog):
    store, spawner, clock, _fleet, launcher = made
    calls = {"n": 0}
    real_stop_idle = launcher.stop_idle

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("spawner socket went away")
        return await real_stop_idle()

    launcher.stop_idle = flaky

    async def run():
        record = await _started(store, launcher, "sub-1")
        clock.t += idle.IDLE_SECONDS
        await _run_until_cancelled(_loop(launcher, clock, tmp_path, sweeps=2))
        return record.id

    with caplog.at_level(logging.ERROR):
        tenant_id = asyncio.run(run())
    assert calls["n"] == 2
    assert ops(spawner, "stop") == [{"op": "stop", "tenant_id": tenant_id}]
    assert "idle sweep failed" in caplog.text


def test_each_stop_is_logged_with_the_tenant(made, caplog):
    store, _spawner, clock, _fleet, launcher = made

    async def run():
        record = await _started(store, launcher, "sub-1")
        clock.t += 20 * 60
        await launcher.stop_idle()
        return record.id

    with caplog.at_level(logging.INFO, logger="hosted"):
        tenant_id = asyncio.run(run())
    assert f"idle stop tenant={tenant_id}" in caplog.text
    assert "for 20 minutes" in caplog.text


# --- after a stop, the next message starts a new container -------------------

def test_after_an_idle_stop_the_next_request_starts_a_new_container(wired, tmp_path):
    """The whole cycle through the real gateway and forwarder: sign in, go
    quiet past the window, one sweep stops the container, and the next real
    request goes through the existing start path -- a new token and a new
    `start` to the spawner, which is a new container from the current image."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        assert await wired.send("GET", "/api/data", host=host, cookie=cookie)
        wired.clock.t += idle.IDLE_SECONDS + 1
        loop = IdleLoop(wired.launcher, disk_path=tmp_path, disk_usage=_quiet_disk)
        stopped = await loop.sweep()
        paused = await wired.send("GET", "/api/data", host=host, cookie=cookie,
                                  headers={"X-Waku-Background": "1"})
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        await wired.stop()
        return tenant_id, stopped, paused, answer

    tenant_id, stopped, paused, answer = asyncio.run(run())
    assert stopped == [tenant_id]
    assert [r["tenant_id"] for r in ops(wired.spawner, "stop")] == [tenant_id]
    assert paused[0] != 200, "a background poll restarted a stopped container"
    assert answer[0] == 200
    starts = ops(wired.spawner, "start")
    assert len(starts) == 2
    assert starts[0]["token"] != starts[1]["token"]
    assert wired.fleet.running() == [tenant_id]


def test_a_streaming_turn_is_not_cut_off_by_a_sweep(wired, tmp_path):
    """Through the real forwarder: a turn is at the container when the clock
    passes the window and a sweep runs. Nothing is stopped and the turn
    finishes; the sweep after it stops the container."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        wired.container.delay = 0.5
        turn = asyncio.create_task(wired.send(
            "POST", "/api/chat/stream", host=host, cookie=cookie,
            body=b'{"m": "x"}',
            headers={"Content-Type": "application/json",
                     "Origin": f"https://{host}"}))
        await asyncio.sleep(0.2)          # the turn is at the container
        wired.clock.t += idle.IDLE_SECONDS * 2
        loop = IdleLoop(wired.launcher, disk_path=tmp_path, disk_usage=_quiet_disk)
        during = await loop.sweep()
        answer = await turn
        after = await loop.sweep()
        await wired.stop()
        return tenant_id, during, answer, after

    tenant_id, during, answer, after = asyncio.run(run())
    assert during == []
    assert answer[0] == 200
    assert b'data: {"n": 2}' in answer[2]
    assert after == [tenant_id]


# --- the window is configurable ----------------------------------------------

def test_the_fleet_uses_the_window_it_was_built_with():
    clock = Clock()
    fleet = idle.Fleet(clock, max_running=2, idle_seconds=5 * 60)
    fleet.adopt("abcdefghijkl")
    clock.t += 5 * 60 - 1
    assert fleet.idle_stops() == []
    clock.t += 1
    assert fleet.idle_stops() == ["abcdefghijkl"]


def test_waku_idle_minutes_defaults_to_fifteen_and_refuses_nonsense():
    assert idle_seconds_from(None) == 15 * 60
    assert idle_seconds_from("") == 15 * 60
    assert idle_seconds_from("30") == 30 * 60
    assert idle_seconds_from(" 1 ") == 60
    for bad in ("0", "-5", "1.5", "15m", "fifteen"):
        with pytest.raises(ValueError, match="WAKU_IDLE_MINUTES"):
            idle_seconds_from(bad)


def test_a_gateway_env_without_the_line_still_starts_on_fifteen(tmp_path):
    """upgrade.sh never rewrites config/, so a VM installed before this has no
    WAKU_IDLE_MINUTES line. Refusing to start would take every tenant down on
    the upgrade that added the idle loop."""
    env = {
        "WAKU_APEX_HOST": "agent.waku.one", "WAKU_GATEWAY_BIND": "127.0.0.1",
        "WAKU_GATEWAY_PORT": "8787", "WAKU_CONTROL_DB": str(tmp_path / "c.db"),
        "WAKU_SPAWNER_SOCKET": "s", "WAKU_GATEWAY_SOCKET": "g",
        "WAKU_PROXY_SOCKET": "p", "WAKU_ADMIN_SOCKET": "a",
        "WAKU_MAX_RUNNING": "4", "WAKU_SUPABASE_URL": "https://x.supabase.co",
        "WAKU_SUPABASE_ISSUER": "https://x.supabase.co/auth/v1",
        "WAKU_SUPABASE_JWKS_URL": "https://x.supabase.co/jwks",
        "WAKU_SUPABASE_AUDIENCE": "aud", "WAKU_SUPABASE_PUBLISHABLE_KEY": "k",
        "WAKU_FREE_TURNS_PER_HOUR": "30", "WAKU_BYOK_TURNS_PER_HOUR": "120",
    }
    assert config_from_env(env).idle_seconds == 15 * 60
    assert config_from_env({**env, "WAKU_IDLE_MINUTES": "45"}).idle_seconds == 45 * 60


# --- the disk warning ---------------------------------------------------------

def test_the_disk_warning_is_logged_once_per_level_not_once_a_minute(made, tmp_path, caplog):
    _store, _spawner, clock, _fleet, launcher = made
    readings = iter([(100, 85, 15)] * 3 + [(100, 90, 10)] + [(100, 50, 50)])

    async def run():
        loop = _loop(launcher, clock, tmp_path, sweeps=5,
                     disk_usage=lambda _path: next(readings))
        await _run_until_cancelled(loop)

    with caplog.at_level(logging.WARNING, logger="hosted"):
        asyncio.run(run())
    warnings = [r.getMessage() for r in caplog.records if "full" in r.getMessage()]
    assert len(warnings) == 2
    assert "85%" in warnings[0] and "90%" in warnings[1]
