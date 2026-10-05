"""autodeploy.sh decides whether to upgrade the VM, and every way it says no.

THE WORLD IS STUBBED, THE DECISIONS ARE REAL. git, curl, flock, timeout,
systemctl and the checkout's upgrade.sh are fakes on PATH or in a fake
checkout; bash, jq, awk and sed are the real ones, because the jq program that
reads GitHub's check runs is exactly the thing that can be wrong.

Every guard in the script's header has a case here that turns red when the
guard is removed. The fake upgrade.sh writes the ref it was given to
$FIX/current, and the fake curl answers each health probe from
$FIX/health_<that ref>, so a test can make one commit unhealthy and the
rollback target healthy.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path

import pytest
import shelllib

from scripts.ci_test_counts import notice
from waku.ops.observability import release_info

AUTODEPLOY = shelllib.DEPLOY / "autodeploy.sh"

OLD = "a" * 40
NEW = "b" * 40

_ID_ROOT = "#!/bin/sh\necho 0\n"

_GIT = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"remote get-url origin"*) echo "${GIT_ORIGIN:-https://github.com/ShenSeanChen/waku-agent.git}" ;;
  *"ls-remote"*) [ "${LS_REMOTE_FAILS:-}" = yes ] && exit 128
                 printf '%s\\trefs/heads/main\\n' "$MAIN_SHA" ;;
  *"status --porcelain"*) [ -n "${GIT_DIRTY:-}" ] && echo " M hosted/README.md" ;;
  *"merge-base --is-ancestor"*) exit "${GIT_ANCESTOR_EXIT:-0}" ;;
  *"rev-parse HEAD"*) echo "$HEAD_SHA" ;;
esac
exit 0
"""

_TIMEOUT = """#!/bin/sh
printf '%s %s\\n' timeout "$*" >> "$WAKU_CALLS"
shift
exec "$@"
"""

_FLOCK = """#!/bin/sh
printf '%s %s\\n' flock "$*" >> "$WAKU_CALLS"
exit "${FLOCK_EXIT:-0}"
"""

# The last argument is the URL. Health probes answer from the file named for
# the ref the fake upgrade.sh last moved to; 200 200 when there is none.
_CURL = """#!/bin/sh
printf '%s %s\\n' curl "$*" >> "$WAKU_CALLS"
for url in "$@"; do :; done
current=$(cat "$FIX/current" 2>/dev/null || echo none)
health=$(cat "$FIX/health_$current" 2>/dev/null || echo "200 200")
case "$url" in
  *"/check-runs/"*"/annotations"*)
    [ "${ANNOTATIONS_FAIL:-}" = yes ] && exit 22
    id=${url#*/check-runs/}; id=${id%%/*}
    cat "$FIX/annotations_$id.json" 2>/dev/null || echo '[]' ;;
  *"/check-runs"*) [ -f "$FIX/checks.json" ] || exit 22; cat "$FIX/checks.json" ;;
  https://api.github.com/*) [ -f "$FIX/commit.json" ] || exit 22; cat "$FIX/commit.json" ;;
  http://*) printf '%s' "${health%% *}" ;;
  https://*) printf '%s' "${health##* }" ;;
esac
exit 0
"""

_UPGRADE = """#!/bin/sh
printf '%s %s lock=%s\\n' upgrade.sh "$*" "${WAKU_DEPLOY_LOCK_HELD:-}" >> "$WAKU_CALLS"
ref=$2
for bad in ${UPGRADE_FAILS:-}; do
  [ "$ref" = "$bad" ] && exit 1
done
echo "$ref" > "$FIX/current"
exit 0
"""


def _checks(**runs):
    """check-runs as GitHub returns them: name -> (status, conclusion)."""
    names = {"skills_and_evals": "skills-and-evals", "hosted_docker": "hosted-docker"}
    return {"total_count": len(runs),
            "check_runs": [{"id": 100 + i, "name": names.get(key, key),
                            "status": status, "conclusion": conclusion,
                            "html_url": f"https://github.com/ShenSeanChen/waku-agent/actions/runs/7/job/{100 + i}",
                            "completed_at": "2026-10-05T03:13:40Z"}
                           for i, (key, (status, conclusion)) in enumerate(runs.items())]}


