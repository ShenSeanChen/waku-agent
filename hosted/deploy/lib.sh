#!/usr/bin/env bash
# The shared shell for every operator script. Sourced, never run.
#
# SIX FUNCTIONS AND NO MORE. Every one of them exists because two scripts need
# it; a helper with one caller belongs in that caller, where a reader can see
# what it does without opening a second file.
#
# Written to parse under bash 3.2, like hosted/image/build.sh. It RUNS on
# Ubuntu 24.04's bash 5.2.

waku_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

waku_die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

waku_require_root() {
  [ "$(id -u)" = 0 ] || waku_die "run this as root: it creates directories owned by three different users, loads an env_file only root can read, and talks to the Docker socket"
}

# WAKU_INSTALL_ENV is an override for the tests, which have no /srv/waku. The
# default is the one install.sh writes.
waku_load_install_env() {
  WAKU_INSTALL_ENV=${WAKU_INSTALL_ENV:-/srv/waku/config/install.env}
  [ -r "$WAKU_INSTALL_ENV" ] || waku_die "$WAKU_INSTALL_ENV is not readable. Run install.sh first, and run this as root."
  # shellcheck disable=SC1090
  . "$WAKU_INSTALL_ENV"
  : "${WAKU_ROOT:?install.env is missing WAKU_ROOT}"
  : "${WAKU_SRC:?install.env is missing WAKU_SRC}"
  : "${WAKU_COMPOSE:?install.env is missing WAKU_COMPOSE}"
  : "${WAKU_DOMAIN:?install.env is missing WAKU_DOMAIN}"
}

# --env-file is what makes ${WAKU_ROOT} and the image tags resolve inside
# compose.yaml, and --project-name pins the stack's name so `docker compose` run
# from any directory reaches the same containers.
waku_compose() {
  docker compose --env-file "$WAKU_INSTALL_ENV" -f "$WAKU_COMPOSE" --project-name waku "$@"
}

# EVERY CHANGE TO RUNTIME STATE GOES THROUGH THE RUNNING GATEWAY (spec, "How the
# services run"). Session caches, container addresses, token issue and requests
# in flight live in the gateway's memory, so no script here reaches the spawner
# directly -- they all come through this one function. -T because there is no
# terminal in a timer unit; --user 10002:10002 because admin.sock is 0600 in a
# 0700 directory owned by that user.
waku_admin() {
  waku_compose exec -T --user 10002:10002 gateway python -m hosted.gateway.admin "$@"
}
