"""Acceptance 2 and acceptance 16, against a real kernel.

C3 OWNS THIS FILE and writes the NETWORK half -- the bridges, the firewall, the
six counted drops, the host's ports, the socket directories. C3 was DEFERRED by
a scope decision, so C2 creates the file with its own half and edits nothing:
when C3 lands it appends, the same way it would have if the order had held.

WHAT IS HERE: the environment-and-files half of acceptance 2 (the platform key
is in no tenant container) and acceptance 16's disk, seccomp and FTW_PHYS
cases. Every one of them reads the kernel's own answer or tries the thing the
field is supposed to prevent; none reads a dict.
"""

from __future__ import annotations

import dockerlib
from spawnerlib import (
    PROJECT_A,
    PROJECT_B,
    SERVICES_TAG,
    TENANT_A,
    TENANT_B,
    TOKEN_ONE,
    TOKEN_TWO,
    ask,
)

from hosted.spawner import template


def test_the_platform_key_is_in_no_tenant_container(spawner, spawner_root):
    """Acceptance 2.

    The KEY -- the platform's own Anthropic key -- never leaves the proxy. What
    a tenant container holds is a per-container proxy TOKEN, useless anywhere
    but the proxy and revoked when the container stops.

    The key is PLANTED in a real environment on this host for the test, so the
    assertion is about a value that exists and must not be in the container --
    not an absence asserted against a string nobody ever set, which passes on
    a typo.
    """
    key = "sk-ant-PLANTED-PLATFORM-KEY-do-not-ship"
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)

    env = dockerlib.inspect(name)["Config"]["Env"]
    assert not [entry for entry in env if key in entry], env

    found = dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-c", f"grep -rl {key!r} /data /work 2>/dev/null || true"],
        read_only=False,
        binds=[f"{spawner_root}/tenants/{TENANT_A}/home:/data",
               f"{spawner_root}/tenants/{TENANT_A}/env:/work"]).stdout.strip()
    assert not found, f"the platform key is in {found}"

    assert key not in dockerlib.logs(name)

    # The control that makes the three assertions above able to fail: the same
    # grep finds the key when it IS there.
    (spawner_root / "canary.txt").write_text(key, encoding="utf-8")
    control = dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-c", f"grep -rl {key!r} /probe 2>/dev/null || true"],
        read_only=False, binds=[f"{spawner_root}:/probe"]).stdout.strip()
    assert control, "the grep finds nothing even when the key is there"
    (spawner_root / "canary.txt").unlink()


def test_a_tenant_cannot_write_past_their_disk_limit(spawner, spawner_root):
    """Acceptance 16's disk clause, on the loop-mounted XFS the hosted-docker
    job makes. Skips elsewhere with the platform named.

    The limit is the 64 MiB the spawner fixture configured, not the 1 GB
    production default, and the assertion is that THE CONFIGURED LIMIT IS THE
    ONE THAT BOUND: under it succeeds, over it fails with ENOSPC. F1's default
    is 1 GB and G1 runs this against it on the VM.
    """
    dockerlib.require_xfs()
    for tenant_id, project_id, token in ((TENANT_A, PROJECT_A, TOKEN_ONE),
                                         (TENANT_B, PROJECT_B, TOKEN_TWO)):
        ask(spawner, {"op": "start", "tenant_id": tenant_id,
                      "project_id": project_id, "timezone": "UTC",
                      "token": token})
    name_a = template.container_name(TENANT_A, template.KIND_TENANT)
    name_b = template.container_name(TENANT_B, template.KIND_TENANT)
    for name in (name_a, name_b):
        dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)

    under = dockerlib.exec_in(
        name_a, ["dd", "if=/dev/zero", "of=/data/fill-under", "bs=1M", "count=48"],
        check=False)
    assert under.returncode == 0, (
        f"48 MiB failed under a 64 MiB limit: {under.stderr[-500:]}")

    over = dockerlib.exec_in(
        name_a, ["dd", "if=/dev/zero", "of=/data/fill-over", "bs=1M", "count=64"],
        check=False)
    assert over.returncode != 0, "64 more MiB succeeded past a 64 MiB hard limit"
    assert "space" in over.stderr.lower() or "quota" in over.stderr.lower(), over.stderr

    # Other tenants keep working. Same clause of acceptance 16, and the reason
    # the limit is per-project rather than per-filesystem.
    other = dockerlib.exec_in(
        name_b, ["dd", "if=/dev/zero", "of=/data/fill", "bs=1M", "count=8"],
        check=False)
    assert other.returncode == 0, (
        f"tenant A filling their quota stopped tenant B: {other.stderr[-500:]}")
    dockerlib.exec_in(name_a, ["rm", "-f", "/data/fill-under", "/data/fill-over"],
                      check=False)
    dockerlib.exec_in(name_b, ["rm", "-f", "/data/fill"], check=False)


