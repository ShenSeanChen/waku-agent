#!/usr/bin/env bash
# The nightly backup: the two platform databases, then every tenant, one at a
# time, into restic.
#
# ONE TENANT AT A TIME IS THE SPEC'S RULE AND IT IS ABOUT DISK: "stage, send to
# restic, delete, then the next tenant, so a backup never needs more than one
# tenant's worth of free disk" -- on the same filesystem the tenants live on.
#
# A FAILED TENANT DOES NOT STOP THE RUN. It is collected and the script exits
# non-zero at the end, because the alternative is that one wedged staging slot
# means nobody on the VM has a backup tonight.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

mode=all
one=""

usage() {
  cat <<'USAGE'
usage: backup.sh [--all] [--tenant <id>] [--snapshot-staged <id>]
                 [--reset-staging <id>] [--init-repository]
  --all              every tenant and both platform databases (the default,
                     and what the timer runs)
  --init-repository  create the restic repository named in config/backup.env,
                     once, before the first backup. Nothing else creates it,
                     and every other mode refuses until it exists
  --tenant <id>      one tenant. RE-COPIES their CURRENT live tree into the
                     staging slot first, so it overwrites anything already
                     staged
  --snapshot-staged <id>
                     send what is ALREADY in the tenant's staging slot to
                     restic, without re-copying anything from the live tree.
                     For a backup that finished and could not be uploaded
  --reset-staging <id>
                     empty one tenant's staging slot, as UID 10001 in a
                     throwaway container. The repair for a slot whose modes the
                     tenant's own files wedged
USAGE
}

# A CLOSED SET WITH DEFAULT-DENY, and it is the one guard between an operator's
# typo and a directory that is not a staging slot being emptied. Both flags
# that take an id reach waku_reset_staging_slot, which runs
# `find /staging -mindepth 1 -delete` over whatever "$WAKU_ROOT/staging/$one"
# resolves to -- so `--reset-staging ..` would empty the staging root itself,
# and `--tenant ../../srv` would take the retry path there.
#
# THE SET ITSELF IS waku_is_tenant_id IN lib.sh, which is where the one copy
# lives now: this file and restore.sh each carried their own, byte for byte
# the same, and F4 would have made it four. AN ID AND NOTHING ELSE IS THIS
# SCRIPT'S OWN DECISION and stays here -- restore.sh and tenant.sh also accept
# an email, and this one has nowhere to put one.

while [ $# -gt 0 ]; do
  waku_needs_value "$1" "$#" --tenant --snapshot-staged --reset-staging \
    || { usage >&2; waku_die "$1 needs a value"; }
  case "$1" in
    --all) mode=all; shift ;;
    --init-repository) mode=init; shift ;;
    --tenant) mode=one; one=$2; shift 2 ;;
    --snapshot-staged) mode=staged; one=$2; shift 2 ;;
    --reset-staging) mode=reset; one=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; waku_die "unknown argument: $1" ;;
  esac
done

# BEFORE waku_require_root, like install.sh's flag checks and for the same
# reason: a script that demands root before telling you an argument is wrong is
# a worse script, and this is also what makes the refusal reachable from a test
# on a maintainer's laptop.
# --all and --init-repository take no tenant; the other three take exactly one.
if [ "$mode" != all ] && [ "$mode" != init ]; then
  waku_is_tenant_id "$one" \
    || waku_die "a tenant id is twelve characters of a-z and 2-7; got '$one'. It is joined to the staging root to make a path this script empties, so anything else is refused here."
fi

waku_require_root
# WAKU_SERVICES_IMAGE is this script's own name to declare, on top of the four
# every consumer reads: waku_reset_staging_slot runs the repair container from
# it. Not copied from another script's list -- see the F2 finding, where a
# fixed list shaped by the first consumer let the second one die on bash's own
# `set -u` message after it had already restarted the services.
waku_load_install_env WAKU_SERVICES_IMAGE

staging="$WAKU_ROOT/staging"

if [ "$mode" = reset ]; then
  waku_flock_staging 60
  waku_reset_staging_slot "$staging/$one"
  waku_log "emptied $staging/$one"
  exit 0
fi

