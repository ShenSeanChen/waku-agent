#!/usr/bin/env bash
# Upgrade this VM to the newest commit on main once CI has passed on it. Run
# by waku-autodeploy.timer every five minutes; off until an operator runs
# `autodeploy.sh --enable` once.
#
# usage: autodeploy.sh             one tick: decide, deploy at most one commit
#        autodeploy.sh --enable    install and start the timer
#        autodeploy.sh --disable   stop the timer
#        autodeploy.sh --status    what is deployed, what failed, whether paused
#
# ONE TICK, IN ORDER, and every step that says no exits before the next:
#
#   1. the kill switch file exists           -> log "paused", exit 0
#   2. another upgrade holds the lock        -> log, exit 0
#   3. git ls-remote reads main's SHA, ONCE. That SHA, not a branch name, is
#      what gets deployed: passing origin/main to upgrade.sh would deploy
#      whatever landed between the check and the fetch
#   4. main equals the last deployed SHA, or is in the failed list -> exit 0
#   5. main does not descend from the last deployed SHA -> refuse, record it.
#      A force-push or a rewritten history is an operator's call, never the
#      timer's
#   6. every required check passed on that exact SHA (GitHub's public API, no
#      token). Pending or missing -> exit 0 and look again next tick. Failed
#      or cancelled -> record it and never look again
#   7. the commit carries GitHub's own verified signature, which a squash-merge
#      made in the GitHub UI always does -> otherwise refuse, record it
#   8. write run/deploy/releases/<sha>.json: the commit, the time, and each
#      required check's result, link and test counts, for the Evals page.
#      Never a reason to stop: a deploy without a record still deploys
#   9. upgrade.sh --ref <sha>, then the gateway directly AND https://<domain>/login
#      must both answer 200
#  10. on any failure, upgrade.sh --ref <last deployed>, the same two checks,
#      and the SHA joins the failed list. If THAT fails too, this script writes
#      the kill switch itself: a timer that rebuilt a broken VM every five
#      minutes would make the outage harder to read, not shorter
#
# THE LAST DEPLOYED SHA IS A FILE, NOT HEAD. upgrade.sh moves the checkout
# before it builds anything, so after a failed build HEAD is the new commit
# while the old images still serve. A script that compared main with HEAD
# would decide it was up to date and never retry, or never roll back.
#
# THIS SCRIPT LIVES IN THE CHECKOUT IT MOVES. Everything runs inside main(),
# which bash reads whole before running it, so upgrade.sh rewriting this file
# mid-tick cannot change what the running tick does. The rollback runs the
# upgrade.sh of the commit that failed; if that file is what broke, the
# rollback fails and the kill switch stops the timer, which is the safe end.
#
# AN UPGRADE CAN CUT OFF A CHAT TURN IN PROGRESS. upgrade.sh recreates the
# gateway when its image changed, and every turn streams through it. Waiting
# for idle tenants is deliberately not in this version.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

usage="usage: autodeploy.sh [--enable | --disable | --status]"

# A CONSTANT, NOT A SETTING. These are the two jobs that gate a merge into
# main. A check named here that stops running leaves every commit "pending"
# and nothing deploys, which is the safe direction to drift in.
required_checks="skills-and-evals hosted-docker"

is_sha() {
  ( LC_ALL=C
    case "$1" in *[!0-9a-f]*) exit 1 ;; esac
    [ "${#1}" -eq 40 ] )
}

