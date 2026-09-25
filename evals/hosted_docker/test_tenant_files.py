"""Acceptance 15 -- no privileged process follows a tenant's symlink.

THE PROVISIONING HALF. F3 adds backup and restore, F4 archive and inspect.

THE SHAPE: plant the symlinks a tenant could plant, run provisioning, and check
the target by CONTENT and MTIME rather than by an exit code. "The command
succeeded" is not "the file was not written".

The `spawner` and `spawner_root` fixtures come from conftest.py; the helpers
from spawnerlib.py. One spawner container serves this module.
"""

from __future__ import annotations

import dockerlib
import pytest
from spawnerlib import (
    PROJECT_A,
    SERVICES_TAG,
    SPAWNER_CONTAINER,
    TENANT_A,
    allowed_bind_sources,
    ask,
    capture_task_containers,
)

from hosted.spawner import template


def _plant(spawner_root, relative: str, target: str) -> None:
    """Make <tenant dir>/<relative> a symlink to `target`, as the tenant would:
    from inside a container, as UID 10001."""
    home = spawner_root / "tenants" / TENANT_A / "home"
    env = spawner_root / "tenants" / TENANT_A / "env"
    mount = home if relative.startswith("SOUL") else env
    dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-euc", f"rm -f /mnt/{relative}; ln -s {target} /mnt/{relative}"],
        read_only=False,
        binds=[f"{mount}:/mnt"])


@pytest.fixture()
def provisioned(spawner, spawner_root):
    ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A})
    return spawner_root


def test_provisioning_does_not_write_through_a_planted_env_symlink(
        spawner, spawner_root, provisioned):
    """core/provision.py's `if env_file.is_symlink(): pass` branch, proved at
    the container level rather than at the unit level.

    THE TARGET IS INSIDE /data, and that is what makes this able to fail. The
    brief planted `.env -> /data/../secret.txt`, which resolves to `/secret.txt`
    inside the container -- on the read-only root, where the write fails
    whatever provision.py does, so the assertion could not tell the guard from
    the containment. A target under the tenant's own home mount is writable, so
    a provision that followed the link would create it.

    Both halves are asserted: the provision SUCCEEDED (a raising provision
    would come back as jsonsock's opaque error and the absence below would
    mean only that the run died), and the target does not exist.
    """
    target = spawner_root / "tenants" / TENANT_A / "home" / "env-link-target.txt"
    _plant(spawner_root, ".env", "/data/env-link-target.txt")
    answer = ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                           "project_id": PROJECT_A})
    assert "error" not in answer, (
        f"provisioning a tenant with a planted .env symlink failed: {answer}. "
        "It must be a no-op on that file, not a crash -- provisioning runs "
        "before every start, so a crash locks the tenant out for good.")
    assert not target.exists(), (
        "provisioning followed a planted .env symlink and wrote its target")


def test_provisioning_does_not_write_through_a_planted_soul_symlink(
        spawner, spawner_root, provisioned):
    """The branch C2 adds to core/provision.py. Both cases: a link to an
    EXISTING path, which exists() already reported as present, and a link to a
    MISSING one, which exists() reported as absent and write_text would have
    followed.

    Both targets are under /data, the tenant's own writable mount, for the same
    reason as the .env case above: a target on the read-only root cannot be
    written whatever provision.py does, so an assertion about it cannot tell
    the guard from the containment.
    """
    home = spawner_root / "tenants" / TENANT_A / "home"
    for state in ("existing", "dangling"):
        target = home / f"soul-{state}.txt"
        if state == "existing":
            dockerlib.run_once(
                SERVICES_TAG,
                ["bash", "-euc", "printf 'UNTOUCHED\n' > /data/soul-existing.txt"],
                read_only=False, binds=[f"{home}:/data"])
        _plant(spawner_root, "SOUL.md", f"/data/soul-{state}.txt")
        answer = ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                               "project_id": PROJECT_A})
        assert "error" not in answer, (
            f"provisioning with a planted SOUL.md symlink failed: {answer}")
        if state == "existing":
            assert target.read_text(encoding="utf-8") == "UNTOUCHED\n", (
                "provisioning wrote the SOUL template through a symlink to a "
                "file the tenant already had")
        else:
            assert not target.exists(), (
                "provisioning followed a dangling SOUL.md symlink and created "
                "its target")


