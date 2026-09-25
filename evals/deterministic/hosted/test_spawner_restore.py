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
    nothing in it. What a FINISHED backup leaves is made here: the two
    directories AND the manifest it writes last. Without the manifest the
    restore guard refuses, which is GC-3 and is what the table above covers.
    """
    staging = config.staging_root / TENANT
    (staging / "home").mkdir(parents=True, exist_ok=True)
    (staging / "env").mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(_WHOLE, encoding="utf-8")


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

    with pytest.raises(RuntimeError, match="backup|manifest"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert len(engine.created) == before, (
        "the refusal came after containers had already run; a restore that "
        "cannot finish must not start by archiving and removing the tenant's "
        "data.")


def _make(staging, layout) -> None:
    """Build one staging shape. `layout` is a dict of path -> contents, where
    None means "a directory" and a string means "a file with this text"."""
    staging.mkdir(parents=True, exist_ok=True)
    for relative, contents in layout.items():
        target = staging / relative
        if contents is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")


_WHOLE = '{"version": 1, "parts": ["home", "env"], "state_db": false}'

# Every shape a backup can leave behind short of a finished one. THE THIRD ROW
# IS THE ONE THAT COST A ROUND: `_BACKUP_SCRIPT`'s FIRST line is
# `mkdir -p /staging/home /staging/env`, so both directories exist -- empty --
# from the first instant of a backup, before the sqlite3 copy and before either
# tar. A guard that asked whether they were directories therefore accepted
# every backup that died after line one: a locked or corrupt state.db failing
# under `set -e`, a full disk, a killed container, a restarted spawner. The
# restore then archived the live tree, emptied both mounts, re-provisioned them
# empty, ran two `tar | tar` pipelines over empty sources that SUCCEEDED --
# pipefail does not help, because nothing failed -- and answered {"ok": True}.
# The tenant came back with an empty state.db and the operator was told it
# worked.
#
# Shape cannot tell a finished backup from an interrupted one. Only the backup
# can say, and it says it last.
_PARTIAL = {
    "nothing at all": {},
    "the staging root and nothing in it": {".keep": ""},
    # mkdir -p ran and then the backup died. The dangerous one.
    "both directories, empty, no manifest": {"home": None, "env": None},
    "both directories with data, no manifest": {"home/state.db": "x",
                                                "env/.env": "y"},
    "a manifest naming a part that is not there": {
        "home": None, "manifest.json": _WHOLE},
    "a manifest that does not parse": {
        "home": None, "env": None, "manifest.json": "{not json"},
    "a manifest that is not an object": {
        "home": None, "env": None, "manifest.json": "[1, 2, 3]"},
    "a manifest from a version this cannot read": {
        "home": None, "env": None,
        "manifest.json": '{"version": 99, "parts": ["home", "env"]}'},
    "a manifest naming no parts": {
        "home": None, "env": None, "manifest.json": '{"version": 1, "parts": []}'},
    "a manifest claiming a state.db that is not there": {
        "home": None, "env": None,
        "manifest.json": '{"version": 1, "parts": ["home", "env"], '
                         '"state_db": true}'},
}


@pytest.mark.parametrize("shape", sorted(_PARTIAL))
def test_a_restore_refuses_anything_that_is_not_a_whole_backup(world, shape):
    """GC-3. The guard must ask the BACKUP whether it finished, not the
    filesystem what it looks like.

    `_BACKUP_SCRIPT` writes `manifest.json` as its LAST action -- after the
    database copy and after both tars -- and removes any previous one as its
    FIRST, so a re-run that dies halfway cannot leave the previous run's
    manifest standing over this run's partial data. A backup that died has no
    manifest and cannot be restored from, which is the property three rounds of
    shape checks were reaching for.

    Everything short of a whole backup is refused BEFORE `_archive` runs,
    because once the old tree is packed away and the two directories are gone,
    a restore that cannot finish has already destroyed what it was restoring.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _make(config.staging_root / TENANT, _PARTIAL[shape])
    before = len(engine.created)

    with pytest.raises(RuntimeError, match="backup|manifest"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert len(engine.created) == before, (
        f"{shape!r} was refused only after containers had already run; a "
        "restore that cannot finish must not start by archiving and removing "
        "the tenant's data.")


def test_a_restore_accepts_a_whole_backup_of_an_empty_tenant(world):
    """The other direction, and the reason the manifest has to be the test
    rather than the directories' contents: a tenant who has written nothing has
    a backup of two EMPTY directories, and that is a complete backup. Shape
    cannot tell it from an interrupted one; the manifest can."""
    runtime, _engine, config, claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _make(config.staging_root / TENANT,
          {"home": None, "env": None, "manifest.json": _WHOLE})
    claimed.clear()

    assert asyncio.run(runtime.task(TENANT, "restore", PROJECT)) == {"ok": True}
    dirs = docker_mod.tenant_dirs(config.tenant_root, TENANT)
    assert set(claimed) == {str(dirs.home), str(dirs.env)}


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

    with pytest.raises(RuntimeError, match="refusing to restore through it"):
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

    `tar`, `zstd`, `sqlite3`, `mkdir`, `find` and `rm` are resolved through
    PATH, so this intercepts them without any of the scripts' absolute
    container paths having to exist. Every stub APPENDS ITS NAME AND ARGUMENTS to $WAKU_TOOL_LOG, so
    the test can see how far the script got, and `tar --create` -- the SOURCE
    end of every pipeline -- exits 1 when asked to, which is the shape of a
    half-read backup.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(parents=True)
    log_line = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$WAKU_TOOL_LOG"\n'
    for name in ("mkdir", "sqlite3", "zstd", "find", "rm"):
        (binaries / name).write_text("#!/bin/sh\n" + log_line + "exit 0\n",
                                     encoding="utf-8")
    code = 1 if failing_tar_create else 0
    (binaries / "tar").write_text(
        "#!/bin/sh\n" + log_line
        + 'for arg in "$@"; do\n'
          f'  if [ "$arg" = "--create" ]; then exit {code}; fi\n'
          "done\n"
          "exit 0\n",
        encoding="utf-8")
    for entry in binaries.iterdir():
        entry.chmod(0o755)
    return binaries


def _run_script(script: str, argv: list[str], binaries, log: Path):
    import os as _os
    import subprocess
    log.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-euc", script, *argv],
        env={**_os.environ, "PATH": f"{binaries}:{_os.environ['PATH']}",
             "WAKU_TOOL_LOG": str(log)},
        capture_output=True, text=True, timeout=30, check=False)
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    return proc.returncode, calls


