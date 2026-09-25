"""Render a tenant's starting files, inside the throwaway container.

NEVER RUN ON THE HOST. The whole point of the throwaway container is that a
tenant can plant symlinks and special files in /data and /work, and no host
process with more privilege than UID 10001 opens a path in there. Inside the
container the two mounts are all that exists, so a planted symlink resolves
inside it.

TenantDirs(Path("/data"), Path("/work")) is the correct construction HERE.
tenant_dirs(root, tenant_id) is the host-side one, and the <root>/<id>/home
shape it builds cannot exist in this process: there is no <root>.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hosted.core.provision import provision
from hosted.core.tenant import TenantDirs
from hosted.spawner.template import SOUL_TEMPLATE_IN_IMAGE


def main() -> int:
    written = provision(TenantDirs(Path("/data"), Path("/work")), SOUL_TEMPLATE_IN_IMAGE)
    for path in written:
        print(f"wrote {path}")
    if not written:
        print("nothing missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
