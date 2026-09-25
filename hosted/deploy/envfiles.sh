#!/usr/bin/env bash
# The body of each config/*.env file install.sh writes. Sourced, never run.
#
# WHY THIS IS A FILE OF ITS OWN. The brief's comment on the gateway block says
# it: "a name missing here raises at startup; a name MISSPELT here sets nothing
# and raises nothing". hosted/deploy/gateway.env.example and
# spawner.env.example are pinned against their modules in BOTH directions by
# test_gateway.py and test_container_template.py -- and install.sh, the thing
# that writes the file the services actually read, was pinned against neither,
# because its heredocs sat inside a script no test can run without root, an
# Ubuntu release and a Docker daemon.
#
# Moving the three bodies into functions makes them reachable:
# evals/deterministic/hosted/test_deploy_scripts.py sources this file, calls
# each one, and compares the names it emitted with
# gateway.config.REQUIRED_ENV_NAMES and spawner.template.ENV_NAMES in both
# directions. A misspelling is now a red test rather than a service that starts
# and quietly ignores a setting the operator believes they made.
#
# EACH FUNCTION READS NAMED GLOBALS, listed above it, and prints to stdout.
# Positional parameters were the alternative and they are worse here: seven
# unlabelled arguments at the call site is how the wrong one ends up third.
# `set -u` in the caller means a global nobody set is an error and not an empty
# line, which is the property that matters.

# Reads: root domain max_running supabase_url supabase_audience
#        supabase_publishable_key
#
# The 16 names in hosted/gateway/config.REQUIRED_ENV_NAMES, in its order.
waku_gateway_env() {
  cat <<EOF
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
}

# Reads: root tenant_image services_image free_model tenant_disk_bytes
#        data_device
#
# The 11 names in hosted/spawner/template.ENV_NAMES, plus WAKU_SPAWNER_SOCKET,
# which is one of the two in OPTIONAL_ENV_NAMES: it has a working default and
# is written anyway, so the socket the spawner binds and the one the gateway is
# told about come from one line of one file.
#
# --free-model writes the model into BOTH model variables: the free tier's
# allowlist is one model, the retrieval gate uses the small model, and a small
# model outside the allowlist would be refused on every turn and silently fail
# open.
waku_spawner_env() {
  cat <<EOF
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
}

# Reads: root platform_key free_model
#
# GROUP D IS DEFERRED, so there is no module pinning these names yet. They are
# group F's, listed in hosted/deploy/proxy.env.example with the same warning,
# and D1 either adopts them or renames them in one commit that changes both
# files. The free-tier numbers are the spec's, not defaults chosen here.
#
# $platform_key is the only secret any of these three functions prints. It
# arrives from a file named by --platform-key-file, never from argv, and
# install.sh pipes this straight into waku_write_config, which creates the
# target 0600 before a byte of it is written.
waku_proxy_env() {
  cat <<EOF
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
}

# Reads: root src domain dns_provider acme_email gateway_address tenant_image
#        services_image caddy_image data_device
#
# Read by every other script in hosted/deploy/ through waku_load_install_env,
# and by `docker compose --env-file`, which is what makes ${WAKU_ROOT} and the
# image tags resolve inside compose.yaml.
waku_install_env() {
  cat <<EOF
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
}

# Reads: restic_repository restic_password_file
#
# THE FOURTH FILE, AND THE ONLY ONE NO SERVICE READS. config/backup.env is
# root's: backup.sh and restore.sh source it through waku_load_backup_env and
# hand it to restic through the environment. It is a body here rather than a
# heredoc in install.sh for the same reason as the other four -- so a test can
# run the writer and the reader against each other instead of reading either.
#
# THE OBJECT STORE'S CREDENTIALS ARE NOT HERE. install.sh has no flag for
# them: the operator appends AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY to
# this file by hand, backup.env.example shows the shape, and waku_write_config
# never overwrites, so a rerun keeps what they added.
waku_backup_env() {
  cat <<EOF
RESTIC_REPOSITORY=$restic_repository
RESTIC_PASSWORD_FILE=$restic_password_file
EOF
}
