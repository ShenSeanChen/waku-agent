"""DETERMINISTIC EVAL -- a team home starts on its agent's key (spec 032
task A4, decisions 1 and 2).

Acme's home is a tenant, so the scheduler and the launcher start it the way
they start Ben's. What differs is what the start carries: the Waku Memory key
of Acme's agent, opened from the sealed value in `team_home`, and the space it
saves into. The spawner turns the space into `WAKU_MEMORY_SPACE`. The proxy is
told the same key, so a model call of the home is charged to the space's
payer. A person's start is the request it was before.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from gatewaylib import AGENT_MEMORY_KEY, SLACK_TOKEN_KEY, Clock, FakeSpawner, ops

from hosted import jsonsock
from hosted.core import idle, requests
from hosted.core.tenant import token_hash
from hosted.gateway.internal import serve_token_lookup
from hosted.gateway.launch import Launcher, StartFailed
from hosted.gateway.secretbox import SecretBox
from hosted.gateway.spawner_client import SpawnerClient
from hosted.gateway.store import ControlDb
from hosted.gateway.team_homes import HomeKeys
from hosted.ports.runtime import RunningContainer
from hosted.spawner import service, template

ACME = "0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11"
AGENT = "5f2d8f0a-1c0b-4d67-a2f4-9f1f6f0f3c22"
BENS_KEY = "mem_sk_" + "b" * 43
TENANT = "k3fq7x2mza4b"
TOKEN = "x" * 43


@pytest.fixture
def made(tmp_path):
    store = ControlDb(tmp_path / "control.db")
    keys = HomeKeys(store, SecretBox(bytes.fromhex(SLACK_TOKEN_KEY)))
    spawner = FakeSpawner()
    clock = Clock()
    launcher = Launcher(store=store, spawner=spawner, fleet=idle.Fleet(clock, max_running=4),
                        now=clock, home_keys=keys)
    yield store, keys, spawner, launcher
    store.close()


def make_home(store, keys, key: str = AGENT_MEMORY_KEY):
    home, _ = store.set_team_home(
        space_id=ACME, space_name="Acme", agent_id=AGENT, agent_name="Waku",
        return_url="https://www.waku.one/spaces/acme", key_sealed=keys.seal(ACME, key))
    return home, store.tenant_by_id(home.tenant_id)


# --- the gateway's start request ----------------------------------------------

def test_a_team_homes_start_carries_the_agents_key_and_the_space(made):
    store, keys, spawner, launcher = made
    _home, tenant = make_home(store, keys)

    asyncio.run(launcher.start(tenant))

    [start] = ops(spawner, "start")
    assert start["tenant_id"] == tenant.id
    assert start["memory_key"] == "mem_sk_" + "A" * 43
    assert start["memory_space"] == "0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11"
    assert start["timezone"] == "UTC"


def test_a_persons_start_is_unchanged(made):
    """Ben's start names his own key and no space, whether or not a team home
    exists beside him."""
    store, keys, spawner, launcher = made
    make_home(store, keys)
    ben = store.create_tenant(sub="sub-ben", email="ben@acme.com", timezone="UTC")
    store.set_memory_key(ben.id, key=BENS_KEY, key_id="k-ben")

    asyncio.run(launcher.start(ben))

    [start] = ops(spawner, "start")
    assert set(start) == {"op", "tenant_id", "project_id", "timezone", "token",
                          "memory_key"}
    assert start["memory_key"] == BENS_KEY


def test_a_team_home_never_starts_on_a_key_from_the_persons_table(made):
    """A row in `memory_key` for the home's tenant is not what it runs on."""
    store, keys, spawner, launcher = made
    _home, tenant = make_home(store, keys)
    store.set_memory_key(tenant.id, key=BENS_KEY, key_id="k-ben")

    asyncio.run(launcher.start(tenant))

    assert ops(spawner, "start")[-1]["memory_key"] == AGENT_MEMORY_KEY


def test_a_replaced_key_is_what_the_next_start_carries(made):
    store, keys, spawner, launcher = made
    _home, tenant = make_home(store, keys)
    next_key = "mem_sk_" + "N" * 43

    async def run():
        await launcher.start(tenant)
        await launcher.stop(tenant.id)
        make_home(store, keys, key=next_key)
        await launcher.start(tenant)

    asyncio.run(run())
    assert [start["memory_key"] for start in ops(spawner, "start")] == [
        AGENT_MEMORY_KEY, next_key]


