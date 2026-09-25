#!/usr/bin/env bash
# The operator's four verbs, and the two that go with inspect.
#
# A CLOSED SET, AND A SUBSET OF THE ADMIN COMMAND'S ELEVEN. tenant.sh is what
# an operator types at 2am; backup and restore have their own scripts with
# their own locking, and offering them here as well would give two ways to do
# one thing, one of which takes no lock. `restart-all` is upgrade.sh --now's,
# `stop-all` is restore.sh --all's, and `resolve` is a lookup restore.sh makes
# on its way to something else -- none of them is a thing an operator does TO
# a tenant.
#
# EVERYTHING GOES THROUGH THE RUNNING GATEWAY (spec, "Deploy and operate"): it
# sets the status, deletes the sessions, revokes the token, clears its caches
# and stops the container in one process. A second process doing any of that
# behind its back would leave the gateway serving from a cache it thinks is
# still true.
#
# Written to parse under bash 3.2, like hosted/image/build.sh. It RUNS on
# Ubuntu 24.04's bash 5.2.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

usage() {
  cat <<'USAGE'
usage: tenant.sh status
       tenant.sh disable|enable|delete|inspect|inspect-stop <email or tenant id>

  status        the tenant ids with a running container
  disable       status disabled, sessions deleted, token revoked, container
                stopped. enable reverses the status
  delete        all of that, then the tree is archived under /srv/waku/archive
                and the row is removed. The archive is in NO restic snapshot
                and the backup timer deletes it after 30 days, so it is a
                grace period and not a backup
  inspect       a stock waku dashboard on that tenant's stopped data, on the
                host's loopback only. Reach it over an SSH tunnel; the tenant
                stays in maintenance until inspect-stop
  inspect-stop  removes that container and lets the tenant start again
USAGE
}

# THE VERB SET IS A CLOSED SET WITH DEFAULT-DENY, and `needs_tenant` is the
# other half of it: a verb that took a tenant and was listed as taking none
# would silently drop the argument and act on the whole fleet.
verb=${1:-}
case "$verb" in
  status)                                     needs_tenant=no ;;
  disable|enable|delete|inspect|inspect-stop) needs_tenant=yes ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; waku_die "unknown verb: ${verb:-<none>}" ;;
esac

