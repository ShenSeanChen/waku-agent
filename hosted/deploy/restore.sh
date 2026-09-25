#!/usr/bin/env bash
# Restore one tenant, or the whole system.
#
# THE ORDER IS THE SPEC'S, and every line of it is load-bearing:
#
#   --all: stop every tenant container through the RUNNING gateway (their
#   tokens may be missing from the restored control.db), then stop the gateway
#   and the proxy, restore both databases as root with their owners and modes,
#   start both services, and only then restore every tenant one by one through
#   the admin command.
#
#   one tenant: restic into the tenant's staging slot, refuse without a
#   manifest, then ask the gateway -- which stops the tenant's container first
#   and holds them in maintenance while the spawner archives the old tree,
#   recreates the two directories empty WITH their project quota, and copies in.
#
# NOTHING HERE WALKS A TENANT'S TREE AS ROOT. restic writes the staging slot;
# the slot is emptied in a container as UID 10001; the spawner does the rest.
#
# NOTHING IS STOPPED UNTIL THE SNAPSHOT HAS BEEN PROVED TO EXIST. The spec's
# order is about the five DESTRUCTIVE stages, and fetching a snapshot into
# staging is not one of them: staging is a handoff area outside every live
# tree. So --all pulls the control snapshot and checks it FIRST, with the
# platform still serving. A repository that is empty, unreachable, or holding
# a control.db that fails PRAGMA integrity_check then costs a refusal and no
# downtime at all, instead of a fleet and two services stopped in front of a
# restore that cannot happen.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

mode=""
one=""
snapshot=latest
stop_fleet=yes

usage() {
  cat <<'USAGE'
usage: restore.sh --tenant <id|email> [--snapshot ID]
       restore.sh --all [--snapshot ID]
  --tenant <id|email>  one tenant, from their own snapshot. The gateway stops
                       their container first; nobody else is interrupted
  --all                the whole system onto this VM: both platform databases
                       and then every tenant, in the spec's order
  --snapshot ID        a restic snapshot id, or `latest` (the default)
  --no-stop-fleet      with --all, do not ask the gateway to stop the tenant
                       containers first. ONLY when the gateway cannot answer
                       its admin socket -- which is the disaster this command
                       exists for. The fleet is still stopped once the gateway
                       is back on the restored database, before any tenant
                       tree is touched
USAGE
}

# A refusal that does not end the run. --all collects a failed tenant and
# carries on, because the alternative is that one unrestorable snapshot means
# nobody else on the VM gets their data back either; the caller decides
# whether to die. The format is waku_die's, so the two read alike on a
# terminal at three in the morning.
refuse() {
  printf 'error: %s\n' "$*" >&2
}

# A CLOSED SET WITH DEFAULT-DENY, DERIVED FROM THIS SCRIPT'S OWN TEXT and not
# copied from backup.sh's. What restore.sh does with a tenant id is wider than
# what backup.sh does with one: it names the staging slot that
# waku_reset_staging_slot bind-mounts and runs `find /staging -mindepth 1
# -delete` inside, the `--include` path restic writes, the `tenant:<id>` restic
# tag, and the argument the gateway acts on. Every one of those is a path or a
# selector, so `..` or a tag with a comma in it is not a bad id, it is a
# different target.
#
# The shape is core/tenant.TENANT_ID_RE, `^[a-z2-7]{12}$`, because that is what
# names a staging slot -- the spawner's own `<staging_root>/<tenant id>`.
#
# LC_ALL=C because a bracket expression follows LC_CTYPE, and a root login
# shell on Ubuntu commonly has a UTF-8 one, under which the range quietly
# widens. The empty string matches no bracket expression at all, which is why
# the length is measured rather than inferred.
is_tenant_id() {
  ( LC_ALL=C
    case "$1" in *[!a-z2-7]*) exit 1 ;; esac
    [ "${#1}" -eq 12 ] )
}

