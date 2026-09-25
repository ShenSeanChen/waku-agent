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

import contextlib

import dockerlib
from spawnerlib import (
    PLANTED_PLATFORM_KEY,
    PROJECT_A,
    PROJECT_B,
    SERVICES_TAG,
    SPAWNER_CONTAINER,
    TENANT_A,
    TENANT_B,
    TOKEN_ONE,
    TOKEN_TWO,
    ask,
)

from hosted.core import tenant
from hosted.spawner import template

# A port nothing else on a runner is likely to hold, high enough to need no
# privilege. The listener below is a stand-in for D1's proxy: the point is only
# that SOMETHING on the host answers on the bridge gateway.
GATEWAY_PROBE_PORT = 18788

_HOST_LISTENER = (
    "import socketserver, sys\n"
    "class H(socketserver.BaseRequestHandler):\n"
    "    def handle(self):\n"
    "        self.request.sendall(b'ok')\n"
    "socketserver.ThreadingTCPServer.allow_reuse_address = True\n"
    "socketserver.ThreadingTCPServer(('0.0.0.0', int(sys.argv[1])), H)"
    ".serve_forever()\n")

HOST_LISTENER_CONTAINER = "waku-gateway-probe"


@contextlib.contextmanager
def _listening_on_the_host(port: int):
    """Something answering on the host, reachable at the bridge gateway.

    `--network host`, so the socket is the HOST's and the bridge gateway
    address routes to it -- which is exactly how D1's proxy will be reached.
    Torn down in a finally, because a stray container on a fixed name breaks
    every later run with a name conflict rather than with a test failure.
    """
    dockerlib.remove(HOST_LISTENER_CONTAINER)
    try:
        dockerlib.start_detached(
            SERVICES_TAG, ["python", "-c", _HOST_LISTENER, str(port)],
            name=HOST_LISTENER_CONTAINER, user="0:0", network="host",
            read_only=False)
        dockerlib.wait_for_listener(HOST_LISTENER_CONTAINER, "127.0.0.1", port)
        yield
    finally:
        dockerlib.remove(HOST_LISTENER_CONTAINER)


