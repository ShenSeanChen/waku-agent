"""The Docker CLI, wrapped, for the third eval tier.

WHY THE CLI AND NOT THE ENGINE API. hosted/spawner/ talks to the Engine API
with aiohttp because the spec says so and because a service should not depend
on a CLI being installed. A test harness is the other case: it has to BUILD
images, which the Engine API does not do in any small way, and it is read by
people debugging a red CI job, who will paste the command into their own shell.
So hosted/ uses the API and evals/hosted_docker/ uses the CLI, and neither
borrows the other's mechanism.

THE THREE OUTCOMES. A Docker test that passes because the container never
started is worse than no test. Every function here keeps the three apart:

  no daemon      -> daemon_version() returns None, and conftest.py skips the
                    whole directory with the daemon's own message
  a broken build -> build_image RAISES DockerError with the build log's tail;
                    it never skips, because a build that does not build is a
                    finding, not an environment
  a healthy run  -> the assertion in the test decides

and wait_for_listener adds the fourth: a probe target that never came up fails
the test BEFORE any assertion about the thing under test, so "A cannot reach B"
can never pass because B was not listening.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Long enough for a cold `uv sync` over the network on a slow runner, short
# enough that a hung daemon fails the job instead of burning the six-hour
# GitHub limit.
BUILD_TIMEOUT = 900
RUN_TIMEOUT = 300
QUICK_TIMEOUT = 60


class DockerError(RuntimeError):
    """Docker answered, and the answer was a failure. Never a skip."""


def _run(args: list[str], *, timeout: int, check: bool) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, check=False, cwd=str(REPO))
    if check and proc.returncode != 0:
        raise DockerError(
            f"docker {' '.join(args)}\nexit {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}")
    return proc


def daemon_version() -> str | None:
    """The daemon's version, or None if there is no daemon to ask.

    `docker version --format '{{.Server.Version}}'` is the probe because it
    exits non-zero with empty stdout when the daemon is down, exits non-zero
    with a FileNotFoundError when the CLI is absent, and never hangs waiting
    for a container. Checked on macOS 25.6.0 with Docker 28.0.4 installed and
    Docker Desktop stopped: exit 1, stdout empty.
    """
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def daemon_reason() -> str:
    """What to put in the skip message. The daemon's own words, not ours."""
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        return "no `docker` on PATH"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`docker version` did not answer: {exc}"
    return (proc.stderr.strip() or proc.stdout.strip()
            or f"`docker version` exited {proc.returncode}")


def build_image(dockerfile: str, tag: str) -> str:
    """Build one image with BuildKit and the repo root as context.

    RAISES on failure. A failed build is not a reason to skip: the daemon
    answered, so the environment is fine and the Dockerfile is not.
    """
    proc = subprocess.run(
        ["docker", "build", "--file", dockerfile, "--tag", tag, str(REPO)],
        capture_output=True, text=True, timeout=BUILD_TIMEOUT, check=False,
        cwd=str(REPO), env={**_env(), "DOCKER_BUILDKIT": "1"})
    if proc.returncode != 0:
        raise DockerError(
            f"building {tag} from {dockerfile} failed with exit {proc.returncode}.\n"
            "A daemon answered at collection time, so read this as a finding "
            "about the Dockerfile first. (If the daemon stopped between "
            "collection and this build, the log tail below says so instead.)\n"
            f"--- build log tail ---\n{(proc.stdout + proc.stderr)[-4000:]}")
    return tag


def _env() -> dict[str, str]:
    return dict(os.environ)


def _flags(*, env, binds, user, network, ip, read_only, extra) -> list[str]:
    args: list[str] = []
    if user:
        args += ["--user", user]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    for bind in binds or []:
        args += ["--volume", bind]
    if network:
        args += ["--network", network]
    if ip:
        args += ["--ip", ip]
    if read_only:
        # TWO of the runtime flags the spec gives a tenant container: a
        # read-only root and a 256 MB tmpfs at /tmp, which is also HOME. NOT
        # the whole shape -- CapDrop ALL, no-new-privileges, the seccomp
        # profile and the memory/CPU/pids limits are C2's, and a container
        # started through this helper has none of them.
        args += ["--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m"]
    args += list(extra or [])
    return args


