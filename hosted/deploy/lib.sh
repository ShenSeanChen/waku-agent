#!/usr/bin/env bash
# The shared shell for every operator script. Sourced, never run.
#
# ELEVEN FUNCTIONS AND NO MORE. Ten of them exist because two or more scripts
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

# --- appended by F3: the backup and restore half ------------------------------

# ONE LOCK, SHARED BY backup.sh AND restore.sh (spec, "Work inside a tenant's
# directories"). They both rewrite the same staging slots, and a backup that
# ran during a restore would snapshot a half-restored tenant and call it the
# latest good copy.
#
# THE DIRECTORY IS CHECKED FIRST so a missing staging/ is a refusal that names
# it rather than bash's own redirection error, which names a file descriptor.
waku_flock_staging() {
  local lock
  [ -d "$WAKU_ROOT/staging" ] || waku_die "$WAKU_ROOT/staging is not a directory. tree.sh creates it; run install.sh first."
  # `>` TRUNCATES THROUGH A SYMLINK and there is no guard on this name for the
  # same reason backup_control has none: the name is a constant, staging is
  # 0700 root:root (tree.sh), and only root could plant something here.
  lock="$WAKU_ROOT/staging/.lock"
  exec 9>"$lock"
  flock -w "${1:-3600}" 9 \
    || waku_die "another backup or restore holds $lock. They share one lock so they never run at once."
}

# THE REPAIR THE SPAWNER HAS NO VERB FOR (designs/backup-restore-integrity.md).
# The backup runs as UID 10001 over a tree the tenant's own files made, so a
# directory the tenant left at mode 0500 with a file under it wedges
# `find -delete`, and every backup from then on fails at the same line.
#
# IN A CONTAINER, AS UID 10001, and that is the spec's rule, not caution: "no
# host process with more privilege than UID 10001 opens a path inside a tenant's
# directories", and a staging slot holds a copy of exactly those. The chmod
# works because 10001 owns what it is repairing. GNU chmod -R ignores symlinks
# it meets while descending, and `find -delete` implies -depth and unlinks
# relative to a directory fd it opened itself, so a planted link is removed as a
# link and never followed.
waku_reset_staging_slot() {
  local slot
  slot=$1
  [ -L "$slot" ] && waku_die "$slot is a symlink; refusing to empty it"
  [ -d "$slot" ] || return 0
  docker run --rm \
    --network none \
    --user 10001:10001 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --volume "$slot:/staging" \
    "$WAKU_SERVICES_IMAGE" \
    bash -euc 'chmod -R u+rwX /staging; find /staging -mindepth 1 -delete'
}

# config/backup.env is restic's own: the repository, the file holding its
# password, and the object store's credentials. $1 is the file, and every
# argument after it is one name the CALLER reads -- the same caller-supplied
# list waku_load_install_env takes, and for the same reason. A fixed superset
# here would make one script refuse to start over a name only the other one
# touches, and the list-as-argument puts the contract on the line a diff shows.
#
# `set -a` because restic reads these from the ENVIRONMENT, not from a file it
# is told about. It is turned off again immediately: everything after this
# point in a caller is ordinary shell state.
#
# `:?` REFUSES EMPTY AS WELL AS UNSET. That is the whole point here. A backup
# that runs with no repository fails loudly; one that runs with an EMPTY
# password does not fail at all -- restic initialises and writes a repository
# with it, reports success every night, and the operator learns at restore
# time, which is the worst moment to learn anything.
#
# THE PASSWORD IS ALWAYS A FILE, and that is a closed set with default-deny:
# install.sh's flag is --restic-password-file, backup.env.example names
# RESTIC_PASSWORD_FILE, and nothing in this deployment writes an inline
# RESTIC_PASSWORD. An operator who adds one by hand is refused here rather than
# handed a second, untested way to hold the credential that can read every
# tenant's data out of the object store.
#
# A REFUSAL NAMES THE FILE, NEVER A LINE AND NEVER A VALUE. This file holds
# that credential, and an error message about a secret is a place the secret
# escapes -- into whatever scrollback, timer journal or CI log was capturing
# stderr at 03:17.
waku_load_backup_env() {
  local file name
  file=$1
  shift
  [ -r "$file" ] || waku_die "$file is not readable. install.sh writes it from --restic-repository and --restic-password-file, and only root may read it."
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  for name in "$@"; do
    eval ": \"\${$name:?$file does not set $name. install.sh writes this file once and never rewrites it, so a name added to the installer later is not in a file written before it -- add the line by hand.}\""
  done
  [ -r "${RESTIC_PASSWORD_FILE:-}" ] \
    || waku_die "the file $file names as RESTIC_PASSWORD_FILE cannot be opened for reading. restic cannot open a repository without it."
  [ -s "${RESTIC_PASSWORD_FILE:-}" ] \
    || waku_die "the file $file names as RESTIC_PASSWORD_FILE is empty. restic would initialise and write a repository with an empty password and report success every night; the operator finds out at restore time."
}