def test_a_team_home_whose_key_does_not_open_is_not_started(made, caplog):
    """control.db restored onto a VM with another sealing key. The home does
    not start with no key, and the spawner is not asked."""
    store, _keys, spawner, _ = made
    _home, tenant = make_home(store, HomeKeys(store, SecretBox(bytes.fromhex("0f" * 32))))
    other = HomeKeys(store, SecretBox(bytes.fromhex("1e" * 32)))
    clock = Clock()
    launcher = Launcher(store=store, spawner=spawner, fleet=idle.Fleet(clock, max_running=4),
                        now=clock, home_keys=other)

    with pytest.raises(StartFailed):
        asyncio.run(launcher.start(tenant))

    assert ops(spawner, "start") == []
    assert launcher.fleet.running() == []
    assert AGENT_MEMORY_KEY not in caplog.text


def test_a_gateway_with_no_sealing_key_starts_no_team_home(made):
    store, keys, spawner, _ = made
    _home, tenant = make_home(store, keys)
    clock = Clock()
    launcher = Launcher(store=store, spawner=spawner, fleet=idle.Fleet(clock, max_running=4),
                        now=clock)

    with pytest.raises(StartFailed):
        asyncio.run(launcher.start(tenant))
    assert ops(spawner, "start") == []


# --- the wire to the spawner --------------------------------------------------

@pytest.fixture
def sock_dir():
    with tempfile.TemporaryDirectory(prefix="waku") as short:
        yield Path(short)


def _sent_by_the_client(sock_dir, **start) -> dict:
    """What SpawnerClient.start writes on the socket."""
    seen: dict = {}

    async def handler(request: dict) -> dict:
        seen.update(request)
        return {"address": "10.88.0.2", "port": 7777}

    async def run():
        server = await jsonsock.serve(sock_dir / "spawner.sock", handler, mode=0o660)
        try:
            await SpawnerClient(sock_dir / "spawner.sock").start(
                TENANT, 2, "UTC", TOKEN, **start)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    return seen


def test_the_client_sends_the_space_only_for_a_team_home(sock_dir):
    team = _sent_by_the_client(sock_dir, memory_key=AGENT_MEMORY_KEY, memory_space=ACME)
    assert team["memory_space"] == ACME
    assert team["memory_key"] == AGENT_MEMORY_KEY

    person = _sent_by_the_client(sock_dir, memory_key=BENS_KEY)
    assert set(person) == {"op", "tenant_id", "project_id", "timezone", "token",
                           "memory_key"}


def _start_payload(**changes) -> dict:
    return {"op": "start", "tenant_id": TENANT, "project_id": 2, "timezone": "UTC",
            "token": TOKEN, "memory_key": AGENT_MEMORY_KEY, **changes}


def test_the_spawner_takes_a_space_on_start():
    parsed = requests.parse(_start_payload(memory_space=ACME))
    assert parsed.memory_space == ACME
    assert requests.parse(_start_payload()).memory_space == ""


@pytest.mark.parametrize("bad", [
    "", "acme", ACME.upper(), ACME + "\nWAKU_HOSTED=0", ACME + " ", ACME[:-1],
    "0b0f3c1e8a0e4c589d550d2b5b1f7a11", 7, None, ["x"]])
def test_the_spawner_refuses_a_space_that_is_not_a_uuid(bad):
    with pytest.raises(requests.Invalid, match="memory_space"):
        requests.parse(_start_payload(memory_space=bad))


@pytest.mark.parametrize("op", ["provision", "stop", "task"])
def test_no_other_operation_takes_a_space(op):
    payload = {"op": op, "tenant_id": TENANT, "project_id": 2, "task": "backup",
               "memory_space": ACME}
    payload = {key: value for key, value in payload.items()
               if key in requests._KEYS[op] or key == "memory_space"}
    with pytest.raises(requests.Invalid, match="memory_space"):
        requests.parse(payload)


