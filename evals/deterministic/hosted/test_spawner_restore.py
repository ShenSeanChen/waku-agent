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


def _stage(config) -> None:
    """The shape _BACKUP_SCRIPT leaves behind.

    FakeEngine runs no script -- it is a model of the daemon's bookkeeping, not
    of bash -- so a `backup` through it creates the staging directory and
    nothing in it. The two subdirectories a real backup writes are made here,
    so the restore guard passes and the test that follows is about what it says
    it is about rather than about the guard.
    """
    staging = config.staging_root / TENANT
    (staging / "home").mkdir(parents=True, exist_ok=True)
    (staging / "env").mkdir(parents=True, exist_ok=True)


def _backed_up(runtime, config) -> None:
    """A backup, and the files it would have written."""
    asyncio.run(runtime.task(TENANT, "backup"))
    _stage(config)


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
    _backed_up(runtime, config)
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
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _backed_up(runtime, config)
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
    _backed_up(runtime, config)
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

    with pytest.raises(RuntimeError, match="staged"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert len(engine.created) == before, (
        "the refusal came after containers had already run; a restore that "
        "cannot finish must not start by archiving and removing the tenant's "
        "data.")


@pytest.mark.parametrize("staged", [
    # Nothing at all -- the common case, and the one the first guard caught.
    (),
    # A directory that exists and is EMPTY. The guard that only asked "is it a
    # directory" let this through, and the destruction then happened: the old
    # tree archived, both directories removed, and the restore container left
    # to fail afterwards. Same shape as C2-1 -- the guard looks right and the
    # dangerous case walks through it.
    ("",),
    # Half a backup. _BACKUP_SCRIPT makes both; one without the other is an
    # interrupted backup, and restoring from it silently drops the other mount.
    ("home",),
    ("env",),
    # Named like the backup's output but not directories.
    ("home/", "env-file"),
])
def test_a_restore_refuses_anything_that_is_not_a_whole_backup(world, staged):
    """NEW-1. The guard has to test CONTENT, not existence.

    _BACKUP_SCRIPT writes `<staging>/home` and `<staging>/env`, so that pair is
    what a restore consumes and what this asks for. Everything short of it is
    refused BEFORE `_archive` runs, because by the time the archive container
    has packed the old tree away and the trees have been removed, a restore
    that cannot finish has already destroyed what it was restoring.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    for entry in staged:
        if entry == "":
            staging.mkdir(parents=True, exist_ok=True)
        elif entry.endswith("/"):
            (staging / entry.rstrip("/")).mkdir(parents=True, exist_ok=True)
        else:
            staging.mkdir(parents=True, exist_ok=True)
            (staging / entry).write_text("not a directory", encoding="utf-8")
    before = len(engine.created)

    with pytest.raises(RuntimeError, match="staged"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert len(engine.created) == before, (
        "the refusal came after containers had already run; a restore that "
        "cannot finish must not start by archiving and removing the tenant's "
        "data.")


def test_a_restore_refuses_a_symlinked_staging_directory(world, tmp_path):
    """`is_dir()` follows a link, and `_backup` refuses one outright -- so
    without this the two guards on the same path disagree about the same class
    of input. Nothing a tenant writes reaches staging_root today; this process
    is root and the module's whole subject is planted links."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    real = tmp_path / "somewhere-else"
    (real / "home").mkdir(parents=True)
    (real / "env").mkdir(parents=True)
    config.staging_root.mkdir(parents=True, exist_ok=True)
    (config.staging_root / TENANT).symlink_to(real)

    with pytest.raises(RuntimeError, match="symlink"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_a_directory_that_survives_its_removal_stops_the_restore(world, monkeypatch):
    """NEW-2. The regression tripwire for this task's Critical, held in place.

    If anything re-creates a tenant directory between the removal and the
    provision, provision() takes its repeat path and that directory keeps XFS
    project 0 for good -- which is C2-1 exactly. The tripwire is what turns
    that back into a loud failure, and until now removing it failed nothing.

    `os.rmdir` is neutered here, which is the same way the reviewer forced a
    survivor by hand.
    """
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _backed_up(runtime, config)
    monkeypatch.setattr(docker_mod.os, "rmdir", lambda path: None)

    with pytest.raises(RuntimeError, match="still there after being removed"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_a_directory_replaced_by_a_dangling_symlink_also_stops_the_restore(
        world, monkeypatch):
    """The tripwire has to be TOTAL, so it uses os.path.lexists.

    `Path.exists()` follows a link and answers False for a dangling one, so a
    directory replaced by a broken symlink read as "successfully removed" --
    and `provision()`'s `mkdir` then raises FileExistsError on the link,
    reaching the operator as jsonsock's opaque error rather than as the thing
    that happened.

    Only root could plant such a link, and the realistic re-creator -- the
    daemon -- makes a directory. The check exists precisely for the
    unrealistic case, so it should cover it.
    """
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _backed_up(runtime, config)
    real_rmdir = docker_mod.os.rmdir

    def rmdir_then_plant(path):
        real_rmdir(path)
        Path(path).symlink_to(Path(path).parent / "does-not-exist")

    monkeypatch.setattr(docker_mod.os, "rmdir", rmdir_then_plant)

    with pytest.raises(RuntimeError, match="still there after being removed"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))




# --- the shell the spawner actually runs ---------------------------------

_STUB_OK = "#!/bin/sh\nexit 0\n"


def _stub_bin(tmp_path, *, failing_tar_create: bool):
    """A PATH holding stand-ins for the tools the three scripts call.

    `tar` is resolved through PATH, so this intercepts it without any of the
    scripts' absolute paths having to exist: the stub never touches the
    filesystem. With `failing_tar_create`, the `--create` end of each pipeline
    -- the SOURCE -- exits 1 and everything else succeeds, which is the exact
    shape of a half-read backup.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(parents=True)
    for name in ("mkdir", "sqlite3", "zstd"):
        (binaries / name).write_text(_STUB_OK, encoding="utf-8")
    code = 1 if failing_tar_create else 0
    (binaries / "tar").write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        f'  if [ "$arg" = "--create" ]; then exit {code}; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8")
    for entry in binaries.iterdir():
        entry.chmod(0o755)
    return binaries


def _run_script(script: str, argv: list[str], binaries) -> int:
    import os as _os
    import subprocess
    return subprocess.run(
        ["bash", "-euc", script, *argv],
        env={**_os.environ, "PATH": f"{binaries}:{_os.environ['PATH']}"},
        capture_output=True, text=True, timeout=30, check=False).returncode


_SCRIPTS = {
    "backup": (docker_mod._BACKUP_SCRIPT, ["backup"]),
    "restore": (docker_mod._RESTORE_SCRIPT, ["restore"]),
    "archive": (docker_mod._ARCHIVE_SCRIPT, ["archive", "a-tenant-20260101T000000Z"]),
}


@pytest.mark.parametrize("name", sorted(_SCRIPTS))
def test_a_failing_source_fails_the_whole_pipeline(tmp_path, name):
    """`bash -euc` alone does NOT fail a pipeline whose FIRST stage failed.

    Measured, not assumed: without `set -o pipefail`,

        tar --create --file - --directory /gone . | tar --extract ... ; echo $?

    prints 0. So a restore that read nothing would have reported SUCCESS and
    the operation would have returned {"ok": True} over a tenant whose old tree
    had just been archived and removed. The review called this a diagnostics
    problem -- a source error masked into a sink error -- and it is worse than
    that: the exit code is 0 and _run_to_completion never raises at all.

    This DRIVES the real script constants through a real bash, with `tar` stubbed
    on PATH so none of their absolute paths has to exist. It is not a check that
    the string "pipefail" appears in the source.
    """
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine; the spawner's scripts run under "
                    "bash -euc inside the services image")
    script, argv = _SCRIPTS[name]

    healthy = _run_script(script, argv, _stub_bin(tmp_path / "ok",
                                                  failing_tar_create=False))
    assert healthy == 0, (
        f"the {name} script fails even when every tool succeeds, so the "
        "assertion below would pass for the wrong reason")

    broken = _run_script(script, argv, _stub_bin(tmp_path / "bad",
                                                 failing_tar_create=True))
    assert broken != 0, (
        f"the {name} script exited 0 with its source `tar --create` failing. "
        "_run_to_completion only raises on a non-zero exit, so this operation "
        "would report success having moved no data.")