# THE OTHER HALF OF --tenant's SET, and the reason restore.sh cannot reuse
# backup.sh's guard: `restore.sh --tenant mei@example.com` is in the operator
# guide, so an email is an accepted value here and is not one there.
#
# An email is NEVER a path. It is turned into a tenant id below and the id is
# put through is_tenant_id before it reaches anything. This set exists for the
# other reason: the value is embedded in one SQL string literal, so it refuses
# the quote that would end that literal, along with every other character that
# is not in an address -- whitespace, a semicolon, a backslash, a dollar sign,
# a slash. Default-deny over the whole string first, then the structure.
is_tenant_email() {
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
# THIS IS A CLOSED SET FOR THE SAME REASON THE TENANT ID IS ONE. The value is
# handed to restic as the positional argument of `restic restore`, so a value
# beginning with `-` is read as a flag -- and restic's restore flags include
# `--target`, which is where the snapshot's contents land.
is_snapshot_id() {
  ( LC_ALL=C
    [ "$1" = latest ] && exit 0
    case "$1" in *[!0-9a-f]*) exit 1 ;; esac
    [ "${#1}" -ge 8 ] && [ "${#1}" -le 64 ] )
}

while [ $# -gt 0 ]; do
  waku_needs_value "$1" "$#" --tenant --snapshot \
    || { usage >&2; waku_die "$1 needs a value"; }
  case "$1" in
    # A SECOND MODE FLAG IS REFUSED, NOT TAKEN. --all and --tenant do very
    # different things and neither is a narrowing of the other, so
    # `restore.sh --all --tenant mei@example.com` has two readings and a
    # last-one-wins rule would silently pick one of them.
    --tenant)
      [ -z "$mode" ] || { usage >&2; waku_die "--tenant and --all are two different restores; give one"; }
      mode=one; one=$2; shift 2 ;;
    --all)
      [ -z "$mode" ] || { usage >&2; waku_die "--tenant and --all are two different restores; give one"; }
      mode=all; shift ;;
    --snapshot) snapshot=$2; shift 2 ;;
    --no-stop-fleet) stop_fleet=no; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; waku_die "unknown argument: $1" ;;
  esac
done
[ -n "$mode" ] || { usage >&2; waku_die "--tenant or --all is required"; }

# BEFORE waku_require_root, like backup.sh's and install.sh's flag checks and
# for the same two reasons: a script that demands root before telling you an
# argument is wrong is a worse script, and it is what makes these refusals
# reachable from a test on a maintainer's laptop.
is_snapshot_id "$snapshot" \
  || waku_die "a snapshot is 'latest' or 8 to 64 hex characters; got '$snapshot'. It is the positional argument of restic restore, so anything else is refused here. List them with: restic snapshots"
if [ "$mode" = one ]; then
  is_tenant_id "$one" || is_tenant_email "$one" \
    || waku_die "--tenant takes a tenant id (twelve characters of a-z and 2-7) or an email address; got '$one'"
  # REFUSED RATHER THAN IGNORED. A one-tenant restore goes through the gateway
  # for everything it does -- it is what stops that tenant's container, inside
  # the admin verb -- so a flag saying "do not ask the gateway" has no reading
  # here, and a flag that is silently ignored is one an operator will believe.
  [ "$stop_fleet" = yes ] \
    || waku_die "--no-stop-fleet is for --all only; a --tenant restore asks the gateway to stop that one container and cannot run without it"
fi

waku_require_root

# EACH MODE DECLARES THE NAMES IT ITSELF READS, on top of the four every
# consumer reads. WAKU_SERVICES_IMAGE is waku_reset_staging_slot's repair
# container and both modes reach it; WAKU_GATEWAY_ADDRESS is the readiness
# probe after the services are started again, which only --all has.
#
# NOT ONE LIST FOR BOTH, and not the list the F3a handover predicted either.
# That note said restore.sh "will need WAKU_DATA_DEVICE" because a one-tenant
# restore recreates the two directories with their project quota -- true of
# the restore, false of this script: the spawner does that inside its own
# container from its own spawner.env, and nothing in this file dereferences
# the name. Declaring it here would refuse to start a restore over a value
# this script never reads, which is the F2 finding in a mirror.
if [ "$mode" = all ]; then
  waku_load_install_env WAKU_SERVICES_IMAGE WAKU_GATEWAY_ADDRESS
else
  waku_load_install_env WAKU_SERVICES_IMAGE
fi

