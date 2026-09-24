"""The files a tenant's container finds on its first start.

Rendered inside a throwaway container as UID 10001, with only that tenant's
two directories mounted, because a tenant can plant symlinks in them and no
host process with more privilege than 10001 may open a path in there.

It only creates what is MISSING, and that is the whole of the contract: a
directory a tenant deleted, an .env or a SOUL.md that is not there, and a
loosened mode on .env. Provisioning runs again before every start, so each of
those is repaired on the next one. What is NOT repaired is the content of a
file that exists -- a half-written .env from a provision that died mid-write
stays half-written, and a SOUL.md the tenant edited stays edited, because
there is no way to tell those two apart from here.
"""

from __future__ import annotations

import os
from pathlib import Path

from hosted.core.tenant import TenantDirs

ENV_MODE = 0o600

# The ONLY line provisioning writes. Everything else the tenant's waku needs
# -- the platform base URL, the platform token and the two model names --
# comes from the container's environment, which outranks .env and which the
# tenant cannot change on disk. Writing them here instead would hand the
# tenant's own dashboard a file it can edit to point the platform token
# somewhere else.
TENANT_ENV_LINES = ("WAKU_PROVIDER=waku-platform",)


def render_env() -> str:
    return "".join(f"{line}\n" for line in TENANT_ENV_LINES)


def provision(dirs: TenantDirs, soul_template: Path) -> list[Path]:
    """Create the two files that are missing. Returns the paths written.

    `dirs` is built directly, not through tenant_dirs(): inside the throwaway
    container the only two paths that exist are the mounts, so C2 calls
    TenantDirs(Path("/data"), Path("/work")). tenant_dirs(root, id) is the
    host-side view, for the spawner and the backup task.

    It does NOT own the directories' mode, ownership or XFS project id. The
    spec gives those to the spawner, on the host, "while they are still
    empty", and setting a mode on a host mount point from inside a container
    would only fight whatever C2 chose. mkdir is here for one case: a tenant
    who deleted a directory from inside their own container gets it back on
    the next start.
    """
    written: list[Path] = []
    for directory in (dirs.home, dirs.env):
        directory.mkdir(parents=True, exist_ok=True)

    env_file = dirs.env / ".env"
    if env_file.is_symlink():
        # A tenant can plant symlinks in their own mounts, which is why this
        # runs as UID 10001 in a throwaway container. Neither branch below may
        # chase one: chmod through a link to a file this process does not own
        # raises PermissionError, and provisioning runs before EVERY start, so
        # that would lock the tenant out of their own container for good.
        pass
    elif not env_file.exists():
        # Created WITH the mode, not corrected into it. write_text() would
        # create the file at the process umask and tighten it a moment later,
        # and .env is where a BYOK key lands the day the free tier stops being
        # the only tier. O_EXCL and O_NOFOLLOW are belt and braces for the gap
        # between the is_symlink() check above and this open: a link planted
        # in between makes this raise instead of writing through it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(env_file, flags, ENV_MODE), "w", encoding="utf-8") as handle:
            handle.write(render_env())
        # The umask can only have made that stricter, never looser; this pins
        # it at exactly ENV_MODE without ever having been looser than it.
        os.chmod(env_file, ENV_MODE)
        written.append(env_file)
    else:
        # Repairing a mode the tenant loosened. Not a write, so not in the
        # list this returns.
        os.chmod(env_file, ENV_MODE)

    soul = dirs.home / "SOUL.md"
    if not soul.exists():
        soul.write_text(soul_template.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(soul)

    return written