GREEN = _checks(skills_and_evals=("completed", "success"),
                hosted_docker=("completed", "success"))


def _setup(tmp_path, *, deployed=OLD, main=NEW, checks=GREEN, verified=True,
           commit_sha=None, extra_env=None):
    root = tmp_path / "waku"
    (root / "config").mkdir(parents=True)
    state = root / "run" / "deploy"
    state.mkdir(parents=True)
    if deployed is not None:
        (state / "deployed").write_text(deployed + "\n", encoding="utf-8")

    src = tmp_path / "src"
    (src / "hosted" / "deploy").mkdir(parents=True)
    upgrade = src / "hosted" / "deploy" / "upgrade.sh"
    upgrade.write_text(_UPGRADE, encoding="utf-8")
    upgrade.chmod(0o755)

    fix = tmp_path / "fix"
    fix.mkdir()
    if checks is not None:
        (fix / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
    commit = {"sha": commit_sha or main,
              "commit": {"verification": {"verified": verified}}}
    (fix / "commit.json").write_text(json.dumps(commit), encoding="utf-8")

    install_env = tmp_path / "install.env"
    install_env.write_text(
        f"WAKU_ROOT='{root}'\nWAKU_SRC='{src}'\n"
        f"WAKU_COMPOSE='{src}/hosted/deploy/compose.yaml'\n"
        "WAKU_DOMAIN='agent.example.test'\nWAKU_GATEWAY_ADDRESS='127.0.0.1:8787'\n",
        encoding="utf-8")
    env = {"WAKU_INSTALL_ENV": str(install_env), "FIX": str(fix),
           "MAIN_SHA": main, "HEAD_SHA": NEW}
    env.update(extra_env or {})
    return env


def _tick(tmp_path, env, args=()):
    return shelllib.run(AUTODEPLOY, list(args), tmp_path=tmp_path, env=env,
                        stubs=["git", "curl", "flock", "timeout", "sleep", "id",
                               "systemctl"],
                        bodies={"git": _GIT, "curl": _CURL, "flock": _FLOCK,
                                "timeout": _TIMEOUT, "id": _ID_ROOT})


def _state(tmp_path, name):
    path = tmp_path / "waku" / "run" / "deploy" / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _kill_switch(tmp_path):
    return tmp_path / "waku" / "config" / "autodeploy.off"


def _upgrades(tmp_path):
    return [line for line in shelllib.calls(tmp_path) if line.startswith("upgrade.sh")]


def _api_calls(tmp_path):
    return [line for line in shelllib.calls(tmp_path)
            if line.startswith("curl") and "api.github.com" in line]


# --- the happy path ------------------------------------------------------------


def test_a_green_verified_commit_is_deployed_and_recorded(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path))
    assert done.returncode == 0, done.stderr
    assert _upgrades(tmp_path) == [f"upgrade.sh --ref {NEW} lock=yes"]
    assert _state(tmp_path, "deployed").strip() == NEW
    assert f"deployed {NEW}" in done.stdout


def test_both_health_probes_run_after_the_upgrade(tmp_path):
    """The gateway directly AND the public https address: a Caddy rebuilt
    without its certificate passes the first and serves nobody."""
    done = _tick(tmp_path, _setup(tmp_path))
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    upgraded = next(i for i, line in enumerate(calls) if line.startswith("upgrade.sh"))
    probes = [line for line in calls[upgraded:] if line.startswith("curl")]
    assert any("http://127.0.0.1:8787/login" in line and "Host: agent.example.test" in line
               for line in probes), probes
    assert any("https://agent.example.test/login" in line for line in probes), probes


def test_every_network_call_carries_a_timeout(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path))
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    curls = [line for line in calls if line.startswith("curl")]
    assert curls
    for line in curls:
        assert "--max-time" in line, line
    for verb in ("ls-remote", "fetch"):
        assert any(line.startswith("timeout ") and f" git {verb}" in line
                   or line.startswith("timeout ") and f" {verb} " in line
                   for line in calls), (verb, calls)


def test_the_target_is_the_sha_main_had_and_never_a_branch_name(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path))
    assert done.returncode == 0, done.stderr
    for line in _upgrades(tmp_path):
        assert "origin/main" not in line
        assert f"--ref {NEW}" in line