_SCRIPTS = {
    "backup": (docker_mod._BACKUP_SCRIPT, ["backup"]),
    "restore": (docker_mod._RESTORE_SCRIPT, ["restore"]),
    "archive": (docker_mod._ARCHIVE_SCRIPT, ["archive", "a-tenant-20260101T000000Z"]),
}


@pytest.mark.parametrize("name", sorted(_SCRIPTS))
def test_a_failing_source_stops_the_script_at_the_first_pipeline(tmp_path, name):
    """`bash -euc` alone does NOT fail a pipeline whose FIRST stage failed.

    Measured, not assumed:

        bash -euc 'tar --create --directory /gone . | tar --extract ...'  -> 0
        bash -euc 'set -o pipefail
                   tar --create --directory /gone . | tar --extract ...'  -> 1

    Exit ZERO -- not "fails with the sink's error". _run_to_completion only
    raises on a non-zero exit, so without pipefail a restore that read nothing
    would have returned {"ok": True} over a tenant whose tree had just been
    archived and removed, and a backup that copied nothing would have reported
    a path an operator would later restore from.

    THE DISCRIMINATOR IS HOW FAR THE SCRIPT GOT, not its exit code alone. Each
    of these scripts has TWO pipelines. With pipefail, a failing source stops
    the script at the first and the second never runs; without it, both run and
    the script carries on to the end. Counting `tar --create` invocations says
    which happened, and it does not depend on any of the scripts' absolute
    container paths existing.

    It drives the three real constants through a real bash. It is not a check
    that the string "pipefail" appears in a source file.
    """
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine; the spawner's scripts run under "
                    "bash -euc inside the services image")
    script, argv = _SCRIPTS[name]
    log = tmp_path / "calls.log"

    _code, healthy = _run_script(script, argv,
                                 _stub_bin(tmp_path / "ok", failing_tar_create=False),
                                 log)
    creates = [line for line in healthy if "--create" in line]
    assert len(creates) == 2, (
        f"the {name} script ran {len(creates)} `tar --create` with every tool "
        f"succeeding, not 2: {healthy}. The count below is meaningless unless "
        "this script really does have two pipelines.")

    code, broken = _run_script(script, argv,
                               _stub_bin(tmp_path / "bad", failing_tar_create=True),
                               log)
    creates = [line for line in broken if "--create" in line]
    assert code != 0, (
        f"the {name} script exited 0 with its source `tar --create` failing. "
        "_run_to_completion only raises on a non-zero exit, so this operation "
        "would report success having moved no data.")
    assert len(creates) == 1, (
        f"the {name} script carried on to its second pipeline after the first "
        f"one's source failed: {broken}. `bash -euc` does not fail a pipeline "
        "on its first stage; `set -o pipefail` is what makes the script stop.")


