"""DETERMINISTIC EVAL -- an upgrade reaches a person who never goes idle.

Seen on 2026-11-19. autodeploy.sh ran upgrade.sh without --now, the tenant
image was rebuilt, and a person who kept messaging their agent still had the
old code twenty minutes later: a running container keeps the image it was
created from, and only the idle stop replaced it, after fifteen minutes with
no request.

Three parts are pinned here, in the order a sweep uses them:

  the spawner   `list` says which containers were created from an older build
                of the tenant image than the tag names now
  the wire      the spawner's answer carries `stale`, and the gateway's client
                reads it
  the gateway   Launcher.stop_stale stops such a container between two turns,
                keeps its next timer, and never stops one that is working

No test sleeps. The fleet takes a clock a test moves by hand.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
import pytest
from gatewaylib import AlwaysOnContainer, Clock, FakeSpawner, ops

from hosted import jsonsock
from hosted.core import idle, tenant
from hosted.gateway.container_api import ContainerApi
from hosted.gateway.launch import Launcher
from hosted.gateway.spawner_client import SpawnerClient, SpawnerError
from hosted.gateway.store import ControlDb
from hosted.gateway.sweep import IdleLoop
from hosted.ports.runtime import RunningContainer
from hosted.spawner import docker as docker_mod
from hosted.spawner import engine as engine_mod
from hosted.spawner import service, template

FRIDAY_ISO, FRIDAY_EPOCH = "2026-11-20T09:00:00+00:00", 1_795_165_200.0
QUIET = {"next_timer_at": None, "events": 0, "outbox": 0, "running": 0}
BETWEEN_TURNS = idle.STALE_QUIET_SECONDS


def _quiet_disk(_path):
    return (100, 10, 90)


@pytest.fixture
def container():
    fake = AlwaysOnContainer()
    fake.start()
    yield fake
    fake.stop()


class Upgraded:
    """A real Launcher on a real ControlDb, a FakeSpawner and a hand-moved
    clock, asking a fake container for its report. `upgrade` says what
    upgrade.sh does to a running container: the image tag moves and the
    container keeps the image it was created from."""

    def __init__(self, tmp_path, port: int, spawner: FakeSpawner | None = None) -> None:
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.store = ControlDb(tmp_path / "upgraded.db", now=self.clock)
        self.spawner = spawner or FakeSpawner()
        self.spawner.address, self.spawner.port = "127.0.0.1", port
        self.fleet = idle.Fleet(self.clock, max_running=4)
        self.session = aiohttp.ClientSession()
        self.launcher = Launcher(store=self.store, spawner=self.spawner, fleet=self.fleet,
                                 now=self.clock, containers=ContainerApi(self.session))

    async def started(self, sub: str = "sub-ana"):
        record = self.store.create_tenant(sub=sub, email=f"{sub}@example.com",
                                          timezone="UTC")
        assert self.fleet.admit(record.id, background=False).action == "start"
        await self.launcher.start(record)
        return record

    def upgrade(self, *tenant_ids: str) -> None:
        self.spawner.old_image.update(tenant_ids)

    async def sweep(self) -> list[str]:
        return await IdleLoop(self.launcher, disk_path=self.tmp_path,
                              disk_usage=_quiet_disk).sweep()

    async def close(self) -> None:
        await self.session.close()
        self.store.close()


# --- the gateway: stopped between turns, and only then -----------------------

def test_a_container_on_an_old_image_is_stopped_a_minute_after_its_last_request(
        tmp_path, container):
    """The bug. The person sent a message a minute ago, which is fourteen
    minutes short of the idle window, and the sweep still replaces the
    container. Their next message starts a new one from the current image."""
    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        stops = ops(made.spawner, "stop")
        token_left = made.store.has_live_token(ana.id)
        next_message = made.fleet.admit(ana.id, background=False).action
        await made.launcher.start(ana)
        again = await made.sweep()
        await made.close()
        return ana.id, stopped, stops, token_left, next_message, again

    ana, stopped, stops, token_left, next_message, again = asyncio.run(run())
    assert BETWEEN_TURNS < idle.IDLE_SECONDS
    assert stopped == [ana]
    assert stops == [{"op": "stop", "tenant_id": ana}]
    assert token_left is False
    assert next_message == "start"
    assert again == [], "the new container runs the current image"


def test_a_container_on_the_current_image_is_left_alone_and_asked_nothing(
        tmp_path, container):
    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started("sub-ana")
        ben = await made.started("sub-ben")
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        running = made.fleet.running()
        await made.close()
        return ana.id, ben.id, stopped, running

    ana, ben, stopped, running = asyncio.run(run())
    assert stopped == [ana]
    assert running == [ben]
    assert container.asked == 1, "only the stale container is asked for a report"


def test_a_request_in_flight_keeps_a_stale_container(tmp_path, container):
    """A turn is a forwarded request, and it is in flight until its last
    byte. The container is replaced only after the request has left."""
    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.upgrade(ana.id)
        made.fleet.enter(ana.id)
        made.clock.t += 10 * BETWEEN_TURNS
        during = await made.sweep()
        made.fleet.leave(ana.id)
        after = await made.sweep()
        await made.close()
        return ana.id, during, after

    ana, during, after = asyncio.run(run())
    assert during == []
    assert container.asked == 1, "a container with a request open is not even asked"
    assert after == [ana]


def test_a_request_seconds_ago_keeps_a_stale_container(tmp_path, container):
    """A message that has just been admitted may not have reached the
    container yet, so its report cannot be believed until a minute passes."""
    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS - 1
        early = await made.sweep()
        made.clock.t += 1
        on_time = await made.sweep()
        await made.close()
        return ana.id, early, on_time

    ana, early, on_time = asyncio.run(run())
    assert early == []
    assert on_time == [ana]


@pytest.mark.parametrize("work", ["running", "events", "outbox"])
def test_a_stale_container_that_reports_work_is_not_stopped(tmp_path, container, work):
    """A run an event or a timer started has no request open, so the fleet
    cannot see it. The container's own report can, and it wins."""
    container.work = {**QUIET, work: 1}

    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        while_working = await made.sweep()
        still_running = made.fleet.running()
        container.work = dict(QUIET)
        made.clock.t += 60
        after = await made.sweep()
        await made.close()
        return ana.id, while_working, still_running, after

    ana, while_working, still_running, after = asyncio.run(run())
    assert while_working == []
    assert still_running == [ana]
    assert after == [ana]


