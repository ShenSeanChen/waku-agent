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
. "$here/envfiles.sh"

root=/srv/waku
dns_module_version=""
domain=""
dns_provider=""
acme_email=""
data_device=""
free_model=""
platform_key=""
platform_key_file=""
dns_env_file=""
secret_value=""
line=""
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
                  --platform-key-file PATH --dns-env-file PATH
                  --supabase-url URL --supabase-publishable-key KEY
                  --supabase-audience AUD
                  [--dns-env NAME=VALUE]... [--dns-module-version @v1.2.3]
                  [--max-running N] [--tenant-disk 1G]
                  [--dns-allow 1.2.3.4,5.6.7.8] [--root /srv/waku]

  <domain>                    the apex, for example agent.waku.one. Tenants get
                              <id>.<domain>, so a wildcard record must exist.
  --dns-provider              a caddy-dns module name, for example route53
  --dns-module-version        pin that module, for example @v1.5.0. Default:
                              whatever xcaddy resolves on the day it builds
  --dns-env-file PATH         a file of NAME=VALUE lines: the DNS provider's
                              CREDENTIALS. Delete it once this has run
  --dns-env NAME=VALUE        one NON-SECRET variable for that module, such as
                              AWS_REGION; repeatable. A secret given this way
                              stays in root's shell history and in `ps` output
                              for the whole install, which this script cannot
                              clean up: put secrets in --dns-env-file
  --data-device               the data disk's block device. xfs_quota needs it
                              inside the spawner's container
  --free-model                the one model the free tier allows. Written into
                              BOTH spawner.env and proxy.env
  --platform-key-file         a file holding the platform's model key, and
                              nothing else. Delete it once this has run
  --max-running               a whole number of at least 1.
                              Default: (memory - 2 GB) / 150 MB
  --tenant-disk               a size of at least 1 byte, optionally K, M or G.
                              Default: 1G. NOT 0: XFS reads bhard=0 as no limit
  --dns-allow                 default: the resolvers in
                              /run/systemd/resolve/resolv.conf
USAGE
}

# A flag that takes a value, given without one, used to die on bash's own
# `$2: unbound variable`. It was a refusal and it ran nothing, but it is the
# one message in this file that does not read like the others. The flags that
# take a value are listed once, as a closed set, and checked before the arm
# that would expand $2.
needs_value() {
  local remaining
  remaining=$2
  case "$1" in
    --dns-provider|--dns-module-version|--acme-email|--data-device|--free-model \
    |--platform-key-file|--dns-env-file|--supabase-url|--supabase-publishable-key \
    |--supabase-audience|--max-running|--tenant-disk|--dns-allow|--root|--dns-env)
      [ "$remaining" -ge 2 ] || { usage >&2; waku_die "$1 needs a value"; } ;;
  esac
}

# Read one credential out of a file and leave it in $secret_value. $2 is the
# flag's name, so the refusals say which file the operator should look at.
#
# NOTHING HERE EVER PRINTS THE VALUE. Every message names the path.
#
# The NUL check is first and it cannot be folded into the character set below:
# command substitution DROPS NUL bytes, so a binary file yields a shorter,
# silently mangled value that the character set then happily accepts
# (sk-ant\0abc becomes sk-antabc). A closed set cannot refuse a character that
# no longer exists by the time it looks, so the file is measured before it is
# read. `tr -d` and `wc -c` are in coreutils on Ubuntu and in the base system
# on macOS, so this check runs wherever the tests do.
read_secret_file() {
  local path flag
  path=$1
  flag=$2
  [ -f "$path" ] || waku_die "$flag: no such file: $path"
  [ -r "$path" ] || waku_die "$flag: cannot read $path"
  [ "$(wc -c <"$path")" = "$(tr -d '\000' <"$path" | wc -c)" ] \
    || waku_die "$flag: $path holds a NUL byte, so it is not a text file. Reading it would silently drop that byte and use whatever was left."
  # Command substitution removes EVERY trailing newline, which is exactly what
  # an editor's trailing newline needs and the only reason a separate strip
  # would exist. There is no separate strip line here because it would be dead
  # code: `${x%$'\n'}` after `$(cat ...)` can never match.
  secret_value=$(cat "$path")
  [ -n "$secret_value" ] || waku_die "$flag: $path is empty"
  # A CLOSED SET: printable, non-space characters only, in the C locale -- a
  # bracket expression follows LC_CTYPE, and a root login shell on Ubuntu
  # commonly has a UTF-8 one, under which multibyte characters count as
  # printable and the set quietly widens. The value is written into an env
  # file as one NAME=VALUE line, so an embedded newline would split it into a
  # line the service reads as a second setting, and a leading or trailing
  # space would be carried into an Authorization header.
  ( LC_ALL=C; case "$secret_value" in *[![:graph:]]*) exit 1 ;; esac ) \
    || waku_die "$flag: $path must hold the value and nothing else -- one line, no spaces or tabs, no blank line before it, and no carriage return (a file saved on Windows ends every line CR LF; run: sed -i 's/\r\$//' $path)"
}

