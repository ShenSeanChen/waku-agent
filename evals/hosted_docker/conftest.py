"""Skip this whole directory when there is nothing here to run it on.

TWO REASONS TO SKIP, AND NEITHER IS A FAILURE:

  hosted/ is absent    -- the sdist ships evals/ and excludes "/hosted"
                          (pyproject.toml), exactly as
                          evals/deterministic/hosted/conftest.py handles
  the daemon is absent -- Docker may be installed with nothing running, which
                          is the state of the maintainers' machines most of the
                          time. Checked on macOS 25.6.0 with Docker 28.0.4
                          installed and Docker Desktop stopped: `docker version
                          --format '{{.Server.Version}}'` exits 1 with empty
                          stdout, in under a second, with no hang.

ONE REASON THAT IS NOT A SKIP: a build that fails. dockerlib.build_image raises
DockerError with the build log, because the daemon answered -- so the
environment is fine and the Dockerfile is not.

This directory is the THIRD eval tier. It is offline, like evals/deterministic,
but it needs a Docker daemon, so `make gate` does not run it
(waku/ops/release_gate.py runs the suites "deterministic" and "judge" and only
those) and neither do the two workflows that run evals/deterministic. Its own
job, `hosted-docker`, is in .github/workflows/hosted-docker.yml.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOSTED = Path(__file__).resolve().parents[2] / "hosted"

if not HOSTED.is_dir():
    collect_ignore_glob = ["*.py"]
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dockerlib import build_image, daemon_reason, daemon_version  # noqa: E402

    if daemon_version() is None:
        # collect_ignore_glob silences a directory without printing a reason,
        # and "silently collected nothing" is the exact shape this plan is
        # trying to avoid. So the reason is reported at collection finish.
        _REASON = daemon_reason()
        collect_ignore_glob = ["*.py"]

        def pytest_report_collectionfinish(config):
            return (f"evals/hosted_docker: skipped entirely -- {_REASON}. "
                    "These tests need a Docker daemon; they are the third eval "
                    "tier and run in the hosted-docker CI job.")

    @pytest.fixture(scope="session")
    def tenant_image() -> str:
        """Built once per session. A failed build raises; it never skips."""
        return build_image("hosted/image/tenant.Dockerfile", "waku-tenant:test")

    @pytest.fixture(scope="session")
    def services_image() -> str:
        return build_image("hosted/image/services.Dockerfile", "waku-services:test")

    # --- C2: the spawner, running as the real service -------------------
    #
    # These are in conftest.py and not in test_spawner.py because three C2 test
    # modules need them and pytest shares a conftest fixture without an import.
    # Importing a fixture by name into a module that also names it as a test
    # argument is `ruff` F811, which is what the brief's shape produced.

    import time as _time  # noqa: E402 - inside the daemon-present branch

    import dockerlib as _dockerlib  # noqa: E402
    import spawnerlib as _spawnerlib  # noqa: E402

    from hosted.core import tenant as _tenant  # noqa: E402

    @pytest.fixture(scope="module")
    def bridges():
        """The two bridges the template names, created directly.

        C3 owns hosted/deploy/networks.sh and the `bridges` fixture that calls
        it; C3 was DEFERRED by a scope decision, so C2 creates the two networks
        here with the addresses hosted/core/tenant.py already holds. When C3
        lands, this fixture is what its networks.sh replaces -- the constants
        do not move, and neither do the tests that read them.

        What this does NOT do, and what C3 still owns: enable_icc=false, the
        firewall chains, and every rule acceptance 1 is about. This is enough
        to give a container its fixed address and no more than that.
        """
        for name, subnet, gateway, extra in (
                (_tenant.TENANT_NETWORK, str(_tenant.TENANT_SUBNET),
                 str(_tenant.TENANT_GATEWAY),
                 ["--ip-range", str(_tenant.DYNAMIC_RANGE)]),
                (_tenant.INSPECT_NETWORK, str(_tenant.INSPECT_SUBNET),
                 str(_tenant.INSPECT_GATEWAY), [])):
            _dockerlib.network_remove(name)
            _dockerlib.network_create(
                name, "--driver", "bridge", "--subnet", subnet,
                "--gateway", gateway,
                "--opt", f"com.docker.network.bridge.name={name}", *extra)
        yield
        for name in (_tenant.TENANT_NETWORK, _tenant.INSPECT_NETWORK):
            _dockerlib.network_remove(name)

    @pytest.fixture(scope="module")
    def spawner(services_image, tenant_image, tmp_path_factory, bridges):
        """The real service, in a container, as root, reachable over its socket.

        THE SPAWNER UNDER TEST RUNS IN A CONTAINER, not in the pytest process.
        That is not a stylistic choice:

          - provision calls os.chown(..., 10001, 10001) and xfs_quota. pytest
            runs as the runner's unprivileged user, so an in-process
            DockerRuntime fails on its first line for a reason that has nothing
            to do with the code.
          - the real spawner is a Compose service running as root with
            CAP_SYS_ADMIN, the Docker socket and the data device. Testing it
            any other way tests a configuration nothing ships.

        <root>/run/spawner is a bind mount, so the Unix socket the service
        binds inside the container appears on the HOST, and the tests talk to
        it with hosted.jsonsock.ask -- the same client the gateway will use.

        `tenant_image` is requested and unused on purpose: the spawner starts
        tenant containers by tag, so the tag has to exist before the first
        start, and a fixture that builds it is how that is guaranteed rather
        than hoped.
        """
        # Pick the root FIRST, then create under it. An earlier draft made the
        # four directories under a tmp path and then reassigned `root` to the
        # XFS mount, so on CI the setup created four directories nothing used.
        mount, device = _dockerlib.xfs_root() or (None, None)
        root = mount if mount is not None else tmp_path_factory.mktemp("waku")
        for name in ("tenants", "archive", "staging", "run/spawner"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env = {
            "WAKU_TENANT_ROOT": f"{root}/tenants",
            "WAKU_ARCHIVE_ROOT": f"{root}/archive",
            "WAKU_STAGING_ROOT": f"{root}/staging",
            "WAKU_TENANT_IMAGE": _spawnerlib.TENANT_TAG,
            "WAKU_SERVICES_IMAGE": _spawnerlib.SERVICES_TAG,
            "WAKU_PLATFORM_BASE_URL": "http://10.88.0.1:8788",
            "WAKU_PLATFORM_MODEL": "waku-test-model",
            "WAKU_PLATFORM_SMALL_MODEL": "waku-test-model",
            "WAKU_TENANT_DISK_BYTES": str(_spawnerlib.TEST_DISK_BYTES),
            # WAKU_DATA_DEVICE=none is what makes this fixture work on a
            # maintainer's Docker Desktop with no XFS anywhere: provisioning
            # logs its warning and sets no quota, and every test that is not
            # about the disk still runs. The two that ARE about the disk call
            # require_xfs() first and skip with the platform named.
            "WAKU_DATA_DEVICE": device or "none",
            "WAKU_SECCOMP_PROFILE": "/app/hosted/image/seccomp.json",
            "WAKU_SPAWNER_SOCKET": f"{root}/run/spawner/spawner.sock",
            "WAKU_LOG_LEVEL": "DEBUG",
        }
        extra = ["--cap-add", "SYS_ADMIN"]
        if device:
            extra += ["--device", device]
        _dockerlib.remove(_spawnerlib.SPAWNER_CONTAINER)
        try:
            _dockerlib.start_detached(
                services_image, ["python", "-m", "hosted.spawner"],
                name=_spawnerlib.SPAWNER_CONTAINER, user="0:0", env=env,
                read_only=False,
                binds=["/var/run/docker.sock:/var/run/docker.sock",
                       f"{root}:{root}"],
                extra=extra)
            socket_path = root / "run" / "spawner" / "spawner.sock"
            for _ in range(60):
                if socket_path.exists():
                    break
                _time.sleep(0.5)
            else:
                raise AssertionError(
                    "the spawner never bound its socket. It is not running, so "
                    "nothing below this line is a test of anything.\n"
                    + _dockerlib.logs(_spawnerlib.SPAWNER_CONTAINER))
            yield socket_path
        finally:
            print(_dockerlib.logs(_spawnerlib.SPAWNER_CONTAINER))
            _dockerlib.remove(_spawnerlib.SPAWNER_CONTAINER)

    @pytest.fixture(scope="module")
    def spawner_root(spawner):
        """The root the `spawner` fixture chose: the XFS mount when there is
        one, a temp directory otherwise. `spawner` yields the socket path and
        the root is its grandparent, so the two can never name different
        trees."""
        return spawner.parent.parent.parent