# restic's repository, password file and object-store credentials, EXPORTED and
# CHECKED BEFORE THE LOCK IS TAKEN. The order is the point: a missing or empty
# restic name discovered after the lock is a run that has already emptied a
# staging slot to make room for a backup it then cannot send anywhere.
waku_load_backup_env "$WAKU_ROOT/config/backup.env" RESTIC_REPOSITORY RESTIC_PASSWORD_FILE

# THE REPOSITORY HAS TO EXIST, AND NOTHING ELSE ON THIS VM CREATES IT.
# install.sh cannot: it writes config/backup.env from --restic-repository and
# --restic-password-file, and the OBJECT STORE'S OWN CREDENTIALS are appended
# to that file by hand afterwards, so at the moment install.sh runs there is
# nothing to authenticate with. Without a check the first thing ever to touch
# the repository is `restic backup` at 03:17 in a timer unit, which fails into
# a journal nobody reads while the operator believes they have backups.
#
# CREATED ONLY BY A HUMAN WHO ASKED, and that is the whole shape of this. The
# obvious alternative -- initialise whenever the repository does not answer --
# builds a worse failure than the one it fixes: RESTIC_REPOSITORY mistyped by
# one character does not answer either, so the nightly run would create a
# second, empty repository at the typo, back up into it every night, report
# success, and leave every real snapshot somewhere restore.sh will not look.
# `restic cat config` cannot tell a repository that is absent from one whose
# address is wrong; an operator can.
#
# --init-repository TAKES NO LOCK. It stages nothing and empties nothing, and
# `restic init` refuses a repository that already has a config, so two of them
# at once is restic's own refusal rather than a race this script has to hold a
# lock against.
if [ "$mode" = init ]; then
  if restic cat config >/dev/null 2>&1; then
    waku_log "the restic repository at $RESTIC_REPOSITORY already exists; nothing to do"
    exit 0
  fi
  restic init \
    || waku_die "could not create the restic repository at $RESTIC_REPOSITORY. restic's own message is above. The three things it is usually about: the repository address in $WAKU_ROOT/config/backup.env, the object store credentials appended to that same file by hand, and network access from this VM."
  waku_log "created the restic repository at $RESTIC_REPOSITORY. It is EMPTY: any snapshot taken before now is in a different repository."
  exit 0
fi

waku_flock_staging

# AFTER THE LOCK AND BEFORE ANYTHING IS STAGED, which is the order F3a's rule
# asks for: "a database that exists and cannot be copied is a backup silently
# missing a file", and the refusal has to land before a slot is emptied. Taking
# the lock empties nothing, so the two names above -- which are read out of a
# FILE and cost nothing -- stay above it and this one, which is a network round
# trip, sits below it. The cost of that choice, said out loud: a repository
# whose address hangs rather than refuses holds the lock while it hangs. That
# is not a new class of failure, because `restic backup` holds the same lock
# across the same network for the whole of every run.
restic cat config >/dev/null 2>&1 \
  || waku_die "the restic repository at $RESTIC_REPOSITORY cannot be opened, so this backup has nowhere to go and nothing has been staged. If this deployment has never backed up, create the repository once with: backup.sh --init-repository . If it has, then the repository address, the password file or the object store credentials in $WAKU_ROOT/config/backup.env no longer reach it -- and the snapshots already there are not lost, they are unreachable from this VM."

failures=""

