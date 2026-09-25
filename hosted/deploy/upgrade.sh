#!/usr/bin/env bash
# Pull, rebuild, restart the services. Tenant containers pick up the new image
# on their next start, or at once with --now.
#
# WHY TENANTS DO NOT NEED RESTARTING BY DEFAULT: a container's environment
# cannot change and each start carries a new proxy token, so every start creates
# a new container from the current image tag. An idle tenant is therefore
# upgraded the moment they come back, for free. --now is for the case where the
# upgrade fixes something a running tenant is suffering from, and it costs every
# open dashboard an interrupted turn.
#
# NOTHING HERE TOUCHES config/ OR tenants/. An upgrade that rewrote config would
# be an install; an upgrade that touched tenants/ would be a restore.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

ref=""
now=no
while [ $# -gt 0 ]; do
  # waku_needs_value (lib.sh) catches --ref given with nothing after it --
  # before anything here expands $2 and dies on bash's own `$2: unbound
  # variable` instead.
  waku_needs_value "$1" "$#" --ref \
    || waku_die "$1 needs a value (usage: upgrade.sh [--ref <git ref>] [--now])"
  case "$1" in
    --ref)
      # AND EMPTY IS REFUSED TOO, separately: waku_needs_value only counts
      # arguments, so `--ref ""` has two of them and passes that check, then
      # `${ref:-origin/main}` below treats an EMPTY ref the same as an unset
      # one -- silently upgrading to origin/main and exiting 0 having ignored
      # what was typed. `--ref <ref>` is half this script's interface and the
      # whole of the rollback advice a failed upgrade gives; it does not fail
      # open.
      [ -n "$2" ] || waku_die "--ref needs a value (usage: upgrade.sh [--ref <git ref>] [--now])"
      ref=$2; shift 2 ;;
    --now) now=yes; shift ;;
    -h|--help) echo "usage: upgrade.sh [--ref <git ref>] [--now]"; exit 0 ;;
    *) waku_die "unknown argument: $1 (usage: upgrade.sh [--ref <git ref>] [--now])" ;;
  esac
done

waku_require_root
waku_load_install_env WAKU_TENANT_IMAGE WAKU_SERVICES_IMAGE WAKU_CADDY_IMAGE \
                      WAKU_DNS_PROVIDER WAKU_GATEWAY_ADDRESS

# CURL IS CHECKED HERE, before anything is fetched or rebuilt, not left to
# fail 60 seconds into the readiness loop below with a message that blames the
# gateway for a missing binary.
command -v curl >/dev/null 2>&1 \
  || waku_die "curl is not on PATH. upgrade.sh needs it to confirm the gateway is answering before it returns; install it and rerun."

# A DIRTY CHECKOUT IS A REFUSAL. Upgrading over local edits either throws them
# away or fails halfway with the tree in a state nobody can name, and this
# checkout is what every service's image is built from.
#
# READS GIT'S STATUS, NOT ONLY ITS OUTPUT: `git status --porcelain` failing --
# no repository there, dubious ownership -- prints to stderr and nothing to
# stdout, so a guard that only tested `[ -n "$(...)" ]` would see an empty
# string and fall through to the checkout. `set -e` happens to catch that one
# line later, at `before=$(git rev-parse HEAD)`, before anything irreversible
# runs -- but a guard whose safety depends on the next line is a guard that
# fails open, not one that refuses by name.
porcelain=$(git -C "$WAKU_SRC" status --porcelain) \
  || waku_die "$WAKU_SRC does not look like a usable git checkout: git status failed there. This checkout is what every image is built from."
if [ -n "$porcelain" ]; then
  waku_die "$WAKU_SRC has uncommitted changes. Commit or discard them; this checkout is what the images are built from."
fi

before=$(git -C "$WAKU_SRC" rev-parse HEAD)
waku_log "fetching"
git -C "$WAKU_SRC" fetch --prune --tags origin
git -C "$WAKU_SRC" checkout --detach "${ref:-origin/main}"
after=$(git -C "$WAKU_SRC" rev-parse HEAD)
waku_log "checkout $before -> $after"

waku_log "rebuilding the tenant and services images"
"$WAKU_SRC/hosted/image/build.sh" \
  --tenant-tag "$WAKU_TENANT_IMAGE" --services-tag "$WAKU_SERVICES_IMAGE"

waku_log "rebuilding caddy with the $WAKU_DNS_PROVIDER module"
DOCKER_BUILDKIT=1 docker build \
  --build-arg "DNS_PROVIDER=$WAKU_DNS_PROVIDER" \
  --file "$WAKU_SRC/hosted/image/caddy.Dockerfile" \
  --tag "$WAKU_CADDY_IMAGE" \
  "$WAKU_SRC/hosted/image" >/dev/null

# The proxy stays at whatever replica count it has: `up -d` with no --scale
# leaves a service scaled to 0 at 0, so an upgrade does not quietly start a
# module that is not there.
waku_log "restarting the services"
waku_compose up -d

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
[ "$ready" = yes ] || waku_die "the gateway did not answer after the upgrade. The previous images are still in the daemon: docker compose -p waku logs gateway, then upgrade.sh --ref $before"

if [ "$now" = yes ]; then
  # THROUGH THE RUNNING GATEWAY, not through the spawner: it issues each tenant
  # a fresh token and records the new address, and a second process doing this
  # behind its back would leave the fleet map pointing at containers that no
  # longer exist.
  waku_log "restarting every running tenant"
  waku_admin restart-all
fi

waku_log "upgraded $before -> $after"