# struct fsxattr: u32 fsx_xflags, fsx_extsize, fsx_nextents, fsx_projid,
# fsx_cowextsize, then 8 pad bytes. 28 bytes, which is the 0x1c in both
# request numbers below.
PROJECT_ID_PROBE = r'''
import fcntl, struct, sys
FSGETXATTR = 0x801C581F
FSSETXATTR = 0x401C5820
fd = open(sys.argv[1], "rb")
buf = bytearray(28)
try:
    fcntl.ioctl(fd, FSGETXATTR, buf, True)
except OSError as exc:
    print("GET-FAILED", exc.errno); sys.exit(0)
print("GET-OK")
flags, extsize, nextents, projid, cowextsize = struct.unpack_from("<5I", buf)
struct.pack_into("<5I", buf, 0, flags, extsize, nextents, 4242, cowextsize)
try:
    fcntl.ioctl(fd, FSSETXATTR, bytes(buf))
except OSError as exc:
    print("SET-FAILED", exc.errno); sys.exit(0)
print("SET-OK")
'''


def test_changing_the_project_id_of_ones_own_file_fails(spawner, tenant_image,
                                                        spawner_root):
    """Acceptance 16's seccomp clause, and the pair that makes it honest.

    TWO IOCTL REQUESTS, ONE VALUE APART, on the same file in the same process:
    FS_IOC_FSGETXATTR (0x801c581f) must SUCCEED and FS_IOC_FSSETXATTR
    (0x401c5820) must fail with EPERM. So it cannot pass because ioctl is
    broken in the container, and it cannot pass because the container never
    started.

    The control: the same probe with `--security-opt seccomp=unconfined`, where
    BOTH succeed. Three outcomes.
    """
    dockerlib.require_xfs()
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)
    dockerlib.exec_in(name, ["python", "-c", "open('/data/mine','w').write('x')"])

    under_profile = dockerlib.exec_in(
        name, ["python", "-c", PROJECT_ID_PROBE, "/data/mine"]).stdout
    assert "GET-OK" in under_profile, (
        f"FS_IOC_FSGETXATTR failed under the profile, so the SET result below "
        f"proves nothing about the ONE rule this profile adds: {under_profile}")
    assert "SET-FAILED 1" in under_profile, (   # 1 == EPERM
        f"FS_IOC_FSSETXATTR was allowed: {under_profile}. A tenant can move "
        "their own file into another XFS project and write past their quota.")

    home = spawner_root / "tenants" / TENANT_A / "home"
    control = dockerlib.run_once(
        tenant_image, ["python", "-c", PROJECT_ID_PROBE, "/data/mine"],
        read_only=False, binds=[f"{home}:/data"],
        extra=["--security-opt", "seccomp=unconfined", "--cap-add", "SYS_ADMIN"]).stdout
    assert "GET-OK" in control and "SET-OK" in control, (
        f"the control did not succeed unconfined: {control}. Then the profile "
        "is not what stopped the write above, and this test proves nothing.")


def _project_id_of_path(path) -> int:
    """Read a file's XFS project id, without writing anything.

    FS_IOC_FSGETXATTR only, in a root container with xfsprogs: the SET half of
    PROJECT_ID_PROBE would change the very thing this is measuring.
    """
    program = (
        "import fcntl, struct, sys\n"
        "buf = bytearray(28)\n"
        "fcntl.ioctl(open(sys.argv[1], 'rb'), 0x801C581F, buf, True)\n"
        "print(struct.unpack_from('<5I', buf)[3])\n")
    out = dockerlib.run_once(
        SERVICES_TAG, ["python", "-c", program, str(path)],
        user="0:0", read_only=False,
        binds=[f"{path.parent}:{path.parent}"]).stdout.strip()
    return int(out)


def test_xfs_quota_does_not_follow_a_symlink_when_it_walks():
    """The named dependency behind xfsquota.repair.

    `project -s` traverses with nftw()'s FTW_PHYS, so it does not descend a
    symlink. Nothing in this repo owns that flag, so a tenant who plants
    `/data/x -> <the filesystem root>` is one xfsprogs change away from having
    the WHOLE filesystem reassigned to their project id and their hard limit.
    The repair path is the only place that walk still happens, and this is what
    keeps its safety a tested fact rather than a comment.
    """
    mount, device = dockerlib.require_xfs()
    walked = mount / "walk-probe"
    outside = mount / "outside"
    outside.mkdir(exist_ok=True)
    walked.mkdir(exist_ok=True)
    (outside / "victim").write_text("x", encoding="utf-8")
    link = walked / "escape"
    if not link.is_symlink():
        link.symlink_to(outside)

    before = _project_id_of_path(outside / "victim")
    dockerlib.run_once(
        SERVICES_TAG,
        ["xfs_quota", "-x", "-c", f"project -s -p {walked} 9911", str(mount)],
        user="0:0", read_only=False,
        binds=[f"{mount}:{mount}"], extra=["--cap-add", "SYS_ADMIN",
                                           "--device", device])
    after = _project_id_of_path(outside / "victim")
    assert after == before, (
        f"xfs_quota followed the symlink: the file outside the walked "
        f"directory moved from project {before} to {after}. FTW_PHYS is no "
        "longer holding, and xfsquota.repair is now a filesystem-wide "
        "project-id reassignment a tenant can aim.")