# owner/name from the checkout's own origin, so an operator running their own
# fork deploys their own main. A CLOSED SET: the value lands in two URLs, and
# anything but github.com over https is refused rather than guessed at.
github_repo() {
  local url
  url=$(git -C "$WAKU_SRC" remote get-url origin) \
    || waku_die "$WAKU_SRC has no origin remote. Automatic upgrades follow origin's main on GitHub."
  url=${url%.git}
  repo=${url#https://github.com/}
  ( LC_ALL=C
    [ "$repo" != "$url" ] || exit 1
    case "$repo" in */*/*|/*|*/|*[!A-Za-z0-9._/-]*) exit 1 ;; esac
    case "$repo" in */*) ;; *) exit 1 ;; esac ) \
    || waku_die "origin is $url. Automatic upgrades read CI results from GitHub's public API, so origin must be https://github.com/<owner>/<repo>."
}

# EVERY NETWORK CALL HAS A TIMEOUT. The timer unit bounds the whole run too,
# but a GitHub request that hangs would hold the deploy lock until then and
# block a manual upgrade with it.
github_api() {
  curl -fsS --max-time 20 \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$repo/$1"
}

already_failed() {
  [ -f "$state/failed" ] && awk -v sha="$1" '$1 == sha { found = 1 } END { exit !found }' "$state/failed"
}

record_failed() {
  printf '%s %s %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" >>"$state/failed"
}

# BOTH ADDRESSES, because they fail differently. upgrade.sh probes the
# gateway over plain HTTP on its internal address, and every upgrade also
# rebuilds Caddy -- a Caddy that came up without its certificate or its DNS
# module passes that probe and serves nobody. About a minute of retries, since
# the services have only just restarted.
healthy() {
  local i gateway public
  i=0
  while [ "$i" -lt 30 ]; do
    gateway=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' \
      -H "Host: $WAKU_DOMAIN" "http://$WAKU_GATEWAY_ADDRESS/login" 2>/dev/null) || true
    public=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' \
      "https://$WAKU_DOMAIN/login" 2>/dev/null) || true
    if [ "$gateway" = 200 ] && [ "$public" = 200 ]; then
      return 0
    fi
    sleep 2
    i=$((i + 1))
  done
  waku_log "health check failed: the gateway answered ${gateway:-nothing} and https://$WAKU_DOMAIN/login answered ${public:-nothing}; both must be 200"
  return 1
}

# THE RELEASE RECORD the Evals page shows (waku/ops/observability.py,
# release_info). upgrade.sh finds it by SHA and build.sh bakes it into the
# tenant image, so every tenant container started from that image can say
# which commit it runs and what CI passed on it.
#
# EVERY FIELD IS READ FROM GITHUB, NONE IS TYPED HERE. Names, conclusions and
# links come from the check runs this tick already judged. The counts come
# from a notice each CI job writes -- title "Deterministic evals", message
# "3094 passed, 0 failed, 2 skipped" -- read back through the public
# annotations API (.github/workflows/validate-skills.yml); a check
# with no such notice records "tests": null and the page says no count was
# recorded. Nothing is ever filled in to look complete.
#
# NEVER FATAL. It returns 1 on any failure and the caller logs it and deploys
# anyway: the record describes a release, it does not gate one.
record_release() {
  local sha run id annotations tests checks name dir
  sha=$1
  checks='[]'
  for name in $required_checks; do
    run=$(printf '%s' "$2" | jq -c --arg name "$name" \
      '[.check_runs[] | select(.name == $name)] | max_by(.id)') || return 1
    id=$(printf '%s' "$run" | jq -r '.id') || return 1
    case "$id" in ''|*[!0-9]*) return 1 ;; esac
    tests=null
    if annotations=$(github_api "check-runs/$id/annotations?per_page=100"); then
      tests=$(printf '%s' "$annotations" | jq -c '
        if type != "array" then null else
          [ .[] | select(.annotation_level == "notice")
            | .title as $label
            | ((.message // "") | capture("^(?<passed>[0-9]+) passed, (?<failed>[0-9]+) failed, (?<skipped>[0-9]+) skipped$"))
            | {label: $label, passed: (.passed | tonumber),
               failed: (.failed | tonumber), skipped: (.skipped | tonumber)} ]
          | first
        end') || tests=null
    fi
    checks=$(printf '%s' "$checks" | jq -c --argjson run "$run" --argjson tests "${tests:-null}" \
      '. + [{name: $run.name, conclusion: $run.conclusion, url: $run.html_url,
             completed_at: $run.completed_at, tests: $tests}]') || return 1
  done
  dir="$state/releases"
  mkdir -p "$dir" || return 1
  jq -nc --arg sha "$sha" --arg repo "$repo" --argjson checks "$checks" \
    --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{sha: $sha, commit_url: "https://github.com/\($repo)/commit/\($sha)",
      deployed_at: $now, checks: $checks}' >"$dir/$sha.json.tmp" || return 1
  mv -f "$dir/$sha.json.tmp" "$dir/$sha.json"
}

# A ROLLBACK REDEPLOYS AN OLDER COMMIT, so its record's time moves to now. Its
# checks are the ones that let it ship the first time and do not change.
restamp_release() {
  local file
  file="$state/releases/$1.json"
  [ -f "$file" ] || return 0
  jq -c --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.deployed_at = $now' "$file" >"$file.tmp" \
    && mv -f "$file.tmp" "$file"
}

upgrade_to() {
  WAKU_DEPLOY_LOCK_HELD=yes "$WAKU_SRC/hosted/deploy/upgrade.sh" --ref "$1"
}

tick() {
  local deployed target listing porcelain runs verdict commit verified

  if [ -e "$kill_switch" ]; then
    waku_log "paused: $kill_switch exists. Remove it to resume automatic upgrades."
    return 0
  fi
  if ! waku_flock_deploy; then
    waku_log "skipped: another upgrade holds $state/lock"
    return 0
  fi

  [ -s "$state/deployed" ] \
    || waku_die "$state/deployed does not exist, so there is no known-good commit to roll back to. Run autodeploy.sh --enable once; it records the commit this VM is running."
  deployed=$(cat "$state/deployed")
  is_sha "$deployed" || waku_die "$state/deployed does not hold a commit SHA."

  github_repo
  listing=$(timeout 60 git ls-remote "https://github.com/$repo" refs/heads/main) \
    || { waku_log "could not read main from github.com/$repo; trying again next tick"; return 0; }
  target=${listing%%[[:space:]]*}
  is_sha "$target" \
    || waku_die "git ls-remote answered something that is not a commit SHA for main: $listing"

  if [ "$target" = "$deployed" ]; then
    waku_log "up to date at $target"
    return 0
  fi
  if already_failed "$target"; then
    waku_log "main is $target, which is in $state/failed; waiting for a newer commit"
    return 0
  fi

  porcelain=$(git -C "$WAKU_SRC" status --porcelain) \
    || waku_die "git status failed in $WAKU_SRC."
  if [ -n "$porcelain" ]; then
    waku_log "not deploying $target: $WAKU_SRC has uncommitted changes, and upgrade.sh refuses a dirty checkout"
    return 0
  fi

  timeout 300 git -C "$WAKU_SRC" fetch --quiet origin main \
    || { waku_log "could not fetch origin main; trying again next tick"; return 0; }
  if ! git -C "$WAKU_SRC" cat-file -e "$target^{commit}" 2>/dev/null; then
    waku_log "main is $target but the fetch did not bring it; trying again next tick"
    return 0
  fi
  if ! git -C "$WAKU_SRC" merge-base --is-ancestor "$deployed" "$target"; then
    waku_log "REFUSED: main $target does not descend from the deployed $deployed, so main was force-pushed or rewritten. The timer never deploys that; run upgrade.sh --ref by hand if it is intended."
    record_failed "$target" "not-a-descendant-of-$deployed"
    return 0
  fi

  runs=$(github_api "commits/$target/check-runs?per_page=100") \
    || { waku_log "could not read the check runs for $target; trying again next tick"; return 0; }
  # The newest run of each name decides, so a re-run that went green counts.
  # "pending" covers a check that has not started yet and is not listed.
  verdict=$(printf '%s' "$runs" | jq -r --arg names "$required_checks" '
    if (.check_runs | type) != "array" then "error" else
      [ ($names | split(" "))[] as $name
        | ([.check_runs[] | select(.name == $name)] | max_by(.id)) as $run
        | if $run == null then "pending"
          elif $run.status != "completed" then "pending"
          elif $run.conclusion == "success" then "success"
          else "failed: \($name) \($run.conclusion)" end ]
      | ((map(select(startswith("failed"))) | first)
         // (if all(. == "success") then "success" else "pending" end))
    end') || verdict=error
  case "$verdict" in
    success) ;;
    pending)
      waku_log "waiting: the checks on $target have not all passed yet ($required_checks)"
      return 0 ;;
    failed:*)
      waku_log "not deploying $target: ${verdict#failed: }"
      record_failed "$target" "check-${verdict#failed: }"
      return 0 ;;
    *)
      waku_log "could not read the check runs for $target from GitHub's answer; trying again next tick"
      return 0 ;;
  esac

  commit=$(github_api "commits/$target") \
    || { waku_log "could not read commit $target from GitHub; trying again next tick"; return 0; }
  verified=$(printf '%s' "$commit" | jq -r --arg sha "$target" '
    if .sha == $sha and .commit.verification.verified == true then "yes" else "no" end') \
    || verified=no
  if [ "$verified" != yes ]; then
    waku_log "REFUSED: $target is not a GitHub-verified commit. A merge made on GitHub always is; this one reached main some other way."
    record_failed "$target" "unverified"
    return 0
  fi

  if record_release "$target" "$runs"; then
    waku_log "recorded the release $target in $state/releases"
  else
    waku_log "could not write the release record for $target; deploying anyway, and the Evals page will show no release"
  fi

  waku_log "deploying $target (was $deployed)"
  if upgrade_to "$target" && healthy; then
    printf '%s\n' "$target" >"$state/deployed.tmp"
    mv -f "$state/deployed.tmp" "$state/deployed"
    waku_log "deployed $target"
    return 0
  fi

  waku_log "FAILED: $target did not come up healthy; rolling back to $deployed"
  restamp_release "$deployed" || true
  if upgrade_to "$deployed" && healthy; then
    record_failed "$target" "unhealthy-rolled-back"
    waku_log "rolled back to $deployed. $target will not be retried; the next commit on main will."
    return 1
  fi

  printf 'written %s by autodeploy.sh: upgrading to %s failed and rolling back to %s failed too.\nRemove this file to resume automatic upgrades, once the VM serves again.\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$target" "$deployed" >"$kill_switch"
  waku_log "ROLLBACK FAILED: upgrading to $target failed and rolling back to $deployed failed too. Wrote $kill_switch, so automatic upgrades are paused. The site may be down: run upgrade.sh --ref $deployed by hand and read docker compose logs."
  return 1
}

enable() {
  local unit_dir head
  unit_dir=${WAKU_SYSTEMD_DIR:-/etc/systemd/system}
  # sed uses | as its delimiter below; the same guard install.sh puts in front
  # of the backup unit.
  case "$here$WAKU_INSTALL_ENV" in
    *[\|\&\\]*) waku_die "the checkout path $here or the config path $WAKU_INSTALL_ENV contains a character this script cannot substitute into the systemd unit safely." ;;
  esac
  github_repo
  mkdir -p "$state"
  chmod 0700 "$state"
  # THE COMMIT RUNNING NOW BECOMES THE KNOWN-GOOD ONE, and only the first time:
  # it is what a failed deploy rolls back to. Run upgrade.sh by hand first so
  # that HEAD is really what serves.
  if [ -s "$state/deployed" ]; then
    waku_log "keeping $state/deployed: $(cat "$state/deployed")"
  else
    head=$(git -C "$WAKU_SRC" rev-parse HEAD)
    is_sha "$head" || waku_die "git rev-parse HEAD in $WAKU_SRC did not answer a commit SHA."
    printf '%s\n' "$head" >"$state/deployed"
    waku_log "recorded $head, the checkout's HEAD, as the deployed commit"
  fi
  sed -e "s|@WAKU_AUTODEPLOY@|$here/autodeploy.sh|g" \
      -e "s|@WAKU_INSTALL_ENV@|$WAKU_INSTALL_ENV|g" \
      "$here/waku-autodeploy.service" >"$unit_dir/waku-autodeploy.service"
  cat "$here/waku-autodeploy.timer" >"$unit_dir/waku-autodeploy.timer"
  chmod 0644 "$unit_dir/waku-autodeploy.service" "$unit_dir/waku-autodeploy.timer"
  systemctl daemon-reload
  systemctl enable --now waku-autodeploy.timer
  waku_log "automatic upgrades enabled, following main on github.com/$repo. Logs: journalctl -u waku-autodeploy. Pause: touch $kill_switch"
}

status() {
  printf 'deployed:    %s\n' "$(cat "$state/deployed" 2>/dev/null || echo 'none (run --enable)')"
  if [ -e "$kill_switch" ]; then
    printf 'paused:      yes, %s exists\n' "$kill_switch"
  else
    printf 'paused:      no\n'
  fi
  printf 'failed:\n'
  if [ -s "$state/failed" ]; then
    tail -n 10 "$state/failed"
  else
    printf '  none\n'
  fi
  systemctl list-timers waku-autodeploy.timer --no-pager || true
}

main() {
  local mode
  mode=tick
  case "${1:-}" in
    "") ;;
    --enable|--disable|--status) mode=${1#--} ;;
    -h|--help) echo "$usage"; exit 0 ;;
    *) waku_die "unknown argument: $1 ($usage)" ;;
  esac
  [ $# -le 1 ] || waku_die "one argument at most ($usage)"

  waku_require_root
  waku_load_install_env WAKU_GATEWAY_ADDRESS
  state="$WAKU_ROOT/run/deploy"
  kill_switch="$WAKU_ROOT/config/autodeploy.off"

  case "$mode" in
    tick) tick ;;
    enable) enable ;;
    disable)
      systemctl disable --now waku-autodeploy.timer
      waku_log "automatic upgrades disabled. autodeploy.sh --enable turns them back on." ;;
    status) status ;;
  esac
}

main "$@"
exit