staging="$WAKU_ROOT/staging"
control_slot="$staging/control"

# restic's repository, password file and object-store credentials, EXPORTED and
# CHECKED BEFORE THE LOCK IS TAKEN, for the reason backup.sh gives: a missing
# or empty restic name discovered after the lock is a run that has already
# emptied a staging slot for a restore it cannot fetch.
waku_load_backup_env "$WAKU_ROOT/config/backup.env" RESTIC_REPOSITORY RESTIC_PASSWORD_FILE

# The default 3600, not backup.sh's --reset-staging 60: a restore waits for a
# running backup rather than refusing, because the operator running this has
# already lost something.
waku_flock_staging

# --- one tenant ---------------------------------------------------------------

# An id or an email in, a tenant id out. Nothing else in this file accepts an
# email, because nothing else in this file has anywhere to put one.
resolve_tenant() {
  local value answer id
  value=$1
  if is_tenant_id "$value"; then
    printf '%s\n' "$value"
    return 0
  fi
  # THROUGH THE GATEWAY'S OWN `resolve` VERB, AND NOT THROUGH SQL OF THIS
  # SCRIPT'S OWN. An earlier version of this function ran
  # `select id from tenant where email = '$value'` inside the gateway's
  # container. It was not injectable -- is_tenant_email is a closed set that
  # refuses the quote -- but it was a SECOND COPY OF A CONTRACT, and it had
  # already drifted from the one it copied: store.tenant_by_email pins its
  # answer with `ORDER BY created_at, id LIMIT 1` because an address is not
  # unique, and this had neither clause. An operator whose address is on two
  # rows got two lines back, and the refusal they saw in the middle of a
  # disaster named neither row. `resolve` calls the same `_find` that
  # `tenant.sh disable <email>` and `waku_admin restore <email>` already use.
  #
  # THE ANSWER IS STILL ONLY A CANDIDATE. The extraction takes whatever sits
  # at the "tenant" key and is_tenant_id in restore_tenant decides -- so a
  # malformed answer, an empty one or two of them is a refusal, not a path.
  answer=$(waku_admin resolve "$value") || {
    refuse "the gateway could not resolve '$value' to a tenant: $answer"
    return 1
  }
  # `[^"]*` rather than an interval expression: BRE `\{12\}` is POSIX and
  # works on GNU and BSD sed alike, but the id's shape is is_tenant_id's job
  # and stating it twice is how the two drift apart.
  id=$(printf '%s' "$answer" | sed -n 's/.*"tenant": *"\([^"]*\)".*/\1/p')
  if [ -z "$id" ]; then
    refuse "the gateway's answer to resolve '$value' names no tenant: $answer"
    return 1
  fi
  printf '%s\n' "$id"
}

