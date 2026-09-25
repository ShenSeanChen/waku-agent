#!/usr/bin/env bash
# One idempotent install of hosted waku on a fresh Ubuntu 24.04 VM.
#
# IDEMPOTENT MEANS TWO THINGS HERE, and the spec names both: a rerun skips
# finished steps, and it NEVER OVERWRITES EXISTING CONFIG. So every config file
# is written only when it is absent, and a rerun with different flags says which
# file it kept rather than quietly changing a value the running services were
# started with. To change one, edit the file and restart that service; F4's
# README says so.
#
# Written to parse under bash 3.2, like hosted/image/build.sh. It RUNS on
# Ubuntu 24.04's bash 5.2.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
src=$(cd "$here/../.." && pwd)
. "$here/lib.sh"
. "$here/checks.sh"

root=/srv/waku
domain=""
dns_provider=""
acme_email=""
data_device=""
free_model=""
platform_key=""
platform_key_file=""
supabase_url=""
supabase_publishable_key=""
supabase_audience=""
max_running=""
tenant_disk="1G"
dns_allow=""
dns_env=""

usage() {
  cat <<'USAGE'
usage: install.sh <domain> --dns-provider NAME --acme-email ADDRESS
                  --data-device /dev/sdb1 --free-model MODEL
                  --platform-key-file PATH
                  --supabase-url URL --supabase-publishable-key KEY
                  --supabase-audience AUD
                  [--dns-env NAME=VALUE]... [--max-running N]
                  [--tenant-disk 1G] [--dns-allow 1.2.3.4,5.6.7.8]
                  [--root /srv/waku]

  <domain>                    the apex, for example agent.waku.one. Tenants get
                              <id>.<domain>, so a wildcard record must exist.
  --dns-provider              a caddy-dns module name, for example route53
  --dns-env NAME=VALUE        a credential for that module; repeatable. It goes
                              into config/caddy.env, root-only
  --data-device               the data disk's block device. xfs_quota needs it
                              inside the spawner's container
  --free-model                the one model the free tier allows. Written into
                              BOTH spawner.env and proxy.env
  --platform-key-file         a file holding the platform's model key, and
                              nothing else. Delete it once this has run
  --max-running               default: (memory - 2 GB) / 150 MB
  --tenant-disk               default: 1G
  --dns-allow                 default: the resolvers in
                              /run/systemd/resolve/resolv.conf
USAGE
}

# A CLOSED SET. An unknown flag is a refusal, not something to ignore: an
# ignored flag is how an operator comes to believe they set a value they did
# not. The first non-flag argument is the domain, and a second one is an error
# for the same reason.
while [ $# -gt 0 ]; do
  case "$1" in
    --dns-provider)             dns_provider=$2; shift 2 ;;
    --acme-email)               acme_email=$2; shift 2 ;;
    --data-device)              data_device=$2; shift 2 ;;
    --free-model)               free_model=$2; shift 2 ;;
    --platform-key-file)
      # RULED BY SEAN, 2026-09-25: the key is READ FROM A FILE, never taken as
      # a value. A value on the command line lands in root's shell history and
      # in the process list, where it outlives the install and is readable by
      # anyone who later gets a shell. The file is the operator's to create,
      # to chmod 600 and to delete afterwards; install.sh only reads it.
      platform_key_file=$2
      [ -f "$platform_key_file" ] || waku_die "--platform-key-file: no such file: $platform_key_file"
      [ -r "$platform_key_file" ] || waku_die "--platform-key-file: cannot read $platform_key_file"
      # Command substitution removes EVERY trailing newline, which is exactly
      # what an editor's trailing newline needs and the only reason a separate
      # strip would exist. There is no separate strip line here because it
      # would be dead code: `${x%$'\n'}` after `$(cat ...)` can never match.
      platform_key=$(cat "$platform_key_file")
      [ -n "$platform_key" ] || waku_die "--platform-key-file: $platform_key_file is empty"
      # A CLOSED SET: printable, non-space characters only. This value is
      # written into config/proxy.env as one KEY=VALUE line, so an embedded
      # newline would split it into a line the proxy reads as a second setting,
      # and a leading or trailing space would be carried into the Authorization
      # header. Refuse the file rather than write a file that reads wrong.
      case "$platform_key" in
        *[![:graph:]]*)
          waku_die "--platform-key-file: $platform_key_file must hold the key and nothing else -- one line, no spaces, no blank lines before it" ;;
      esac
      shift 2 ;;
    --supabase-url)             supabase_url=${2%/}; shift 2 ;;
    --supabase-publishable-key) supabase_publishable_key=$2; shift 2 ;;
    --supabase-audience)        supabase_audience=$2; shift 2 ;;
    --max-running)              max_running=$2; shift 2 ;;
    --tenant-disk)              tenant_disk=$2; shift 2 ;;
    --dns-allow)                dns_allow=$2; shift 2 ;;
    --root)                     root=$2; shift 2 ;;
    --dns-env)                  dns_env="$dns_env$2