@pytest.mark.parametrize("answer", ["no such route", "not listening", "not the agreed shape"])
def test_a_stale_container_that_gives_no_report_waits_for_the_idle_stop(
        tmp_path, container, answer):
    """Without a report nobody knows whether a run is in progress. The idle
    stop may stop such a container after fifteen quiet minutes; this path
    may not stop it after one."""
    port = container.port
    if answer == "no such route":
        container.work = None
    elif answer == "not the agreed shape":
        container.work = {"outbox": "plenty"}

    async def run():
        made = Upgraded(tmp_path, port)
        ana = await made.started()
        made.store.set_wakeup(ana.id, FRIDAY_EPOCH)
        made.upgrade(ana.id)
        if answer == "not listening":
            container.stop()
        made.clock.t += BETWEEN_TURNS
        early = await made.sweep()
        made.clock.t += idle.IDLE_SECONDS
        late = await made.sweep()
        wakeup = made.store.wakeup(ana.id)
        await made.close()
        return ana.id, early, late, wakeup

    ana, early, late, wakeup = asyncio.run(run())
    assert early == []
    assert late == [ana]
    assert wakeup == FRIDAY_EPOCH


def test_a_request_that_arrives_while_the_container_is_asked_stops_the_stop(
        tmp_path, container):
    box = {}
    container.on_ask = lambda: box["fleet"].touch(box["tenant"])

    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        box["fleet"], box["tenant"] = made.fleet, ana.id
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        stops = ops(made.spawner, "stop")
        await made.close()
        return stopped, stops

    stopped, stops = asyncio.run(run())
    assert container.asked == 1
    assert (stopped, stops) == ([], [])


class ReplacedWhileListing(FakeSpawner):
    """A spawner whose `list` answer is already out of date when it arrives:
    `meanwhile` runs after the answer was taken and before it is returned."""

    meanwhile = None

    async def list(self) -> list[RunningContainer]:
        answer = await super().list()
        if self.meanwhile is not None:
            await self.meanwhile()
        return answer


def test_a_container_started_while_the_spawner_was_asked_is_not_stopped(
        tmp_path, container):
    """`list` is an await. A container that was replaced during it is a new
    one on the current image, and the answer that called its tenant stale
    describes a container that is gone."""
    async def run():
        spawner = ReplacedWhileListing()
        made = Upgraded(tmp_path, container.port, spawner)
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS

        async def replaced():
            await made.launcher.stop(ana.id)
            assert made.fleet.admit(ana.id, background=False).action == "start"
            await made.launcher.start(ana)
            made.clock.t += BETWEEN_TURNS

        spawner.meanwhile = replaced
        stopped = await made.sweep()
        stops = len(ops(spawner, "stop"))
        running = made.fleet.running()
        await made.close()
        return ana.id, stopped, stops, running

    ana, stopped, stops, running = asyncio.run(run())
    assert stopped == []
    assert stops == 1, "the only stop is the one that replaced the container"
    assert running == [ana]