# --- the shared roots, per tenant ----------------------------------------


@pytest.mark.parametrize("task,root,binds_at", [
    ("backup", "staging_root", "/staging"),
    ("archive", "archive_root", "/archive"),
])
def test_a_task_is_handed_its_own_directory_under_a_shared_root(world, task, root,
                                                                binds_at):
    """GC-1. A task container runs as UID 10001 and creates files in what it is
    given, so what it is given must be writable by 10001 -- and it must be THIS
    tenant's directory, not the shared root, or every tenant's staging and
    every tenant's archive sit inside every other tenant's task container.

    `_backup` chowned its staging; `_archive` never learned that and was handed
    the shared archive root, root-owned at 0755, so `zstd -o /archive/...` was
    EACCES, `bash -euc` exited non-zero, and every archive -- and therefore
    every restore, which archives first -- failed with jsonsock's opaque error.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    engine.created.clear()

    asyncio.run(runtime.task(TENANT, task))

    shared = getattr(config, root)
    mine = shared / TENANT
    assert mine.is_dir(), f"{task} did not create {mine}"
    assert mine.stat().st_mode & 0o777 == docker_mod.TENANT_DIR_MODE, (
        f"{mine} is not {oct(docker_mod.TENANT_DIR_MODE)}, so another tenant's "
        "task container could read it if a bind is ever wider than it should be")

    binds = [bind for _name, body in engine.created
             for bind in body["HostConfig"]["Binds"]]
    assert f"{mine}:{binds_at}" in binds, (
        f"{task} did not mount its own directory at {binds_at}: {binds}")
    assert f"{shared}:{binds_at}" not in binds, (
        f"{task} mounted the SHARED {root} at {binds_at}. Every other tenant's "
        "data under it is then inside a container running as 10001 with this "
        "tenant's files.")
    for bind in binds:
        source = bind.split(":", 1)[0]
        assert source != str(shared), (
            f"{task} mounted the shared {root}: {bind}")


def test_a_shared_root_entry_that_is_a_symlink_is_refused(world, tmp_path):
    """The same guard `_backup` already had, now on the shared path both tasks
    go through. This process is root: `mkdir(exist_ok=True)` re-checks with
    `is_dir()`, which follows a link, and `chown` follows too."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    victim = tmp_path / "victim"
    victim.mkdir()
    config.archive_root.mkdir(parents=True, exist_ok=True)
    (config.archive_root / TENANT).symlink_to(victim)

    with pytest.raises(RuntimeError, match="refusing to use it"):
        asyncio.run(runtime.task(TENANT, "archive"))