"; shift 2 ;;
    -h|--help)                  usage; exit 0 ;;
    -*)                         usage >&2; waku_die "unknown flag: $1" ;;
    *)
      [ -z "$domain" ] || { usage >&2; waku_die "two domains given: $domain and $1"; }
      domain=$1; shift ;;
  esac
done

for pair in \
  "domain:$domain" "--dns-provider:$dns_provider" "--acme-email:$acme_email" \
  "--data-device:$data_device" "--free-model:$free_model" \
  "--platform-key-file:$platform_key" "--supabase-url:$supabase_url" \
  "--supabase-publishable-key:$supabase_publishable_key" \
  "--supabase-audience:$supabase_audience"
do
  name=${pair%%:*}
  value=${pair#*:}
  [ -n "$value" ] || { usage >&2; waku_die "$name is required"; }
done

# THE DOMAIN'S SHAPE IS AN ARGUMENT CHECK, so it belongs here with the other
# argument checks and not below with the checks on the machine. A tenant host
# is <id>.<domain> and <id> is a DNS label, so a single-label apex cannot carry
# a wildcard certificate: it fails at ACME time, after the VM is built, unless
# it fails here. Putting it above waku_require_root is the same judgement as
# putting the flag parser above it -- a script that demands root before telling
# you an argument is wrong is a worse script, and it is also what makes this
# refusal reachable from a test on a maintainer's laptop.
case "$domain" in
  *.*) : ;;
  *) waku_die "<domain> must be a hostname with a dot, for example agent.waku.one; got $domain" ;;
esac

waku_require_root

# --- refusals, before anything is created ---------------------------------

# Ubuntu 24.04. Not a taste: the unit file, the resolv.conf path and the Docker
# packages below are that release's.
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = "24.04" ] \
  || waku_die "this installs on Ubuntu 24.04; this VM says ${ID:-unknown} ${VERSION_ID:-unknown}"

[ -b "$data_device" ] || waku_die "--data-device $data_device is not a block device"

waku_xfs_prjquota_ok /proc/mounts "$root" \
  || waku_die "$root must be an XFS filesystem mounted with project quotas. hosted/README.md says how to format and mount the data disk; without it every tenant shares one unbounded disk."

command -v jq >/dev/null 2>&1 || apt_needed=yes
command -v curl >/dev/null 2>&1 || apt_needed=yes

# --- packages ---------------------------------------------------------------
#
# docker.io and docker-compose-v2 from Ubuntu 24.04's own archive: one apt
# source, one upgrade path, and versions the distribution supports. restic,
# sqlite3 and zstd are for F3's backup and restore; jq and curl are for the two
# Supabase checks below.
export DEBIAN_FRONTEND=noninteractive
if [ "${apt_needed:-}" = yes ] || ! command -v docker >/dev/null 2>&1; then
  waku_log "installing packages"
  apt-get update
  apt-get install --yes --no-install-recommends \
    docker.io docker-compose-v2 restic sqlite3 zstd jq curl
fi
systemctl enable --now docker >/dev/null

# --- the two Supabase refusals ----------------------------------------------
#
# Both of these are about the project, not about this VM, so they run BEFORE the
# tree is created: an operator whose project has open signup should find out
# before they have a half-built machine.
jwks=$(mktemp) settings=$(mktemp)
trap 'rm -f "$jwks" "$settings"' EXIT
curl -fsS "$supabase_url/auth/v1/.well-known/jwks.json" -o "$jwks" \
  || waku_die "could not read the project's JWKS at $supabase_url/auth/v1/.well-known/jwks.json"
waku_jwks_is_asymmetric "$jwks" \
  || waku_die "the Supabase project at $supabase_url does not publish asymmetric signing keys. The gateway verifies access tokens with a PUBLIC key; an HS256 project would make the gateway hold the secret that mints them. Switch the project to ES256 or RS256 signing keys and run this again."
curl -fsS "$supabase_url/auth/v1/settings" -H "apikey: $supabase_publishable_key" -o "$settings" \
  || waku_die "could not read the project's public auth settings at $supabase_url/auth/v1/settings"
waku_signup_is_closed "$settings" \
  || waku_die "the Supabase project at $supabase_url has open signup. This deployment is invite-only (design section 15: a free tier is an abuse target). Turn off 'Allow new users to sign up' in the project's Auth settings and run this again."

# --- derived values ----------------------------------------------------------

