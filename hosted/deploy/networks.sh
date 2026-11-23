#!/usr/bin/env bash
# The two bridges, before any service starts.
#
# THE ADDRESSES LIVE IN hosted/core/tenant.py AND ARE COPIED HERE ON PURPOSE.
# A shell script cannot import a Python constant, and the alternatives -- a
# printer module, a generated file -- put a build step between the operator and
# the network their tenants sit on. So the values are duplicated and
# evals/hosted_docker/test_compose.py::test_networks_sh_creates_both_bridges_with_icc_off
# runs this script against a real daemon and reads the result back with
# `docker network inspect`, comparing it to core/tenant.py's constants. The
# duplicate is checked by behaviour, not by grepping this file.
#
# enable_icc=false is the load-bearing option: a user-defined bridge allows
# container-to-container traffic by default, and a tenant's dashboard has no
# authentication of its own.
#
# WHAT THIS SCRIPT DOES NOT DO, and what task C3 of spec 001 still owns:
# the DOCKER-USER forward rules, the dropped private and link-local ranges, the
# DNS exception, the host's INPUT rules on these interfaces and the
# net.bridge.bridge-nf-call-iptables assertion. They all live in
# hosted/deploy/firewall.sh, which is not in the tree yet. Creating the bridges
# is not the same as making them safe, and until C3 lands a tenant container on
# waku-tenants can reach the VM's private network and the cloud metadata
# service.
set -euo pipefail

create_bridge() {
  name=$1
  subnet=$2
  gateway=$3
  shift 3
  if docker network inspect "$name" >/dev/null 2>&1; then
    echo "networks.sh: $name already exists"
    return 0
  fi
  echo "networks.sh: creating $name $subnet"
  docker network create \
    --driver bridge \
    --subnet "$subnet" \
    --gateway "$gateway" \
    --opt com.docker.network.bridge.name="$name" \
    --opt com.docker.network.bridge.enable_icc=false \
    "$@" \
    "$name" >/dev/null
}

# The tenant bridge. Its IPAM range for dynamic allocation is the subnet's last
# /24; every tenant container takes a FIXED address derived from its XFS project
# id, so nothing is ever allocated from that range -- it is there so Docker can
# never hand a tenant's address to something else.
create_bridge waku-tenants 10.88.0.0/16 10.88.0.1 --ip-range 10.88.255.0/24

# The inspect bridge. `tenant.sh inspect` runs a stock dashboard on a tenant's
# stopped data, so it is tenant-controlled code and gets the same rules. It is
# NOT on the tenant bridge, because an inspect container takes a dynamic address
# and the fixed-address scheme rests on nothing but tenant containers being on
# 10.88/16.
create_bridge waku-inspect 10.89.0.0/24 10.89.0.1
