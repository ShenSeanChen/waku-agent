# syntax=docker/dockerfile:1.7
# The tenant image: one stock waku, and nothing that knows about the platform.
#
# Built from the SAME COMMIT's waku/ source as the services that start it, so a
# tenant is never running a different waku from the one the contract tests ran
# against (spec, "Images").
#
# It knows nothing about hosted/: its build context admits waku/, skills/ and
# the packaging files and NOTHING else (tenant.Dockerfile.dockerignore). That
# is not a style choice. It is the half of the waku/ <-> hosted/ import
# boundary that evals/deterministic/test_hosted_boundary.py cannot see, because
# that test reads *.py only and says so in its own docstring.
#
# uv 0.6.13 is pinned, not floated: uv.lock is revision 3, and the version that
# installs it has to be one verified to read it. `uv lock --check` against this
# lock with 0.6.13 resolves 304 packages and exits 0.
FROM ghcr.io/astral-sh/uv:0.6.13-python3.12-bookworm-slim

# tzdata, because the container is started with TZ=<the tenant's own zone> and
# waku resolves "remind me tomorrow at 9" in it (acceptance 4). Without the
# zone database every zone silently reads as UTC and the assistant is wrong by
# hours with no error anywhere.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# 10001 is THE uid. Every tenant container and every throwaway container that
# touches a tenant's files runs as it, and the containers share the host's user
# namespace, so this is the same 10001 that owns tenants/<id>/ on the host.
RUN groupadd --gid 10001 waku \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin waku

WORKDIR /app
# README.md and the two license files are not documentation here: pyproject.toml
# declares readme = "README.md" and license-files = ["LICENSE", "LICENSE-BRAND",
# "waku/ops/static/fonts/OFL-*.txt"], so hatchling cannot build the project
# without them and `uv sync` below would fail. The font licenses live under
# waku/ and arrive with it.
COPY pyproject.toml uv.lock README.md LICENSE LICENSE-BRAND ./
COPY waku ./waku
COPY skills ./skills

# --frozen installs exactly uv.lock and resolves nothing, so an image build can
# never become a dependency change nobody reviewed. --extra notion is the one
# allowed Connection that needs an extra; Tavily needs none (spec, "Images").
# UV_LINK_MODE=copy: the cache and the venv are on different layers, and uv's
# default hardlink mode warns on every build.
ENV UV_LINK_MODE=copy
RUN uv sync --frozen --extra notion

# PYTHONDONTWRITEBYTECODE, because the root filesystem is read-only at runtime:
# without it every import tries to write a .pyc under /app and the failures are
# silent, which is the worst of both.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# /work is the working directory so waku's find_dotenv(usecwd=True) finds the
# tenant's own .env and no other (waku/config.py:35). /data and /work are both
# bind mounts at runtime; the empty directories here only give the mounts
# somewhere to land.
RUN mkdir -p /data /work && chown 10001:10001 /data /work
WORKDIR /work
USER 10001:10001
EXPOSE 7777

# No WAKU_* variable is set here on purpose. Every one of them --
# WAKU_HOME, WAKU_DASHBOARD_HOST, WAKU_DASHBOARD_PORT, TZ, the four
# WAKU_PLATFORM_* and HOME -- comes from the spawner's fixed template (spec,
# "The spawner"). Setting any of them in two places is how the two drift.
CMD ["python", "-m", "waku.ops.dashboard"]
