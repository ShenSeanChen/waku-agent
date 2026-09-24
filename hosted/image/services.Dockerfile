# syntax=docker/dockerfile:1.7
# The services image: the gateway, the metering proxy and the spawner.
#
# Its build context admits hosted/ and the packaging files and NOTHING else --
# in particular, not waku/. That is the enforcement, not a convention: a
# `RUN python -c "import waku"` added to this file fails the build, because
# waku/ is not in the context to import.
FROM ghcr.io/astral-sh/uv:0.6.13-python3.12-bookworm-slim

# xfsprogs gives the spawner xfs_quota, which it runs as root with CAP_SYS_ADMIN
# and the data disk's block device to set a tenant's project limit from inside
# this container. sqlite3 is SQLite's online backup, run as UID 10001 in a
# throwaway container so no privileged process opens a path a tenant can plant.
# zstd is the archive format for a deleted or pre-restore tenant tree.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends xfsprogs sqlite3 zstd \
 && rm -rf /var/lib/apt/lists/*

# The same 10001 as the tenant image: every throwaway container that touches a
# tenant's files runs as it, and most of those run this image.
RUN groupadd --gid 10001 waku \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin waku

WORKDIR /app
COPY pyproject.toml uv.lock LICENSE LICENSE-BRAND ./
# --no-install-project: building waku-agent itself would need waku/__init__.py
# (the version, via [tool.hatch.version]) and README.md (the readme), and this
# image's allowlist admits neither. The base dependencies -- anthropic, openai,
# python-dotenv, rich -- come along with the lock; nothing in hosted/ imports
# them, and evals/deterministic/test_hosted_boundary.py is what keeps that true.
ENV UV_LINK_MODE=copy
RUN uv sync --frozen --no-install-project --extra hosted

COPY hosted ./hosted

# PYTHONPATH=/app rather than an installed package: hosted/ is not a
# distribution and never becomes one (pyproject.toml excludes "/hosted" from
# the sdist and the wheel ships only waku/).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# No USER: this image runs as three different users. The spawner runs it as
# root with CAP_SYS_ADMIN, the gateway as 10002, the proxy as 10003, and every
# throwaway file task as 10001. Compose and the spawner each say which.
CMD ["python", "-m", "hosted.spawner"]