# --- the wakeup is kept exactly as an idle stop keeps it ---------------------

def test_a_stale_stop_stores_the_timer_the_container_reports(tmp_path, container):
    """The container says its next timer is Friday 09:00. It is stopped a
    minute after its last request, and the scheduler still has that time."""
    container.work = {**QUIET, "next_timer_at": FRIDAY_ISO}

    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        wakeup = made.store.wakeup(ana.id)
        due = made.store.due_wakeups(FRIDAY_EPOCH)
        await made.close()
        return ana.id, stopped, wakeup, due

    ana, stopped, wakeup, due = asyncio.run(run())
    assert stopped == [ana]
    assert wakeup == FRIDAY_EPOCH
    assert due == [ana]


def test_a_stale_stop_clears_an_older_wakeup_when_no_timer_is_left(tmp_path, container):
    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.store.set_wakeup(ana.id, FRIDAY_EPOCH)
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        wakeup = made.store.wakeup(ana.id)
        await made.close()
        return ana.id, stopped, wakeup

    ana, stopped, wakeup = asyncio.run(run())
    assert stopped == [ana]
    assert wakeup is None


def test_a_busy_stale_container_keeps_the_wakeup_it_had(tmp_path, container):
    """A container that is not stopped has its own timer loop, and the stored
    wakeup belongs to the stop that did not happen."""
    container.work = {**QUIET, "running": 1, "next_timer_at": None}

    async def run():
        made = Upgraded(tmp_path, container.port)
        ana = await made.started()
        made.store.set_wakeup(ana.id, FRIDAY_EPOCH)
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        wakeup = made.store.wakeup(ana.id)
        await made.close()
        return stopped, wakeup

    assert asyncio.run(run()) == ([], FRIDAY_EPOCH)


# --- the sweep survives a spawner that cannot answer -------------------------

def test_a_spawner_that_cannot_list_stops_nothing_and_is_logged(
        tmp_path, container, caplog):
    class Deaf(FakeSpawner):
        async def list(self) -> list[RunningContainer]:
            raise SpawnerError("the daemon did not answer")

    async def run():
        made = Upgraded(tmp_path, container.port, Deaf())
        ana = await made.started()
        made.upgrade(ana.id)
        made.clock.t += BETWEEN_TURNS
        stopped = await made.sweep()
        running = made.fleet.running()
        await made.close()
        return ana.id, stopped, running

    with caplog.at_level(logging.WARNING, logger="hosted"):
        ana, stopped, running = asyncio.run(run())
    assert stopped == []
    assert running == [ana]
    assert "which containers run an old image" in caplog.text


def test_nothing_running_means_the_spawner_is_not_asked(tmp_path, container):
    async def run():
        made = Upgraded(tmp_path, container.port)
        stopped = await made.sweep()
        lists = ops(made.spawner, "list")
        await made.close()
        return stopped, lists

    assert asyncio.run(run()) == ([], [])


# --- the fleet's question ----------------------------------------------------

def test_quiet_for_is_the_idle_question_with_another_window():
    clock = Clock()
    fleet = idle.Fleet(clock, max_running=2)
    fleet.adopt("ana")
    assert not fleet.quiet_for("ana", 60)
    clock.t += 60
    assert fleet.quiet_for("ana", 60)
    assert not fleet.is_idle("ana")
    fleet.enter("ana")
    assert not fleet.quiet_for("ana", 60)
    fleet.leave("ana")
    fleet.set_status("ana", idle.STOPPED)
    assert not fleet.quiet_for("ana", 60)
    assert not fleet.quiet_for("nobody", 60)


# --- the spawner: which containers are stale ---------------------------------

CONFIG = template.SpawnerConfig(
    tenant_root=Path("/srv/waku/tenants"),
    archive_root=Path("/srv/waku/archive"),
    staging_root=Path("/srv/waku/staging"),
    tenant_image="waku-tenant:test",
    services_image="waku-services:test",
    platform_base_url="http://10.88.0.1:8788",
    platform_model="a-model",
    platform_small_model="a-model",
    tenant_disk_bytes=1073741824,
    data_device="none",
    seccomp_profile="{}",
)
GOOD = "k3fq7x2mza4b"
OLD_BUILD, NEW_BUILD = "sha256:" + "a" * 64, "sha256:" + "b" * 64


class _ImageEngine:
    def __init__(self, entries, current) -> None:
        self._entries = entries
        self._current = current
        self.asked: list[str] = []

    async def containers(self, *, label=None, all_states=False):
        return self._entries

    async def image_id(self, name: str) -> str:
        self.asked.append(name)
        if isinstance(self._current, Exception):
            raise self._current
        return self._current


