#!/usr/bin/env bash
# Move this deployment to another VM. Two halves, one per machine.
#
# WHY IT IS NOT ONE COMMAND: the second half runs on a machine this one has no
# credentials for, and a migration script that could SSH into the new VM as
# root would be a credential on the old VM able to take over the new one. So
# --out leaves the old VM stopped and prints exactly what to run; --in does the
# rest on the new VM, where the operator already is.
#
# DOWNTIME IS MINUTES because all state is one directory tree and one object
# store (design section 11).
#
# Written to parse under bash 3.2, like hosted/image/build.sh. It RUNS on
# Ubuntu 24.04's bash 5.2.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

mode=""
snapshot=latest
snapshot_given=no

usage() {
  cat <<'USAGE'
usage: migrate.sh --out              on the OLD VM: stop, final backup, and the
                                     instructions for the new one
       migrate.sh --in [--snapshot ID]
                                     on the NEW VM, after install.sh: restore
                                     everything from the repository
  --snapshot ID  a restic snapshot id, or `latest` (the default). --in only
USAGE
}

while [ $# -gt 0 ]; do
  waku_needs_value "$1" "$#" --snapshot \
    || { usage >&2; waku_die "$1 needs a value"; }
  case "$1" in
    # A SECOND MODE FLAG IS REFUSED, NOT TAKEN, for restore.sh's reason with a
    # worse consequence. `migrate.sh --out --in` under a last-one-wins rule is
    # a RESTORE over the machine the operator meant to leave: --in runs
    # restore.sh --all, which stops the fleet, replaces control.db and
    # ledger.db and archives and re-creates every tenant's tree.
    --out)
      [ -z "$mode" ] || { usage >&2; waku_die "--out and --in are the two halves of a migration and run on two different machines; give one"; }
      mode=out; shift ;;
    --in)
      [ -z "$mode" ] || { usage >&2; waku_die "--out and --in are the two halves of a migration and run on two different machines; give one"; }
      mode=in; shift ;;
    --snapshot) snapshot=$2; snapshot_given=yes; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; waku_die "unknown argument: $1" ;;
  esac
done
[ -n "$mode" ] || { usage >&2; waku_die "--out (on the old VM) or --in (on the new one) is required"; }

# THE SNAPSHOT'S CLOSED SET IS lib.sh's, the same one restore.sh puts the value
# through. It is checked HERE TOO rather than left to restore.sh, for the
# reason every other refusal in this file sits above waku_require_root: a
# refusal an operator can reach without root is a refusal a test can reach
# without root.
waku_is_snapshot_id "$snapshot" \
  || waku_die "a snapshot is 'latest' or 8 to 64 hex characters; got '$snapshot'. It is the positional argument of restic restore, so anything else is refused here. List them with: restic snapshots"

# REFUSED RATHER THAN IGNORED. --out takes no snapshot -- it MAKES one -- and a
# flag silently dropped is a flag the operator believes took effect. The
# mistake it catches is real: `migrate.sh --out --snapshot 1a2b3c4d` reads like
# "migrate from that snapshot" and would instead overwrite it with a new one.
[ "$mode" = in ] || [ "$snapshot_given" = no ] \
  || waku_die "--snapshot is for --in only: --out takes the final backup rather than choosing one. Run migrate.sh --in --snapshot $snapshot on the NEW VM."

waku_require_root

# EACH MODE DECLARES THE NAMES IT ITSELF READS, on top of the four every
# consumer reads (lib.sh, and the F2 finding it records). --out prints the
# flags the new VM's install.sh needs, so it dereferences WAKU_DNS_PROVIDER and
# WAKU_ACME_EMAIL; --in reads neither and would otherwise refuse to restore a
# disaster over a name it never touches.
if [ "$mode" = out ]; then
  waku_load_install_env WAKU_DNS_PROVIDER WAKU_ACME_EMAIL
else
  waku_load_install_env
fi

if [ "$mode" = in ]; then
  waku_log "restoring everything onto this VM"
  "$here/restore.sh" --all --snapshot "$snapshot"
  cat <<EOF

Restored. Before moving DNS:

  1. sudo $here/tenant.sh status     -- the gateway answers
  2. Sign in as one existing tenant and confirm they land on the same tenant id
     with their own data (spec, acceptance 17).
  3. Then move the apex and the wildcard records to this VM.
EOF
  exit 0
fi

# --- the old VM ---------------------------------------------------------------

# ONE NAME=VALUE LINE OUT OF A CONFIG FILE, OR A REFUSAL. The values below are
# printed into a command line the operator pastes on the new VM, and awk over a
# missing file prints nothing and exits non-zero INSIDE a heredoc expansion,
# where neither errexit nor pipefail can see it -- so the operator would be
# handed `--free-model` with nothing after it and find out when install.sh
# refuses, on the new machine, with the old one already stopped. Read and
# checked first, printed second.
env_value() {
  local file name value
  file=$1
  name=$2
  [ -r "$file" ] || waku_die "$file is not readable, so this migration cannot tell you what $name was set to on this VM. Read it by hand before you stop anything."
  value=$(awk -F= -v n="$name" '$1 == n { sub(/^[^=]*=/, ""); print; exit }' "$file")
  [ -n "$value" ] || waku_die "$file does not set $name, so this migration cannot tell you what the new VM's install.sh should be given for it."
  printf '%s\n' "$value"
}