# One --dns-env NAME=VALUE, checked and appended. Shared by the flag and by
# every line of --dns-env-file so the two cannot diverge: the flag is the
# convenient one and the file is the safe one, and an operator who moves a
# variable from one to the other must not find it accepted in one place and
# refused in the other.
add_dns_env() {
  waku_env_pair_ok "$1" \
    || waku_die "$2: expected NAME=VALUE with a printable, space-free value, got: $1. A line with no '=' is accepted by Compose and leaves the variable UNSET, which looks exactly like a credential you set."
  dns_env="$dns_env$1
"
}

# A CLOSED SET. An unknown flag is a refusal, not something to ignore: an
# ignored flag is how an operator comes to believe they set a value they did
# not. The first non-flag argument is the domain, and a second one is an error
# for the same reason.
while [ $# -gt 0 ]; do
  needs_value "$1" "$#"
  case "$1" in
    --dns-provider)             dns_provider=$2; shift 2 ;;
    --dns-module-version)       dns_module_version=$2; shift 2 ;;
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
      read_secret_file "$platform_key_file" --platform-key-file
      platform_key=$secret_value
      shift 2 ;;
    --dns-env-file)
      # THE SAME RULING, APPLIED TO THE OTHER CREDENTIAL. The DNS provider's
      # secret can rewrite the zone for this domain, which means it can point
      # the apex and every tenant host anywhere and mint a certificate for
      # them -- a strictly larger blast radius than the model key's. Passed as
      # --dns-env AWS_SECRET_ACCESS_KEY=..., it sat in root's .bash_history and
      # in `ps` output for the whole install, which spans an apt-get, three
      # image builds and the ACME wait. install.sh can clean up neither.
      dns_env_file=$2
      [ -f "$dns_env_file" ] || waku_die "--dns-env-file: no such file: $dns_env_file"
      [ -r "$dns_env_file" ] || waku_die "--dns-env-file: cannot read $dns_env_file"
      dns_env_lines=0
      # `|| [ -n "$line" ]` so a final line with no newline is still read.
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        add_dns_env "$line" "--dns-env-file $dns_env_file"
        dns_env_lines=$((dns_env_lines + 1))
      done <"$dns_env_file"
      [ "$dns_env_lines" -gt 0 ] \
        || waku_die "--dns-env-file: $dns_env_file holds no NAME=VALUE line. An empty credentials file would leave Caddy with no way to answer the DNS-01 challenge, and the failure would surface as a certificate that never issues."
      shift 2 ;;
    --supabase-url)             supabase_url=${2%/}; shift 2 ;;
    --supabase-publishable-key) supabase_publishable_key=$2; shift 2 ;;
    --supabase-audience)        supabase_audience=$2; shift 2 ;;
    --max-running)              max_running=$2; shift 2 ;;
    --tenant-disk)              tenant_disk=$2; shift 2 ;;
    --dns-allow)                dns_allow=$2; shift 2 ;;
    --root)                     root=$2; shift 2 ;;
    --dns-env)                  add_dns_env "$2" --dns-env; shift 2 ;;
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
  "--platform-key-file:$platform_key" "--dns-env-file:$dns_env_file" \
  "--supabase-url:$supabase_url" \
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

# The refusal carries the two commands rather than pointing at a document,
# because hosted/README.md does not answer this question yet -- F4 of spec 001
# writes that page -- and a refusal that sends an operator to a page with no
# answer in it is a refusal they follow into a dead end.
waku_xfs_prjquota_ok /proc/mounts "$root" \
  || waku_die "$root must be an XFS filesystem mounted with project quotas; without it every tenant shares one unbounded disk. On a second disk, for example /dev/sdb1:
    mkfs.xfs -q /dev/sdb1
    mkdir -p $root
    echo '/dev/sdb1 $root xfs defaults,prjquota 0 2' >>/etc/fstab
    mount $root
    xfs_quota -x -c 'state -p' $root   # expect: Enforcement: ON"

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
# A CLOSED SET ON THE VALUE. The earlier shape rejected characters and so
# admitted `0` while its own message said "positive integer" --
# WAKU_MAX_RUNNING=0 is a VM on which no tenant can ever start, and
# waku_max_running's floor of 1 never applies to a value given by flag. A
# leading zero is refused too: it is how a number becomes octal downstream.
case "$max_running" in
  ''|0*|*[!0-9]*) waku_die "--max-running must be a whole number of at least 1, written without a leading zero; got $max_running" ;;
esac

if [ -z "$dns_allow" ]; then
  dns_allow=$(waku_resolvers /run/systemd/resolve/resolv.conf) \
    || waku_die "no upstream resolver in /run/systemd/resolve/resolv.conf; pass --dns-allow. (/etc/resolv.conf on Ubuntu names only the stub 127.0.0.53, which is not an address a firewall rule can usefully open.)"
fi

