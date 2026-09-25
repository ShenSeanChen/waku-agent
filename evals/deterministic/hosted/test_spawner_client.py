"""SpawnerClient against a real AF_UNIX socket -- no Docker needed.

Nothing in the repository constructs a SpawnerClient yet (E3 is the first
caller), so without this file every line of `_ask`, `provision`, `start`,
`stop`, `list`, `task` and `_container` runs in no eval at all: the busy
branch, the list and per-element type guards (added for plan-review finding
E-12), and `_container`'s address/port validation all had no proved failing
mode. `test_internal_api.py` already answers one scripted JSON line per
connection over a real Unix socket for the gateway's and proxy's own
sockets; this file does the same for the spawner's wire shape, with a
handler this file writes instead of `hosted.spawner.service.handle` -- the
point is `spawner_client.py`'s own parsing, not the spawner's, which is
group C's `evals/hosted_docker/`.

asyncio.run around each coroutine, no pytest-asyncio, matching every other
socket test in this directory.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from hosted import jsonsock
from hosted.gateway.spawner_client import (
    QUICK_ASK_TIMEOUT,
    START_ASK_TIMEOUT,
    TASK_ASK_TIMEOUT,
    SpawnerBusy,
    SpawnerClient,
    SpawnerError,
)
from hosted.ports.runtime import RunningContainer


async def _closing(server):
    server.close()
    await server.wait_closed()


@pytest.fixture
def sock_dir():
    """A short directory for the socket. AF_UNIX paths are capped at 104
    bytes on macOS and 108 on Linux, and pytest's own tmp_path spells the
    test's name into the path -- long enough to go past that cap for some of
    the longer test names below. test_internal_api.py's `sock_dir` fixture
    does the same, for the same reason."""
    with tempfile.TemporaryDirectory(prefix="waku") as short:
        yield Path(short)


def _script(answer: dict, *, capture: dict | None = None):
    """A one-shot handler: record the request (if asked) and answer with the
    same object every time. Every test here needs at most one connection."""
    async def handler(request: dict) -> dict:
        if capture is not None:
            capture.clear()
            capture.update(request)
        return answer
    return handler


def test_provision_sends_exactly_the_keys_the_spawner_admits(sock_dir):
    captured: dict = {}

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(sock, _script({}, capture=captured), mode=0o660)
        try:
            client = SpawnerClient(sock)
            await client.provision("aaaaaaaaaaaa", 2)
        finally:
            await _closing(server)

    asyncio.run(run())
    assert captured == {"op": "provision", "tenant_id": "aaaaaaaaaaaa", "project_id": 2}


def test_start_sends_exactly_the_keys_and_parses_the_container(sock_dir):
    captured: dict = {}

    async def run():
        sock = sock_dir / "spawner.sock"
        answer = {"address": "10.88.0.5", "port": 8000}
        server = await jsonsock.serve(sock, _script(answer, capture=captured), mode=0o660)
        try:
            client = SpawnerClient(sock)
            return await client.start("aaaaaaaaaaaa", 3, "Asia/Shanghai", "t" * 43)
        finally:
            await _closing(server)

    running = asyncio.run(run())
    assert captured == {"op": "start", "tenant_id": "aaaaaaaaaaaa", "project_id": 3,
                        "timezone": "Asia/Shanghai", "token": "t" * 43}
    assert running == RunningContainer(tenant_id="aaaaaaaaaaaa", address="10.88.0.5", port=8000)


def test_stop_sends_exactly_the_tenant_id(sock_dir):
    captured: dict = {}

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(sock, _script({}, capture=captured), mode=0o660)
        try:
            client = SpawnerClient(sock)
            await client.stop("aaaaaaaaaaaa")
        finally:
            await _closing(server)

    asyncio.run(run())
    assert captured == {"op": "stop", "tenant_id": "aaaaaaaaaaaa"}


def test_list_parses_every_container_the_spawner_reports(sock_dir):

    async def run():
        sock = sock_dir / "spawner.sock"
        answer = {"containers": [
            {"tenant_id": "aaaaaaaaaaaa", "address": "10.88.0.2", "port": 7777},
            {"tenant_id": "bbbbbbbbbbbb", "address": "10.88.0.3", "port": 7777},
        ]}
        server = await jsonsock.serve(sock, _script(answer), mode=0o660)
        try:
            client = SpawnerClient(sock)
            return await client.list()
        finally:
            await _closing(server)

    containers = asyncio.run(run())
    assert containers == [
        RunningContainer(tenant_id="aaaaaaaaaaaa", address="10.88.0.2", port=7777),
        RunningContainer(tenant_id="bbbbbbbbbbbb", address="10.88.0.3", port=7777),
    ]


def test_list_refuses_a_containers_field_that_is_not_a_list(sock_dir):
    """A number, not a string: a string `containers` field is iterable and
    would raise through the per-element guard below even with this one
    deleted, which would make this test pass for the wrong reason. A number
    is not iterable at all, so `for entry in entries` would raise a bare
    TypeError -- outside (SpawnerError, OSError), the only exceptions every
    caller of `Launcher.resync` catches -- if this guard did not exist."""

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(
            sock, _script({"containers": 5}), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerError):
                await client.list()
        finally:
            await _closing(server)

    asyncio.run(run())


def test_list_refuses_an_element_that_is_not_an_object(sock_dir):
    """Plan-review finding E-12: the list itself can be a list, and still
    carry an entry that is not. Same AttributeError-outside-the-caught-types
    consequence as the whole-field guard above, one level down."""

    async def run():
        sock = sock_dir / "spawner.sock"
        answer = {"containers": [
            {"tenant_id": "aaaaaaaaaaaa", "address": "10.88.0.2", "port": 7777},
            "not-an-object",
        ]}
        server = await jsonsock.serve(sock, _script(answer), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerError):
                await client.list()
        finally:
            await _closing(server)

    asyncio.run(run())


def test_a_container_with_no_address_is_refused_not_forwarded_to(sock_dir):
    """`_container`'s own docstring names the consequence: the gateway
    FORWARDS to whatever comes out of here, so a blank address would become a
    request to `http://:7777`, which aiohttp resolves to the gateway's own
    host."""

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(sock, _script({"port": 7777}), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerError):
                await client.start("aaaaaaaaaaaa", 2, "UTC", "t" * 43)
        finally:
            await _closing(server)

    asyncio.run(run())


def test_a_container_with_a_non_positive_port_is_refused(sock_dir):

    async def run():
        sock = sock_dir / "spawner.sock"
        answer = {"address": "10.88.0.2", "port": 0}
        server = await jsonsock.serve(sock, _script(answer), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerError):
                await client.start("aaaaaaaaaaaa", 2, "UTC", "t" * 43)
        finally:
            await _closing(server)

    asyncio.run(run())


def test_task_omits_project_id_when_it_is_zero_and_sends_it_otherwise(sock_dir):
    """core/requests.py admits project_id on `task` and only requires it for
    `restore`; a zero would be refused by `is_project_id`, so the client
    omits rather than defaults it. Both branches, against the actual wire."""
    captured: dict = {}

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(sock, _script({"ok": True}, capture=captured),
                                      mode=0o660)
        try:
            client = SpawnerClient(sock)
            await client.task("aaaaaaaaaaaa", "backup")
            without_project = dict(captured)
            await client.task("aaaaaaaaaaaa", "restore", project_id=5)
            with_project = dict(captured)
            return without_project, with_project
        finally:
            await _closing(server)

    without_project, with_project = asyncio.run(run())
    assert without_project == {"op": "task", "tenant_id": "aaaaaaaaaaaa", "task": "backup"}
    assert with_project == {"op": "task", "tenant_id": "aaaaaaaaaaaa", "task": "restore",
                            "project_id": 5}


def test_a_busy_answer_raises_spawnerbusy_and_not_the_plain_error(sock_dir):
    """The maintenance mark, enforced spawner-side so it survives a gateway
    restart. The gateway answers this with the maintenance sentence rather
    than a five-hundred, which only works if it is a distinct exception type
    from every other spawner refusal."""

    async def run():
        sock = sock_dir / "spawner.sock"
        answer = {"error": "a task container holds this tenant", "code": "busy"}
        server = await jsonsock.serve(sock, _script(answer), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerBusy) as caught:
                await client.stop("aaaaaaaaaaaa")
            return type(caught.value)
        finally:
            await _closing(server)

    exc_type = asyncio.run(run())
    assert exc_type is SpawnerBusy, "must be SpawnerBusy itself, not a generic SpawnerError"


def test_a_plain_error_is_spawnererror_and_never_spawnerbusy(sock_dir):

    async def run():
        sock = sock_dir / "spawner.sock"
        server = await jsonsock.serve(sock, _script({"error": "no such image"}), mode=0o660)
        try:
            client = SpawnerClient(sock)
            with pytest.raises(SpawnerError) as caught:
                await client.stop("aaaaaaaaaaaa")
            return type(caught.value)
        finally:
            await _closing(server)

    exc_type = asyncio.run(run())
    assert exc_type is SpawnerError, "a code-less error must not read as the maintenance mark"


def test_an_unreachable_socket_propagates_rather_than_a_spawner_answer(sock_dir):
    """No server at all. SpawnerClient adds no handling of its own here --
    jsonsock.Unreachable, an OSError, must reach the caller unchanged, which
    is what every `except (SpawnerError, OSError)` in launch.py relies on."""

    async def run():
        client = SpawnerClient(sock_dir / "not-there.sock")
        with pytest.raises(jsonsock.Unreachable):
            await client.list()

    asyncio.run(run())


def test_the_three_timeouts_are_the_specs_numbers():
    """Pinned literals, not a comparison of a value to the constant that
    sets it: START_ASK_TIMEOUT's own docstring derives it from the gateway's
    15-second start budget, and QUICK_ASK_TIMEOUT and TASK_ASK_TIMEOUT are
    the brief's own values."""
    assert START_ASK_TIMEOUT == 30.0
    assert QUICK_ASK_TIMEOUT == 15.0
    assert TASK_ASK_TIMEOUT == 900.0