def run_once(tag, cmd, *, env=None, binds=None, user="10001:10001", network=None,
             read_only=True, extra=None, check=True) -> subprocess.CompletedProcess:
    args = ["run", "--rm", *_flags(env=env, binds=binds, user=user, network=network,
                                   ip=None, read_only=read_only, extra=extra),
            tag, *cmd]
    return _run(args, timeout=RUN_TIMEOUT, check=check)


def start_detached(tag, cmd=None, *, name, env=None, binds=None, user="10001:10001",
                   network=None, ip=None, read_only=True, extra=None) -> str:
    args = ["run", "--detach", "--name", name,
            *_flags(env=env, binds=binds, user=user, network=network, ip=ip,
                    read_only=read_only, extra=extra),
            tag, *(cmd or [])]
    return _run(args, timeout=QUICK_TIMEOUT, check=True).stdout.strip()


def exec_in(container, cmd, *, user=None, check=True) -> subprocess.CompletedProcess:
    args = ["exec"]
    if user:
        args += ["--user", user]
    args += [container, *cmd]
    return _run(args, timeout=RUN_TIMEOUT, check=check)


def logs(container: str) -> str:
    proc = _run(["logs", container], timeout=QUICK_TIMEOUT, check=False)
    return proc.stdout + proc.stderr


def inspect(container: str) -> dict:
    return json.loads(_run(["inspect", container], timeout=QUICK_TIMEOUT,
                           check=True).stdout)[0]