# --- the two platform databases ---------------------------------------------
#
# EACH ONE INSIDE ITS OWNER'S CONTAINER (spec): control.db as UID 10002 in the
# gateway's, ledger.db as UID 10003 in the proxy's. No process on this VM can
# open both, which is the point of the one-writer rule, and backup.sh as root
# only ever COPIES the finished files out.
backup_control() {
  local slot
  waku_log "control.db, inside the gateway's container"
  waku_compose exec -T --user 10002:10002 gateway \
    sqlite3 "$WAKU_ROOT/control/control.db" \
    ".backup '$WAKU_ROOT/control/backup/control.db'"
  # A SINGLE SQLITE FILE CARRIES ITS OWN COMPLETENESS CHECK, which is why the
  # two databases need no manifest and a tenant's tar of many files does. A
  # `.backup` that was killed leaves a file that fails this.
  waku_compose exec -T --user 10002:10002 gateway \
    sqlite3 "$WAKU_ROOT/control/backup/control.db" 'PRAGMA integrity_check' \
    | grep -qx ok \
    || waku_die "the copy of control.db does not pass PRAGMA integrity_check; refusing to snapshot it"

  if [ -f "$WAKU_ROOT/ledger/ledger.db" ]; then
    waku_log "ledger.db, inside the proxy's container"
    waku_compose exec -T --user 10003:10003 proxy \
      sqlite3 "$WAKU_ROOT/ledger/ledger.db" \
      ".backup '$WAKU_ROOT/ledger/backup/ledger.db'"
    waku_compose exec -T --user 10003:10003 proxy \
      sqlite3 "$WAKU_ROOT/ledger/backup/ledger.db" 'PRAGMA integrity_check' \
      | grep -qx ok \
      || waku_die "the copy of ledger.db does not pass PRAGMA integrity_check; refusing to snapshot it"
  else
    # GROUP D IS DEFERRED, so no proxy has ever created this file. A database
    # that does not exist has nothing to copy; one that EXISTS and cannot be
    # copied is a backup silently missing a file, and the `exec` above fails
    # loudly in that case.
    waku_log "no $WAKU_ROOT/ledger/ledger.db yet (group D of spec 001); nothing to back up"
  fi

  # NO SYMLINK GUARD HERE, UNLIKE A TENANT'S SLOT, and the asymmetry is
  # deliberate. waku_reset_staging_slot refuses a symlinked slot because a
  # tenant's slot name comes from a tenant id and the tree under it was made by
  # the tenant's own files. This name is the constant "control", staging is
  # 0700 root:root (tree.sh), and a tenant id is twelve characters of [a-z2-7]
  # and so can never be that string -- so the only process that could plant a
  # symlink here is root, which already owns everything it would reach.
  slot="$staging/control"
  mkdir -p "$slot"
  chmod 0700 "$slot"
  # ROOT MAY CLEAR THIS ONE. Unlike a tenant's slot, everything in it was
  # written by root from the platform's own databases; no tenant has ever had a
  # path inside it.
  rm -f "$slot"/*.db
  cp "$WAKU_ROOT/control/backup/control.db" "$slot/control.db"
  if [ -f "$WAKU_ROOT/ledger/backup/ledger.db" ]; then
    cp "$WAKU_ROOT/ledger/backup/ledger.db" "$slot/ledger.db"
  fi
  restic backup --tag control --host "$WAKU_DOMAIN" "$slot"
}

# --- one tenant --------------------------------------------------------------
backup_tenant() {
  local id slot
  id=$1
  slot="$staging/$id"
  waku_log "tenant $id"

  # THROUGH THE RUNNING GATEWAY. It holds the maintenance mark and the project
  # id; the spawner refuses a start while a task container for the tenant
  # exists, so a person signing in mid-backup is told to wait rather than given
  # a container over a tree being copied.
  if ! waku_admin backup "$id" >/dev/null; then
    # THE ONE RETRY, AND IT IS THE REASON --reset-staging EXISTS. A slot whose
    # modes the tenant's own files wedged fails at the same line forever, and
    # no spawner verb repairs it.
    waku_log "$id: the backup failed; emptying its staging slot and trying once more"
    waku_reset_staging_slot "$slot" || return 1
    waku_admin backup "$id" >/dev/null || return 1
  fi

  snapshot_slot "$id" || return 1
}

# --- what is already in the slot, sent to restic ------------------------------
#
# SHARED BY backup_tenant AND --snapshot-staged, and it is the whole of the
# second one. Extracted rather than copied: a second `restic backup` line with
# its own tag and host is the drift this project keeps paying for.
snapshot_slot() {
  local id slot
  id=$1
  slot="$staging/$id"

  # THE BACKUP'S OWN DECLARATION THAT IT FINISHED. Nothing about the SHAPE of
  # the slot can tell a finished backup from one that died: `mkdir -p
  # /staging/home /staging/env` is the backup script's first line, so two empty
  # directories are what an interrupted backup AND an empty tenant both leave.
  # The script removes any previous manifest first and writes a new one last, so
  # its presence is the only fact worth reading here.
  if [ -L "$slot/manifest.json" ] || [ ! -f "$slot/manifest.json" ]; then
    waku_log "$id: no manifest.json in $slot, so that backup did not finish; not snapshotting it"
    return 1
  fi

  restic backup --tag "tenant:$id" --host "$WAKU_DOMAIN" "$slot" || return 1

  # ONLY NOW. The slot is the handoff area and restic is the thing that keeps
  # copies; clearing before the snapshot would be the one-slot problem with no
  # compensation at all.
  waku_reset_staging_slot "$slot" || return 1
}

# THE SLOT AS IT STANDS, WITH NOTHING RE-COPIED INTO IT. The mode exists
# because `--tenant` is not it and an operator reaching for a remedy will
# believe it is: `waku_admin backup` runs the spawner's _BACKUP_SCRIPT, whose
# first two lines are `rm -f /staging/manifest.json` and `find /staging/home
# /staging/env -mindepth 1 -delete`, so --tenant EMPTIES the slot and re-copies
# the tenant's CURRENT live tree before snapshotting it.
#
# That is exactly wrong in the one case a staged-but-unsent backup matters: the
# upload failed, the live tree has since gone bad, and the slot holds the last
# good copy on this VM. restore.sh refuses to delete such a slot and names this
# command; before it existed, the remedy it could name would have destroyed the
# copy it had just protected.
if [ "$mode" = staged ]; then
  snapshot_slot "$one" \
    || waku_die "nothing in $staging/$one was sent to restic. A slot with no manifest.json is not a finished backup, and this mode never re-copies from the live tree -- see backup.sh --tenant $one for that, which OVERWRITES the slot."
  waku_log "sent the staged backup of $one to restic"
  exit 0
fi

if [ "$mode" = one ]; then
  backup_tenant "$one" || waku_die "the backup of $one did not finish"
  waku_log "backed up $one"
  exit 0
fi

backup_control

# THE LIST COMES FROM THE COPY, not from the live database: the copy is
# consistent by construction and nothing is writing to it. Deleted tenants are
# left out -- their tree is already an archive.
tenants=$(sqlite3 "$staging/control/control.db" \
  "select id from tenant where status in ('active','disabled') order by id")

# READ RATHER THAN SPLIT, so every expansion below stays quoted. Each row is
# put through the same closed set the flags are: the id is joined to the
# staging root to make a path this script later empties, and a row that is not
# an id is a failure to report, not a path to act on.
while IFS= read -r id; do
  [ -n "$id" ] || continue
  if ! waku_is_tenant_id "$id"; then
    waku_log "control.db names a tenant '$id' that is not a tenant id; not touching it"
    failures="$failures $id"
    continue
  fi
  backup_tenant "$id" || failures="$failures $id"
done <<EOF
$tenants
EOF

# 7 daily and 4 weekly (spec, "Deploy and operate"), applied to each tag
# separately, so one tenant's nightly run cannot age out another's snapshots.
#
# HOST AND TAGS, NOT TAGS ALONE, AND THAT IS NOT DECORATION. --group-by
# REPLACES restic's default host,paths grouping rather than adding to it, and
# this runs with --prune and no --host filter. Two VMs pointed at one
# --restic-repository -- a one-flag mistake nothing here refuses -- would put
# both hosts' `control` snapshots in a single retention group, so VM A's 03:17
# run would keep 7 daily across BOTH and delete VM B's surplus. Every snapshot
# above carries --host "$WAKU_DOMAIN" for exactly this reason; dropping it here
# would make setting it pointless.
waku_log "pruning: 7 daily, 4 weekly, per host and tag"
restic forget --group-by host,tags --keep-daily 7 --keep-weekly 4 --prune

# Archives older than 30 days (spec, "archive on delete"). -maxdepth 1 -type f:
# the archive directory holds only .tar.zst FILES, written by the spawner, so
# this deletes files and never descends into anything.
#
# ARCHIVES ARE IN NO SNAPSHOT, SAID HERE BECAUSE THIS IS THE LINE THAT DELETES
# THEM. restic is given $staging/control and one $staging/<tenant> at a time
# and nothing else, so a deleted tenant's tar has exactly one copy, on this VM,
# for its whole retention window -- and then this removes it. That is a grace
# period, not a backup, and a reader who finds a `find -delete` in a file
# called backup.sh should not have to infer which one it is.
waku_log "removing archives older than 30 days"
find "$WAKU_ROOT/archive" -maxdepth 1 -type f -name '*.tar.zst' -mtime +30 -delete

[ -z "$failures" ] || waku_die "these tenants were not backed up:$failures"
waku_log "backup finished"