# --- the release record the Evals page shows -----------------------------------


def _release(tmp_path, sha):
    path = tmp_path / "waku" / "run" / "deploy" / "releases" / f"{sha}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _junit(tmp_path, tests, failures, skipped):
    report = tmp_path / "junit.xml"
    report.write_text(f'<testsuites><testsuite tests="{tests}" failures="{failures}" '
                      f'errors="0" skipped="{skipped}"/></testsuites>', encoding="utf-8")
    return report


def test_a_deploy_records_each_check_its_link_and_the_counts_ci_reported(tmp_path):
    """The counts come from the notice scripts/ci_test_counts.py prints in CI,
    so this also pins the message format the two sides share."""
    env = _setup(tmp_path)
    line = notice("Deterministic evals", _junit(tmp_path, 3096, 0, 2))
    title, message = line.removeprefix("::notice title=").split("::", 1)
    (Path(env["FIX"]) / "annotations_100.json").write_text(json.dumps([
        {"annotation_level": "warning", "title": "", "message": "Node.js 20 is deprecated."},
        {"annotation_level": "notice", "title": title, "message": message}]), encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode == 0, done.stderr

    record = _release(tmp_path, NEW)
    assert record["sha"] == NEW
    assert record["commit_url"] == f"https://github.com/ShenSeanChen/waku-agent/commit/{NEW}"
    assert record["deployed_at"].endswith("Z")
    det, docker = record["checks"]
    assert det["name"] == "skills-and-evals" and det["conclusion"] == "success"
    assert det["url"].endswith("/job/100")
    assert det["tests"] == {"label": "Deterministic evals", "passed": 3094, "failed": 0, "skipped": 2}
    # hosted-docker wrote no notice here: no count, and nothing made up
    assert docker["name"] == "hosted-docker" and docker["tests"] is None

    # the page's reader accepts exactly what autodeploy writes
    shown = release_info(tmp_path / "waku" / "run" / "deploy" / "releases" / f"{NEW}.json")
    assert shown["short_sha"] == NEW[:7]
    assert shown["checks"][0]["tests"]["passed"] == 3094

    calls = shelllib.calls(tmp_path)
    recorded = max(i for i, c in enumerate(calls) if "/annotations" in c)
    upgraded = next(i for i, c in enumerate(calls) if c.startswith("upgrade.sh"))
    assert recorded < upgraded, "the record must exist before upgrade.sh builds the image"


def test_unreadable_annotations_still_deploy_with_no_counts(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"ANNOTATIONS_FAIL": "yes"}))
    assert done.returncode == 0, done.stderr
    assert _upgrades(tmp_path) == [f"upgrade.sh --ref {NEW} lock=yes"]
    assert [c["tests"] for c in _release(tmp_path, NEW)["checks"]] == [None, None]


def test_a_rollback_moves_the_old_release_time_to_now(tmp_path):
    env = _setup(tmp_path, extra_env={"UPGRADE_FAILS": NEW})
    releases = tmp_path / "waku" / "run" / "deploy" / "releases"
    releases.mkdir()
    (releases / f"{OLD}.json").write_text(json.dumps(
        {"sha": OLD, "deployed_at": "2020-01-01T00:00:00Z", "checks": []}), encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode != 0
    assert _release(tmp_path, OLD)["deployed_at"] != "2020-01-01T00:00:00Z"


def test_build_sh_bakes_the_record_into_the_tenant_image(tmp_path):
    """upgrade.sh hands the record to build.sh; build.sh passes it as one
    build argument, and the tenant image names the file it lands in."""
    record = tmp_path / "release.json"
    record.write_text('{"sha": "x"}', encoding="utf-8")
    docker = """#!/bin/sh
for argument in "$@"; do printf 'ARG %s\\n' "$argument" >> "$WAKU_CALLS"; done
"""
    build = shelllib.DEPLOY.parent / "image" / "build.sh"
    done = shelllib.run(build, ["--tenant-only", "--release-file", str(record)],
                        tmp_path=tmp_path, stubs=["docker"], bodies={"docker": docker})
    assert done.returncode == 0, done.stderr
    assert 'ARG WAKU_RELEASE_JSON={"sha": "x"}' in shelllib.calls(tmp_path)
    dockerfile = (shelllib.DEPLOY.parent / "image" / "tenant.Dockerfile").read_text(encoding="utf-8")
    assert "ARG WAKU_RELEASE_JSON" in dockerfile
    assert "ENV WAKU_RELEASE_FILE=/etc/waku/release.json" in dockerfile


# --- each guard that says no ---------------------------------------------------


def test_the_kill_switch_stops_everything(tmp_path):
    env = _setup(tmp_path)
    _kill_switch(tmp_path).write_text("", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode == 0, done.stderr
    assert "paused" in done.stdout
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls
                if line.startswith(("git", "curl", "upgrade.sh", "flock"))], calls


def test_a_held_lock_skips_the_tick(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"FLOCK_EXIT": "1"}))
    assert done.returncode == 0, done.stderr
    assert "another upgrade holds" in done.stdout
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith(("git", "curl", "upgrade.sh"))]


