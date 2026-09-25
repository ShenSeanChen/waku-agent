"""DockerRuntime: the five operations ports.TenantRuntime names, and no sixth.

The spawner is root with CAP_SYS_ADMIN and the data disk's block device. EVERY
NEW OPERATION HERE IS A NEW PRIVILEGED VERB, so this class implements exactly
the five in the Protocol and exactly the five tasks core/requests.TASKS names.
Widening either set is a spec change.

What each verb can and cannot do, so a reviewer can check the list rather than
the code:

  provision  creates two empty directories under the tenant root, sets their
             XFS project id and limit, hands them to UID 10001, and runs ONE
             throwaway container with no network and only those two mounts.
             It cannot touch any path outside <tenant_root>/<validated id>/,
             because tenant_dirs refuses anything that is not a tenant id.
  start      creates and starts ONE container, from the fixed template, at the
             address the project id derives. It cannot choose an image, a
             network, a mount, a capability or a user: every one of those is
             in template.py and none is a parameter.
  stop       stops the container labelled with this tenant id. It cannot stop
             a container it did not label.
  list       reads. It writes nothing.
  task       runs ONE throwaway container with a command from a five-entry
             table. The command is not a parameter: the caller names a task,
             and the table names the command.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

from hosted import log
from hosted.core.tenant import (
    address_for_project,
    is_project_id,
    is_tenant_id,
    tenant_dirs,
)
from hosted.ports.runtime import RunningContainer
from hosted.spawner import template, xfsquota
from hosted.spawner.engine import Engine

_LOG = log.get(__name__)

TENANT_DIR_MODE = 0o700

# The one value of WAKU_DATA_DEVICE that means "there is no XFS here, set no
# quota". It has to be typed, install.sh never writes it, and every provision
# under it logs a WARNING. It exists because evals/hosted_docker/ has to be
# able to start a spawner on a machine with no XFS -- a maintainer's Docker
# Desktop, or the hosted-docker job's steps that run before the loop mount --
# and the alternative, letting an empty string mean the same thing, is how a
# misspelling in install.sh silently turns every tenant's disk limit off.
# config_from_env refuses an EMPTY value for every name, so the two cannot be
# confused. test_container_template.py pins both halves.
NO_QUOTA_DEVICE = "none"

# `sqlite3 .backup` is SQLite's ONLINE backup: it is safe against a live
# database, which a plain copy is not. The *-wal, *-shm and *-journal files are
# left out on purpose -- they belong to the live database and would corrupt the
# copy on restore -- and every other file is copied as-is, symlinks kept as
# symlinks, because restic stores a symlink as a symlink and never follows it.
_BACKUP_SCRIPT = """
mkdir -p /staging/home /staging/env
if [ -f /data/state.db ]; then
  sqlite3 /data/state.db ".backup '/staging/home/state.db'"
fi
tar --create --file - --directory /data \
    --exclude 'state.db' --exclude '*-wal' --exclude '*-shm' --exclude '*-journal' . \
  | tar --extract --file - --directory /staging/home
tar --create --file - --directory /work \
    --exclude '*-wal' --exclude '*-shm' --exclude '*-journal' . \
  | tar --extract --file - --directory /staging/env