def test_a_tenant_cannot_make_provisioning_overwrite_their_own_env(
        spawner, spawner_root, provisioned):
    """The reachable case, and the one the containment does NOT stop on its own.

    /tmp, /data and /work are all writable in the throwaway container, so
    before C2's fix a tenant pointing home/SOUL.md at /work/.env got their
    own .env replaced with the SOUL template on their next start. Nothing
    outside those three paths is writable, so it was self-harm only -- but
    self-harm caused by the platform, on a path the tenant did not name, is
    still a defect.
    """
    env_file = spawner_root / "tenants" / TENANT_A / "env" / ".env"
    _plant(spawner_root, "SOUL.md", "/work/.env")
    ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A})
    assert "WAKU_PROVIDER=waku-platform" in env_file.read_text(encoding="utf-8")


def test_provisioning_runs_as_10001_on_no_network_with_only_two_mounts(
        spawner, spawner_root):
    seen = capture_task_containers(
        spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A}, TENANT_A)
    assert len(seen) == 1, [c["Config"]["Labels"] for c in seen]
    container = seen[0]
    assert container["Config"]["Labels"][template.LABEL_KIND] == template.KIND_PROVISION
    assert container["Config"]["User"] == "10001:10001"
    assert container["HostConfig"]["NetworkMode"] == "none"
    sources = {bind.split(":", 1)[0].rstrip("/")
               for bind in container["HostConfig"]["Binds"]}
    # DEFAULT-DENY, the same allowlist the task containers are held to.
    assert sources == allowed_bind_sources(spawner_root, TENANT_A, "provision"), (
        f"provisioning mounted {sorted(sources)}")


def test_provisioning_repairs_a_loosened_env_mode(spawner, spawner_root, provisioned):
    """It runs before EVERY start, so a mode the tenant loosened is repaired on
    the next one."""
    env_file = spawner_root / "tenants" / TENANT_A / "env" / ".env"
    dockerlib.run_once(SERVICES_TAG, ["chmod", "0644", "/work/.env"],
                       read_only=False,
                       binds=[f"{env_file.parent}:/work"])
    ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A})
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_provisioning_is_idempotent(spawner, spawner_root, provisioned):
    """A second provision writes nothing, and a SOUL.md the tenant edited stays
    edited."""
    soul = spawner_root / "tenants" / TENANT_A / "home" / "SOUL.md"
    dockerlib.run_once(SERVICES_TAG,
                       ["bash", "-euc", "rm -f /data/SOUL.md; echo 'MINE' > /data/SOUL.md"],
                       read_only=False, binds=[f"{soul.parent}:/data"])
    mine = soul.read_text(encoding="utf-8")
    ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A})
    assert soul.read_text(encoding="utf-8") == mine


def test_a_second_start_issues_no_recursive_walk(spawner, spawner_root, provisioned):
    """C-1's Docker half. `xfs_quota project -s` descends the directory, so it
    may run only on one this process just created. The spawner logs each
    xfs_quota argv at DEBUG; the second provision must show a `limit -p` and no
    `project -s`."""
    dockerlib.require_xfs()
    marker = "project -s"
    before = dockerlib.logs(SPAWNER_CONTAINER).count(marker)
    ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                  "project_id": PROJECT_A})
    after = dockerlib.logs(SPAWNER_CONTAINER)
    assert after.count(marker) == before, (
        "provisioning an existing directory issued a recursive project walk. "
        "That directory holds whatever the tenant wrote and this process is "
        "root with CAP_SYS_ADMIN.")
    assert "limit -p" in after
