#!/usr/bin/env bash
# Pure checks: no side effects, no network, every input a path or a string.
#
# SOURCED, NEVER RUN. install.sh sources this file and so does
# evals/deterministic/hosted/test_install_sh.py, which is the whole reason the
# first six functions take a path instead of reading /proc/meminfo themselves:
# a function that reads a fixed path can only be tested on a machine that has
# it, and macOS has no /proc at all.
#
# EVERY ONE OF THEM IS DEFAULT-DENY. A JWKS document with no `keys` array, a
# settings document with no `disable_signup` field, a /proc/mounts line for a
# different mount point: all of them exit non-zero. An installer that treats
# "I could not tell" as "yes" is how a VM ends up serving with open signup.
#
# THE SEVENTH, waku_ports_free_or_ours, CANNOT TAKE A PATH, and that is a
# deliberate break from the other six, not an oversight. What it decides --
# whether :80 and :443 are free, or held by THIS deployment's own Caddy
# container rather than something else -- is not in any file: it is live
# process and container state, read through `ss` and `docker` on PATH. Tests
# reach it the way test_install_sh.py already reaches docker, curl and
# apt-get: stub executables placed first on PATH. It is still default-deny --
# no listener passes, our own container's compose labels pass, and everything
# else, including "the tool to check is missing", is a refusal.
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
  # `0*` is what refuses 0 and 0G, and it also refuses 010G, which bash would
  # otherwise read as octal.
  case "$number" in
    ''|0*|*[!0-9]*) return 1 ;;
  esac
  result=$((number * unit))
  # The overflow guard, and the ONLY one: `*[!0-9]*` constrains the characters
  # and says nothing about the magnitude, so 9999999999999G used to come back
  # as 1413189099967217664. A product that does not divide back has wrapped.
  #
  # Two more checks stood here and both are gone, because both were dead. A
  # digit-count bound was redundant with this line (measured: every input
  # 19 digits and longer is refused by the division either way), and a
  # `[ "$result" -gt 0 ]` was unreachable once `0*` refuses a zero numerator --
  # a product that wrapped to zero or below fails the division first. A line
  # that reads as a guard and can never fire is worse than no line, because the
  # next reader trusts it.
  [ $((result / unit)) -eq "$number" ] || return 1
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

# The NAME half of a NAME=VALUE line, printed only when it is safe to print.
#
# A REFUSAL ABOUT A CREDENTIAL MUST NAME THE FILE AND THE LINE, NEVER THE LINE'S
# CONTENT. This exists so that install.sh can still say something useful about
# which setting is wrong: a variable's NAME is not a secret, its VALUE is. The
# name comes back only when it passes the same closed set waku_env_pair_ok
# applies to it -- if the line has no `=`, or the part before the `=` is not a
# name, there is nothing here that can be shown and this prints nothing, so the
# caller falls back to the position alone.
waku_env_pair_name() {
  local name
  case "$1" in
    [A-Za-z_]*=*) name=${1%%=*} ;;
    *) return 1 ;;
  esac
  ( LC_ALL=C; case "$name" in *[![:alnum:]_]*) exit 1 ;; esac ) || return 1
  printf '%s\n' "$name"
}

