#!/usr/bin/env bash
# Build the tenant and services images from the repo root.
#
# One command, so the tests, install.sh (F1) and upgrade.sh (F2) build the same
# way. Written to parse under bash 3.2 (the macOS default) so `bash -n` is a
# real check on a maintainer's laptop; it RUNS on Ubuntu 24.04's bash 5.2.
#
# DOCKER_BUILDKIT=1 is set rather than assumed. The per-Dockerfile ignore files
# (tenant.Dockerfile.dockerignore, services.Dockerfile.dockerignore) are a
# BuildKit feature; the legacy builder reads only .dockerignore at the context
# root, and this repo has none, so a legacy build would send the whole checkout
# including .env to the daemon.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

tenant_tag=waku-tenant:test
services_tag=waku-services:test
build_tenant=yes
build_services=yes

while [ $# -gt 0 ]; do
  case "$1" in
    --tenant-tag)   tenant_tag=$2; shift 2 ;;
    --services-tag) services_tag=$2; shift 2 ;;
    --tenant-only)   build_services=no; shift ;;
    --services-only) build_tenant=no; shift ;;
    -h|--help)
      echo "usage: build.sh [--tenant-tag TAG] [--services-tag TAG] [--tenant-only|--services-only]"
      exit 0 ;;
    *) echo "build.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# --tenant-only and --services-only cancel out. A script that is handed both
# and exits 0 having built nothing is the worst possible answer: the caller --
# install.sh, upgrade.sh or a CI job -- goes on to `docker run` a tag that was
# never built, and the error surfaces somewhere else entirely.
if [ "$build_tenant" = no ] && [ "$build_services" = no ]; then
  echo "build.sh: --tenant-only and --services-only cancel out, nothing would be built" >&2
  exit 2
fi

export DOCKER_BUILDKIT=1

if [ "$build_tenant" = yes ]; then
  echo "building $tenant_tag from $root"
  docker build \
    --file "$here/tenant.Dockerfile" \
    --tag "$tenant_tag" \
    "$root"
fi

if [ "$build_services" = yes ]; then
  echo "building $services_tag from $root"
  docker build \
    --file "$here/services.Dockerfile" \
    --tag "$services_tag" \
    "$root"
fi