restore_tenant() {
  local id slot
  id=$1
  # THE ONE FUNNEL, AND THAT IS WHY THE CLOSED SET SITS HERE RATHER THAN AT
  # THE CALLERS. Three different sources reach this line -- a --tenant flag, an
  # email turned into an id, and a row read out of control.db -- and every one
  # of them continues into a bind mount, a `find -delete`, a restic tag and a
  # path restic writes. A row is trusted for its content and checked for its
  # shape, because a poisoned row is the same hole through a different door.
  if ! is_tenant_id "$id"; then
    refuse "'$id' is not a tenant id (twelve characters of a-z and 2-7). It would be joined to $staging to make a path this restore empties, so it is refused instead."
    return 1
  fi
  slot="$staging/$id"
  waku_log "restoring tenant $id from snapshot $snapshot"

  # A SLOT THAT ALREADY CARRIES A MANIFEST IS A FINISHED BACKUP NOBODY
  # SNAPSHOTTED, AND IT IS REFUSED BEFORE ANYTHING IS EMPTIED. backup.sh
  # clears the slot only AFTER `restic backup` returns, so when the upload
  # fails -- network, object store, credentials, quota, the ordinary failures
  # -- the slot keeps a complete, manifest-bearing copy and the tenant is
  # reported as a failure. That copy is then the only current copy of this
  # tenant on the VM, and emptying the slot is the first thing this function
  # would otherwise do: destroy, and then discover at the manifest check that
  # there was no snapshot to replace it with. That is the shape
  # designs/backup-restore-integrity.md exists about.
  #
  # A REFUSAL AND NOT A REORDER. Emptying first is necessary -- restic merges
  # into what is there, and a slot holding part of another backup would hand
  # the spawner a mixture of two. So the precondition is checked instead, and
  # the operator is told which of the two things they have.
  #
  # `-f` AND NOT `-L`: a symlinked manifest is not a finished backup's
  # declaration (the spawner's _read_manifest refuses one), so it falls
  # through to being emptied like any other junk.
  if [ -f "$slot/manifest.json" ] && [ ! -L "$slot/manifest.json" ]; then
    refuse "$slot already holds a finished backup that was never sent to restic -- backup.sh clears the slot only after a successful upload, so this is the only current copy of $id on this VM. Refusing to delete it. Send it with: backup.sh --tenant $id ; or discard it with: backup.sh --reset-staging $id"
    return 1
  fi

  # Empty first, as UID 10001 in a container: restic merges into what is there,
  # and a slot holding a previous restore would hand the spawner a mixture of
  # two backups -- the union-of-every-backup defect, one level out.
  waku_reset_staging_slot "$slot" || return 1

  # --target / restores the absolute path the snapshot holds, which is exactly
  # this slot; --include bounds it to that path, so a snapshot that somehow held
  # more could not write anywhere else.
  #
  # --host, and it is not decoration. With the default `latest` snapshot,
  # --tag and --host are how restic chooses WHICH snapshot; backup.sh sets
  # --host on every snapshot it takes. Two VMs sharing one --restic-repository
  # is a one-flag mistake nothing refuses, and without --host `latest` for a
  # tag can be the other VM's.
  restic restore "$snapshot" --tag "tenant:$id" --host "$WAKU_DOMAIN" \
    --include "$slot" --target / || return 1

  # THE BACKUP'S OWN DECLARATION THAT IT FINISHED, CHECKED BEFORE THE GATEWAY
  # IS ASKED FOR ANYTHING. Asking is what archives this tenant's live tree and
  # removes it, so a snapshot from a backup that did not finish has to be
  # refused here or the tenant is left with nothing. The manifest travels
  # inside the snapshot -- backup.sh refuses to snapshot a slot without one --
  # so this also proves the restore brought back the whole slot and not only
  # home/ and env/.
  #
  # -L first: `-f` follows a symlink, so presence alone accepts a link to a
  # perfectly good manifest sitting above a slot that holds nothing.
  if [ -L "$slot/manifest.json" ] || [ ! -f "$slot/manifest.json" ]; then
    refuse "the snapshot for $id has no manifest.json, so it was taken from a backup that did not finish. Refusing: the restore would archive this tenant's live data and replace it with whatever that run managed to copy. Pick an older snapshot with: restic snapshots --tag tenant:$id"
    return 1
  fi

  # THROUGH THE RUNNING GATEWAY, which stops the tenant's container first. CI
  # proved that is not hygiene: a restore that deleted a running container's
  # directories left its bind mount on a dead inode, and the next docker exec
  # failed with "possible container breakout detected".
  waku_admin restore "$id" >/dev/null || return 1
  waku_reset_staging_slot "$slot" || return 1
  waku_log "restored $id"
}

if [ "$mode" = one ]; then
  one_id=$(resolve_tenant "$one") \
    || waku_die "could not turn '$one' into a tenant id; nothing has been restored"
  restore_tenant "$one_id" \
    || waku_die "the restore of $one_id did not finish; read the refusal above"
  exit 0
fi

# --- the whole system ----------------------------------------------------------

# STAGE 0, AND NOTHING IS STOPPED YET. See the header: the spec's five stages
# are the destructive ones, and pulling a snapshot into staging is not one of
# them.
waku_log "fetching the platform databases from snapshot $snapshot"
if [ -L "$control_slot" ]; then
  waku_die "$control_slot is a symlink; refusing to restore through it"