if [ -z "$max_running" ]; then
  max_running=$(waku_max_running /proc/meminfo) \
    || waku_die "could not read MemTotal from /proc/meminfo; pass --max-running"
fi
case "$max_running" in ''|*[!0-9]*) waku_die "--max-running must be a positive integer, got $max_running" ;; esac

if [ -z "$dns_allow" ]; then
  dns_allow=$(waku_resolvers /run/systemd/resolve/resolv.conf) \
    || waku_die "no upstream resolver in /run/systemd/resolve/resolv.conf; pass --dns-allow. (/etc/resolv.conf on Ubuntu names only the stub 127.0.0.53, which is not an address a firewall rule can usefully open.)"
fi

tenant_disk_bytes=$(waku_bytes "$tenant_disk") \
  || waku_die "--tenant-disk takes a number with an optional K, M or G, got $tenant_disk"

tenant_image=waku-tenant:current
services_image=waku-services:current
caddy_image=waku-caddy:current
gateway_address=127.0.0.1:8787

# --- the tree and the bridges ------------------------------------------------

waku_log "creating $root"
"$here/tree.sh" "$root"

waku_log "creating the two bridges"
"$here/networks.sh"

# --- config ------------------------------------------------------------------
#
# write_config NEVER OVERWRITES. It reads the file's body from stdin, writes it
# 0600 root-only, and says what it kept when the file is already there.
write_config() {
  target="$root/config/$1"
  if [ -e "$target" ]; then
    waku_log "keeping the existing $target"
    cat >/dev/null
    return 0
  fi
  install -o 0 -g 0 -m 0600 /dev/null "$target"
  cat >"$target"
  waku_log "wrote $target"
}

write_config install.env <<EOF
WAKU_ROOT=$root
WAKU_SRC=$src
WAKU_COMPOSE=$src/hosted/deploy/compose.yaml
WAKU_DOMAIN=$domain
WAKU_DNS_PROVIDER=$dns_provider
WAKU_ACME_EMAIL=$acme_email
WAKU_GATEWAY_ADDRESS=$gateway_address
WAKU_TENANT_IMAGE=$tenant_image
WAKU_SERVICES_IMAGE=$services_image
WAKU_CADDY_IMAGE=$caddy_image
WAKU_DATA_DEVICE=$data_device
WAKU_INSTALLED_COMMIT=$(git -C "$src" rev-parse HEAD 2>/dev/null || echo unknown)
EOF

# The 16 names in hosted/gateway/config.REQUIRED_ENV_NAMES, in its order. A name
# missing here raises at startup; a name MISSPELT here sets nothing and raises
# nothing, which is why that tuple and gateway.env.example are pinned against
# each other in both directions.
write_config gateway.env <<EOF
WAKU_APEX_HOST=$domain
WAKU_GATEWAY_BIND=127.0.0.1
WAKU_GATEWAY_PORT=8787
WAKU_CONTROL_DB=$root/control/control.db
WAKU_SPAWNER_SOCKET=$root/run/spawner/spawner.sock
WAKU_GATEWAY_SOCKET=$root/run/gateway/gateway.sock
WAKU_PROXY_SOCKET=$root/run/proxy/proxy.sock
WAKU_ADMIN_SOCKET=$root/run/admin/admin.sock
WAKU_MAX_RUNNING=$max_running
WAKU_SUPABASE_URL=$supabase_url
WAKU_SUPABASE_ISSUER=$supabase_url/auth/v1
WAKU_SUPABASE_JWKS_URL=$supabase_url/auth/v1/.well-known/jwks.json
WAKU_SUPABASE_AUDIENCE=$supabase_audience
WAKU_SUPABASE_PUBLISHABLE_KEY=$supabase_publishable_key
WAKU_FREE_TURNS_PER_HOUR=30
WAKU_BYOK_TURNS_PER_HOUR=120
EOF

# The 11 names in hosted/spawner/template.ENV_NAMES, plus WAKU_SPAWNER_SOCKET,
# which is one of the two in OPTIONAL_ENV_NAMES: it has a working default, and
# it is written anyway so the socket path the spawner binds and the one the
# gateway is told about come from one line of one file.
#
# --free-model writes the model into BOTH model variables: the free tier's
# allowlist is one model, the retrieval gate uses the small model, and a small
# model outside the allowlist would be refused on every turn and silently fail
# open.
write_config spawner.env <<EOF
WAKU_TENANT_ROOT=$root/tenants
WAKU_ARCHIVE_ROOT=$root/archive
WAKU_STAGING_ROOT=$root/staging
WAKU_TENANT_IMAGE=$tenant_image
WAKU_SERVICES_IMAGE=$services_image
WAKU_PLATFORM_BASE_URL=http://10.88.0.1:8788
WAKU_PLATFORM_MODEL=$free_model
WAKU_PLATFORM_SMALL_MODEL=$free_model
WAKU_TENANT_DISK_BYTES=$tenant_disk_bytes
WAKU_DATA_DEVICE=$data_device
WAKU_SECCOMP_PROFILE=/app/hosted/image/seccomp.json
WAKU_SPAWNER_SOCKET=$root/run/spawner/spawner.sock
EOF