# --- appended by F4: the operator's closed sets --------------------------------

# THE ONE COPY OF EACH. backup.sh and restore.sh each carried their own
# is_tenant_id, byte for byte the same function, and F4 needed a third for
# tenant.sh and a fourth for migrate.sh's snapshot. Four copies of a closed set
# is four places for it to drift, and this directory's whole history is copies
# drifting: the SQL that resolved an email lived in restore.sh AND in the
# gateway and had already lost an ORDER BY. So the sets live here and the
# scripts say which one they accept.
#
# A SCRIPT STILL DECIDES ITS OWN SET. backup.sh takes an id and NOT an email;
# restore.sh and tenant.sh take either. That is a difference in what the script
# accepts, not in what an id is, so it stays at the call site as
# `waku_is_tenant_id "$x"` versus `waku_is_tenant_id "$x" || waku_is_tenant_email "$x"`.
#
# LC_ALL=C on every one: a bracket expression follows LC_CTYPE, and a root
# login shell on Ubuntu commonly has a UTF-8 one, under which the ranges
# quietly widen. Each runs in a subshell so the setting cannot leak.

# core/tenant.TENANT_ID_RE, `^[a-z2-7]{12}$`. This is the shape that names a
# staging slot, an archive directory, a restic tag and a bind mount, so `..`
# or a tag with a comma in it is not a bad id, it is a different target. The
# empty string matches no bracket expression at all, which is why the length is
# measured rather than inferred.
waku_is_tenant_id() {
  ( LC_ALL=C
    case "$1" in *[!a-z2-7]*) exit 1 ;; esac
    [ "${#1}" -eq 12 ] )
}

# AN EMAIL IS NEVER A PATH. It is resolved to a tenant id by the gateway and
# the ID is what reaches anything. This set exists for the other reasons: the
# value is one argv word handed to `python -m hosted.gateway.admin`, and
# tenant.sh prints it back inside a command line an operator copies and pastes.
# Default-deny over the whole string first, then the structure.
waku_is_tenant_email() {
  ( LC_ALL=C
    case "$1" in *[!A-Za-z0-9._%+@-]*) exit 1 ;; esac
    local_part=${1%%@*}
    domain=${1#*@}
    # Exactly one @: the local part must not be empty, and what follows the
    # first @ must not hold another.
    [ -n "$local_part" ] || exit 1
    [ "$local_part" != "$1" ] || exit 1
    case "$domain" in
      *@*) exit 1 ;;
      *.*) ;;
      *) exit 1 ;;
    esac
    [ "${#1}" -le 254 ] )
}

# A restic snapshot id is hex: eight characters short, sixty-four long. The
# only other accepted word is `latest`, which is restic's own.
#
# A CLOSED SET FOR THE SAME REASON THE TENANT ID IS ONE: the value is the
# positional argument of `restic restore`, so a value beginning with `-` is
# read as a flag -- and restic's restore flags include `--target`, which is
# where the snapshot's contents land.
waku_is_snapshot_id() {
  ( LC_ALL=C
    [ "$1" = latest ] && exit 0
    case "$1" in *[!0-9a-f]*) exit 1 ;; esac
    [ "${#1}" -ge 8 ] && [ "${#1}" -le 64 ] )
}

# --- appended by the group F final review: the backup unit, rendered ----------

# The backup unit with both of its placeholders filled in, on stdout.
#
# ONE CALLER, AND HERE ANYWAY, for waku_write_config's reason quoted at the top
# of this file: it lived inside install.sh's systemd block, below
# waku_require_root and an /etc/os-release read, where no test in any tier could
# reach it -- and what it decides is which paths a unit that runs at 03:17 will
# use. The unit file's own text was checked by a test (the placeholders are
# there); that the installer substitutes both of them was checked by nothing.
#
# TWO PLACEHOLDERS, AND THE SECOND ONE IS THE ONE THAT WAS MISSING.
# @WAKU_BACKUP@ is the script; @WAKU_INSTALL_ENV@ is the config file every
# script reads through waku_load_install_env, whose default is
# /srv/waku/config/install.env. A VM installed with --root elsewhere therefore
# had a nightly backup that died at 03:17 saying "run install.sh first", in a
# unit, in a journal nobody reads -- the same hazard the script placeholder
# closes, through the other door, in the same block.
#
# `sed` USES `|` AS ITS DELIMITER, so the caller checks both paths for `|`, `&`
# and `\` before calling. A refusal there names the path; there is nothing this
# function could say that the caller cannot say better.
waku_render_backup_unit() {
  sed -e "s|@WAKU_BACKUP@|$2|g" -e "s|@WAKU_INSTALL_ENV@|$3|g" "$1"
}
