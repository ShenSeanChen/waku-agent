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
import ipaddress
import json
import os
import socket
import time
from pathlib import Path

from hosted import log
from hosted.core.tenant import (
    TENANT_SUBNET,
    TenantDirs,
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

# EVERY SCRIPT HERE THAT HAS A PIPELINE SETS `pipefail`, and `bash -euc` alone
# does not. MEASURED, not assumed:
#
#   bash -euc 'tar --create --file - --directory /gone . | tar --extract ...'
#
# exits 0. Not "fails with the sink's error" -- exits ZERO. _run_to_completion
# only raises on a non-zero exit, so without this line a restore that read
# nothing would have returned {"ok": True} over a tenant whose old tree had
# just been archived and removed, and a backup that copied nothing would have
# reported a path the operator would later restore from.
# test_spawner_restore.py::test_a_failing_source_fails_the_whole_pipeline drives
# these three constants through a real bash with `tar` stubbed on PATH.
#
# `sqlite3 .backup` is SQLite's ONLINE backup: it is safe against a live
# database, which a plain copy is not. The *-wal, *-shm and *-journal files are
# left out on purpose -- they belong to the live database and would corrupt the
# copy on restore -- and every other file is copied as-is, symlinks kept as
# symlinks, because restic stores a symlink as a symlink and never follows it.
# THE MANIFEST IS WRITTEN LAST AND REMOVED FIRST, and that ordering is the
# whole of GC-3. `mkdir -p /staging/home /staging/env` is line one, so both
# directories exist -- empty -- from the first instant of a backup, before the
# database copy and before either tar. Every guard that asked what staging
# LOOKED LIKE therefore accepted a backup that died after line one, and the
# restore then destroyed a live tenant and answered {"ok": True}.
#
# Shape cannot tell a finished backup from an interrupted one. So the backup
# DECLARES it: manifest.json is the last thing written, naming the parts it
# wrote and whether it copied a database. Removing any previous manifest FIRST
# is the other half -- without that, a second backup that died halfway would
# sit under the first backup's manifest, which would say this partial data is
# whole.
#
# AND THE PART DIRECTORIES ARE CLEARED, which is the same mistake one level up.
# `rm -f` took the old manifest and nothing took the old home/ and env/, so the
# manifest described THIS backup while the directories held the union of every
# backup ever taken. Two consequences, both silent: a file the tenant deleted
# came back on the next restore, and a manifest saying `state_db: false` sitting
# above an earlier backup's home/state.db restored a database it says was never
# copied -- after archiving and wiping the live tree, answering {"ok": True}.
# A fact derived from the wrong source, for the fifth time in this file.
#
# It costs nothing that is still worth having: the `rm -f` on the line above
# has already made the previous backup unrestorable.
#
# `find -delete` and not `rm -rf`: -delete implies -depth, traverses
# FTS_PHYSICAL and unlinks relative to a directory fd it opened itself, so a
# symlink among a tenant's backed-up files is removed as a link and never
# descended. `-mindepth 1` keeps the two directories themselves. The same
# idiom, for the same reasons, as _EMPTY_SCRIPT.
_MANIFEST_NAME = "manifest.json"
_MANIFEST_VERSION = 1
_BACKUP_PARTS = ("home", "env")
# A manifest is under a hundred bytes. The cap is here so a root
# process never reads an unbounded file that sits beside tenant data.
_MANIFEST_MAX_BYTES = 4096
# The first sixteen bytes of every SQLite database. Checked because
# is_file() is satisfied by a zero-byte file, which is what a killed
# backup leaves behind.
_SQLITE_MAGIC = b"SQLite format 3\x00"

_BACKUP_SCRIPT = """
set -o pipefail
rm -f /staging/manifest.json
mkdir -p /staging/home /staging/env
find /staging/home /staging/env -mindepth 1 -delete
if [ -f /data/state.db ]; then
  sqlite3 /data/state.db ".backup '/staging/home/state.db'"
  state_db=true
else
  state_db=false
fi
tar --create --file - --directory /data \
    --exclude 'state.db' --exclude '*-wal' --exclude '*-shm' --exclude '*-journal' . \
  | tar --extract --file - --directory /staging/home
tar --create --file - --directory /work \
    --exclude '*-wal' --exclude '*-shm' --exclude '*-journal' . \
  | tar --extract --file - --directory /staging/env
printf '{"version":1,"parts":["home","env"],"state_db":%s}\n' "$state_db" \
  > /staging/manifest.json
"""

_RESTORE_SCRIPT = """
set -o pipefail
tar --create --file - --directory /staging/home . | tar --extract --file - --directory /data
tar --create --file - --directory /staging/env  . | tar --extract --file - --directory /work
"""

# zstd because the services image carries it and a deleted tenant's tree is
# kept, not read. The two mounts go into one archive so a restore that needs
# the pre-restore state gets both halves or neither.
_ARCHIVE_SCRIPT = """
set -o pipefail
tar --create --directory /data . | zstd -q -o "/archive/$1-home.tar.zst"
tar --create --directory /work . | zstd -q -o "/archive/$1-env.tar.zst"
"""

# Emptying a tenant's tree is a WALK, so it happens HERE -- as UID 10001,
# inside a throwaway container, with only that tenant's mounts -- and never on
# the host. `find -delete` implies -depth, traverses FTS_PHYSICAL and unlinks
# relative to a directory fd it opened itself, so a planted symlink is removed
# as a link and never descended; -mindepth 1 leaves the mount point alone. The
# host's own part is one rmdir per directory, a single syscall over a directory
# that is now empty, which fails loudly rather than reaching for a recursive
# remove.
#
# BOTH MOUNTS IN ONE RUN, and that is not tidiness -- see
# _empty_and_remove_trees for why two runs cannot work.
_EMPTY_SCRIPT = """
for mount in "$@"; do
  find "$mount" -mindepth 1 -delete
done
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
        # A previous container with this name may be mid-removal by the
        # AutoRemove reaper. Removing by name first clears it when it is still
        # there and is a no-op when it is gone -- and Engine.remove accepts the
        # daemon's 409 "removal already in progress", which is what that window
        # actually answers. Without that, this line raised EngineError on the
        # start hot path for the exact race it was written to absorb.
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

    async def tenant_ids(self) -> list[str]:
        """Every tenant container this spawner labelled, by id.

        A DIFFERENT QUESTION FROM `list`, AND DELIBERATELY WIDER. `list`
        answers "may the gateway forward to this container?", so it drops one
        with no address on the tenant bridge and one at an address its project
        id does not derive -- both right, because forwarding to either is
        sending a signed-in person somewhere the platform never meant. This
        answers "is this container ours?", which is what STOPPING needs, and
        the containers in the difference are exactly the ones a restore is
        about to delete the directories out from under. The failure that costs
        is in designs/backup-restore-integrity.md: a bind mount left on a dead
        inode, and the next `docker exec` reporting "possible container
        breakout detected" in the middle of a disaster recovery.

        THE LABEL AND THE ID SHAPE ARE THE WHOLE FILTER. No address check, no
        control.db join -- the spawner opens no database, and a tenant the
        restored control.db will not know is precisely a container that must
        still be stopped. `is_tenant_id` stays because the id is a closed set
        everywhere else it is used and `stop` joins it to a container name.
        """
        ids = []
        for entry in await self._engine.containers(
                label=f"{template.LABEL_KIND}={template.KIND_TENANT}"):
            tenant_id = (entry.get("Labels") or {}).get(template.LABEL_TENANT, "")
            if not is_tenant_id(tenant_id):
                _LOG.warning("ignoring container %s with label tenant=%r",
                             entry.get("Id", "")[:12], tenant_id)
                continue
            ids.append(tenant_id)
        return sorted(set(ids))

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
            if not _is_a_tenant_address(address):
                # The gateway FORWARDS to whatever this returns, so an address
                # off the tenant subnet is a request the platform would make
                # somewhere it never meant to.
                #
                # THE STRONGEST RULE AVAILABLE HERE, AND ITS LIMIT, STATED.
                # `list` has the tenant id but not the project id, so it cannot
                # check the address is THAT tenant's -- only that it is inside
                # TENANT_SUBNET, outside DYNAMIC_RANGE, and therefore one that
                # some project id derives. Only something that can already
                # create containers could put a wrong-but-valid address here,
                # so this is hardening; the check beside it on the tenant id is
                # the precedent.
                _LOG.warning("tenant=%s has address %r, which no project id "
                             "derives; not listing it", tenant_id, address)
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
        # restore is dispatched BEFORE the table and is not in it. It is the
        # one task that takes a second argument, and a table entry for it
        # would be a dead branch that calls _restore one argument short the
        # day somebody removes the special case below as redundant.
        if task == "restore":
            return await self._restore(tenant_id, project_id)
        handler = {
            "backup": self._backup,
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
        return await handler(tenant_id)

    def _staging(self, tenant_id: str) -> Path:
        return self._config.staging_root / tenant_id

    def _tenant_directory_under(self, root: Path, tenant_id: str) -> Path:
        """A per-tenant directory under one of the platform's shared roots,
        created and handed to UID 10001.

        PER TENANT, NOT THE SHARED ROOT. A task container runs as 10001 and has
        to create files in whatever it is given, so the directory it is given
        must be writable by 10001 -- and chowning the SHARED root would hand
        every tenant's staging and every tenant's archive to every tenant's
        task container in one go.

        WHAT SEPARATES TWO TENANTS HERE IS THE MOUNT, NOT THE MODE. Every
        tenant's container runs as the same UID 10001, so 0700 does not keep
        tenant A out of tenant B's archive -- it keeps other UIDS on the host
        out of both. The only thing that stops A reaching B's files is that no
        container is ever given B's directory, which is why
        evals/hosted_docker's `allowed_bind_sources` is an exact set and why
        this returns one tenant's path rather than a root.

        `_backup` learned this and `_archive` did not, which is how every
        archive -- and therefore every restore, which archives first -- failed
        with EACCES and reached the operator as jsonsock's opaque error.
        """
        directory = root / tenant_id
        if directory.is_symlink():
            # Path.mkdir(exist_ok=True) re-checks with is_dir(), which FOLLOWS
            # a link, and os.chown follows too. Nothing a tenant can write to
            # includes these roots today; refused anyway, because this process
            # is root and the subject of this file is planted links.
            raise RuntimeError(f"{directory} is a symlink; refusing to use it")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, TENANT_DIR_MODE)
        os.chown(directory, template.TENANT_UID, template.TENANT_UID,
                 follow_symlinks=False)
        return directory

    async def _backup(self, tenant_id: str) -> dict:
        """Copy the tenant's two directories into their staging slot.

        ONE SLOT PER TENANT, AND STARTING A BACKUP INVALIDATES IT. The first
        two lines of _BACKUP_SCRIPT remove the manifest and empty both part
        directories, so a backup that dies leaves no restorable backup at all
        -- not the old one and not the new one. That is survivable only because
        staging is a HANDOFF area: F3's backup.sh takes a restic snapshot from
        it and restic keeps every snapshot. Nothing here enforces that, so an
        operator running `task backup` twice by hand with no restic in between
        has destroyed their only copy and been told twice that it worked. The
        shape that fixes it -- staging into <staging_root>/<id>/<timestamp>/,
        with restore naming the directory -- adds a key to the task request and
        is a spec decision, not a fix.

        AND NOTHING REPAIRS STAGING. The script runs as UID 10001 over a tree
        this tenant's own files made, so a directory mode 10001 cannot traverse
        -- an 0500 directory with a file under it -- wedges `find -delete`, and
        every backup from then on fails at the same line with no verb to clear
        it. This is not a regression: the previous shape wedged identically at
        `tar --extract`. It is worth F3 knowing that `backup.sh` needs a way to
        reset a tenant's staging slot, because the spawner has none.
        """
        # THE PATH COMES FROM THE HELPER, and that is not tidiness: the first
        # version called the helper for its side effect and then bound a path
        # it had computed separately. Two expressions for one location is
        # exactly the asymmetry GC-1 was -- `_backup` chowning one path while
        # `_archive` bound another -- coming back through the door beside it.
        # The helper does the symlink refusal, the mkdir, the mode and the
        # chown, and returns the one path all four applied to.
        staging = self._tenant_directory_under(self._config.staging_root, tenant_id)
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

        BOTH DIRECTORIES ARE REMOVED IN ONE CONTAINER RUN, and the reason is
        C2-1. template.task_container binds BOTH tenant paths unconditionally,
        and the Docker daemon creates a missing `Binds` source as a root-owned
        directory -- the documented behaviour of the legacy bind form and the
        reason `--mount` exists. So removing them one per run cannot work: the
        second run's create re-makes the first's directory, behind provision's
        back, and provision then takes its FileExistsError branch and calls
        set_limit instead of claim. That directory keeps XFS project 0 --
        uncounted and unlimited -- for the rest of that tenant's life, while
        the other one is fine and the asymmetry is invisible. A tenant who got
        a restore could then fill the shared data disk and stop every other
        tenant on the VM.

        So: one run empties both mounts, the host then removes both, and
        nothing binds either path again until provision() has re-created them.
        Its create path runs for BOTH, and `claim()` sets the project id on two
        empty directories -- the only place in this class where `project -s`
        runs, on exactly what the spec describes. The check below is what makes
        a future regression loud instead of silent.

        NOTHING HERE STOPS A RUNNING TENANT CONTAINER, and F3's caller does:
        `_act` calls `launcher.stop(tenant.id)` before this task, and
        `restore.sh --all` stops the whole fleet through `stop-all` first. The
        sequencing belongs with the caller, which already has `stop` as a verb.
        What the caller CANNOT reach is a task or inspect container -- `stop`
        names a KIND_TENANT container and nothing else -- so those are refused
        here instead; see the first lines of the body.
        """
        # EVERY CONTAINER HOLDING THIS TENANT'S MOUNTS MUST BE GONE, NOT JUST
        # THEIR DASHBOARD. `_refuse_if_busy` was called only from `start`, and
        # the note below said so -- but the containers it blocks on,
        # KIND_TASK and KIND_INSPECT, bind `home` and `env` exactly as the
        # tenant container does (template.task_container,
        # template.inspect_container), and those are the two directories the
        # lines below remove and re-create.
        #
        # An inspect container is the one that bites: it is operator-started,
        # AutoRemove is deliberately off, and it lives until `inspect-stop`.
        # An operator who inspects a tenant and then runs `restore.sh --all`
        # gets the failure designs/backup-restore-integrity.md records -- a
        # bind mount left on a removed directory, and the next `docker exec`
        # reporting "possible container breakout detected" in the middle of a
        # disaster recovery. The gateway's `stop-all` cannot reach them: it
        # stops by tenant id and `stop` only knows how to name a KIND_TENANT
        # container.
        #
        # A REFUSAL AND NOT A WIDER STOP, deliberately. An inspect container
        # holds a dashboard someone is looking at, and killing it from under a
        # restore is a decision an operator should make with `inspect-stop`,
        # not one this method should make for them. Busy is the maintenance
        # answer the gateway already knows how to render.
        #
        # This runs BEFORE the manifest read and long before `_archive`, so a
        # refusal here costs nothing: the tenant's tree is untouched.
        await self._refuse_if_busy(tenant_id)
        staging = self._staging(tenant_id)
        if staging.is_symlink():
            # is_dir() FOLLOWS a link; _backup refuses one outright. Two guards
            # on the same path must not disagree about the same class of input.
            raise RuntimeError(
                f"{staging} is a symlink; refusing to restore through it")
        # THE BACKUP SAYS WHETHER IT FINISHED. Nothing about the shape of
        # staging can: `mkdir -p /staging/home /staging/env` is the backup's
        # first line, so two empty directories are what a backup that died
        # immediately leaves AND what a backup of an empty tenant leaves, and
        # no amount of looking tells them apart. This reads the manifest the
        # backup writes last. It runs BEFORE _archive, because once the old
        # tree is packed away and the two directories are gone, a restore that
        # cannot finish has already destroyed what it was restoring.
        self._read_manifest(tenant_id, staging)
        archive = await self._archive(tenant_id, suffix="pre-restore")
        dirs = tenant_dirs(self._config.tenant_root, tenant_id)
        await self._empty_and_remove_trees(tenant_id, dirs)
        for directory in (dirs.home, dirs.env):
            # lexists, not exists: exists() follows a link and answers False
            # for a dangling one, so a directory replaced by a broken symlink
            # read as "successfully removed" and provision()'s mkdir then
            # raised FileExistsError on the link -- reaching the operator as
            # jsonsock's opaque error rather than as the thing that happened.
            if os.path.lexists(directory):
                raise RuntimeError(
                    f"{directory} is still there after being removed, so "
                    "provision() would take its repeat path and never claim a "
                    "project id for it. Something re-created it -- almost "
                    "certainly a container bind. See this method's docstring.")
        await self.provision(tenant_id, project_id)
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _RESTORE_SCRIPT],
            extra_binds=(f"{self._staging(tenant_id)}:/staging:ro",))
        await self._run_to_completion(body, tenant_id, "restore")
        _LOG.info("restored tenant=%s (old tree at %s)", tenant_id, archive["path"])
        return {"ok": True}

    def _read_manifest(self, tenant_id: str, staging: Path) -> dict:
        """The backup's own declaration that it finished, or a refusal.

        Read on the HOST, as root, and that is allowed: <staging_root>/<id> is
        the platform's own directory, the manifest sits at its top level under
        a name the platform chose, and the tenant's data goes into home/ and
        env/ beneath it -- `tar --extract` refuses absolute and `..` paths, so
        nothing a tenant wrote can become this file. O_NOFOLLOW and the
        is_symlink check are there anyway, because this is root opening a path
        near tenant data and the cost is one flag.
        """
        manifest = staging / _MANIFEST_NAME
        if manifest.is_symlink():
            raise RuntimeError(
                f"{manifest} is a symlink; refusing to restore through it")
        try:
            with open(os.open(manifest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)),
                      encoding="utf-8") as handle:
                raw = handle.read(_MANIFEST_MAX_BYTES + 1)
        except OSError as exc:
            raise RuntimeError(
                f"no usable backup for {tenant_id} at {staging}: {exc}. A "
                "backup writes manifest.json LAST, so one that is missing "
                "means the backup never finished -- restoring from it would "
                "archive this tenant's live data and then replace it with "
                "whatever the interrupted run managed to copy.") from exc
        if len(raw) > _MANIFEST_MAX_BYTES:
            raise RuntimeError(f"{manifest} is larger than a manifest should be")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"{manifest} is not JSON, so this backup cannot be read: "
                f"{exc}") from exc
        if not isinstance(parsed, dict) or parsed.get("version") != _MANIFEST_VERSION:
            raise RuntimeError(
                f"{manifest} is not a version {_MANIFEST_VERSION} backup "
                f"manifest: {parsed!r}")
        parts = parsed.get("parts")
        if not isinstance(parts, list) or sorted(parts) != sorted(_BACKUP_PARTS):
            raise RuntimeError(
                f"{manifest} names parts {parts!r}; a backup writes "
                f"{list(_BACKUP_PARTS)}.")
        for part in _BACKUP_PARTS:
            if not (staging / part).is_dir():
                raise RuntimeError(
                    f"{manifest} names {part!r} and {staging / part} is not "
                    "there. The backup and what is on disk disagree; refusing "
                    "rather than restoring half of it.")
        self._check_state_db(manifest, staging, parsed.get("state_db"))
        return parsed

    @staticmethod
    def _check_state_db(manifest: Path, staging: Path, claimed: object) -> None:
        """The manifest and the file on disk must agree, BOTH WAYS.

        `is_file()` was not enough: a zero-byte state.db, or a truncated one,
        satisfies it and restores an assistant with no memory while the
        manifest says a database was copied. `sqlite3 .backup` writes a whole
        database or fails, so the header is what is checked.

        The `false` direction matters too, now that the backup clears its part
        directories: a manifest saying no database was copied, sitting above a
        file that is one, means the two disagree about what this backup is --
        and the whole point of the manifest is that IT, not the filesystem,
        says what happened.
        """
        # A TYPE CHECK, like every other field. `state_db` was the one field
        # read with `.get()` and compared to True, so `"true"` as a string, `1`,
        # and a missing key all fell through to "no database" -- the lenient
        # answer, reached by three different kinds of malformed manifest. F3
        # writes these from restic, so "malformed" stops being hypothetical.
        if not isinstance(claimed, bool):
            # RuntimeError and not TypeError (ruff TRY004), because EVERY
            # refusal this reader makes is one class: the caller is
            # service.handle, which lets them all propagate to jsonsock's
            # opaque answer, and a second exception type here is a second thing
            # for some future caller to catch differently.
            raise RuntimeError(  # noqa: TRY004
                f"{manifest} has state_db={claimed!r}, which is not a boolean. "
                "A manifest this process cannot read exactly is one it will not "
                "restore from.")
        state_db = staging / "home" / "state.db"
        if claimed:
            # THE SAME CARE AS manifest.json, TWO LEVELS UP. That one gets an
            # is_symlink() refusal and O_NOFOLLOW; this was a bare root open
            # inside `home/`, which is the tenant-derived half of staging. Not
            # reachable today -- `tar --extract` refuses absolute and `..`
            # paths -- and reachable through F3's restic path, which writes
            # these directories from somewhere this process did not control.
            if state_db.is_symlink():
                raise RuntimeError(
                    f"{state_db} is a symlink; refusing to read through it")
            if not state_db.is_file():
                raise RuntimeError(
                    f"{manifest} says this backup copied a database and "
                    f"{state_db} is not there. Restoring would give the tenant "
                    "an empty assistant and report success.")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with open(os.open(state_db, flags), "rb") as handle:
                header = handle.read(len(_SQLITE_MAGIC))
            if header != _SQLITE_MAGIC:
                raise RuntimeError(
                    f"{state_db} does not begin with SQLite's header, so it is "
                    "empty or truncated. `sqlite3 .backup` writes a whole "
                    "database or fails; this is what a killed backup leaves.")
        elif state_db.exists():
            raise RuntimeError(
                f"{manifest} says this backup copied no database and "
                f"{state_db} is there. The manifest and the staging directory "
                "disagree about what this backup is.")

    async def _archive(self, tenant_id: str, suffix: str = "") -> dict:
        """Pack both of a tenant's directories into the archive root, zstd.

        The same shape as every other task: a throwaway container, the services
        image, no network, the tenant's two mounts and the ONE extra the task
        needs -- the archive root, which is the platform's own directory and
        not any tenant's.
        """
        archive = self._tenant_directory_under(self._config.archive_root, tenant_id)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"{tenant_id}-{stamp}" + (f"-{suffix}" if suffix else "")
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            command=["bash", "-euc", _ARCHIVE_SCRIPT, "archive", name],
            # THIS TENANT'S archive directory, not the shared root. The
            # container runs as 10001 and creates two files here, so it must be
            # writable by 10001 -- and binding the shared root would put every
            # other tenant's archives inside a tenant-owned container.
            extra_binds=(f"{archive}:/archive",))
        await self._run_to_completion(body, tenant_id, "archive")
        path = archive / name
        _LOG.info("archived tenant=%s to %s-{home,env}.tar.zst", tenant_id, path)
        return {"path": str(path)}

    async def _empty_and_remove_trees(self, tenant_id: str, dirs: TenantDirs) -> None:
        """Empty BOTH of a tenant's directories from INSIDE one container, then
        rmdir both on the host.

        ONE RUN, AND THE RMDIRS AFTER IT. task_container binds both tenant
        paths unconditionally and the daemon creates a missing bind source, so
        a second container run -- or a rmdir between two runs -- re-makes what
        the first removed, root-owned, behind provision's back. That was C2-1
        and it cost the restored tenant their disk quota. There is no version
        of this that removes one directory at a time.

        The emptying is a walk over a tree the tenant wrote, so it runs as UID
        10001 in a throwaway container with only that tenant's mounts -- the
        same rule as provisioning, for the same reason. What the host does is
        one rmdir per directory, a single syscall on a directory that is now
        empty: it refuses a symlink with ENOTDIR and a non-empty directory with
        ENOTEMPTY, and it fails loudly rather than falling back to a recursive
        remove. If `find` fails for any reason, `bash -euc` exits non-zero,
        _run_to_completion raises, and the host never reaches rmdir at all.

        The directories have to GO, not just be emptied: provision's create
        path is what calls xfsquota.claim, and claim is the only thing that may
        set a project id -- on a directory nothing has written to yet.
        """
        body = template.task_container(
            self._config, tenant_id=tenant_id,
            # "/data" and "/work" are the mounts task_container always makes,
            # named here as literals because they are this container's whole
            # world; the host paths behind them are dirs.home and dirs.env.
            command=["bash", "-euc", _EMPTY_SCRIPT, "empty-trees", "/data", "/work"])
        await self._run_to_completion(body, tenant_id, "empty")
        for directory in (dirs.home, dirs.env):
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


def _is_a_tenant_address(address: str) -> bool:
    """Is this an address some project id derives?

    address_for_project is `subnet.network_address + project_id`, so this runs
    it backwards and asks is_project_id about the offset. An ALLOWLIST derived
    from the one function that mints these, not a list of ranges to exclude:
    it refuses the bridge gateway (offset 1, where the proxy listens), every
    address in DYNAMIC_RANGE (offsets past LAST_PROJECT_ID), and anything off
    the subnet, without naming any of them.

    It cannot say WHOSE address it is, because `list` has no project id to
    compare against; see its call site for that limit.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.version != 4 or parsed not in TENANT_SUBNET:
        return False
    return is_project_id(int(parsed) - int(TENANT_SUBNET.network_address))


def _free_loopback_port() -> int:
    """Ask the kernel for a port nothing is on, and publish the inspect
    container there. Racy in principle and not in practice: an operator runs
    one of these at a time, and a lost race is a create that fails loudly."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