fi
mkdir -p "$control_slot"
chmod 0700 "$control_slot"
# EMPTIED WHOLE, not `rm -f *.db`. A ledger.db left behind by an earlier
# restore would be installed as though this snapshot had carried it -- the
# union-of-every-backup defect designs/backup-restore-integrity.md is about --
# and a control.db-wal left beside a DIFFERENT database is a rollback SQLite
# would apply without complaining. ROOT MAY CLEAR THIS ONE, unlike a tenant's
# slot: everything in it was written by root from the platform's own
# databases and no tenant has ever had a path inside it.
find "$control_slot" -mindepth 1 -delete
restic restore "$snapshot" --tag control --host "$WAKU_DOMAIN" \
  --include "$control_slot" --target /

# TWO GUARDS AND TWO SENTENCES, not one disjunction. They refuse different
# things -- a link standing in for the database, and no database at all -- and
# a shared message made the pair untestable: a fixture linking to a path that
# does not exist on the machine running the test is refused by the SECOND
# check whichever way the first one is written, so the symlink arm could be
# deleted with the suite green. That is the third shape conventions.md names,
# the fixture reaching the guard and then agreeing with the mutant.
if [ -L "$control_slot/control.db" ]; then
  waku_die "$control_slot/control.db is a symlink; refusing to install through it. install(1) would copy whatever it names over the database that holds every tenant."
fi
if [ ! -f "$control_slot/control.db" ]; then
  waku_die "the control snapshot holds no control.db; nothing can be restored from it"
fi
# A SINGLE SQLITE FILE CARRIES ITS OWN COMPLETENESS CHECK, which is why these
# two need no manifest and a tenant's many files do. `-x` anchors the whole
# line: sqlite3 answers a corrupt database with lines like `*** in database
# main *** page 4 is broken`, and `broken` contains `ok`. An empty answer --
# the shape pipefail cannot see, because nothing failed -- matches nothing and
# is refused too.
sqlite3 "$control_slot/control.db" 'PRAGMA integrity_check' | grep -qx ok \
  || waku_die "the restored control.db does not pass PRAGMA integrity_check; refusing to install it over the live one"

if [ -L "$control_slot/ledger.db" ]; then
  waku_die "$control_slot/ledger.db is a symlink; refusing to install through it"
fi
if [ -f "$control_slot/ledger.db" ]; then
  sqlite3 "$control_slot/ledger.db" 'PRAGMA integrity_check' | grep -qx ok \
    || waku_die "the restored ledger.db does not pass PRAGMA integrity_check; refusing to install it over the live one"
fi

# STAGE 1. From here on the platform is down, and everything it depends on has
# been checked.
#
# NOT `|| true`. If the gateway cannot be asked to stop the fleet, a container
# survives into the restore and the restore deletes the directories under it:
# the dead-inode failure CI produced, whose first symptom is `docker exec`
# reporting "possible container breakout detected" in the middle of a disaster
# recovery. So the default is to stop, and to stop the RUN if that fails.
#
# AND THE ESCAPE IS EXPLICIT, because the failure it guards against is the
# disaster this command exists for. `waku_admin` is `docker compose exec ...
# gateway python -m hosted.gateway.admin`, which exits 2 when the socket does
# not answer -- so on a VM whose gateway is down or crash-looping, and whose
# control.db is the broken thing, the default refuses at stage 1 and the
# primary recovery path does not run. --no-stop-fleet is how an operator says
# "the gateway cannot answer; I know". It is survivable rather than merely
# permitted, because stage 3b below stops the fleet once the gateway is back
# on the restored database and BEFORE any tenant tree is touched.
if [ "$stop_fleet" = yes ]; then
  waku_log "stopping every tenant container"
  waku_admin stop-all
else
  waku_log "--no-stop-fleet: not asking the gateway to stop the fleet. Any tenant container still running keeps its bind mount into a tree this restore will replace; they are stopped once the gateway is back, before any tenant is restored."
fi

waku_log "stopping the gateway and the proxy"
waku_compose stop gateway proxy