def diff(container: str) -> list[str]:
    out = _run(["diff", container], timeout=QUICK_TIMEOUT, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def remove(container: str) -> None:
    _run(["rm", "--force", "--volumes", container], timeout=QUICK_TIMEOUT, check=False)


def remove_image(tag: str) -> None:
    """Drop a THROWAWAY tag. Not in the brief's published interface; added for
    the context probe, which builds a second tag from a planted checkout and
    must not leave it behind on a maintainer's machine.

    NEVER CALL THIS ON waku-tenant:test OR waku-services:test. The session
    fixtures in conftest.py build those once and every later test in groups C,
    E and F reuses them; a fixture that "cleaned up" after itself would make
    the next file rebuild from scratch, turning a cache hit into a cold
    `uv sync` per test file. Throwaway tags built inside a single test are the
    only callers this is for.
    """
    _run(["image", "rm", "--force", tag], timeout=QUICK_TIMEOUT, check=False)


def network_create(name: str, *args: str) -> None:
    _run(["network", "create", *args, name], timeout=QUICK_TIMEOUT, check=True)


def network_remove(name: str) -> None:
    _run(["network", "rm", name], timeout=QUICK_TIMEOUT, check=False)


def chown_to_tenant(path: Path, tag: str) -> None:
    """Hand a host directory to UID 10001 without needing root on the host.

    pytest may be running as anybody. `docker run --user 0:0` is root inside
    the container, and the containers share the host's user namespace, so the
    chown lands on the host inode as 10001:10001 -- the same number the tenant
    container runs as.
    """
    run_once(tag, ["chown", "-R", "10001:10001", "/mnt"],
             user="0:0", binds=[f"{path}:/mnt"], read_only=False)


def wait_for_listener(container: str, host: str, port: int, *, timeout: float = 30.0) -> None:
    """Fail the test if the probe target never came up.

    Called on the container that is SUPPOSED to answer, from a vantage point
    the rule under test does not cover, before any assertion that something
    else cannot reach it. Without this, "A cannot reach B" passes on a bridge
    that was never created, a listener that crashed, and a typo in the port.
    """
    deadline = time.monotonic() + timeout
    last = ""
    # probe_tcp RAISES when the probe did not run, and that is right everywhere
    # else in this file. Here it is wrong on its own: this function is called
    # the instant after `docker run --detach` returns, which is exactly when
    # `docker exec` answers "Container <id> is not running" -- so one transient
    # failure on iteration 1 would kill the loop and the whole timeout budget
    # would go unspent. It is caught and KEPT, not swallowed: if the deadline
    # passes with the probe never once having run, this re-raises the probe's
    # own DockerError, because "docker exec was broken for 30s" and "the
    # listener never bound" are different bugs and the reader needs the right
    # one. evals/deterministic/hosted/test_dockerlib_retry.py drives all three.
    probe_failure: DockerError | None = None
    while time.monotonic() < deadline:
        try:
            if probe_tcp(container, host, port, timeout=2.0):
                return
            probe_failure = None
        except DockerError as exc:
            probe_failure = exc
        last = logs(container)[-2000:]
        time.sleep(0.5)
    if probe_failure is not None:
        raise probe_failure
    raise AssertionError(
        f"the probe target never came up: nothing answered on {host}:{port} from "
        f"{container} within {timeout:.0f}s. This is not a pass for whatever "
        f"unreachability test follows.\n--- its logs ---\n{last}")


def probe_tcp(container: str, host: str, port: int, *, timeout: float = 3.0) -> bool:
    """One TCP connect, from inside `container`, using only the stdlib.

    Both images carry a Python; neither carries curl, nc or telnet, and adding
    one to the tenant image to make a test easier would put it in front of
    every tenant too.

    IT RAISES WHEN THE PROBER IS NOT ALIVE, and that is the whole point of the
    YES/NO protocol. An earlier draft returned `proc.returncode == 0`, so a
    `docker exec` that failed for ANY reason -- the container had exited, the
    daemon hiccuped, python was not on that user's PATH -- came back False, and
    every `assert not probe_tcp(...)` in the suite read that as "correctly
    blocked". The plan's own rule is that an unreachability assertion is
    preceded by a reachability assertion from a vantage point the rule does not
    cover; that rule was applied to the TARGET everywhere and to the PROBER
    nowhere. Here it is applied to the prober: the probe prints YES or NO, and
    anything else means the probe did not run.
    """
    program = (
        "import socket, sys\n"
        f"s = socket.socket(); s.settimeout({timeout!r})\n"
        "try:\n"
        f"    s.connect(({host!r}, {port!r}))\n"
        "except Exception as exc:\n"
        "    print('NO', type(exc).__name__); sys.exit(0)\n"
        "print('YES'); sys.exit(0)\n"
    )
    proc = exec_in(container, ["python", "-c", program], check=False)
    out = proc.stdout.strip()
    if out.startswith("YES"):
        return True
    if out.startswith("NO"):
        return False
    raise DockerError(
        f"the probe did not run in {container}: exit {proc.returncode}, "
        f"stdout {proc.stdout[-500:]!r}, stderr {proc.stderr[-500:]!r}.\n"
        "This is NOT 'the connection was blocked'. A prober that is not alive "
        "makes every unreachability assertion in this suite pass for free.")


def assert_alive(container: str) -> None:
    """The prober's own liveness, asserted once per fixture.

    A container that can open a socket at all can open the sockets the
    unreachability tests then say it cannot. Without this, "A cannot reach B"
    is also what a dead A looks like.
    """
    proc = exec_in(container, ["python", "-c", "print('ALIVE')"], check=False)
    assert proc.returncode == 0 and "ALIVE" in proc.stdout, (
        f"{container} is not running python, so it cannot probe anything and "
        f"nothing that uses it proves anything: {proc.stderr[-500:]}")


# --- XFS project quotas -------------------------------------------------
#
# There is exactly one XFS filesystem with project quotas anywhere this suite
# runs, and the hosted-docker job makes it: a loop-mounted 2 GB image, because
# GitHub's runners have no XFS. It exports the mountpoint and the block device,
# and everything that needs a project quota reads them from here.
#
# The maintainers' machines are macOS on APFS. There is no xfs_quota there and
# no way to get one -- the quota is a property of the kernel that owns the
# filesystem. So these skip, and the skip NAMES the platform rather than
# reading as an unexplained absence.

# The one mount option that means project quotas are ENFORCED. An allowlist of
# one, not a blocklist: XFS spells the accounting-only mount `pqnoenforce`,
# which accepts every xfs_quota command, reports the limit back, and enforces
# nothing. A check that merely excluded `pqnoenforce` would admit the next
# spelling of the same hole.
PRJQUOTA_OPTION = "prjquota"

# Named rather than inlined so the offline eval can point it at a fixture.
# There is no other way to exercise this on a maintainer's machine: macOS has
# no /proc/mounts at all.
PROC_MOUNTS = Path("/proc/mounts")


def _prjquota_enforced(proc_mounts: str, mount: str) -> bool:
    """Does /proc/mounts say `mount` is XFS with project quotas enforcing?

    The contract with .github/workflows/hosted-docker.yml: that job mounts with
    `-o prjquota` and asserts `Enforcement: ON` before exporting the two
    variables, and this reads back the half of that verdict a non-root process
    can see. `xfs_quota -x -c 'state -p'` is the other half and needs root, so
    it stays in the job.
    """
    for line in proc_mounts.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        # /proc/mounts octal-escapes spaces and tabs in the mountpoint.
        point = fields[1].replace("\\040", " ").replace("\\011", "\t")
        if point != mount:
            continue
        if fields[2] != "xfs":
            return False
        return PRJQUOTA_OPTION in fields[3].split(",")
    return False


def xfs_root() -> tuple[Path, str] | None:
    """(mountpoint, device) of an XFS filesystem with project quotas, or None.

    Four ways to have none, and the caller's skip message says which: not
    Linux, the variables unset, a mountpoint or device that is not there, or a
    mount that is not actually enforcing project quotas. The last is CHECKED
    rather than assumed -- a filesystem mounted WITHOUT prjquota accepts every
    xfs_quota command and enforces nothing, so a test against it passes while
    proving the opposite.
    """
    if platform.system() != "Linux":
        return None
    mount = os.environ.get("WAKU_XFS_MOUNT")
    device = os.environ.get("WAKU_XFS_DEVICE")
    if not mount or not device:
        return None
    if not Path(mount).is_dir() or not Path(device).exists():
        return None
    try:
        proc_mounts = PROC_MOUNTS.read_text(encoding="utf-8")
    except OSError:
        return None
    if not _prjquota_enforced(proc_mounts, mount):
        return None
    return Path(mount), device


def require_xfs() -> tuple[Path, str]:
    """(mountpoint, device), or skip naming which of the four is missing.

    A skip is never a pass. The hosted-docker job is the one place this returns
    rather than skips, which is why that job also asserts the suite collected
    something and prints every skip reason: a green run that skipped every
    quota test is the shape this plan exists to prevent.
    """
    root = xfs_root()
    if root is not None:
        return root
    if platform.system() != "Linux":
        pytest.skip(
            f"XFS project quotas are Linux-only; this is {platform.system()}. "
            "The tenant disk limit is verified in the hosted-docker CI job on a "
            "loop-mounted XFS image, and on the real VM in G1.")
    mount = os.environ.get("WAKU_XFS_MOUNT")
    device = os.environ.get("WAKU_XFS_DEVICE")
    if not mount or not device or not Path(mount).is_dir() or not Path(device).exists():
        pytest.skip(
            "no XFS filesystem with project quotas: WAKU_XFS_MOUNT and "
            "WAKU_XFS_DEVICE are not both set to a real mountpoint and device. "
            "The hosted-docker job creates one; see .github/workflows/hosted-docker.yml.")
    pytest.skip(
        f"{mount} is not an XFS mount with project quotas enforcing: /proc/mounts "
        f"does not list it as xfs with the `{PRJQUOTA_OPTION}` option. A mount made "
        "with `-o pqnoenforce` accounts and enforces nothing, so every quota test "
        "would pass while proving the opposite. The hosted-docker job mounts with "
        "`-o prjquota` and asserts `Enforcement: ON`; see "
        ".github/workflows/hosted-docker.yml.")
