"""The tenant's disk limit, applied while the directories are still empty.

RUN FROM INSIDE THE SPAWNER'S OWN CONTAINER, as root with CAP_SYS_ADMIN and the
data disk's block device passed in with `devices:` -- which is what xfs_quota
needs to set a project limit from inside a container (spec, "The spawner").

WHILE THEY ARE STILL EMPTY is the whole ordering. A directory that already has
files in it gets its project id set by a recursive walk, and a recursive walk
over a tree a tenant wrote is a privileged process following a tenant's
symlink. Setting it on an empty directory with the INHERIT flag means every
file created later is born into the project.

The two argv builders are separate from the running so that a test with no XFS
anywhere -- every maintainer machine -- can still pin the exact commands. That
is a DRIFT CHECK. The guard is evals/hosted_docker/test_isolation.py, on the
loop-mounted XFS the hosted-docker job makes, where a tenant writing past the
limit actually gets an error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hosted import log

_LOG = log.get(__name__)


def project_argv(device: str, path: str, project_id: int) -> list[str]:
    """Set the project id AND the inherit flag on one empty directory.

    `project -s` is the setup form: it applies the id to the path given with
    -p and turns on inheritance, so files created under it later belong to the
    project without anybody walking the tree again.
    """
    return ["xfs_quota", "-x", "-c", f"project -s -p {path} {project_id}", device]


def limit_argv(device: str, project_id: int, hard_bytes: int) -> list[str]:
    """A HARD limit only. A soft limit warns and keeps accepting writes, which
    for a per-tenant disk cap is the same as no limit."""
    return ["xfs_quota", "-x", "-c", f"limit -p bhard={hard_bytes} {project_id}", device]


async def _run(argv: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {process.returncode}: "
            f"{err.decode('utf-8', 'replace')[:500]}")
    _LOG.debug("xfs_quota ok: %s", " ".join(argv))


async def claim(device: str, path: Path, project_id: int, hard_bytes: int) -> None:
    """THE CREATE PATH ONLY. Call this on a directory this process has just
    created and nothing has yet written to.

    `project -s` descends the directory. On an EMPTY directory that descent
    visits nothing, which is why the spec puts it "while they are still empty"
    and why DockerRuntime.provision calls this from the FileExistsError-free
    branch and nowhere else.
    """
    await _run(project_argv(device, str(path), project_id))
    await _run(limit_argv(device, project_id, hard_bytes))


async def set_limit(device: str, project_id: int, hard_bytes: int) -> None:
    """EVERY LATER CALL. Keyed on the project id; it names no path, so there is
    nothing for it to walk.

    This is the half of the old `apply()` that is safe to run on every start.
    The other half is not, and separating them is C-1 from the plan review.
    """
    await _run(limit_argv(device, project_id, hard_bytes))


async def repair(device: str, path: Path, project_id: int, hard_bytes: int) -> None:
    """AN OPERATOR RUNS THIS, BY HAND, KNOWING WHAT IT DOES. Nothing in the
    spawner's request path ever reaches it.

    It re-applies the project id to a POPULATED tree, which is a root,
    CAP_SYS_ADMIN recursive descent over a directory a tenant controls the
    shape of. It exists because a tenant whose quota was lost -- a restore that
    died between recreating the directories and extracting into them, a
    filesystem moved between hosts -- has no other way back, and the answer to
    that cannot be "start their container and hope", because that is how the
    walk ends up on the hot path.

    IT IS NOT A SPAWNER VERB. It is not in core/requests.OPERATIONS or TASKS,
    there is no socket message that reaches it, and F4's tenant.sh documents it
    as a root command on the host. Every new privileged verb is a new attack
    surface, and this one would be the worst of them: a verb whose whole job is
    to make root walk a tenant's tree.

    WHAT KEEPS IT SAFE, NAMED. xfsprogs' `project -s` traverses with nftw()'s
    FTW_PHYS, so it does not follow symlinks: a planted `/data/x -> /` is
    visited as a link and not descended, and the filesystem root does not have
    its project id reassigned. That is a third-party implementation detail, not
    a guarantee this project owns, so it is stated here and PINNED by
    test_isolation.py::test_xfs_quota_does_not_follow_a_symlink_when_it_walks,
    which plants that exact link and asserts the outer filesystem's project id
    is unchanged. An unnamed dependency on somebody else's traversal flag is
    how this becomes a hole in two years.
    """
    _LOG.warning(
        "REPAIR: re-applying project %s to the populated tree at %s. This walks "
        "a tenant-controlled directory as root.", project_id, path)
    await _run(project_argv(device, str(path), project_id))
    await _run(limit_argv(device, project_id, hard_bytes))