# STAGE 2. COPY BESIDE, THEN DROP THE LOG, THEN RENAME. Three steps and the
# order of all three is load-bearing.
#
# `mv` IS WHAT MAKES THIS ATOMIC, NOT `install`. An earlier version of this
# block said install(1) "writes to a temporary file first", which is true of
# BSD install -- the man page on a maintainer's macOS laptop says so, which is
# where the sentence came from -- and FALSE of GNU coreutils, which is what
# Ubuntu 24.04 runs. GNU install is cp plus chown plus chmod: it unlinks the
# destination and reopens it O_CREAT|O_TRUNC. No temporary, no rename. So on
# the platform this actually runs on, a kill mid-copy left a TRUNCATED
# control.db, and with the log already removed there was nothing to recover
# it from -- worse than either of the two orders the -wal decision below
# compares. install still does the owner, the group and the mode in one call,
# which is why it is still here; rename(2) keeps all three.
#
# THE -wal AND -shm GO BEFORE THE RENAME. They belong to the OLD database and
# would be read as part of the new one. With the copy already complete beside
# it, a run killed at any point leaves either the old database with its log
# (before the rm), the old database without its log -- older and internally
# consistent (between), or the new database (after the rename). No window
# leaves a torn file at the live path.
waku_log "replacing control.db"
install -o 10002 -g 10002 -m 0600 "$control_slot/control.db" \
        "$WAKU_ROOT/control/control.db.new"
rm -f "$WAKU_ROOT/control/control.db-wal" "$WAKU_ROOT/control/control.db-shm"
mv -f "$WAKU_ROOT/control/control.db.new" "$WAKU_ROOT/control/control.db"

if [ -f "$control_slot/ledger.db" ]; then
  waku_log "replacing ledger.db"
  install -o 10003 -g 10003 -m 0600 "$control_slot/ledger.db" \
          "$WAKU_ROOT/ledger/ledger.db.new"
  rm -f "$WAKU_ROOT/ledger/ledger.db-wal" "$WAKU_ROOT/ledger/ledger.db-shm"
  mv -f "$WAKU_ROOT/ledger/ledger.db.new" "$WAKU_ROOT/ledger/ledger.db"
else
  waku_log "the snapshot holds no ledger.db (group D of spec 001); leaving the spend ledger alone"
fi

# STAGE 3.
waku_log "starting the gateway and the proxy"
waku_compose start gateway proxy

waku_log "waiting for the gateway"
ready=no
i=0
while [ $i -lt 60 ]; do
  if curl -fsS -o /dev/null -H "Host: $WAKU_DOMAIN" "http://$WAKU_GATEWAY_ADDRESS/login"; then
    ready=yes
    break
  fi
  sleep 1
  i=$((i + 1))
done
[ "$ready" = yes ] || waku_die "the gateway did not answer after the databases were restored; no tenant has been restored yet"

# STAGE 3b. AGAIN, AND ON THE RESTORED DATABASE THIS TIME. Two different
# containers are caught here and neither is caught at stage 1: one that was
# started by a sign-in landing between stage 1 and the gateway going down, and
# -- under --no-stop-fleet -- every container that was running all along. This
# is the line that makes --no-stop-fleet survivable rather than merely
# permitted, and it runs before the first tenant tree is touched. On an
# ordinary run it answers {"stopped": []} and costs one round trip.
waku_log "stopping any tenant container that survived the restart"
waku_admin stop-all

# STAGE 4. THE LIST COMES FROM THE RESTORED COPY IN STAGING, not from the live
# database: it is the same bytes, it was integrity-checked a moment ago, and
# reading it leaves no root-owned -wal beside a database the gateway has just
# opened as UID 10002. Deleted tenants are left out -- their tree is an archive
# and there is no snapshot tagged for them.
failures=""
tenants=$(sqlite3 "$control_slot/control.db" \
  "select id from tenant where status in ('active','disabled') order by id")

# READ RATHER THAN SPLIT, so every expansion below stays quoted. Each row goes
# through restore_tenant's closed set; a row that is not a tenant id is a
# failure to report, not a path to act on.
#
# A FAILED TENANT DOES NOT STOP THE RUN, for backup.sh's reason turned around:
# after a disaster, one unrestorable snapshot must not mean nobody else gets
# their data back.
while IFS= read -r id; do
  [ -n "$id" ] || continue
  restore_tenant "$id" || failures="$failures $id"
done <<EOF
$tenants
EOF

[ -z "$failures" ] || waku_die "these tenants were not restored:$failures"
waku_log "restore finished"