# ARITY IS PART OF THE SET. A script that read $1 and $2 and ignored the rest
# would answer `tenant.sh status mei@example.test` with the whole fleet, which
# is not the question the operator asked and not an answer they can tell apart
# from the one they wanted. `tenant.sh delete a b` is the same shape with a
# worse verb: it deletes one of the two people it names and says nothing about
# the other.
if [ "$needs_tenant" = yes ]; then
  [ $# -ge 2 ] || { usage >&2; waku_die "$verb needs an email address or a tenant id"; }
  [ $# -le 2 ] || { usage >&2; waku_die "$verb takes one tenant; got $(($# - 1)) arguments"; }
  who=$2
else
  [ $# -le 1 ] || { usage >&2; waku_die "status takes no tenant: it answers for the whole fleet"; }
  who=""
fi

# A CLOSED SET ON THE TENANT, BEFORE THE GATEWAY IS ASKED ANYTHING, and before
# waku_require_root -- like backup.sh's, restore.sh's and install.sh's flag
# checks, for their two reasons: a script that demands root before telling you
# an argument is wrong is a worse script, and it is what makes these refusals
# reachable from a test on a maintainer's laptop.
#
# THE GATEWAY CHECKING TOO IS NOT A REASON TO SKIP IT HERE. Three things this
# script does with the value are its own:
#   - it becomes one argv word of `python -m hosted.gateway.admin`, whose
#     parser reads a leading `-` as an OPTION. `tenant.sh disable -h` would
#     print the admin command's help and exit 0, and the operator would be told
#     their command ran;
#   - after `inspect` it is printed back inside a command line the operator
#     copies and pastes, so a newline or a `;` in it renders a second line that
#     looks like part of the instructions;
#   - `delete` is the one verb here that cannot be undone, and the set is the
#     only thing between a mistyped word and the gateway's own `_find`.
# waku_is_tenant_id and waku_is_tenant_email are lib.sh's, shared with
# restore.sh, which accepts exactly the same two.
if [ "$needs_tenant" = yes ]; then
  waku_is_tenant_id "$who" || waku_is_tenant_email "$who" \
    || waku_die "$verb takes a tenant id (twelve characters of a-z and 2-7) or an email address; got '$who'"
fi

waku_require_root

# NO EXTRA NAMES DECLARED, AND THAT IS THE DECISION, not an omission. This
# script reaches install.env through waku_compose (WAKU_INSTALL_ENV,
# WAKU_COMPOSE) and nothing else; all four of waku_load_install_env's own names
# cover it. Declaring a name this file never dereferences would refuse to
# disable a tenant over a value it does not read, which is F2's finding in a
# mirror -- the same shape restore.sh refused WAKU_DATA_DEVICE for.
waku_load_install_env

# THE ANSWER IS PRINTED WHETHER OR NOT THE GATEWAY AGREED, and that is the
# whole reason this is not a bare `answer=$(waku_admin ...)`. The admin command
# prints ITS ONE JSON OBJECT TO STDOUT and exits 1 when that object carries an
# `error` key -- so under `set -e` a plain assignment ends the run with the
# refusal still inside the dead subshell's captured output and NOTHING on the
# operator's terminal. `|| status=$?` keeps the exit status (1 for a refusal, 2
# for a gateway that did not answer its socket) and lets the line below show
# what it said.
status=0
if [ "$needs_tenant" = yes ]; then
  answer=$(waku_admin "$verb" "$who") || status=$?
else
  answer=$(waku_admin "$verb") || status=$?
fi
printf '%s\n' "$answer"
[ "$status" = 0 ] || exit "$status"

# One field out of the answer, or nothing.
#
# sed AND NOT jq, which is restore.sh's judgement on the same answer from the
# same command, for the same reason: this deployment already parses the
# gateway's JSON with sed in the one script it matters most in, and a second
# idiom for the same job is a second thing to keep true. The answer is one JSON
# object the gateway itself wrote with a fixed set of keys, so the expression
# does not have to survive arbitrary JSON -- and whatever it extracts goes
# through a closed set below before it is printed as part of a command.
#
# The two shapes are different and so are the two expressions: `.port` is a
# number and `.archive` is a string.
#
# THE NUMBER'S EXPRESSION ENDS AT A JSON DELIMITER, and that is not tidiness.
# Without the `[,}]` the digits are simply the longest run of digits that
# follows the key, so `"port": 22abc` would be read as 22 -- a fact taken from
# a source that does not carry it, which is the one defect
# designs/backup-restore-integrity.md is named after. With it, a value that is
# not a JSON number matches nothing, comes back empty, and is refused below.
number_field() {
  printf '%s' "$answer" | sed -n "s/.*\"$1\":[[:space:]]*\([0-9][0-9]*\)[,}].*/\1/p"
}

string_field() {
  printf '%s' "$answer" | sed -n "s/.*\"$1\":[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

if [ "$verb" = delete ]; then
  archive=$(string_field archive)
  if [ -n "$archive" ]; then
    cat <<EOF

The tree was packed into:

  $archive-home.tar.zst
  $archive-env.tar.zst

THAT IS THE ONLY COPY. Archives are in no restic snapshot, and backup.sh
deletes them after 30 days. Copy them somewhere else if this tenant may ask
for their data back.
EOF
  fi
fi

if [ "$verb" = inspect ]; then
  port=$(number_field port)
  # A CLOSED SET ON A VALUE THIS SCRIPT DID NOT PRODUCE. The port comes back
  # from another process and goes straight into a command line the operator
  # pastes into their own shell, so it is a port number or it is refused.
  # Empty covers every answer with no usable `port` at all -- a missing key, a
  # value that is not a JSON number, a body that is not the object this script
  # expects. `0` and anything with a leading zero are refused too: ssh reads a
  # leading zero as octal, and nothing listens on port 0.
  #
  # THERE IS NO `*[!0-9]*` ARM because there could be no fixture for one:
  # number_field yields digits or nothing, so a non-digit can never arrive
  # here. A guard nothing can reach is a line the next reader would trust.
  ( LC_ALL=C
    case "$port" in ""|0*) exit 1 ;; esac
    [ "$port" -le 65535 ] ) \
    || waku_die "the gateway did not name a usable port for the inspect dashboard. Its answer is on the line above; the container may not have been published. Stop it with: sudo $here/tenant.sh inspect-stop $who"
  cat <<EOF

The inspect dashboard is on this VM's loopback only. From your laptop:

  ssh -N -L 7777:127.0.0.1:$port <this VM>

then open http://127.0.0.1:7777

The tenant stays in maintenance -- their own container will not start -- until:

  sudo $here/tenant.sh inspect-stop $who
EOF
fi