tenant_disk_bytes=$(waku_bytes "$tenant_disk") \
  || waku_die "--tenant-disk takes a whole number of at least 1, written without a leading zero, with an optional K, M or G; got $tenant_disk. Zero is refused on purpose: XFS reads bhard=0 as NO limit, so --tenant-disk 0 would put every tenant on an unbounded disk."

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
# waku_write_config (lib.sh) NEVER OVERWRITES, writes atomically, and creates
# the target at 0600 before a byte goes into it. The BODIES are in envfiles.sh
# rather than here, so that a deterministic test can call them and pin every
# name against the module that reads it -- a name misspelt here sets nothing
# and raises nothing, which is the failure this whole arrangement is for.

install_env=$root/config/install.env
install_env_existed=no
if [ -e "$install_env" ]; then
  install_env_existed=yes
fi

waku_install_env | waku_write_config "$install_env"

# A RERUN KEEPS CONFIG BUT REBUILDS IMAGES, and those two halves can disagree.
# --dns-provider is the case that bites: caddy.env and install.env are kept,
# but the caddy image is rebuilt with the NEW module, so the module compiled
# into Caddy and the WAKU_DNS_PROVIDER its Caddyfile substitutes name different
# things and Caddy refuses to start on a module it does not have. "A rerun says
# which file it kept" covered only half of what a flag reaches, so a rerun
# whose flags contradict the kept install.env is refused -- here, before the
# other four files are written and long before an image is built.
#
# Spelt out rather than looped: the values are paths, a domain and an email,
# and every separator a loop could use is a character one of them may contain.
if [ "$install_env_existed" = yes ]; then
  differs() {
    [ "$2" = "$3" ] || waku_die "$1 was '$3' when this VM was installed and is '$2' now. $install_env is kept on a rerun and never rewritten, so the two would disagree -- and for --dns-provider that means a Caddy image built with one module and a Caddyfile naming another, which Caddy refuses to start on. Pass the original value, or edit $install_env and the config file for the service it belongs to and restart that service."
  }
  # shellcheck disable=SC1090
  ( . "$install_env"
    differs "--root" "$root" "${WAKU_ROOT:-}"
    differs "the checkout" "$src" "${WAKU_SRC:-}"
    differs "<domain>" "$domain" "${WAKU_DOMAIN:-}"
    differs "--dns-provider" "$dns_provider" "${WAKU_DNS_PROVIDER:-}"
    differs "--acme-email" "$acme_email" "${WAKU_ACME_EMAIL:-}"
    differs "--data-device" "$data_device" "${WAKU_DATA_DEVICE:-}" )
fi

waku_gateway_env | waku_write_config "$root/config/gateway.env"
waku_spawner_env | waku_write_config "$root/config/spawner.env"
waku_proxy_env   | waku_write_config "$root/config/proxy.env"
printf '%s' "$dns_env" | waku_write_config "$root/config/caddy.env"

waku_log "the two credentials are now in $root/config/, root-only at 0600: the model key in proxy.env and the DNS credentials in caddy.env. Delete $platform_key_file and $dns_env_file."

# --- images ------------------------------------------------------------------

waku_log "building the tenant and services images"
"$src/hosted/image/build.sh" --tenant-tag "$tenant_image" --services-tag "$services_image"

waku_log "building caddy with the $dns_provider${dns_module_version} module"
[ -n "$dns_module_version" ] \
  || waku_log "NOTE: --dns-module-version was not given, so xcaddy resolves the caddy-dns module's latest version at build time and a rebuild on another day can produce a different Caddy."
DOCKER_BUILDKIT=1 docker build \
  --build-arg "DNS_PROVIDER=$dns_provider" \
  --build-arg "DNS_PROVIDER_VERSION=$dns_module_version" \
  --file "$src/hosted/image/caddy.Dockerfile" \
  --tag "$caddy_image" \
  "$src/hosted/image" >/dev/null

# --- the firewall unit -------------------------------------------------------
#
# C3 IS DEFERRED. Enabling a unit whose ExecStart is missing gives a VM that
# fails a unit at every boot, which trains the operator to ignore a red
# systemctl. So: install and enable it when the script is there, and say
# exactly what is open when it is not.
#
# THE UNIT IS RENDERED, NOT COPIED, and that is the fix for a hole the first
# draft had: the file carries @WAKU_FIREWALL@ and install.sh substitutes the
# very path it just tested. It used to carry a hardcoded
# /srv/waku/src/hosted/deploy/firewall.sh while the guard tested
# "$here/firewall.sh" -- so on any VM whose checkout is not at /srv/waku/src,
# the guard passed and the unit installed pointed at nothing, producing exactly
# the fails-at-every-boot outcome the paragraph above says it avoids.
if [ -x "$here/firewall.sh" ]; then
  case "$here" in
    *[\|\&]*) waku_die "the checkout path $here contains a character this script cannot substitute into the systemd unit safely. Move the checkout." ;;
  esac
  install -o 0 -g 0 -m 0644 /dev/null /etc/systemd/system/waku-firewall.service
  sed "s|@WAKU_FIREWALL@|$here/firewall.sh|g" "$here/waku-firewall.service" \
    >/etc/systemd/system/waku-firewall.service
  systemctl daemon-reload
  systemctl enable --now waku-firewall.service
  waku_log "firewall rules applied and enabled from $here/firewall.sh. --dns-allow: $dns_allow"
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