def test_no_known_good_commit_refuses_and_names_enable(tmp_path):
    """With no deployed file there is nothing to roll back to."""
    done = _tick(tmp_path, _setup(tmp_path, deployed=None))
    assert done.returncode != 0
    assert "--enable" in done.stderr
    assert not _upgrades(tmp_path)


def test_main_equal_to_deployed_does_nothing_and_asks_github_nothing(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, main=OLD))
    assert done.returncode == 0, done.stderr
    assert "up to date" in done.stdout
    assert not _upgrades(tmp_path)
    assert not _api_calls(tmp_path)


def test_a_sha_that_already_failed_is_not_looked_at_again(tmp_path):
    env = _setup(tmp_path)
    (tmp_path / "waku" / "run" / "deploy" / "failed").write_text(
        f"{NEW} 2026-10-03T14:00:00Z unhealthy-rolled-back\n", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode == 0, done.stderr
    assert not _upgrades(tmp_path)
    assert not _api_calls(tmp_path)


def test_a_sha_that_does_not_descend_from_deployed_is_refused(tmp_path):
    """A force-pushed main is an operator's call, never the timer's."""
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"GIT_ANCESTOR_EXIT": "1"}))
    assert done.returncode == 0, done.stderr
    assert "does not descend" in done.stdout
    assert not _upgrades(tmp_path)
    assert NEW in _state(tmp_path, "failed")
    calls = shelllib.calls(tmp_path)
    assert any(f"merge-base --is-ancestor {OLD} {NEW}" in line for line in calls)


def test_a_pending_check_waits_without_recording_anything(tmp_path):
    checks = _checks(skills_and_evals=("completed", "success"),
                     hosted_docker=("in_progress", None))
    done = _tick(tmp_path, _setup(tmp_path, checks=checks))
    assert done.returncode == 0, done.stderr
    assert "waiting" in done.stdout
    assert not _upgrades(tmp_path)
    assert _state(tmp_path, "failed") == ""
    assert _state(tmp_path, "deployed").strip() == OLD


def test_a_required_check_missing_from_the_answer_is_pending(tmp_path):
    checks = _checks(skills_and_evals=("completed", "success"))
    done = _tick(tmp_path, _setup(tmp_path, checks=checks))
    assert done.returncode == 0, done.stderr
    assert "waiting" in done.stdout
    assert not _upgrades(tmp_path)
    assert _state(tmp_path, "failed") == ""


def test_a_failed_check_is_recorded_and_never_deployed(tmp_path):
    checks = _checks(skills_and_evals=("completed", "failure"),
                     hosted_docker=("completed", "success"))
    done = _tick(tmp_path, _setup(tmp_path, checks=checks))
    assert done.returncode == 0, done.stderr
    assert not _upgrades(tmp_path)
    assert NEW in _state(tmp_path, "failed")
    assert "skills-and-evals failure" in done.stdout


def test_a_cancelled_check_is_recorded_and_never_deployed(tmp_path):
    """hosted-docker cancels a run when a newer push lands; that commit is
    skipped and the newer one deploys."""
    checks = _checks(skills_and_evals=("completed", "success"),
                     hosted_docker=("completed", "cancelled"))
    done = _tick(tmp_path, _setup(tmp_path, checks=checks))
    assert done.returncode == 0, done.stderr
    assert not _upgrades(tmp_path)
    assert NEW in _state(tmp_path, "failed")