def _entry(created_from) -> dict:
    entry = {
        "Id": "deadbeefcafe",
        "Labels": {template.LABEL_TENANT: GOOD,
                   template.LABEL_KIND: template.KIND_TENANT},
        "NetworkSettings": {"Networks": {tenant.TENANT_NETWORK: {
            "IPAddress": tenant.address_for_project(tenant.FIRST_PROJECT_ID)}}},
    }
    if created_from is not None:
        entry["ImageID"] = created_from
    return entry


def _stale(created_from, current) -> bool:
    engine = _ImageEngine([_entry(created_from)], current)
    listed = asyncio.run(docker_mod.DockerRuntime(CONFIG, engine).list())
    assert engine.asked == ["waku-tenant:test"], "the TENANT image tag is the one compared"
    assert len(listed) == 1
    return listed[0].stale


def test_list_calls_a_container_from_an_older_build_stale():
    assert _stale(OLD_BUILD, NEW_BUILD) is True


def test_list_does_not_call_a_container_from_the_current_build_stale():
    assert _stale(NEW_BUILD, NEW_BUILD) is False


@pytest.mark.parametrize("created_from, current", [
    (OLD_BUILD, ""),                                    # no image has the tag
    (OLD_BUILD, engine_mod.EngineError("GET -> 500")),  # the daemon cannot say
    (None, NEW_BUILD),                                  # the entry names no image
    ("", NEW_BUILD),
    (7, NEW_BUILD),
])
def test_list_calls_nothing_stale_when_it_cannot_compare(created_from, current):
    """A restart nobody can justify is worse than a container that waits for
    its idle stop, so every unknown is "not stale"."""
    assert _stale(created_from, current) is False


@pytest.mark.parametrize("status, body, expected", [
    (200, {"Id": NEW_BUILD, "RepoTags": ["waku-tenant:test"]}, NEW_BUILD),
    (404, {"message": "No such image: waku-tenant:test"}, ""),
    (200, {"Id": 7}, ""),
])
def test_the_engine_reads_the_id_a_tag_names_now(monkeypatch, status, body, expected):
    seen: list[tuple] = []

    async def call(self, method, path, **kwargs):
        seen.append((method, path, kwargs.get("expect")))
        assert status in kwargs["expect"]
        return body

    monkeypatch.setattr(engine_mod.Engine, "_call", call)
    found = asyncio.run(engine_mod.Engine(Path("/no-daemon.sock")).image_id("waku-tenant:test"))
    assert found == expected
    assert seen == [("GET", "/images/waku-tenant:test/json", (200, 404))]


# --- the wire between them ---------------------------------------------------

def test_the_spawners_list_answer_carries_stale_and_the_client_reads_it(monkeypatch):
    """Both halves of one key. The producer is service.handle and the reader
    is SpawnerClient.list; the answer of the first is fed to the second."""
    address = tenant.address_for_project(tenant.FIRST_PROJECT_ID)

    class Runtime:
        async def list(self):
            return [RunningContainer(GOOD, address, 7777, stale=True),
                    RunningContainer("aaaaaaaaaaaa", address, 7777)]

    answer = asyncio.run(service.handle(Runtime(), {"op": "list"}))
    assert [entry["stale"] for entry in answer["containers"]] == [True, False]

    async def fake_ask(path, request, *, timeout=2.0):
        return answer

    monkeypatch.setattr(jsonsock, "ask", fake_ask)
    listed = asyncio.run(SpawnerClient(Path("/unused")).list())
    assert [(c.tenant_id, c.stale) for c in listed] == [(GOOD, True), ("aaaaaaaaaaaa", False)]


@pytest.mark.parametrize("entry", [
    {},                    # a spawner from before the key existed
    {"stale": "true"},
    {"stale": 1},
    {"stale": None},
])
def test_only_a_true_from_the_spawner_makes_a_container_stale(monkeypatch, entry):
    async def fake_ask(path, request, *, timeout=2.0):
        return {"containers": [{"tenant_id": GOOD, "address": "10.88.0.2",
                                "port": 7777, **entry}]}

    monkeypatch.setattr(jsonsock, "ask", fake_ask)
    listed = asyncio.run(SpawnerClient(Path("/unused")).list())
    assert [c.stale for c in listed] == [False]


def test_a_start_answer_is_never_stale(monkeypatch):
    """Every start creates a container from the current image."""
    async def fake_ask(path, request, *, timeout=2.0):
        return {"address": "10.88.0.2", "port": 7777}

    monkeypatch.setattr(jsonsock, "ask", fake_ask)
    started = asyncio.run(SpawnerClient(Path("/unused")).start(GOOD, 2, "UTC", "t" * 43))
    assert started.stale is False
