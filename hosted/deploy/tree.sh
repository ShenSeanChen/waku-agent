#!/usr/bin/env bash
# Every directory under the data root, with the owner and mode the spec's two
# tables give it. Idempotent: it sets the owner and mode on every run, so a
# directory somebody chmodded by hand is repaired rather than left wrong.
set -euo pipefail

root=${1:?usage: tree.sh <root>}

# CHOWN BEFORE CHMOD, and that order is load-bearing: chown may clear the
# set-group-ID bit, so a chmod 2750 followed by a chown leaves a 0750 directory
# -- and a socket bound inside it then takes the SERVING process's group instead
# of the peer's, which locks the peer out with EACCES and no explanation.
#
# The three 2750 directories are the spec's "How the services run" table:
# install.sh creates each socket directory with the PEER's group and the setgid
# bit, so a socket bound inside it takes that group even though the serving
# process is not in it. run/ itself is 0755 because 10002 and 10003 have to
# traverse it to reach their own directory.
while read -r path mode owner group; do
  case "$path" in ''|'#'*) continue ;; esac
  mkdir -p "$root/$path"
  chown "$owner:$group" "$root/$path"
  chmod "$mode" "$root/$path"
done <<'TREE'
config          0700 0     0
control         0700 10002 10002
control/backup  0700 10002 10002
ledger          0700 10003 10003
ledger/backup   0700 10003 10003
run             0755 0     0
run/gateway     2750 10002 10003
run/proxy       2750 10003 10002
run/spawner     2750 0     10002
run/admin       0700 10002 10002
tenants         0700 0     0
archive         0700 0     0
staging         0700 0     0
TREE