def test_a_tenant_cannot_open_a_socket_to_another_tenants_dashboard(spawner):
    """The ONE piece of acceptance 1 this branch carries, and the reason it is
    here rather than waiting for C3.

    A tenant's dashboard has NO AUTHENTICATION of its own -- the gateway in
    front of it is the whole of it -- and a user-defined Docker bridge allows
    container-to-container traffic by DEFAULT. So with ICC on, tenant A opens
    TCP to 10.88.0.<B>:7777 and reads tenant B's chat log, memory and SQL
    console. `enable_icc=false` in core/tenant.BRIDGE_OPTIONS is what closes
    it, and this is the test that tries the connection.

    THE CONTROL IS THE HALF THAT MATTERS, and it takes TWO probes, not one.
    `probe_tcp` returning False is also what a broken prober, a dead container,
    a dashboard that never bound and a container that never joined the network
    look like.

      - 127.0.0.1:7777 proves the prober works and the dashboard is up. It
        does NOT prove the container is on the tenant bridge at all: a
        container with no route off its own loopback passes it and then passes
        every cross-tenant assertion below for the wrong reason.
      - TENANT_GATEWAY:<a port the host is listening on> proves the container's
        OFF-LOOPBACK networking works. ICC is documented not to block the
        bridge gateway -- deliberately, because that is where the proxy will
        listen on 10.88.0.1:8788 -- so this is the one route that must still
        work after enable_icc=false, and the one D1 depends on.

    With both, "A cannot reach B" becomes "A's off-container networking works
    and still cannot reach B".

    WHAT THIS DOES NOT COVER, and nothing on this branch does: the DOCKER-USER
    forward rules, the dropped link-local, CGNAT and private ranges, the DNS
    exception, the host's INPUT rules, and the inspect bridge's egress. All of
    those are C3's firewall.sh and C3 is deferred. A green run here means
    tenant A cannot reach tenant B ON THE BRIDGE; it does not mean a tenant
    container is confined to it.
    """
    for tenant_id, project_id, token in ((TENANT_A, PROJECT_A, TOKEN_ONE),
                                         (TENANT_B, PROJECT_B, TOKEN_TWO)):
        answer = ask(spawner, {"op": "start", "tenant_id": tenant_id,
                               "project_id": project_id, "timezone": "UTC",
                               "token": token})
        assert "error" not in answer, answer
    name_a = template.container_name(TENANT_A, template.KIND_TENANT)
    name_b = template.container_name(TENANT_B, template.KIND_TENANT)
    port = template.DASHBOARD_PORT
    for name in (name_a, name_b):
        dockerlib.wait_for_listener(name, "127.0.0.1", port)

    assert dockerlib.probe_tcp(name_a, "127.0.0.1", port) is True, (
        "tenant A cannot reach its OWN dashboard, so the refusal below is not "
        "evidence of anything about the bridge")
    assert dockerlib.probe_tcp(name_b, "127.0.0.1", port) is True, (
        "tenant B cannot reach its OWN dashboard")

    gateway = str(tenant.TENANT_GATEWAY)
    with _listening_on_the_host(GATEWAY_PROBE_PORT):
        for name in (name_a, name_b):
            assert dockerlib.probe_tcp(name, gateway, GATEWAY_PROBE_PORT) is True, (
                f"{name} cannot reach the bridge gateway {gateway}:"
                f"{GATEWAY_PROBE_PORT}, so it has no route off its own "
                "loopback and every refusal below would pass for a container "
                "that never joined the network. This is also the route the "
                "proxy will need on 10.88.0.1:8788 -- if enable_icc=false is "
                "what broke it, D1 is broken too.")

    address_a = tenant.address_for_project(PROJECT_A)
    address_b = tenant.address_for_project(PROJECT_B)
    assert dockerlib.probe_tcp(name_a, address_b, port) is False, (
        f"tenant A opened TCP to {address_b}:{port} -- tenant B's dashboard, "
        "which has no authentication of its own. enable_icc=false is not on "
        "the tenant bridge.")
    assert dockerlib.probe_tcp(name_b, address_a, port) is False, (
        f"tenant B opened TCP to {address_a}:{port}. ICC blocks a direction at "
        "a time in nobody's implementation, so this failing alone would be "
        "stranger than both failing.")


def test_a_restored_tenant_keeps_their_own_project_id(spawner, spawner_root):
    """C2-1, against a real XFS.

    `provision()`'s CREATE path is the only thing that issues `project -s`, so
    a directory that comes back by any other route -- the daemon creating a
    missing bind source, say -- keeps XFS project 0: uncounted and unlimited,
    for the rest of that tenant's life. `env` was fine and `home` was not, and
    the asymmetry is invisible from anywhere except here.

    BOTH DIRECTORIES ARE CHECKED, and against the tenant's OWN project id
    rather than against "not zero": a restore that claimed a fresh id would
    pass a not-zero check while giving the tenant a new accounting bucket and
    a new fixed bridge address.
    """
    dockerlib.require_xfs()
    ask(spawner, {"op": "stop", "tenant_id": TENANT_A})
    assert "error" not in ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                                        "project_id": PROJECT_A})
    assert "error" not in ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                                        "task": "backup"})
    # The backup declares itself finished by writing this LAST, and the restore
    # below refuses without it. Asserted here so a backup that stopped writing
    # the manifest reads as that, rather than as a restore that mysteriously
    # refuses -- the offline half models the manifest, and this is the only
    # place the real _BACKUP_SCRIPT is what writes one.
    manifest = spawner_root / "staging" / TENANT_A / "manifest.json"
    assert manifest.is_file(), (
        f"{manifest} is not there after a backup. _BACKUP_SCRIPT writes it as "
        "its last action; without it the restore below correctly refuses and "
        "this test would fail for the wrong reason.")
    home = spawner_root / "tenants" / TENANT_A / "home"
    env = spawner_root / "tenants" / TENANT_A / "env"
    before = (_project_id_of_path(home), _project_id_of_path(env))
    assert before == (PROJECT_A, PROJECT_A), (
        f"the directories carry {before} before the restore, not "
        f"{PROJECT_A} -- so the assertion after it would prove nothing")

    answer = ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                           "task": "restore", "project_id": PROJECT_A})
    assert "error" not in answer, answer

    after = (_project_id_of_path(home), _project_id_of_path(env))
    assert after == (PROJECT_A, PROJECT_A), (
        f"after the restore the two directories carry {after}, not "
        f"({PROJECT_A}, {PROJECT_A}). A directory in project 0 has no disk "
        "limit at all, and the tenant can fill the shared data disk.")