def test_a_green_rerun_after_a_red_run_counts(tmp_path):
    """The newest run of each name decides."""
    checks = {"check_runs": [
        {"id": 1, "name": "skills-and-evals", "status": "completed", "conclusion": "failure"},
        {"id": 5, "name": "skills-and-evals", "status": "completed", "conclusion": "success"},
        {"id": 3, "name": "hosted-docker", "status": "completed", "conclusion": "success"}]}
    done = _tick(tmp_path, _setup(tmp_path, checks=checks))
    assert done.returncode == 0, done.stderr
    assert _upgrades(tmp_path) == [f"upgrade.sh --ref {NEW} lock=yes"]


def test_an_answer_without_check_runs_deploys_nothing(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, checks={"message": "API rate limit exceeded"}))
    assert done.returncode == 0, done.stderr
    assert not _upgrades(tmp_path)
    assert _state(tmp_path, "failed") == ""


def test_an_unverified_commit_is_refused(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, verified=False))
    assert done.returncode == 0, done.stderr
    assert "not a GitHub-verified commit" in done.stdout
    assert not _upgrades(tmp_path)
    assert NEW in _state(tmp_path, "failed")


def test_a_verified_answer_about_a_different_commit_is_refused(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, commit_sha="c" * 40))
    assert done.returncode == 0, done.stderr
    assert not _upgrades(tmp_path)


def test_a_dirty_checkout_is_left_alone(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"GIT_DIRTY": "yes"}))
    assert done.returncode == 0, done.stderr
    assert "uncommitted changes" in done.stdout
    assert not _upgrades(tmp_path)


@pytest.mark.parametrize("answer", ["--upload-pack=evil", "abc1234"])
def test_ls_remote_answering_anything_but_a_full_sha_is_refused(tmp_path, answer):
    done = _tick(tmp_path, _setup(tmp_path, main=answer))
    assert done.returncode != 0
    assert "not a commit SHA" in done.stderr
    assert not _upgrades(tmp_path)


@pytest.mark.parametrize("origin", ["git@example.test:x/y.git", "example.test/waku-agent",
                                    "https://github.com/x/y/z"])
def test_an_origin_that_is_not_a_github_repository_is_refused(tmp_path, origin):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"GIT_ORIGIN": origin}))
    assert done.returncode != 0
    assert "must be https://github.com/" in done.stderr
    assert not _upgrades(tmp_path)


# --- failure, rollback and the kill switch -------------------------------------


def test_a_failed_upgrade_rolls_back_and_records_the_sha(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"UPGRADE_FAILS": NEW}))
    assert done.returncode != 0
    assert _upgrades(tmp_path) == [f"upgrade.sh --ref {NEW} lock=yes",
                                   f"upgrade.sh --ref {OLD} lock=yes"]
    assert _state(tmp_path, "deployed").strip() == OLD
    assert NEW in _state(tmp_path, "failed")
    assert not _kill_switch(tmp_path).exists()