free_model=$(env_value "$WAKU_ROOT/config/spawner.env" WAKU_PLATFORM_MODEL)
tenant_disk=$(env_value "$WAKU_ROOT/config/spawner.env" WAKU_TENANT_DISK_BYTES)
max_running=$(env_value "$WAKU_ROOT/config/gateway.env" WAKU_MAX_RUNNING)
supabase_url=$(env_value "$WAKU_ROOT/config/gateway.env" WAKU_SUPABASE_URL)
supabase_audience=$(env_value "$WAKU_ROOT/config/gateway.env" WAKU_SUPABASE_AUDIENCE)
supabase_key=$(env_value "$WAKU_ROOT/config/gateway.env" WAKU_SUPABASE_PUBLISHABLE_KEY)
restic_repository=$(env_value "$WAKU_ROOT/config/backup.env" RESTIC_REPOSITORY)
restic_password_file=$(env_value "$WAKU_ROOT/config/backup.env" RESTIC_PASSWORD_FILE)

# THE FRONT DOOR CLOSES FIRST, and that ordering is the whole correctness of
# this half. `stop-all` stops every tenant container; the gateway is still
# serving while the backup runs, so without this a sign-in landing in that
# window starts a container, the tenant writes a turn, and the final backup --
# already taken for them -- does not have it. Their data is on a VM nobody is
# going to start again. Stopping Caddy costs the same downtime as the rest of
# the migration and closes the window entirely.
waku_log "closing the front door: stopping caddy"
waku_compose stop caddy

waku_log "stopping every tenant container"
waku_admin stop-all

# NOT `|| true`, and the refusal leaves the operator STUCK RATHER THAN MOVED
# ON. A migration whose final backup failed has nothing on the new VM to
# restore, and the honest thing to do is stop here with the old VM's data
# intact and its stack still up apart from Caddy.
waku_log "final backup"
"$here/backup.sh" --all \
  || waku_die "the final backup did not finish, so there is nothing to migrate from. NOTHING HAS BEEN MOVED and this VM's data is untouched. Read the refusal above, then put the front door back with:
    docker compose --env-file $WAKU_INSTALL_ENV -f $WAKU_COMPOSE --project-name waku start caddy"

waku_log "stopping the stack"
waku_compose stop

cat <<EOF

The old VM is stopped and its final backup is in the repository.

On the NEW VM, in this order:

  1. Format and mount the data disk as XFS with project quotas, and clone this
     repository to $WAKU_SRC, the same path this VM uses. hosted/README.md
     has both commands.
  2. Copy these files across by hand, as root, mode 0600. They hold the
     platform model key, the DNS credentials and the restic repository's
     details, and install.sh never overwrites a config file that is already
     there:
       $WAKU_ROOT/config/backup.env
       $WAKU_ROOT/config/caddy.env
       $WAKU_ROOT/config/proxy.env
       $restic_password_file     (the restic password, named by backup.env)
     NOT install.env: it carries this VM's --data-device and its checkout path,
     and install.sh writes the new VM's own.
  2b. AND $WAKU_ROOT/archive, if anything is in it.
     Archives are in no restic snapshot, so migrate.sh --in cannot bring them
     back: every tenant deleted in the last 30 days has their only copy there,
     and it goes away with this VM. Check before you destroy the old machine:
       du -sh $WAKU_ROOT/archive
  3. Run install.sh on the new VM. It STILL NEEDS --platform-key-file,
     --dns-env-file and --free-model even though proxy.env and caddy.env came
     across in step 2 and are kept: they are required flags, and a rerun keeps
     the files it finds. If the original credential files are gone, the values
     are in the copies you just made -- WAKU_PLATFORM_KEY in proxy.env, and
     caddy.env is itself a file of NAME=VALUE lines, which is what
     --dns-env-file takes.

     The values this VM was installed with:
       domain                      $WAKU_DOMAIN
       --dns-provider              $WAKU_DNS_PROVIDER
       --acme-email                $WAKU_ACME_EMAIL
       --free-model                $free_model
       --max-running               $max_running
       --tenant-disk               $tenant_disk   (bytes)
       --supabase-url              $supabase_url
       --supabase-audience         $supabase_audience
       --supabase-publishable-key  $supabase_key
       --restic-repository         $restic_repository
       --restic-password-file      $restic_password_file
       --data-device               <the NEW VM's disk, not this one's>
  4. sudo $here/migrate.sh --in
  5. Point the apex and the wildcard DNS records at the new VM. Only then: the
     certificate is issued by DNS-01, so the new VM can hold it before any
     traffic moves.

This VM's stack is stopped and its data is untouched. To roll back, start it
again with:
    docker compose --env-file $WAKU_INSTALL_ENV -f $WAKU_COMPOSE --project-name waku start
EOF