def test_an_archive_container_can_write_into_the_directory_it_is_given(
        spawner, spawner_root):
    """GC-1, the half no offline test can see.

    `_archive` hands a container running as UID 10001 a directory and the
    container creates two files in it. Before this, that directory was the
    SHARED archive root, mkdir'd by the spawner as root at 0755 -- so `zstd -o`
    was EACCES, `bash -euc` exited non-zero, and every archive, and therefore
    every restore, failed with jsonsock's opaque error.

    The files are checked by NAME ON THE HOST, not by the task's exit code: a
    task that answered without writing is the shape this whole group has been
    paying for.

    It also archives a SECOND tenant and asserts neither directory holds the
    other's files. With every tenant on UID 10001, the mode separates them from
    other host users and not from each other -- the mount is what separates
    tenants, and `allowed_bind_sources` is where that is asserted. This is the
    end state that mount produces.
    """
    for tenant_id, project_id, token in ((TENANT_A, PROJECT_A, TOKEN_ONE),
                                         (TENANT_B, PROJECT_B, TOKEN_TWO)):
        assert "error" not in ask(spawner, {"op": "provision",
                                            "tenant_id": tenant_id,
                                            "project_id": project_id}), token
        answer = ask(spawner, {"op": "task", "tenant_id": tenant_id,
                               "task": "archive"})
        assert "error" not in answer, (
            f"archiving {tenant_id} failed: {answer}. If this is EACCES, the "
            "archive directory was not handed to UID 10001.")

    for tenant_id, other in ((TENANT_A, TENANT_B), (TENANT_B, TENANT_A)):
        directory = spawner_root / "archive" / tenant_id
        written = sorted(entry.name for entry in directory.iterdir())
        assert written, (
            f"{directory} is empty after an archive, so the task reported "
            "success without writing anything")
        assert all(name.endswith(".tar.zst") for name in written), written
        assert all(name.startswith(tenant_id) for name in written), (
            f"{directory} holds {written}, which is not all this tenant's")
        assert not [name for name in written if other in name], (
            f"{other}'s archive is in {tenant_id}'s directory: {written}")


def test_a_backup_does_not_resurrect_a_file_the_tenant_deleted(spawner, spawner_root):
    """NEW-2, against the real scripts.

    Staging holds ONE backup. Before this, `rm -f manifest.json` invalidated
    the old one and nothing emptied the old `home/` and `env/`, so the
    directories accumulated the union of every backup ever taken: a file the
    tenant deleted was still in staging, and the next restore put it back.

    Back up a file, delete it from the tenant, back up again, restore, and it
    must be gone. Every step is asserted so a failure says which one broke --
    in particular the file must BE there after the first backup, or the absence
    at the end means only that it never arrived.
    """
    dockerlib.require_xfs()
    ask(spawner, {"op": "stop", "tenant_id": TENANT_A})
    assert "error" not in ask(spawner, {"op": "provision", "tenant_id": TENANT_A,
                                        "project_id": PROJECT_A})
    home = spawner_root / "tenants" / TENANT_A / "home"
    staged_home = spawner_root / "staging" / TENANT_A / "home"

    dockerlib.run_once(SERVICES_TAG,
                       ["bash", "-euc", "printf 'old\n' > /data/deleted-later.txt"],
                       read_only=False, binds=[f"{home}:/data"])
    assert "error" not in ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                                        "task": "backup"})
    assert (staged_home / "deleted-later.txt").is_file(), (
        "the first backup did not stage the file, so the absence asserted at "
        "the end of this test would mean nothing")

    dockerlib.run_once(SERVICES_TAG, ["rm", "/data/deleted-later.txt"],
                       read_only=False, binds=[f"{home}:/data"])
    assert not (home / "deleted-later.txt").exists()

    assert "error" not in ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                                        "task": "backup"})
    assert not (staged_home / "deleted-later.txt").exists(), (
        "the second backup left the first backup's file in staging. The "
        "manifest describes this backup and the directories hold every backup "
        "ever taken.")

    assert "error" not in ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                                        "task": "restore",
                                        "project_id": PROJECT_A})
    assert not (home / "deleted-later.txt").exists(), (
        "a file the tenant deleted came back through a restore.")