# A hostname, as a CLOSED SET ON THE VALUE.
#
# `*.*` was the whole of this check and it is not a check: `.`, `..`, `a..b`,
# `a b.c`, `*.waku.one` and `http://a.b` all satisfy it, and each of them then
# fails at Caddy or at ACME -- AFTER the VM is built, which is precisely what a
# preflight exists to prevent.
#
# What is accepted: two or more labels separated by single dots; each label
# 1 to 63 characters of lowercase letters, digits and hyphens, not beginning or
# ending with a hyphen; 253 characters in total at most; and a last label of at
# least two characters that is not all digits, which is what stops an IPv4
# address. No leading or trailing dot, no empty label, no wildcard, no scheme,
# no space, no upper case -- every one of those falls out of the set rather
# than being listed as a way to be wrong.
#
# LOWERCASE ONLY, deliberately. hosted/gateway/config.py lowercases
# WAKU_APEX_HOST when it reads it, so a mixed-case value would leave
# config/install.env saying one thing and the running gateway using another,
# and the operator reading the file would be reading the wrong answer.
waku_is_hostname() {
  local name
  name=$1
  case "$name" in
    ''|*[!a-z0-9.-]*) return 1 ;;
    .*|*.|*..*) return 1 ;;
    *.*) : ;;
    *) return 1 ;;
  esac
  [ ${#name} -le 253 ] || return 1
  ( LC_ALL=C
    IFS=.
    set -f
    last=""
    for label in $name; do
      case "$label" in
        -*|*-) exit 1 ;;
      esac
      [ ${#label} -le 63 ] || exit 1
      last=$label
    done
    [ ${#last} -ge 2 ] || exit 1
    case "$last" in
      *[!0-9]*) exit 0 ;;
      *) exit 1 ;;
    esac )
}

# :80 and :443, as A CLOSED SET WITH DEFAULT-DENY: no listener -> pass; our own
# compose project's caddy container -> pass; anything else, including "the
# tool needed to tell is missing" -> refuse.
#
# WHY NOT "THE PORT IS FREE": that breaks idempotence, which is F1's own
# recorded check -- a rerun changes nothing. On a rerun THIS DEPLOYMENT'S OWN
# Caddy container correctly holds both ports, so a naive free-port guard would
# refuse the install it is meant to protect on every run after the first.
#
# WHY NOT A PROCESS NAME: a hand-built `caddy` binary and this deployment's
# container both answer to `caddy` in `ps`, so a name match would pass on a
# VM that already has one holding the ports -- which is exactly the collision
# this exists to catch. "Ours" comes from Docker instead: the compose PROJECT
# and SERVICE labels Compose stamps on every container it starts
# (com.docker.compose.project, com.docker.compose.service; compose.yaml names
# this deployment's project "waku" and its Caddy service "caddy"). Caddy runs
# with `network_mode: host` (compose.yaml), so its process is not hidden
# behind a container network namespace -- the pid `ss` reports on the host IS
# the container's own process, with no indirection to undo.
#
# THE PORT-LISTING TOOL IS `ss`, iproute2's, the one Ubuntu 24.04 ships by
# default (iproute2 is Priority: important on every Ubuntu server image) --
# not `lsof` (its own package, not installed by default) and not `netstat`
# (net-tools, also not installed by default on 24.04). Either would be a new
# default dependency for a script AGENTS.md hard rule 3 forbids one in.
# `ss -H -tlnp`: TCP, listening, numeric, with the process that owns each
# socket -- `-H` drops the header line, and the state column is still checked
# below rather than trusted, in case a given `ss` build does not honour it.
# ABSENT IS A REFUSAL, NOT A PASS. `command -v ss` failing, or `ss` itself
# failing, exits this function non-zero with a message that says so -- the
# same "I could not tell" default-deny as every check above it.
#
# STDOUT ON A REFUSAL ONLY: one line per foreign listener, naming the port,
# the pid, the process, and the container when it is one and is not ours --
# so install.sh's refusal can say what holds the port without a second call
# into ss or docker, which is the whole reason waku_die's messages in this
# file never say only "refused": an operator told just "port in use" goes and
# runs `ss` themselves. Silent on success, like the other six.
#
# ONE FUNCTION, NOT A waku_env_pair_ok/waku_env_pair_name PAIR, and that is
# deliberate: splitting the decision from the message would mean asking `ss`
# and `docker` twice for the same answer, and a container that stopped or
# started between the two calls would make the message describe a machine
# that no longer exists. Reading it once and deciding from that one read
# cannot disagree with itself.
#
# $@ is the list of ports to check, given as plain decimal strings (install.sh
# passes 80 443); each row `ss` reports for a port not in that list is
# ignored, so this can be called with one port in a test and both in
# install.sh without duplicating the logic.
#
# ONE PROCESS PER SOCKET IS ASSUMED. SO_REUSEPORT letting two processes share
# one listening socket is not a shape this deployment's Caddy, or the
# hand-built one it replaces, produces, so only one pid is read per row.
waku_ports_free_or_ours() {
  local ss_output line state localaddr port want p name pid \
        container project service cname conflicts
  conflicts=""

  if ! command -v ss >/dev/null 2>&1; then
    printf 'ss is not on PATH. Nothing here can tell whether :%s already has a listener, and treating "cannot tell" as "free" is exactly the mistake this check exists to refuse.\n' "$*"
    return 1
  fi
  if ! ss_output=$(ss -H -tlnp 2>/dev/null); then
    printf 'ss -H -tlnp failed to run. Nothing here can tell whether :%s already has a listener, and treating "cannot tell" as "free" is exactly the mistake this check exists to refuse.\n' "$*"
    return 1
  fi

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    state=$(printf '%s' "$line" | awk '{print $1}')
    [ "$state" = LISTEN ] || continue
    localaddr=$(printf '%s' "$line" | awk '{print $4}')
    port=${localaddr##*:}
    want=no
    for p in "$@"; do
      [ "$port" = "$p" ] && want=yes
    done
    [ "$want" = yes ] || continue

    # users:(("caddy",pid=2345,fd=12)) -- the name and pid of the (assumed
    # one) process behind this socket.
    name=$(printf '%s' "$line" | sed -n 's/.*"\([^"]*\)",pid=\([0-9]*\).*/\1/p')
    pid=$(printf '%s' "$line" | sed -n 's/.*"\([^"]*\)",pid=\([0-9]*\).*/\2/p')
    if [ -z "$pid" ]; then
      conflicts="${conflicts}:$port has a listener ss did not report a pid for -- ss said: $line
"
      continue
    fi

    # Every running container in one call, matched on its own pid -- not one
    # `docker inspect` per container, which is what a VM with many tenant
    # containers running would otherwise pay for on every install rerun.
    container=""
    if command -v docker >/dev/null 2>&1; then
      container=$(docker ps -q 2>/dev/null \
        | xargs -r docker inspect --format \
          '{{.State.Pid}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.Name}}' \
          2>/dev/null \
        | awk -F'|' -v want="$pid" '$1 == want { print; exit }')
    fi

    project="" service="" cname=""
    if [ -n "$container" ]; then
      project=$(printf '%s' "$container" | awk -F'|' '{print $2}')
      service=$(printf '%s' "$container" | awk -F'|' '{print $3}')
      cname=$(printf '%s' "$container" | awk -F'|' '{print $4}')
      cname=${cname#/}
    fi

    # THE WHOLE FINDING, IN ONE LINE: matched on the compose project and
    # service labels, never on $name, which is `caddy` for a hand-built
    # binary just as often as for this deployment's own container.
    if [ "$project" = waku ] && [ "$service" = caddy ]; then
      continue
    fi

    if [ -n "$cname" ]; then
      conflicts="${conflicts}:$port is held by pid $pid ($name), container $cname
"
    else
      conflicts="${conflicts}:$port is held by pid $pid ($name)
"
    fi
  done <<PORTLIST
$ss_output
PORTLIST

  [ -z "$conflicts" ] || { printf '%s' "$conflicts"; return 1; }
  return 0
}