def test_an_unhealthy_public_address_rolls_back(tmp_path):
    env = _setup(tmp_path)
    (Path(env["FIX"]) / f"health_{NEW}").write_text("200 502", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode != 0
    assert _upgrades(tmp_path)[-1] == f"upgrade.sh --ref {OLD} lock=yes"
    assert _state(tmp_path, "deployed").strip() == OLD
    assert NEW in _state(tmp_path, "failed")
    assert "health check failed" in done.stdout


def test_an_unhealthy_gateway_rolls_back(tmp_path):
    env = _setup(tmp_path)
    (Path(env["FIX"]) / f"health_{NEW}").write_text("000 200", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode != 0
    assert _upgrades(tmp_path)[-1] == f"upgrade.sh --ref {OLD} lock=yes"
    assert _state(tmp_path, "deployed").strip() == OLD


def test_a_failed_rollback_writes_the_kill_switch(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path, extra_env={"UPGRADE_FAILS": f"{NEW} {OLD}"}))
    assert done.returncode != 0
    assert _kill_switch(tmp_path).exists()
    assert "ROLLBACK FAILED" in done.stdout
    assert _state(tmp_path, "deployed").strip() == OLD


def test_an_unhealthy_rollback_writes_the_kill_switch(tmp_path):
    env = _setup(tmp_path)
    (Path(env["FIX"]) / f"health_{NEW}").write_text("200 502", encoding="utf-8")
    (Path(env["FIX"]) / f"health_{OLD}").write_text("200 502", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode != 0
    assert _kill_switch(tmp_path).exists()


def test_the_tick_after_a_failed_rollback_does_nothing(tmp_path):
    env = _setup(tmp_path, extra_env={"UPGRADE_FAILS": f"{NEW} {OLD}"})
    _tick(tmp_path, env)
    (tmp_path / "calls.log").write_text("", encoding="utf-8")
    done = _tick(tmp_path, env)
    assert done.returncode == 0, done.stderr
    assert "paused" in done.stdout
    assert not _upgrades(tmp_path)


# --- enable, disable, status, arguments ----------------------------------------


def test_enable_installs_both_units_with_their_paths_substituted(tmp_path):
    env = _setup(tmp_path, deployed=None)
    units = tmp_path / "systemd"
    units.mkdir()
    done = _tick(tmp_path, {**env, "WAKU_SYSTEMD_DIR": str(units)}, ["--enable"])
    assert done.returncode == 0, done.stderr
    service = (units / "waku-autodeploy.service").read_text(encoding="utf-8")
    assert "@" not in service.replace("network-online", "")
    assert f"ExecStart={shelllib.DEPLOY}/autodeploy.sh" in service
    assert f"Environment=WAKU_INSTALL_ENV={env['WAKU_INSTALL_ENV']}" in service
    assert (units / "waku-autodeploy.timer").exists()
    calls = shelllib.calls(tmp_path)
    assert "systemctl daemon-reload" in calls
    assert "systemctl enable --now waku-autodeploy.timer" in calls


def test_enable_records_the_running_commit_once_and_never_overwrites_it(tmp_path):
    env = _setup(tmp_path, deployed=None)
    units = tmp_path / "systemd"
    units.mkdir()
    env = {**env, "WAKU_SYSTEMD_DIR": str(units)}
    assert _tick(tmp_path, env, ["--enable"]).returncode == 0
    assert _state(tmp_path, "deployed").strip() == NEW
    (tmp_path / "waku" / "run" / "deploy" / "deployed").write_text(OLD + "\n")
    assert _tick(tmp_path, env, ["--enable"]).returncode == 0
    assert _state(tmp_path, "deployed").strip() == OLD


def test_disable_stops_the_timer(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path), ["--disable"])
    assert done.returncode == 0, done.stderr
    assert "systemctl disable --now waku-autodeploy.timer" in shelllib.calls(tmp_path)


def test_status_names_the_deployed_commit_and_the_kill_switch(tmp_path):
    env = _setup(tmp_path)
    _kill_switch(tmp_path).write_text("", encoding="utf-8")
    done = _tick(tmp_path, env, ["--status"])
    assert done.returncode == 0, done.stderr
    assert OLD in done.stdout
    assert "paused:      yes" in done.stdout
    assert not _upgrades(tmp_path)


def test_an_unknown_argument_is_refused(tmp_path):
    done = _tick(tmp_path, _setup(tmp_path), ["--now"])
    assert done.returncode != 0
    assert "unknown argument: --now" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the units themselves ------------------------------------------------------


def _unit(name):
    unit = configparser.ConfigParser(strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(shelllib.DEPLOY / name)
    return unit


def test_the_service_unit_carries_placeholders_and_a_timeout():
    service = _unit("waku-autodeploy.service")["Service"]
    assert service["ExecStart"] == "@WAKU_AUTODEPLOY@"
    assert service["Environment"] == "WAKU_INSTALL_ENV=@WAKU_INSTALL_ENV@"
    assert service["Type"] == "oneshot"
    # A oneshot's default TimeoutStartSec is infinity, and a tick holds the
    # deploy lock a manual upgrade also needs.
    assert service.get("TimeoutStartSec")
    assert "Restart" not in service


def test_the_timer_fires_every_five_minutes():
    assert _unit("waku-autodeploy.timer")["Timer"]["OnCalendar"] == "*:0/5"
