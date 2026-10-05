"""Render a tenant's starting files, inside the throwaway container.

NEVER RUN ON THE HOST. The whole point of the throwaway container is that a
tenant can plant symlinks and special files in /data and /work, and no host
process with more privilege than UID 10001 opens a path in there. Inside the
container the two mounts are all that exists, so a planted symlink resolves
inside it.

TenantDirs(Path("/data"), Path("/work")) is the correct construction HERE.
tenant_dirs(root, tenant_id) is the host-side one, and the <root>/<id>/home
shape it builds cannot exist in this process: there is no <root>.

WAKU_TREG_BASE_URL is the one variable read here, and the spawner sets it
(template.provision_env) only when this deployment runs the treg relay: then
mcp.json gets a treg entry pointing at the metering proxy (spec 004 E).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from hosted.core.provision import provision
from hosted.core.tenant import TenantDirs
from hosted.spawner.template import SOUL_TEMPLATE_IN_IMAGE, TREG_BASE_URL_ENV


def main() -> int:
    written = provision(TenantDirs(Path("/data"), Path("/work")), SOUL_TEMPLATE_IN_IMAGE,
                        treg_base_url=os.environ.get(TREG_BASE_URL_ENV, ""))
    for path in written:
        print(f"wrote {path}")
    if not written:
        print("nothing missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