def test_a_backup_clears_the_previous_one_before_it_writes(tmp_path):
    """NEW-2. Staging holds ONE backup, not the union of every backup taken.

    `rm -f manifest.json` invalidated the old backup and nothing emptied the
    old `home/` and `env/`, so the manifest described THIS backup while the
    directories held everything every previous one had left. A file the tenant
    deleted came back on the next restore, and a manifest saying
    `state_db: false` above an earlier backup's `home/state.db` restored a
    database it said was never copied -- after archiving and wiping the live
    tree, answering {"ok": True}.

    ORDER IS THE WHOLE OF IT, so order is what this asserts, from the call log
    of a real bash run:

      1. the manifest goes FIRST, so a backup that dies after this point leaves
         nothing restorable rather than something that lies;
      2. both part directories are emptied BEFORE anything is copied in;
      3. and only then do the tars run.

    The Docker half -- that a deleted file really does not come back -- is
    test_isolation.py::test_a_backup_does_not_resurrect_a_file_the_tenant_deleted.
    """
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine")
    log = tmp_path / "calls.log"
    _code, calls = _run_script(docker_mod._BACKUP_SCRIPT, ["backup"],
                               _stub_bin(tmp_path / "ok", failing_tar_create=False),
                               log)

    def first(predicate) -> int:
        for index, line in enumerate(calls):
            if predicate(line):
                return index
        raise AssertionError(f"no call matching that: {calls}")

    removed_manifest = first(lambda line: line.startswith("rm ")
                             and "manifest.json" in line)
    cleared = first(lambda line: line.startswith("find ") and "-delete" in line)
    first_copy = first(lambda line: "--create" in line or line.startswith("sqlite3 "))

    assert removed_manifest < cleared < first_copy, (
        "a backup must invalidate the old manifest, then empty both part "
        f"directories, then copy. The order was: {calls}")
    cleared_line = calls[cleared]
    for part in ("/staging/home", "/staging/env"):
        assert part in cleared_line, (
            f"{part} is not emptied before the copy: {cleared_line!r}. "
            "Whatever a previous backup left there survives into this one.")
    assert "-mindepth 1" in cleared_line, (
        "without -mindepth 1 the mount points themselves go, and the tars then "
        f"extract into paths that are not there: {cleared_line!r}")


# --- the manifest reader's own branches -----------------------------------
#
# Three guards were written and none was driven. A guard nobody drove is a
# guard nobody knows the shape of, which is most of what this task has cost.

_SQLITE_HEADER = b"SQLite format 3\x00"


def test_a_symlinked_manifest_is_refused(world, tmp_path):
    """Written as `O_NOFOLLOW` plus an `is_symlink()` check and never driven.

    This is root opening a path a few directories from tenant data. The tar
    extraction cannot put a link here -- `tar --extract` refuses absolute and
    `..` paths and the parts go into home/ and env/ -- so it is not reachable
    today, which is exactly why it had no test and exactly why it needs one:
    the next change to where staging comes from is F3's restic restore.
    """
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    _make(staging, {"home": None, "env": None})
    elsewhere = tmp_path / "somebody-elses-manifest.json"
    elsewhere.write_text(_WHOLE, encoding="utf-8")
    (staging / "manifest.json").symlink_to(elsewhere)

    with pytest.raises(RuntimeError, match="refusing to restore through it"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_a_manifest_larger_than_a_manifest_is_refused(world):
    """The 4 KiB cap, driven. A manifest is under a hundred bytes; the cap is
    there so a root process never reads an unbounded file sitting beside tenant
    data, and `_MANIFEST_MAX_BYTES + 1` is read so the cap can be detected
    rather than silently truncating into a parse error."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    padding = " " * (docker_mod._MANIFEST_MAX_BYTES + 100)
    _make(staging, {"home": None, "env": None,
                    "manifest.json": _WHOLE.rstrip("}") + f', "pad": "{padding}"}}'})

    with pytest.raises(RuntimeError, match="larger than a manifest"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_a_manifest_at_the_cap_is_still_read(world):
    """The other side of the boundary, so the cap cannot be tightened into
    refusing manifests that are fine."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    body = _WHOLE.rstrip("}") + ', "pad": "%s"}'
    padding = "x" * (docker_mod._MANIFEST_MAX_BYTES - len(body % ""))
    _make(staging, {"home": None, "env": None, "manifest.json": body % padding})
    assert len((staging / "manifest.json").read_text(encoding="utf-8")) == \
        docker_mod._MANIFEST_MAX_BYTES

    assert asyncio.run(runtime.task(TENANT, "restore", PROJECT)) == {"ok": True}


