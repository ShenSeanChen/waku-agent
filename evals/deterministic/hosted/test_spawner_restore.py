"""DETERMINISTIC EVAL -- a restored tenant comes back with their disk quota.

THE DEFECT THIS EXISTS FOR (C2-1). `_restore` removes the tenant's two
directories and then calls `provision()`, whose CREATE path is the only thing
that ever issues `xfs_quota project -s`. An earlier shape removed them in two
separate container runs, and `template.task_container` binds BOTH tenant paths
unconditionally -- so the second run's create re-made the first run's
directory. The Docker daemon creates a missing bind source as a root-owned
directory, so `home` was back before `provision()` looked, `mkdir` raised
FileExistsError, and `home` took the `set_limit` branch: XFS project 0,
uncounted and unlimited, for the rest of that tenant's life. `env` was fine.
The asymmetry was invisible, and a tenant who got a restore could then fill the
shared data disk and stop every other tenant on the VM.

THE FAKE DAEMON BELOW CREATES MISSING BIND SOURCES, which is the whole point:
without that behaviour modelled, this file passes against the broken sequence.
It is the documented behaviour of the legacy `Binds` form and the reason
`--mount` exists.

This is an OFFLINE guard, not a drift check. The Docker half is
evals/hosted_docker/test_isolation.py::test_a_restored_tenant_keeps_their_own_project_id,
which reads the real XFS project id off `home` after a real restore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hosted.spawner import docker as docker_mod
from hosted.spawner import template

TENANT = "k3fq7x2mza4b"
PROJECT = 4242


class FakeEngine:
    """Enough of the Engine to drive a whole task, plus the one daemon
    behaviour that caused C2-1: a missing `Binds` source is created.

    Every container is treated as having run successfully and written nothing.
    That is the right abstraction here: this file is about the ORDER of the
    calls and the state of the directories between them, not about what the
    containers do.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.remade: list[str] = []

    async def create(self, name: str, body: dict) -> str:
        for bind in body["HostConfig"]["Binds"]:
            source = Path(bind.split(":", 1)[0])
            if not source.exists():
                # THE DAEMON'S OWN BEHAVIOUR, modelled. Root-owned, empty.
                source.mkdir(parents=True)
                self.remade.append(str(source))
        self.created.append((name, body))
        return f"id-{len(self.created)}"

    async def start(self, container: str) -> None:
        return None

    async def wait(self, container: str) -> int:
        return 0

    async def logs(self, container: str) -> str:
        return ""

    async def remove(self, container: str, *, force: bool = True) -> None:
        return None

    async def stop(self, container: str, *, timeout: int = 10) -> None:
        return None

    async def containers(self, *, label=None, all_states=False) -> list[dict]:
        return []


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A runtime whose xfs_quota calls are recorded instead of run, over a real
    temporary directory tree so `mkdir`, `rmdir` and `exists` are the real
    syscalls and not a model of them."""
    claimed: list[str] = []
    limited: list[int] = []

    async def fake_claim(device, path, project_id, hard_bytes):
        claimed.append(str(path))

    async def fake_set_limit(device, project_id, hard_bytes):
        limited.append(project_id)

    monkeypatch.setattr(docker_mod.xfsquota, "claim", fake_claim)
    monkeypatch.setattr(docker_mod.xfsquota, "set_limit", fake_set_limit)
    # chown needs root; the ownership is not what this file is about.
    monkeypatch.setattr(docker_mod.os, "chown", lambda *a, **k: None)

    config = template.SpawnerConfig(
        tenant_root=tmp_path / "tenants",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        tenant_image="waku-tenant:test",
        services_image="waku-services:test",
        platform_base_url="http://10.88.0.1:8788",
        platform_model="a-model",
        platform_small_model="a-model",
        tenant_disk_bytes=1073741824,
        data_device="/dev/sdb1",
        seccomp_profile='{"defaultAction":"SCMP_ACT_ERRNO"}',
    )
    engine = FakeEngine()
    runtime = docker_mod.DockerRuntime(config, engine)
    return runtime, engine, config, claimed, limited


def test_a_restore_claims_a_project_id_for_both_directories(world):
    """C2-1. Both of the tenant's directories must go through provision's
    CREATE path, because `claim` is the only thing that issues `project -s`
    and `project -s` is the only thing that sets the project id and the
    inherit flag.

    Asserted as a SET of paths rather than a count: `home` getting two claims
    and `env` none would satisfy a count and is exactly the shape of the bug.
    """
    runtime, _engine, config, claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    asyncio.run(runtime.task(TENANT, "backup"))
    claimed.clear()

    asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    dirs = docker_mod.tenant_dirs(config.tenant_root, TENANT)
    assert set(claimed) == {str(dirs.home), str(dirs.env)}, (
        f"restore claimed {sorted(claimed)}; it must claim BOTH directories. "
        "A directory that misses the create path keeps XFS project 0 -- "
        "uncounted and unlimited -- for the rest of that tenant's life, and "
        "the tenant can then fill the shared data disk.")


def test_a_restore_never_lets_a_container_re_create_a_directory_it_removed(world):
    """The MECHANISM behind C2-1, asserted directly so a future refactor that
    goes back to one container run per directory fails here with the reason
    rather than somewhere downstream with a quota number.

    `template.task_container` binds both tenant paths unconditionally, and the
    daemon creates a missing bind source. So removing the two directories in
    two container runs cannot work: the second run's create re-makes the
    first's. One run empties both; the host then removes both.
    """
    runtime, engine, _config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    asyncio.run(runtime.task(TENANT, "backup"))
    engine.remade.clear()

    asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert engine.remade == [], (
        f"the daemon re-created {engine.remade} as a bind source. A directory "
        "the restore just removed came back root-owned behind provision's "
        "back, so provision took its FileExistsError branch and never claimed "
        "a project id for it.")


def test_a_restore_leaves_both_directories_present_and_provisioned(world):
    """The end state, so the two assertions above cannot both hold for a
    restore that left the tenant with no directories at all."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    asyncio.run(runtime.task(TENANT, "backup"))
    asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    dirs = docker_mod.tenant_dirs(config.tenant_root, TENANT)
    assert dirs.home.is_dir() and dirs.env.is_dir()


def test_provisioning_an_existing_tenant_still_takes_the_repeat_path(world):
    """The other direction, so the C2-1 fix cannot be "claim on every
    provision", which is the recursive walk over a tenant-written tree that
    the whole design exists to keep off the hot path."""
    runtime, _engine, _config, claimed, limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    claimed.clear()
    limited.clear()

    asyncio.run(runtime.provision(TENANT, PROJECT))

    assert claimed == [], (
        f"a second provision claimed {claimed}. `project -s` is a recursive "
        "descent and that directory now holds whatever the tenant wrote.")
    assert limited == [PROJECT, PROJECT]


def test_a_restore_with_nothing_staged_refuses_instead_of_erasing_the_tenant(world):
    """The SAME daemon behaviour, one bind further along.

    The restore container binds `<staging_root>/<id>:/staging:ro`. With that
    directory missing the daemon creates it -- empty -- and the restore then
    extracts nothing over a tenant whose old tree has just been packed away
    and removed. A restore that erases is worse than a restore that refuses,
    so this refuses BEFORE the archive container runs.
    """
    runtime, engine, _config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    before = len(engine.created)

    with pytest.raises(RuntimeError, match="nothing staged"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert len(engine.created) == before, (
        "the refusal came after containers had already run; a restore that "
        "cannot finish must not start by archiving and removing the tenant's "
        "data.")