def test_each_call_asks_with_its_own_timeout_not_a_shared_one(monkeypatch):
    """The docstring's whole argument -- "a single timeout would have to be
    the largest, and then a spawner that has died holds a browser request
    open for fifteen minutes" -- is only true if each method actually passes
    its own number through. jsonsock.ask is monkeypatched here rather than
    timed against a slow real server, because proving a 900-second default
    would otherwise cost 900 seconds."""

    seen: dict[str, float] = {}

    async def fake_ask(path, request, *, timeout=2.0):
        seen[request["op"]] = timeout
        if request["op"] == "list":
            return {"containers": []}
        if request["op"] == "start":
            return {"address": "10.88.0.2", "port": 7777}
        return {}

    monkeypatch.setattr(jsonsock, "ask", fake_ask)

    async def run():
        client = SpawnerClient(Path("/unused"))
        await client.provision("aaaaaaaaaaaa", 2)
        await client.start("aaaaaaaaaaaa", 2, "UTC", "t" * 43)
        await client.stop("aaaaaaaaaaaa")
        await client.list()
        await client.task("aaaaaaaaaaaa", "backup")

    asyncio.run(run())
    assert seen == {
        "provision": QUICK_ASK_TIMEOUT,
        "start": START_ASK_TIMEOUT,
        "stop": QUICK_ASK_TIMEOUT,
        "list": QUICK_ASK_TIMEOUT,
        "task": TASK_ASK_TIMEOUT,
    }