@pytest.mark.parametrize("contents,why", [
    (b"", "a zero-byte file, which is what a killed `sqlite3 .backup` leaves"),
    (b"not a database at all", "a file that is not a database"),
    (b"SQLite format 2\x00" + b"\x00" * 100, "an older header this cannot read"),
])
def test_a_manifest_claiming_a_database_needs_a_real_one(world, contents, why):
    """`is_file()` was the check, and a zero-byte state.db satisfies it.

    A backup killed between creating the file and filling it leaves exactly
    that, and the manifest above it says a database was copied. The restore
    then archives and wipes the live tree, extracts an empty database, and
    reports success -- the tenant's assistant comes back with no memory and
    nothing says why. `sqlite3 .backup` writes a whole database or fails, so
    the header is what is asked for.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    _make(staging, {"home": None, "env": None,
                    "manifest.json": '{"version": 1, "parts": ["home", "env"], '
                                     '"state_db": true}'})
    (staging / "home" / "state.db").write_bytes(contents)

    before = len(engine.created)
    with pytest.raises(RuntimeError, match="SQLite's header"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))
    # NF-3. The old second assertion was `"state.db" in str(exc)`, which cannot
    # fail while the `match=` above passes: the message interpolates the path,
    # and the path ends in state.db. What is worth asserting instead is what
    # the refusal PREVENTED -- no container ran, so the tenant's live tree was
    # never archived or removed for a backup that could not restore. `why`
    # names which malformed database this parameter is.
    assert len(engine.created) == before, (
        f"the restore ran containers before refusing {why}; the refusal has to "
        "come before the archive, or the tenant has already been destroyed.")


def test_a_manifest_claiming_a_database_accepts_a_real_one(world):
    """The presence half: a backup that really did copy a database restores."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    _make(staging, {"home": None, "env": None,
                    "manifest.json": '{"version": 1, "parts": ["home", "env"], '
                                     '"state_db": true}'})
    (staging / "home" / "state.db").write_bytes(_SQLITE_HEADER + b"\x00" * 500)

    assert asyncio.run(runtime.task(TENANT, "restore", PROJECT)) == {"ok": True}


def test_a_manifest_denying_a_database_above_one_is_refused(world):
    """The other direction, which only became checkable once the backup clears
    its part directories: a `state_db: false` manifest sitting above a real
    database means this staging holds two backups' worth of truth, and the
    manifest is the one that is supposed to be authoritative."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    _make(staging, {"home": None, "env": None, "manifest.json": _WHOLE})
    (staging / "home" / "state.db").write_bytes(_SQLITE_HEADER + b"\x00" * 500)

    with pytest.raises(RuntimeError, match="disagree"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


@pytest.mark.parametrize("claimed", ['"true"', "1", "0", "null", '"yes"', "[]"])
def test_a_manifest_whose_state_db_is_not_a_boolean_is_refused(world, claimed):
    """NF-2. `state_db` was the one field read with `.get()` and compared to
    `True`, so `"true"` as a string, `1`, and a missing key all fell through to
    the LENIENT answer -- "this backup copied no database" -- reached by three
    different kinds of malformed manifest.

    Every other field is type-checked. F3 writes these manifests from restic
    snapshots, so a manifest this process did not produce stops being
    hypothetical the moment that lands.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _make(config.staging_root / TENANT,
          {"home": None, "env": None,
           "manifest.json": '{"version": 1, "parts": ["home", "env"], '
                            f'"state_db": {claimed}}}'})
    before = len(engine.created)

    with pytest.raises(RuntimeError, match="not a boolean"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))
    assert len(engine.created) == before