"""

_RESTORE_SCRIPT = """
tar --create --file - --directory /staging/home . | tar --extract --file - --directory /data
tar --create --file - --directory /staging/env  . | tar --extract --file - --directory /work
"""

# zstd because the services image carries it and a deleted tenant's tree is
# kept, not read. The two mounts go into one archive so a restore that needs
# the pre-restore state gets both halves or neither.
_ARCHIVE_SCRIPT = """
tar --create --directory /data . | zstd -q -o "/archive/$1-home.tar.zst"
tar --create --directory /work . | zstd -q -o "/archive/$1-env.tar.zst"
"""

# Emptying a tenant's tree is a WALK, so it happens HERE -- as UID 10001,
# inside a throwaway container, with only that tenant's mounts -- and never on
# the host. `find -delete` removes a planted symlink as a link and does not
# descend it. The host's own part is one rmdir per directory, which is a single
# syscall over a directory this process just watched become empty; it fails
# loudly if anything is left rather than reaching for a recursive remove.
_EMPTY_SCRIPT = """
find "$1" -mindepth 1 -delete
"""


class Busy(RuntimeError):
    """A task or inspect container holds this tenant.

    The maintenance mark lives in the gateway's memory and would be lost on a
    restart, so the spawner enforces it too: it labels every task and inspect
    container with its tenant id and refuses `start` while one exists. Two
    dashboards never run on the same state.db (spec, "Maintenance").
    """


class DockerRuntime:
    def __init__(self, config: template.SpawnerConfig, engine: Engine) -> None:
        self._config = config
        self._engine = engine
        # Per tenant, created on first use. Never pruned: an asyncio.Lock is
        # 48 bytes and a VM holds about 65,000 tenants, so the whole table is
        # under 4 MB at the theoretical maximum, and a pruned lock is a race.
        self._locks: dict[str, asyncio.Lock] = {}

    # --- provisioning ----------------------------------------------------

    async def provision(self, tenant_id: str, project_id: int) -> None:
        """Create what is missing. Runs before EVERY start, so a failed first
        provision is repaired on the next one.

        THE CREATE PATH AND THE REPEAT PATH ARE DIFFERENT, and this is the most
        important seven lines in the class.

        `xfs_quota project -s -p <path> <id>` is a RECURSIVE DESCENT. On an
        empty directory it visits nothing. On a populated one it is a root,
        CAP_SYS_ADMIN walk over a tree the tenant wrote -- the exact thing the
        spec forbids in three separate sentences, and the thing xfsquota.py's
        own docstring forbids in the same words. Since provisioning runs before
        EVERY start, calling it unconditionally would put that walk on the
        platform's hot path, on every start, for every tenant, forever.

        So: `claim()` only on a directory this call just created, and
        `set_limit()` on every later start. set_limit is keyed on the project
        id and names no path, so there is nothing for it to walk. A tenant
        whose project id was somehow lost is an operator's `xfsquota.repair`,
        run knowingly, and never something that happens behind a tenant's first
        request.
        """
        config = self._config
        dirs = tenant_dirs(config.tenant_root, tenant_id)   # refuses a bad id
        for directory in (dirs.home, dirs.env):
            # ORDER MATTERS AND IS THE WHOLE SECURITY PROPERTY:
            #   1. create it EMPTY, owned by root -- mkdir WITHOUT exist_ok, so
            #      "I created this" and "this was already here" are two
            #      different code paths and not one boolean nobody checks
            #   2. on the create path only, set the project id while it is
            #      still empty
            #   3. hand it to 10001
            #   4. only then let a container write into it
            try:
                directory.mkdir(parents=True)
                created = True
            except FileExistsError:
                created = False
            os.chmod(directory, TENANT_DIR_MODE)
            await self._apply_quota(tenant_id, directory, project_id, created)
            os.chown(directory, template.TENANT_UID, template.TENANT_UID)

        body = template.task_container(
            config, tenant_id=tenant_id,
            command=["python", "-m", "hosted.spawner.provision_main"],
            # NOT KIND_TASK. See template.KIND_PROVISION: labelling the
            # spawner's own bookkeeping as an operator task makes a tenant's
            # start refuse their own retried start with {"code": "busy"}.
            kind=template.KIND_PROVISION)
        await self._run_to_completion(body, tenant_id, "provision")
        _LOG.info("provisioned tenant=%s project=%s", tenant_id, project_id)

    async def _apply_quota(self, tenant_id: str, directory: Path,
                           project_id: int, created: bool) -> None:
        config = self._config
        if config.data_device == NO_QUOTA_DEVICE:
            # The one escape, and it has to be typed. install.sh never writes
            # it; only a test harness on a filesystem that is not XFS does.
            # Logged at WARNING on every provision rather than once at startup,
            # because a deployment that has drifted into this state has no disk
            # limits at all and should say so in every line an operator greps
            # for a tenant id.
            _LOG.warning(
                "tenant=%s provisioned with NO DISK QUOTA: WAKU_DATA_DEVICE "
                "is %r. This is a test setting.", tenant_id, NO_QUOTA_DEVICE)
            return
        if created:
            await xfsquota.claim(config.data_device, directory, project_id,
                                 config.tenant_disk_bytes)
        else:
            # No path, so no walk. This is also what picks up a changed
            # --tenant-disk on the next start, which is a small bonus and not
            # the reason it is here.
            await xfsquota.set_limit(config.data_device, project_id,
                                     config.tenant_disk_bytes)

    async def _run_to_completion(self, body: dict, tenant_id: str, what: str) -> str:
        """Create, start, wait, read the logs, remove. Raises on a non-zero exit.

        AutoRemove is OFF for every container this runs (template.task_container
        sets it False) and the `finally` below is what removes them. With it on,
        the daemon starts reaping the instant the container exits -- which is
        the instant `wait` returns -- so the `logs` call on the next line races
        the reaper and intermittently answers 404, which Engine._call turns into
        EngineError. Every start goes through provision goes through here, so
        that race would be an intermittent failure of the hot path, reaching the
        tenant as a start that failed for no stated reason and the operator as
        jsonsock's opaque "the handler failed".
        """
        name = (f"{template.container_name(tenant_id, template.KIND_TASK)}"
                f"-{what}-{os.getpid()}-{time.monotonic_ns()}")
        container = await self._engine.create(name, body)
        try:
            await self._engine.start(container)
            code = await self._engine.wait(container)
            output = await self._engine.logs(container)
            if code != 0:
                raise RuntimeError(f"{what} for {tenant_id} exited {code}: {output[-1000:]}")
            return output
        finally:
            await self._engine.remove(container)

    # --- the tenant's own container --------------------------------------

    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str) -> RunningContainer:
        if not is_tenant_id(tenant_id):
            # start() validates its OWN id before it does anything, including
            # before the label query below. The socket path has already run
            # core/requests.parse, so this is the in-process path's guard.
            raise ValueError(f"not a tenant id: {tenant_id!r}")
        # One tenant, one start at a time. Without this, the gateway's
        # documented retry ("a start that does not answer within 15 seconds ...
        # Try again.") can run concurrently with the start it is retrying, and
        # the two race over the same container name and the same directories.
        async with self._lock_for(tenant_id):
            return await self._start_locked(tenant_id, project_id, timezone, token)

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    async def _start_locked(self, tenant_id: str, project_id: int, timezone: str,
                            token: str) -> RunningContainer:
        await self._refuse_if_busy(tenant_id)
        await self.stop(tenant_id)          # exactly one container per tenant
        await self.provision(tenant_id, project_id)

        body = template.tenant_container(
            self._config, tenant_id=tenant_id, project_id=project_id,
            timezone=timezone, token=token)
        name = template.container_name(tenant_id, template.KIND_TENANT)
        # A previous container with this name may be mid-removal; the daemon
        # answers 409 and the name frees a moment later. Remove by name first,
        # which is a no-op when it is already gone.
        await self._engine.remove(name)
        container = await self._engine.create(name, body)
        await self._engine.start(container)
        address = address_for_project(project_id)
        _LOG.info("started tenant=%s project=%s address=%s token=%s tz=%s",
                  tenant_id, project_id, address, log.redact(token), timezone)
        return RunningContainer(tenant_id=tenant_id, address=address,
                                port=template.DASHBOARD_PORT)

    async def stop(self, tenant_id: str) -> None:
        if not is_tenant_id(tenant_id):
            raise ValueError(f"not a tenant id: {tenant_id!r}")
        name = template.container_name(tenant_id, template.KIND_TENANT)
        await self._engine.stop(name)
        # AutoRemove usually does this; a container that never started does not
        # get reaped, and its NAME would then block the next start.
        await self._engine.remove(name)
        _LOG.info("stopped tenant=%s", tenant_id)

    async def list(self) -> list[RunningContainer]:
        running = []
        for entry in await self._engine.containers(
                label=f"{template.LABEL_KIND}={template.KIND_TENANT}"):
            tenant_id = (entry.get("Labels") or {}).get(template.LABEL_TENANT, "")
            if not is_tenant_id(tenant_id):
                # A container carrying our kind label and a tenant id that is
                # not one is not ours. Logged, not adopted: the gateway treats
                # everything this returns as a tenant it may forward to.
                _LOG.warning("ignoring container %s with label tenant=%r",
                             entry.get("Id", "")[:12], tenant_id)
                continue
            networks = (entry.get("NetworkSettings") or {}).get("Networks") or {}
            address = (networks.get(template.TENANT_NETWORK) or {}).get("IPAddress", "")
            if not address:
                _LOG.warning("tenant=%s has no address on %s; not listing it",
                             tenant_id, template.TENANT_NETWORK)
                continue
            running.append(RunningContainer(tenant_id=tenant_id, address=address,
                                            port=template.DASHBOARD_PORT))
        return running

    async def _refuse_if_busy(self, tenant_id: str) -> None:
        """The maintenance mark, enforced by the spawner so it survives a
        gateway restart.

        It blocks on template.BLOCKING_KINDS -- KIND_TASK and KIND_INSPECT,
        which are the OPERATOR's containers. It does NOT block on
        KIND_PROVISION, the spawner's own bookkeeping, which every start
        creates on its way to starting the container: blocking on that would
        make a tenant's own retried start refuse itself with {"code": "busy"}
        and tell them they are under maintenance by their own first request.

        ONE query, not one per kind. The earlier shape issued the identical
        label query twice and filtered it differently each time.
        """
        found = await self._engine.containers(
            label=f"{template.LABEL_TENANT}={tenant_id}", all_states=False)
        for entry in found:
            kind = (entry.get("Labels") or {}).get(template.LABEL_KIND)
            if kind in template.BLOCKING_KINDS:
                raise Busy(
                    f"tenant {tenant_id} has a {kind} container running. "
                    "Starting their dashboard now would put two processes "
                    "on one state.db.")

    # --- the five file tasks ---------------------------------------------
    #
    # The COMMAND IS NOT A PARAMETER. The caller names a task from
    # core/requests.TASKS, and this table names the command. A spawner that
    # took a command would be a remote shell running as root's child with the
    # tenant's data mounted.

    async def task(self, tenant_id: str, task: str, project_id: int = 0) -> dict:
        """`project_id` is REQUIRED for restore and ignored by the other four.

        It is here because `restore` recreates the tenant's two directories
        empty and has to give them back their own project id, and the spawner
        has no way to look one up: the directories have just been removed, and
        "The spawner opens neither database". The gateway holds the value --
        the spec has it send the project id at provisioning and at start -- so
        it sends it here too. See the note in core/requests.py about widening
        _KEYS["task"].
        """
        if not is_tenant_id(tenant_id):
            raise ValueError(f"not a tenant id: {tenant_id!r}")
        if task == "restore" and not is_project_id(project_id):
            raise ValueError(
                "restore needs the tenant's project_id: it recreates the two "
                "directories empty and has to give them back their own id. A "
                "new id would give the tenant a new XFS accounting bucket AND "
                "a new fixed bridge address.")
        handler = {
            "backup": self._backup,
            "restore": self._restore,
            "archive": self._archive,
            "inspect": self._inspect,
            "inspect-stop": self._inspect_stop,
        }.get(task)
        if handler is None:
            # Unreachable through the socket -- core/requests.parse refuses a
            # task outside TASKS before this is called -- and kept, because
            # this class is also callable in-process by the gateway and a
            # KeyError here would read as a Docker fault.
            raise ValueError(f"not a task: {task!r}")
        if task == "restore":
            return await self._restore(tenant_id, project_id)
        return await handler(tenant_id)

    def _staging(self, tenant_id: str) -> Path:
        return self._config.staging_root / tenant_id

    async def _backup(self, tenant_id: str) -> dict:
        staging = self._staging(tenant_id)
        staging.mkdir(parents=True, exist_ok=True)
        os.chown(staging, template.TENANT_UID, template.TENANT_UID)
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _BACKUP_SCRIPT],
            extra_binds=(f"{staging}:/staging",))
        await self._run_to_completion(body, tenant_id, "backup")
        _LOG.info("backed up tenant=%s to %s", tenant_id, staging)
        return {"path": str(staging)}

    async def _restore(self, tenant_id: str, project_id: int) -> dict:
        """Pack the old tree away, recreate the directories empty WITH their
        project quota, and only then copy in.

        Packing and removing matters: a moved directory keeps its XFS project
        id, so an old tree left on disk would keep counting against the
        tenant's quota. The project id is never reused, so the tombstone in
        control.db stays; nothing here frees or reclaims one.

        THE PROJECT ID COMES FROM THE CALLER, and there is no other place it
        could come from. An earlier draft called a `_project_id_of(tenant_id)`
        helper, which cannot be written: `task` carried no project id, the two
        directories have just been removed so there is no inode left to read it
        off, and the spawner may not open control.db. Inventing one instead
        would give the restored tenant a new XFS accounting bucket AND a new
        fixed bridge address, which breaks the one claim the fixed-address
        scheme rests on -- that a stale address in the gateway's memory "can
        only reach nothing or the same tenant".

        Because provision() creates the directories fresh here, its create path
        runs and `claim()` sets the project id on two empty directories. That
        is the only place in the class where `project -s` runs, and it runs on
        exactly what the spec describes.
        """
        archive = await self._archive(tenant_id, suffix="pre-restore")
        dirs = tenant_dirs(self._config.tenant_root, tenant_id)
        for directory in (dirs.home, dirs.env):
            await self._remove_tree(tenant_id, directory)
        await self.provision(tenant_id, project_id)
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _RESTORE_SCRIPT],
            extra_binds=(f"{self._staging(tenant_id)}:/staging:ro",))
        await self._run_to_completion(body, tenant_id, "restore")
        _LOG.info("restored tenant=%s (old tree at %s)", tenant_id, archive["path"])
        return {"ok": True}

    async def _archive(self, tenant_id: str, suffix: str = "") -> dict:
        """Pack both of a tenant's directories into the archive root, zstd.

        The same shape as every other task: a throwaway container, the services
        image, no network, the tenant's two mounts and the ONE extra the task
        needs -- the archive root, which is the platform's own directory and
        not any tenant's.
        """
        self._config.archive_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"{tenant_id}-{stamp}" + (f"-{suffix}" if suffix else "")
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _ARCHIVE_SCRIPT, "archive", name],
            extra_binds=(f"{self._config.archive_root}:/archive",))
        await self._run_to_completion(body, tenant_id, "archive")
        path = self._config.archive_root / name
        _LOG.info("archived tenant=%s to %s-{home,env}.tar.zst", tenant_id, path)
        return {"path": str(path)}

    async def _remove_tree(self, tenant_id: str, directory: Path) -> None:
        """Empty a tenant's directory from INSIDE a container, then rmdir it.

        The emptying is a walk over a tree the tenant wrote, so it runs as UID
        10001 in a throwaway container with only that tenant's mounts -- the
        same rule as provisioning, for the same reason. What the host does is
        one rmdir, a single syscall on a directory that is now empty, which
        fails loudly rather than falling back to a recursive remove.

        The directory has to GO, not just be emptied: provision's create path
        is what calls xfsquota.claim, and claim is the only thing that may set
        a project id -- on a directory nothing has written to yet.
        """
        # A KeyError here rather than a default: "/work" as a fallback would
        # send an unrecognised directory to the wrong mount silently.
        mount = {"home": "/data", "env": "/work"}[directory.name]
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _EMPTY_SCRIPT, "empty-tree", mount])
        await self._run_to_completion(body, tenant_id, "empty")
        os.rmdir(directory)
        _LOG.info("removed tenant=%s directory=%s", tenant_id, directory)

    async def _inspect(self, tenant_id: str) -> dict:
        """A stock dashboard on a stopped tenant's data, on loopback only.

        The operator reaches it over an SSH tunnel. It is a BLOCKING kind, so
        while it exists the tenant's own start is refused -- which is the whole
        point: two dashboards never run on one state.db.
        """
        host_port = _free_loopback_port()
        body = template.inspect_container(self._config, tenant_id=tenant_id,
                                          host_port=host_port)
        name = template.container_name(tenant_id, template.KIND_INSPECT)
        await self._engine.remove(name)
        container = await self._engine.create(name, body)
        await self._engine.start(container)
        _LOG.info("inspect container up for tenant=%s on 127.0.0.1:%s",
                  tenant_id, host_port)
        return {"port": host_port, "address": "127.0.0.1"}

    async def _inspect_stop(self, tenant_id: str) -> dict:
        name = template.container_name(tenant_id, template.KIND_INSPECT)
        await self._engine.stop(name)
        # NOT AutoRemove, so this is what removes it: an inspect container that
        # reaped itself the moment the operator's dashboard crashed would leave
        # the tenant in maintenance with nothing to explain it.
        await self._engine.remove(name)
        _LOG.info("inspect container down for tenant=%s", tenant_id)
        return {"ok": True}


def _free_loopback_port() -> int:
    """Ask the kernel for a port nothing is on, and publish the inspect
    container there. Racy in principle and not in practice: an operator runs
    one of these at a time, and a lost race is a create that fails loudly."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
