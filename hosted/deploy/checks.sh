#!/usr/bin/env bash
# Pure checks: no side effects, no network, every input a path or a string.
#
# SOURCED, NEVER RUN. install.sh sources this file and so does
# evals/deterministic/hosted/test_install_sh.py, which is the whole reason the
# six functions take a path instead of reading /proc/meminfo themselves: a
# function that reads a fixed path can only be tested on a machine that has it,
# and macOS has no /proc at all.
#
# EVERY ONE OF THEM IS DEFAULT-DENY. A JWKS document with no `keys` array, a
# settings document with no `disable_signup` field, a /proc/mounts line for a
# different mount point: all of them exit non-zero. An installer that treats
# "I could not tell" as "yes" is how a VM ends up serving with open signup.
#
# Written to parse under bash 3.2 (the macOS default), like
# hosted/image/build.sh, so `bash -n` on a maintainer's laptop is a real check.
# It RUNS on Ubuntu 24.04's bash 5.2.

# (VM memory - 2 GB) / 150 MB, the spec's formula, floored at 1.
# 16 GB of MemTotal gives 95, which is the number the spec names.
waku_max_running() {
  awk '/^MemTotal:/ { kb = $2 }
       END { if (kb == 0) exit 1
             n = int((kb * 1024 - 2 * 1024 * 1024 * 1024) / (150 * 1024 * 1024))
             if (n < 1) n = 1
             print n }' "$1"
}

# The real upstream resolvers, which is why the caller passes
# /run/systemd/resolve/resolv.conf and not /etc/resolv.conf: on Ubuntu 24.04
# the latter names only the stub 127.0.0.53, and a firewall rule opening DNS to
# a loopback address lets nothing through while looking as though it had.
# Loopback addresses are dropped here rather than refused, so a file that names
# the stub AND a real resolver still works.
#
# The trailing `grep .` is the default-deny: an empty result is a refusal, not
# an empty allow-list. Every caller runs under `set -o pipefail`, so a `grep`
# that matches nothing fails the whole pipeline.
waku_resolvers() {
  awk '$1 == "nameserver" { print $2 }' "$1" \
    | grep -v '^127\.' \
    | grep -v '^::1$' \
    | paste -sd, - \
    | grep .
}

# `exit 0` inside an awk rule still runs END, and an END that exits replaces the
# status -- so this sets a flag and decides once, in END. Getting that wrong
# gives a check that passes on every filesystem.
#
# The option is compared with `==` against each comma-separated field, never
# matched as a substring: `noprjquota` contains `prjquota` and means the
# opposite of it.
waku_xfs_prjquota_ok() {
  awk -v want="$2" '
    $2 == want && $3 == "xfs" {
      n = split($4, opts, ",")
      for (i = 1; i <= n; i++) if (opts[i] == "prjquota") ok = 1
    }
    END { exit (ok ? 0 : 1) }' "$1"
}

# Asymmetric only (spec, "Deploy and operate"). An HS256 project publishes an
# `oct` key, which is a shared secret: the gateway would need that secret to
# verify a token, so every tenant's browser would be one leak away from minting
# its own sign-ins.
#
# A closed set: RSA or EC, and at least one key. Anything else -- a third key
# type, a `keys` that is not an array, a document with no `keys` at all -- is a
# refusal.
waku_jwks_is_asymmetric() {
  jq -e 'if (.keys | type) == "array" and (.keys | length) > 0
         then [.keys[] | .kty] | all(. == "RSA" or . == "EC")
         else false end' "$1" >/dev/null
}

# Invite-only (design section 15: "a free tier is an abuse target"). A missing
# field reads as null, null == true is false, and jq -e exits 1 -- which is the
# default-deny this needs, and the reason the comparison is `== true` rather
# than a truthiness test. The string "true" is not true either.
waku_signup_is_closed() {
  jq -e '.disable_signup == true' "$1" >/dev/null
}

# A CLOSED SET ON THE VALUE, NOT ON THE CHARACTERS. That distinction cost a
# finding: an earlier shape refused everything that was not a digit string and
# so accepted `0`, and `--tenant-disk 0` writes WAKU_TENANT_DISK_BYTES=0, which
# hosted/spawner/xfsquota.py turns into `xfs_quota -c 'limit -p bhard=0'` --
# AND bhard=0 MEANS NO LIMIT IN XFS. A flag that reads as "the smallest
# possible quota" produced an unbounded one, silently, with none of the
# WAKU_DATA_DEVICE=none warnings to say so. That is the exact outcome the XFS
# preflight refusal exists to prevent, reached through the one flag whose
# comment called itself a closed set.
#
# What is accepted, and nothing else: a decimal number from 1 upwards with no
# leading zero, optionally followed by one K, M or G, whose value in bytes
# fits in the arithmetic that computes it. No "0", no "0G", no "010G" (bash
# reads a leading zero as octal), no "1.5G", no "1GB", no "1T", no sign, no
# space, and no magnitude that wraps.
waku_bytes() {
  local value number unit result
  value=$1
  case "$value" in
    *G|*g) number=${value%?}; unit=1073741824 ;;
    *M|*m) number=${value%?}; unit=1048576 ;;
    *K|*k) number=${value%?}; unit=1024 ;;
    *)     number=$value;     unit=1 ;;
  esac
  case "$number" in
    ''|0*|*[!0-9]*) return 1 ;;
  esac
  # 19 digits or more cannot be compared or multiplied here without wrapping,
  # so it is refused before any arithmetic touches it.
  case "$number" in
    ???????????????????*) return 1 ;;
  esac
  result=$((number * unit))
  # The overflow guard. `*[!0-9]*` constrains the characters and says nothing
  # about the magnitude: 9999999999999G used to come back as
  # 1413189099967217664. A product that does not divide back has wrapped.
  [ $((result / unit)) -eq "$number" ] || return 1
  [ "$result" -gt 0 ] || return 1
  echo "$result"
}

# One NAME=VALUE line for an env_file, as a closed set.
#
# Compose reads config/caddy.env line by line. A line with no `=` is accepted
# by Compose and leaves the variable UNSET -- measured against a real
# `docker compose config` -- so an operator who mistyped a credential comes to
# believe they set a value they did not, which is the sentence written above
# install.sh's flag parser. A newline inside a value writes a second variable
# nobody asked for.
#
# NAME: a letter or underscore, then letters, digits and underscores.
# VALUE: one or more printable, non-space characters, IN THE C LOCALE. The
# locale matters: a bracket expression follows LC_CTYPE, so under a UTF-8
# login shell -- which a root shell on Ubuntu 24.04 commonly has -- multibyte
# characters count as printable and the "closed" set quietly widens. The
# subshell pins it for the length of the test and nothing else.
waku_env_pair_ok() {
  local pair name value
  pair=$1
  case "$pair" in
    [A-Za-z_]*=*) : ;;
    *) return 1 ;;
  esac
  name=${pair%%=*}
  value=${pair#*=}
  ( LC_ALL=C
    case "$name" in *[![:alnum:]_]*) exit 1 ;; esac
    case "$value" in ''|*[![:graph:]]*) exit 1 ;; esac
    exit 0 )
}
