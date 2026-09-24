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