def test_a_manifest_with_no_state_db_key_is_refused(world):
    """The missing-key case, which is the one a hand-written or older manifest
    actually has. It used to mean "no database", which is a decision this
    process was making on the manifest's behalf."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _make(config.staging_root / TENANT,
          {"home": None, "env": None,
           "manifest.json": '{"version": 1, "parts": ["home", "env"]}'})

    with pytest.raises(RuntimeError, match="not a boolean"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_a_symlinked_state_db_is_refused(world, tmp_path):
    """NF-2's other half. `manifest.json` gets an is_symlink() refusal and
    O_NOFOLLOW two levels up; `home/state.db` was a bare root open, inside the
    half of staging that comes from the tenant's own files. Unreachable through
    `tar --extract`, which refuses absolute and `..` paths -- and reachable
    through F3's restic path, which fills these directories from somewhere this
    process did not control."""
    runtime, _engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    staging = config.staging_root / TENANT
    _make(staging, {"home": None, "env": None,
                    "manifest.json": '{"version": 1, "parts": ["home", "env"], '
                                     '"state_db": true}'})
    real = tmp_path / "somebody-elses.db"
    real.write_bytes(_SQLITE_HEADER + b"\x00" * 500)
    (staging / "home" / "state.db").symlink_to(real)

    with pytest.raises(RuntimeError, match="refusing to read through it"):
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))


def test_backup_and_archive_mount_the_directory_the_helper_prepared(world):
    """NF-4. The NEW-5 fix -- `_backup` taking the shared helper's return value
    instead of computing a second path beside it -- was the only change in that
    commit with no eval, so reverting it left the suite green. That is GC-1's
    asymmetry exactly: one expression chowns and another one binds.

    Asserted as an IDENTITY between what the helper prepared and what the
    container was given, for both callers, rather than by comparing two strings
    that happen to agree today.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))

    for task, root, mount in (("backup", config.staging_root, "/staging"),
                              ("archive", config.archive_root, "/archive")):
        engine.created.clear()
        prepared = runtime._tenant_directory_under(root, TENANT)
        asyncio.run(runtime.task(TENANT, task))
        bound = {bind.split(":", 1)[0] for _name, body in engine.created
                 for bind in body["HostConfig"]["Binds"]
                 if bind.split(":")[1] == mount}
        assert bound == {str(prepared)}, (
            f"{task} mounted {bound} at {mount}, and the helper prepared "
            f"{prepared}. Whatever the helper chowned to 10001 is not what the "
            "container was handed, which is EACCES for one of them and a "
            "second expression for one location for both.")

def _busy_with(engine, kind: str) -> None:
    """The daemon reports one container of `kind` holding this tenant.

    `_refuse_if_busy` reads the label query, so this is what an operator's
    `tenant.sh inspect` -- or a task that outlived its run -- looks like from
    inside the runtime.
    """
    async def containers(*, label=None, all_states=False):
        if label == f"{template.LABEL_TENANT}={TENANT}":
            return [{"Id": "deadbeefcafe",
                     "Labels": {template.LABEL_TENANT: TENANT,
                                template.LABEL_KIND: kind}}]
        return []

    engine.containers = containers


@pytest.mark.parametrize("kind", sorted(template.BLOCKING_KINDS))
def test_a_restore_refuses_while_a_container_holds_the_tenants_mounts(world, kind):
    """The dead-inode failure, from the one direction `stop-all` cannot reach.

    `task_container` and `inspect_container` bind `home` and `env` exactly as
    the tenant container does, and those are the two directories a restore
    removes and re-creates. But the gateway stops a tenant by id and `stop`
    only knows how to name a KIND_TENANT container -- so an inspect container,
    which is operator-started and lives until `inspect-stop`, survives both
    `launcher.stop` and `stop-all` and then has its mount removed underneath
    it. `designs/backup-restore-integrity.md` records what that costs: the next
    `docker exec` reports "possible container breakout detected".

    THE TREE MUST BE UNTOUCHED AFTERWARDS. An exit code alone would pass with
    the refusal placed after `_archive`, which is the point at which the
    tenant's live directories have already been packed away and removed.
    """
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _backed_up(runtime, config)
    dirs = docker_mod.tenant_dirs(config.tenant_root, TENANT)
    (dirs.home / "state.db").write_bytes(b"SQLite format 3\x00")
    _busy_with(engine, kind)

    with pytest.raises(docker_mod.Busy) as refused:
        asyncio.run(runtime.task(TENANT, "restore", PROJECT))

    assert kind in str(refused.value)
    assert (dirs.home / "state.db").read_bytes() == b"SQLite format 3\x00"
    # And nothing was packed away: _archive runs after this point, so an
    # archive here would mean the refusal landed too late to matter.
    assert list((config.archive_root / TENANT).glob("*pre-restore*")) == []


def test_a_restore_is_not_refused_by_the_spawners_own_provision_container(world):
    """KIND_PROVISION is the spawner's own bookkeeping, not an operator's
    container, and `_refuse_if_busy` has always excluded it. A restore that
    refused itself over one would be unrunnable -- provision() creates one on
    the way through the restore itself."""
    runtime, engine, config, _claimed, _limited = world
    asyncio.run(runtime.provision(TENANT, PROJECT))
    _backed_up(runtime, config)
    _busy_with(engine, template.KIND_PROVISION)

    assert asyncio.run(runtime.task(TENANT, "restore", PROJECT)) == {"ok": True}