class RecordingRuntime:
    """A runtime that takes exactly what TenantRuntime.start names."""

    def __init__(self) -> None:
        self.starts: list[dict] = []

    async def start(self, tenant_id, project_id, timezone, token, memory_key="",
                    connectors=(), **more):
        self.starts.append({"memory_key": memory_key, **more})
        return RunningContainer(tenant_id=tenant_id, address="10.88.0.2", port=7777)


def test_the_service_hands_the_space_to_the_runtime():
    runtime = RecordingRuntime()

    async def run():
        team = await service.handle(runtime, _start_payload(memory_space=ACME))
        person = await service.handle(runtime, _start_payload(memory_key=BENS_KEY))
        return team, person

    team, person = asyncio.run(run())
    assert "error" not in team and "error" not in person
    assert runtime.starts == [{"memory_key": AGENT_MEMORY_KEY, "memory_space": ACME},
                              {"memory_key": BENS_KEY}]


# --- the container's environment ----------------------------------------------

@pytest.fixture
def spawner_config(tmp_path):
    return template.SpawnerConfig(
        tenant_root=Path("/srv/waku/tenants"), archive_root=Path("/srv/waku/archive"),
        staging_root=Path("/srv/waku/staging"), tenant_image="waku-tenant:test",
        services_image="waku-services:test", platform_base_url="", platform_model="",
        platform_small_model="", tenant_disk_bytes=1024, data_device="/dev/null",
        seccomp_profile="{}")


def _env(config, **more) -> list[str]:
    return template.tenant_container(config, tenant_id=TENANT, project_id=2,
                                     timezone="UTC", token=TOKEN, **more)["Env"]


def test_a_team_homes_container_gets_the_key_and_the_space(spawner_config):
    env = _env(spawner_config, memory_key=AGENT_MEMORY_KEY, memory_space=ACME)

    assert "WAKU_MEMORY_SPACE=0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11" in env
    assert f"WAKU_MEMORY_API_KEY={AGENT_MEMORY_KEY}" in env


def test_a_persons_container_gets_no_space(spawner_config):
    env = _env(spawner_config, memory_key=BENS_KEY)

    assert not [line for line in env if line.startswith("WAKU_MEMORY_SPACE")]
    assert env == _env(spawner_config, memory_key=BENS_KEY, memory_space="")


def test_the_template_refuses_a_space_that_is_not_a_uuid(spawner_config):
    with pytest.raises(ValueError, match="space"):
        _env(spawner_config, memory_space=ACME + "\nEVIL=1")


# --- what the proxy charges with ----------------------------------------------

def test_the_proxy_is_told_the_agents_key_for_a_team_home(made, sock_dir):
    """The proxy charges each model call with the key the gateway answers, so
    a team home's calls are charged with the same key its container holds."""
    store, keys, _spawner, _launcher = made
    _home, team = make_home(store, keys)
    ben = store.create_tenant(sub="sub-ben", email="ben@acme.com", timezone="UTC")
    store.set_memory_key(ben.id, key=BENS_KEY, key_id="k-ben")
    # A row a bug could leave behind: it is still not what the team is charged with.
    store.set_memory_key(team.id, key=BENS_KEY, key_id="k-wrong")
    tokens = {name: store.issue_token(tenant.id)
              for name, tenant in (("team", team), ("ben", ben))}
    sock = sock_dir / "gateway.sock"

    async def run():
        server = await serve_token_lookup(sock, store, memory_key=keys.memory_key)
        try:
            return {name: await jsonsock.ask(sock, {"op": "token", "hash": token_hash(token)})
                    for name, token in tokens.items()}
        finally:
            server.close()
            await server.wait_closed()

    answers = asyncio.run(run())
    assert answers["team"] == {"tenant": team.id, "status": "active",
                               "memory_key": AGENT_MEMORY_KEY}
    assert answers["ben"] == {"tenant": ben.id, "status": "active", "memory_key": BENS_KEY}


def test_a_team_home_whose_key_does_not_open_is_charged_with_no_key(made):
    store, keys, _spawner, _launcher = made
    _home, team = make_home(store, keys)
    store.set_memory_key(team.id, key=BENS_KEY, key_id="k-wrong")
    other = HomeKeys(store, SecretBox(bytes.fromhex("1e" * 32)))

    assert other.memory_key(team.id) == ""
