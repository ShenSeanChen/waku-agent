#!/usr/bin/env bash
# The shared shell for every operator script. Sourced, never run.
#
# SEVEN FUNCTIONS AND NO MORE. Six of them exist because two scripts need them;
# a helper with one caller belongs in that caller, where a reader can see what
# it does without opening a second file.
#
# waku_write_config is the seventh and it has one caller. It is here anyway,
# and for a reason worth writing down: it lived inside install.sh, where no
# test in any tier could reach it, and it is the SOLE implementation of the
# spec's "a rerun never overwrites existing config" as well as the function
# that writes both of this deployment's secrets at mode 0600. A function that
# decides whether a secret is overwritten and that nothing can call is a
# function nothing can check.
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

# Write one config file from stdin, once. $1 is the full path.
#
# NEVER OVERWRITES (spec: "a rerun skips finished steps and never overwrites
# existing config"). When the file is there it says so, DRAINS STDIN and
# returns 0 -- draining matters because the caller's body is a heredoc, and a
# function that returned without reading it would leave the writer blocked or,
# worse on a short body, silently discard it half-read.
#
# ATOMIC, and that is not decoration. The earlier shape created the target and
# then `cat`ted into it, so a run killed between the two left a TRUNCATED env
# file -- and the next run's "never overwrite" then kept it, logging "keeping
# the existing ..." over a half-written gateway.env. That is the one state this
# design cannot repair by rerunning. The body is written to a temporary file
# beside the target, in the same directory so the rename cannot cross a
# filesystem, and appears at its name complete or not at all.
#
# WHAT THE RENAME DOES NOT COVER, said out loud: if the PRODUCER on the other
# end of the pipe emitted half a body and then stopped, the half would be
# renamed into place and a rerun would keep it. It cannot happen with the four
# producers in envfiles.sh, because `set -u` makes an unset variable fatal when
# the heredoc is expanded -- before `cat` writes anything -- and a signal kills
# the whole pipeline, which is the case the rename does cover. A caller that
# ever pipes something fallible in here has to check it itself.
#
# MODE 0600 FROM BIRTH, set by the umask in the subshell that creates the file
# rather than by a chmod afterwards: config/proxy.env holds the platform's
# model key, and a file that is briefly 0644 is a file that was briefly
# readable. Ownership is root's because install.sh calls waku_require_root
# before it reaches here and config/ is 0700 root:root from tree.sh; it is not
# forced with `install -o 0 -g 0`, which would make this function unrunnable --
# and therefore untestable -- as anyone but root.
waku_write_config() {
  local target tmp
  target=$1
  if [ -e "$target" ]; then
    waku_log "keeping the existing $target"
    cat >/dev/null
    return 0
  fi
  [ -d "$(dirname "$target")" ] || waku_die "cannot write $target: $(dirname "$target") is not a directory"
  tmp=$target.tmp.$$
  # REMOVED, then CREATED under the umask. Truncating a file that is already
  # there would keep whatever mode it already had, and a stale $$ from before a
  # reboot is the way that happens. There is no chmod after this: the umask
  # above already makes the file 0600 at birth, and a chmod that can never
  # change anything is a line the next reader would trust.
  rm -f "$tmp"
  ( umask 077; : >"$tmp" ) || waku_die "cannot create $tmp"
  cat >"$tmp"
  mv -f "$tmp" "$target"
  waku_log "wrote $target"
}