def test_the_platform_key_is_in_no_tenant_container(spawner, spawner_root):
    """Acceptance 2.

    The KEY -- the platform's own Anthropic key -- never leaves the proxy. What
    a tenant container holds is a per-container proxy TOKEN, useless anywhere
    but the proxy and revoked when the container stops.

    THE KEY IS REALLY PLANTED, and the first assertion is that it is. The
    `spawner` fixture puts PLANTED_PLATFORM_KEY in the SPAWNER's environment --
    the privileged process that builds every container's Env -- so a template
    that passed its own environment through, or a future `**os.environ`, is
    caught here. An earlier version of this test declared the key as a local
    string that was never introduced anywhere: every absence below was an
    absence with no source, and the three assertions could not fail. The
    docstring claimed the opposite, which is what made it worth finding.

    Four assertions and two controls:

      CONTROL A -- the key IS in the spawner's own environment, so "not in the
      tenant's" is a statement about something that exists.
      CONTROL B -- the same `grep` finds the key when it is there.
    """
    key = PLANTED_PLATFORM_KEY
    spawner_env = dockerlib.inspect(SPAWNER_CONTAINER)["Config"]["Env"]
    assert [entry for entry in spawner_env if key in entry], (
        "the planted key is not in the spawner's own environment, so every "
        "absence below is an absence with no source and none of it can fail. "
        "conftest.py's `spawner` fixture is what plants it.")

    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)

    env = dockerlib.inspect(name)["Config"]["Env"]
    assert not [entry for entry in env if key in entry], (
        f"the platform key reached the tenant container's environment: {env}")

    found = dockerlib.run_once(
        SERVICES_TAG,
        ["bash", "-c", f"grep -rl {key!r} /data /work 2>/dev/null || true"],
        read_only=False,
        binds=[f"{spawner_root}/tenants/{TENANT_A}/home:/data",
               f"{spawner_root}/tenants/{TENANT_A}/env:/work"]).stdout.strip()
    assert not found, f"the platform key is in {found}"

    assert key not in dockerlib.logs(name)

    # CONTROL B: the same grep, over a directory the key IS in.
    (spawner_root / "canary.txt").write_text(key, encoding="utf-8")
    try:
        control = dockerlib.run_once(
            SERVICES_TAG,
            ["bash", "-c", f"grep -rl {key!r} /probe 2>/dev/null || true"],
            read_only=False, binds=[f"{spawner_root}:/probe"]).stdout.strip()
        assert control, "the grep finds nothing even when the key is there"
    finally:
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
    # os.open, not open(): this is called on DIRECTORIES as well as files, and
    # open(dir, 'rb') raises IsADirectoryError. A directory's project id is the
    # interesting one -- it is what XFS_DIFLAG_PROJINHERIT rides on, so it is
    # what every file created later inherits.
    program = (
        "import fcntl, os, struct, sys\n"
        "buf = bytearray(28)\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
        "fcntl.ioctl(fd, 0x801C581F, buf, True)\n"
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
