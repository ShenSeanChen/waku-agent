#!/usr/bin/env bash
# The shared shell for every operator script. Sourced, never run.
#
# EIGHT FUNCTIONS AND NO MORE. Seven of them exist because two or more scripts
# need them; a helper with one caller belongs in that caller, where a reader
# can see what it does without opening a second file.
#
# waku_write_config is the eighth and it has one caller. It is here anyway,
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

# A flag that takes a value, given without one, used to die on bash's own
# `$2: unbound variable` -- a refusal that ran nothing, but the one message in
# whichever script hit it that did not read like the others. Moved here from
# install.sh because upgrade.sh needed the same check for --ref, and F3 and F4
# add four more argument parsers between them.
#
# $1 is the flag under consideration, $2 is the caller's $# AT THE POINT OF
# THE CALL (so "at least 2 left" means the flag and its value are both still
# on the line), and every argument after that is one flag name that takes a
# value -- the caller's own closed set. NEVER DIES ITSELF: it returns 1 so the
# caller keeps its own message and its own usage text, and returns 0 both when
# the flag is not one that takes a value and when enough remains for it.
waku_needs_value() {
  local flag remaining name
  flag=$1
  remaining=$2
  shift 2
  for name in "$@"; do
    if [ "$flag" = "$name" ]; then
      [ "$remaining" -ge 2 ]
      return
    fi
  done
  return 0
}

# WAKU_INSTALL_ENV is an override for the tests, which have no /srv/waku. The
# default is the one install.sh writes.
#
# THE FOUR NAMES BELOW ARE EVERY CALLER'S; ANYTHING ELSE IS THE CALLER'S TO
# NAME. install.sh only ever needed WAKU_ROOT, WAKU_SRC, WAKU_COMPOSE and
# WAKU_DOMAIN, so those four were the whole list -- and a second consumer that
# reads a fifth name from install.env got no check at all: it died on bash's
# own `set -u` message, sometimes well past the point of no return, or --
# through `waku_compose`'s --env-file, where a missing name is a WARNING and a
# BLANK, not an error -- it did not die at all. Found by upgrade.sh (spec 001
# task F2) missing WAKU_GATEWAY_ADDRESS.
#
# THE FIX IS A CALLER-SUPPLIED LIST, NOT A FIXED SUPERSET. A fixed list of
# every name every script might ever read would make backup.sh refuse to start
# over WAKU_CADDY_IMAGE, which it never touches, and it would need editing
# every time any script grows a dereference. The list-as-argument puts the
# contract at the call site, on the same line a `git diff` shows, so a script
# that forgets to declare a name it reads is the thing future review has to
# catch -- not a thing this function can catch for it.
#
# `:?` REFUSES EMPTY AS WELL AS UNSET, which matters here: `--data-device ""`
# or a truncated install.env produce an empty value, not an absent one, and a
# `[ -z ]` alternative would let that through.
waku_load_install_env() {
  local name
  WAKU_INSTALL_ENV=${WAKU_INSTALL_ENV:-/srv/waku/config/install.env}
  [ -r "$WAKU_INSTALL_ENV" ] || waku_die "$WAKU_INSTALL_ENV is not readable. Run install.sh first, and run this as root."
  # shellcheck disable=SC1090
  . "$WAKU_INSTALL_ENV"
  : "${WAKU_ROOT:?install.env is missing WAKU_ROOT}"
  : "${WAKU_SRC:?install.env is missing WAKU_SRC}"
  : "${WAKU_COMPOSE:?install.env is missing WAKU_COMPOSE}"
  : "${WAKU_DOMAIN:?install.env is missing WAKU_DOMAIN}"
  for name in "$@"; do
    eval ": \"\${$name:?install.env is missing $name. It was added to install.sh after this VM was installed, and install.env is never rewritten on a rerun -- add the line by hand and rerun.}\""
  done
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

# The temporary a config write is part way through, so a caller's EXIT trap
# can take it away when a signal lands between the create and the rename.
# Empty at every other moment.
WAKU_WRITE_TMP=""

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
  # WAKU_WRITE_TMP so the caller's EXIT trap can take the temporary away when
  # a signal ends the run between the create and the rename. install.sh sets
  # that trap; a caller that does not is left with one 0600 file in a 0700
  # root-only directory, which is the same exposure as the target it was
  # going to become.
  WAKU_WRITE_TMP=$tmp
  # A FAILED WRITE TAKES ITS TEMPORARY WITH IT. Without this the run stopped
  # with a 0600 file holding PART OF A SECRET sitting beside the target under
  # a name nothing would ever clean up or look at again.
  cat >"$tmp" || { rm -f "$tmp"; WAKU_WRITE_TMP=""; waku_die "could not write $tmp"; }
  mv -f "$tmp" "$target"
  # NOT CLEARED AFTER THE RENAME, and that is deliberate rather than an
  # oversight. A `WAKU_WRITE_TMP=""` stood here and nothing could turn it red:
  # once the rename has happened the name points at a file that no longer
  # exists, so a later trap firing on it removes nothing, and no path can
  # observe the difference. It is the same judgement as the two dead guards in
  # waku_bytes and the dead chmod above -- a line that reads as cleanup and can
  # never matter is a line the next reader trusts. The value is cleared on the
  # one path where it WOULD matter: a failed write, where the temporary is
  # removed by hand a line above.
  waku_log "wrote $target"
}