# GROUP D IS DEFERRED, so there is no module pinning these names yet. They are
# this plan's, listed in hosted/deploy/proxy.env.example with the same warning,
# and D1 either adopts them or renames them in one commit that changes both
# files. The free-tier numbers are the spec's, not defaults chosen here.
write_config proxy.env <<EOF
WAKU_PROXY_BIND=10.88.0.1
WAKU_PROXY_PORT=8788
WAKU_LEDGER_DB=$root/ledger/ledger.db
WAKU_GATEWAY_SOCKET=$root/run/gateway/gateway.sock
WAKU_PROXY_SOCKET=$root/run/proxy/proxy.sock
WAKU_PLATFORM_KEY=$platform_key
WAKU_FREE_MODELS=$free_model
WAKU_FREE_MONTHLY_CAP_USD=1
WAKU_FREE_CONCURRENT_CALLS=4
WAKU_FREE_REQUESTS_PER_MINUTE=60
WAKU_MAX_TOKENS_CEILING=4096
WAKU_MAX_BODY_BYTES=4194304
WAKU_UPSTREAM_BASE_URL=https://api.anthropic.com
EOF

printf '%s' "$dns_env" | write_config caddy.env

waku_log "the platform key is now in $root/config/proxy.env, root-only. Delete $platform_key_file."

# --- images ------------------------------------------------------------------

waku_log "building the tenant and services images"
"$src/hosted/image/build.sh" --tenant-tag "$tenant_image" --services-tag "$services_image"

waku_log "building caddy with the $dns_provider module"
DOCKER_BUILDKIT=1 docker build \
  --build-arg "DNS_PROVIDER=$dns_provider" \
  --file "$src/hosted/image/caddy.Dockerfile" \
  --tag "$caddy_image" \
  "$src/hosted/image" >/dev/null

# --- the firewall unit -------------------------------------------------------
#
# C3 IS DEFERRED. Enabling a unit whose ExecStart is missing gives a VM that
# fails a unit at every boot, which trains the operator to ignore a red
# systemctl. So: install and enable it when the script is there, and say
# exactly what is open when it is not.
if [ -x "$here/firewall.sh" ]; then
  install -o 0 -g 0 -m 0644 "$here/waku-firewall.service" /etc/systemd/system/waku-firewall.service
  systemctl daemon-reload
  systemctl enable --now waku-firewall.service
  waku_log "firewall rules applied and enabled. --dns-allow: $dns_allow"
else
  waku_log "WARNING: $here/firewall.sh is not there, so no firewall unit was installed."
  waku_log "WARNING: task C3 of spec 001 owns that script. Until it lands, a tenant container can reach the VM's private network and the cloud metadata service. Keep this deployment invite-only, and attach no instance role or service account to this VM."
fi

# --- the stack ---------------------------------------------------------------

WAKU_INSTALL_ENV=$root/config/install.env
waku_load_install_env

# GROUP D IS DEFERRED: `python -m hosted.proxy` does not exist, so the service is
# declared and scaled to zero rather than left to restart-loop. One consequence,
# and the README says it too: a free-tier model call fails inside the tenant's
# container with a refused connection, so the first deployment is BYOK.
scale=""
if [ ! -f "$src/hosted/proxy/__main__.py" ]; then
  scale="--scale proxy=0"
  waku_log "WARNING: hosted/proxy/__main__.py is not in this checkout (group D of spec 001), so the metering proxy is not started. There is NO SPEND CAP and no free tier: tenants must add their own key in Models."
fi

waku_log "starting the stack"
# shellcheck disable=SC2086
waku_compose up -d $scale

waku_log "waiting for the gateway"
ready=no
i=0
while [ $i -lt 60 ]; do
  if curl -fsS -o /dev/null -H "Host: $domain" "http://$gateway_address/login"; then
    ready=yes
    break
  fi
  sleep 1
  i=$((i + 1))
done
[ "$ready" = yes ] || waku_die "the gateway did not answer on http://$gateway_address/login within 60 seconds. Read: docker compose -p waku logs gateway"

waku_log "installed. apex https://$domain, tenants https://<id>.$domain"
waku_log "Caddy issues the certificate on the first request to the apex; the first one can take a minute while DNS-01 propagates."
