"""The files a tenant's container finds on its first start.

Rendered inside a throwaway container as UID 10001, with only that tenant's
two directories mounted, because a tenant can plant symlinks in them and no
host process with more privilege than 10001 may open a path in there.

It only creates what is missing. Provisioning runs again before every start,
so a first provision that failed halfway is repaired on the next one, and a
tenant who deleted their own SOUL.md gets a fresh one rather than a crash.
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
    if not env_file.exists():
        env_file.write_text(render_env(), encoding="utf-8")
        os.chmod(env_file, ENV_MODE)
        written.append(env_file)

    soul = dirs.home / "SOUL.md"
    if not soul.exists():
        soul.write_text(soul_template.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(soul)

    return written
