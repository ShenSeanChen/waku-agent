"""Acceptance 15 -- no privileged process follows a tenant's symlink.

THE PROVISIONING HALF. F3 adds backup and restore, F4 archive and inspect.

THE SHAPE: plant the symlinks a tenant could plant, run provisioning, and check
the target by LISTING ITS PARENT rather than by an exit code or a `.exists()`.
"The command succeeded" is not "the file was not written", and `Path.exists()`
returns False for a path it cannot reach -- see `_absent` below, which is C2-5.

EVERY TEST GETS ITS OWN TENANT. `provision()` deliberately never repairs a
symlinked `.env` (core/provision.py), so a test that plants one on a shared
tenant leaves it planted and the next three tests in the module read or chmod a
dangling link and error. That was C2-4. `tenant` is function-scoped and mints a
fresh id and project id per test; the `spawner` container behind it is still
module-scoped, so this costs a provision, not a container.

The `spawner` and `spawner_root` fixtures come from conftest.py; the helpers
from spawnerlib.py.
"""

from __future__ import annotations

import itertools
import os

import dockerlib
import pytest
from spawnerlib import (
    SERVICES_TAG,
    SPAWNER_CONTAINER,
    allowed_bind_sources,
    ask,
    ask_ok,
    capture_task_containers,
)

from hosted.core.tenant import ALPHABET, FIRST_PROJECT_ID
from hosted.spawner import template

# Far enough above test_spawner.py's PROJECT_A/PROJECT_B that the two modules
# cannot hand two tenants one project id and one fixed address.
_FIRST = FIRST_PROJECT_ID + 100
_COUNTER = itertools.count()


def _mint(n: int) -> str:
    """A tenant id of the right shape: twelve characters of [a-z2-7]."""
    suffix = "".join(ALPHABET[(n >> shift) & 31] for shift in (10, 5, 0))
    return f"tfiles{suffix}aaa"[:12]


@pytest.fixture()
def tenant(spawner):
    """A freshly provisioned tenant nothing else in this module touches.

    Its own id AND its own project id: two tenants sharing a project id share
    an XFS accounting bucket and a fixed bridge address, and a test that
    planted a symlink for another test to trip over is exactly what C2-4 was.
    """
    n = next(_COUNTER)
    tenant_id, project_id = _mint(n), _FIRST + n
    answer = ask(spawner, {"op": "provision", "tenant_id": tenant_id,
                           "project_id": project_id})
    assert "error" not in answer, answer
    return tenant_id, project_id


def _plant(spawner_root, tenant_id: str, relative: str, target: str) -> None:
    """Make <tenant dir>/<relative> a symlink to `target`, as the tenant would:
    from inside a container, as UID 10001."""
    home = spawner_root / "tenants" / tenant_id / "home"
    env = spawner_root / "tenants" / tenant_id / "env"
    mount = home if relative.startswith("SOUL") else env
    dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-euc", f"rm -f /mnt/{relative}; ln -s {target} /mnt/{relative}"],
        read_only=False,
        binds=[f"{mount}:/mnt"])


def _absent(directory, name: str) -> bool:
    """Is `name` missing from `directory`, ASKED IN A WAY THAT CAN FAIL?

    C2-5. `Path.exists()` swallows PermissionError and returns False, and these
    directories are mode 0700 owned by UID 10001 -- so on an unprivileged test
    process `not target.exists()` passes whatever provision.py did. That was
    the thirteenth cannot-fail test on this project, and it was guarding the
    security fix this task exists to land.

    os.listdir RAISES on a directory it cannot read, so "I could not look" and
    "I looked and it was not there" stop being the same answer.
    """
    return name not in os.listdir(directory)


def test_provisioning_does_not_write_through_a_planted_env_symlink(
        spawner, spawner_root, tenant):
    """core/provision.py's `if env_file.is_symlink(): pass` branch, proved at
    the container level rather than at the unit level.

    THE TARGET IS INSIDE /data, and that is what makes this able to fail. The
    brief planted `.env -> /data/../secret.txt`, which resolves to
    `/secret.txt` inside the container -- on the read-only root, where the
    write fails whatever provision.py does, so the assertion could not tell
    the guard from the containment. A target under the tenant's own home
    mount is writable, so a provision that followed the link would create it.

    Both halves are asserted: the provision SUCCEEDED (a raising provision
    comes back as jsonsock's opaque error, and an absence asserted after a
    dead run means nothing), and the target is not in the directory listing.
    """
    tenant_id, project_id = tenant
    home = spawner_root / "tenants" / tenant_id / "home"
    _plant(spawner_root, tenant_id, ".env", "/data/env-link-target.txt")
    answer = ask(spawner, {"op": "provision", "tenant_id": tenant_id,
                           "project_id": project_id})
    assert "error" not in answer, (
        f"provisioning a tenant with a planted .env symlink failed: {answer}. "
        "It must be a no-op on that file, not a crash -- provisioning runs "
        "before every start, so a crash locks the tenant out for good.")
    assert _absent(home, "env-link-target.txt"), (
        "provisioning followed a planted .env symlink and wrote its target")


@pytest.mark.parametrize("state", ["existing", "dangling"])
def test_provisioning_does_not_write_through_a_planted_soul_symlink(
        spawner, spawner_root, tenant, state):
    """The branch C2 adds to core/provision.py. Both cases: a link to an
    EXISTING path, which exists() already reported as present, and a link to a
    MISSING one, which exists() reported as absent and write_text would have
    followed.

    Both targets are under /data, the tenant's own writable mount, for the same
    reason as the .env case above: a target on the read-only root cannot be
    written whatever provision.py does.

    PARAMETRISED rather than looped, so each case gets its own tenant from the
    fixture and the `existing` plant cannot survive into the `dangling` case.
    """
    tenant_id, project_id = tenant
    home = spawner_root / "tenants" / tenant_id / "home"
    name = f"soul-{state}.txt"
    if state == "existing":
        dockerlib.run_once(
            SERVICES_TAG,
            ["bash", "-euc", f"printf 'UNTOUCHED\\n' > /data/{name}"],
            read_only=False, binds=[f"{home}:/data"])
    _plant(spawner_root, tenant_id, "SOUL.md", f"/data/{name}")
    answer = ask(spawner, {"op": "provision", "tenant_id": tenant_id,
                           "project_id": project_id})
    assert "error" not in answer, (
        f"provisioning with a planted SOUL.md symlink failed: {answer}")
    if state == "existing":
        assert (home / name).read_text(encoding="utf-8") == "UNTOUCHED\n", (
            "provisioning wrote the SOUL template through a symlink to a file "
            "the tenant already had")
    else:
        assert _absent(home, name), (
            "provisioning followed a dangling SOUL.md symlink and created "
            "its target")


def test_a_tenant_cannot_make_provisioning_overwrite_their_own_env(
        spawner, spawner_root, tenant):
    """The reachable case, and the one the containment does NOT stop on its own.

    /tmp, /data and /work are all writable in the throwaway container, so
    before C2's fix a tenant pointing home/SOUL.md at /work/.env got their own
    .env replaced with the SOUL template on their next start. Nothing outside
    those three paths is writable, so it was self-harm only -- but self-harm
    caused by the platform, on a path the tenant did not name, is still a
    defect.
    """
    tenant_id, project_id = tenant
    env_file = spawner_root / "tenants" / tenant_id / "env" / ".env"
    _plant(spawner_root, tenant_id, "SOUL.md", "/work/.env")
    answer = ask(spawner, {"op": "provision", "tenant_id": tenant_id,
                           "project_id": project_id})
    assert "error" not in answer, answer
    assert "WAKU_PROVIDER=waku-platform" in env_file.read_text(encoding="utf-8")


def test_provisioning_runs_as_10001_on_no_network_with_only_two_mounts(
        spawner, spawner_root, tenant):
    tenant_id, project_id = tenant
    seen = capture_task_containers(
        spawner, {"op": "provision", "tenant_id": tenant_id,
                  "project_id": project_id}, tenant_id)
    assert len(seen) == 1, [c["Config"]["Labels"] for c in seen]
    container = seen[0]
    assert container["Config"]["Labels"][template.LABEL_KIND] == template.KIND_PROVISION
    assert container["Config"]["User"] == "10001:10001"
    assert container["HostConfig"]["NetworkMode"] == "none"
    sources = {bind.split(":", 1)[0].rstrip("/")
               for bind in container["HostConfig"]["Binds"]}
    # DEFAULT-DENY, the same allowlist the task containers are held to.
    assert sources == allowed_bind_sources(spawner_root, tenant_id, "provision"), (
        f"provisioning mounted {sorted(sources)}")


def test_provisioning_repairs_a_loosened_env_mode(spawner, spawner_root, tenant):
    """It runs before EVERY start, so a mode the tenant loosened is repaired on
    the next one."""
    tenant_id, project_id = tenant
    env_file = spawner_root / "tenants" / tenant_id / "env" / ".env"
    assert env_file.stat().st_mode & 0o777 == 0o600, (
        "the provisioned .env is not 0600 to start with, so the assertion "
        "below would pass without anything being repaired")
    dockerlib.run_once(SERVICES_TAG, ["chmod", "0644", "/work/.env"],
                       read_only=False,
                       binds=[f"{env_file.parent}:/work"])
    assert env_file.stat().st_mode & 0o777 == 0o644, "the loosening did not take"
    ask_ok(spawner, {"op": "provision", "tenant_id": tenant_id,
                     "project_id": project_id})
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_provisioning_is_idempotent(spawner, spawner_root, tenant):
    """A second provision writes nothing, and a SOUL.md the tenant edited stays
    edited."""
    tenant_id, project_id = tenant
    soul = spawner_root / "tenants" / tenant_id / "home" / "SOUL.md"
    dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-euc", "rm -f /data/SOUL.md; printf 'MINE\\n' > /data/SOUL.md"],
        read_only=False, binds=[f"{soul.parent}:/data"])
    mine = soul.read_text(encoding="utf-8")
    assert mine == "MINE\n"
    ask_ok(spawner, {"op": "provision", "tenant_id": tenant_id,
                     "project_id": project_id})
    assert soul.read_text(encoding="utf-8") == mine


def test_a_second_start_issues_no_recursive_walk(spawner, tenant):
    """C-1's Docker half. `xfs_quota project -s` descends the directory, so it
    may run only on one this process just created. The spawner logs each
    xfs_quota argv at DEBUG; the second provision must show a `limit -p` and no
    `project -s`.

    The `tenant` fixture has already provisioned once, so the count below is
    taken AFTER the create path has had its one legitimate walk."""
    dockerlib.require_xfs()
    tenant_id, project_id = tenant
    marker = "project -s"
    before = dockerlib.logs(SPAWNER_CONTAINER).count(marker)
    assert before, (
        "the spawner logged no `project -s` at all, so the count below is "
        "asserting against nothing -- check WAKU_LOG_LEVEL and that the "
        "fixture's first provision took the create path")
    ask_ok(spawner, {"op": "provision", "tenant_id": tenant_id,
                     "project_id": project_id})
    after = dockerlib.logs(SPAWNER_CONTAINER)
    assert after.count(marker) == before, (
        "provisioning an existing directory issued a recursive project walk. "
        "That directory holds whatever the tenant wrote and this process is "
        "root with CAP_SYS_ADMIN.")
    assert "limit -p" in after
