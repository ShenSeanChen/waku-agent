"""upgrade.sh's refusals, its order, and the arguments that drive it.

WHAT CAN GO WRONG HERE IS ORDER, not shell. An upgrade that restarts the
services before rebuilding the images restarts them onto the old ones; an
upgrade that runs restart-all before the gateway answers again restarts every
tenant through a gateway that is not there. Both look like success.

Also covered, added on review: the four guards that had no fixture at all
(waku_require_root, the readiness waku_die, -h|--help, and --ref itself --
both that it is honoured and that the default is origin/main), the dirty
guard reading git's own exit status rather than only its output, and a
regression test for the lib.sh finding this task exists to surface --
waku_load_install_env used to validate only the four names install.sh itself
needs, so a fifth name upgrade.sh dereferences died on bash's own `set -u`
message well after the images were rebuilt and the services restarted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import shelllib

UPGRADE = shelllib.DEPLOY / "upgrade.sh"

_ID_ROOT = "#!/bin/sh\necho 0\n"

_GIT_DIRTY = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"status --porcelain"*) echo " M waku/loop/agent.py" ;;
  *"rev-parse"*) echo 0000000000000000000000000000000000000000 ;;
esac
exit 0
"""

_GIT_CLEAN = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"status --porcelain"*) : ;;
  *"rev-parse"*) echo 0000000000000000000000000000000000000000 ;;
esac
exit 0
"""

# git status itself fails -- no repository there, dubious ownership -- which
# prints to stderr and nothing to stdout. A guard that only reads the output
# sees an empty string, the same as a clean tree.
_GIT_STATUS_FAILS = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"status --porcelain"*) echo "fatal: not a git repository" >&2; exit 128 ;;
  *"rev-parse"*) echo 0000000000000000000000000000000000000000 ;;
esac
exit 0
"""

# ONE LINE PER ARGUMENT, because `$*` collapses them. The recorder in
# shelllib joins argv with spaces, so `--build-arg DNS_PROVIDER=cloudflare` and
# `--build-arg "DNS_PROVIDER=cloudflare {env.CLOUDFLARE_API_TOKEN}"` are the
# same text once a space is all that separates the words -- which is exactly
# the difference the module split makes, and exactly what an assertion on that
# text cannot see. Measured: the first version of
# test_xcaddy_is_handed_the_module_and_not_the_whole_directive passed with the
# split deleted.
_DOCKER_PER_ARG = """#!/bin/sh
printf '%s %s\\n' docker "$*" >> "$WAKU_CALLS"
for argument in "$@"; do printf 'ARG %s\\n' "$argument" >> "$WAKU_CALLS"; done
exit 0
"""

_CURL_NEVER_READY = """#!/bin/sh
printf '%s %s\\n' curl "$*" >> "$WAKU_CALLS"
exit 1
"""


def _install_env(tmp_path, *, dns_provider="route53", module_version="@v1.5.0",
                 omit_module_version=False):
    """install.env as waku_install_env writes it: every value SINGLE-QUOTED.

    The quoting is not cosmetic here. Unquoted, the documented two-word
    `--dns-provider` is read by bash as an assignment followed by a command, so
    this loader dies with `command not found` -- and every fixture in this file
    used to write the one-word `route53`, which is why no test in any tier ever
    carried the documented value across the writer-to-reader seam.
    """
    src = tmp_path / "src"
    (src / "hosted" / "image").mkdir(parents=True)
    build = src / "hosted" / "image" / "build.sh"
    build.write_text('#!/bin/sh\nprintf "%s %s\\n" build.sh "$*" >> "$WAKU_CALLS"\n',
                     encoding="utf-8")
    build.chmod(0o755)
    env_file = tmp_path / "install.env"
    lines = [f"WAKU_ROOT='{tmp_path}/waku'",
             f"WAKU_SRC='{src}'",
             f"WAKU_COMPOSE='{src}/hosted/deploy/compose.yaml'",
             "WAKU_DOMAIN='example.test'",
             f"WAKU_DNS_PROVIDER='{dns_provider}'",
             "WAKU_ACME_EMAIL='ops@example.test'",
             "WAKU_GATEWAY_ADDRESS='127.0.0.1:8787'",
             "WAKU_TENANT_IMAGE='waku-tenant:current'",
             "WAKU_SERVICES_IMAGE='waku-services:current'",
             "WAKU_CADDY_IMAGE='waku-caddy:current'"]
    if not omit_module_version:
        lines.append(f"WAKU_DNS_MODULE_VERSION='{module_version}'")
    env_file.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return {"WAKU_INSTALL_ENV": str(env_file)}


def test_a_dirty_checkout_is_refused_before_anything_is_built(tmp_path):
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_DIRTY, "id": _ID_ROOT})
    assert done.returncode != 0
    assert "uncommitted changes" in done.stderr
    # NOT JUST "nothing was built": the guard's own reason for existing is
    # that `git checkout --detach` over uncommitted edits throws them away or
    # leaves the tree in a state nobody can name. A version of this assertion
    # that excluded only docker and build.sh stayed green when the guard was
    # moved BELOW fetch and checkout -- the mutant performed exactly the data
    # loss the guard exists to prevent, and the assertion could not see it.
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith(("docker", "build.sh"))]
    assert not [line for line in calls if " fetch " in line or " checkout " in line]


def test_git_failing_to_report_status_is_refused_by_name(tmp_path):
    """The dirty guard used to read only git's OUTPUT: `[ -n "$(git status
    --porcelain)" ]` sees an empty string both when the tree is clean and
    when git itself failed and printed nothing to stdout. `set -e` caught the
    failure one line later, at `before=$(git rev-parse HEAD)`, before
    anything irreversible ran -- but that made the refusal a bare `fatal:`
    from git rather than a named one, and made safety here depend on the next
    line rather than on this guard. The guard now reads git's exit status."""
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_STATUS_FAILS, "id": _ID_ROOT})
    assert done.returncode != 0
    assert "does not look like a usable git checkout" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith(("docker", "build.sh"))]


def test_the_images_are_rebuilt_before_the_services_restart(tmp_path):
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    built = next(i for i, line in enumerate(calls) if line.startswith("build.sh"))
    restarted = next(i for i, line in enumerate(calls) if " up -d" in line)
    assert built < restarted


def test_restart_all_runs_only_after_the_gateway_answers(tmp_path):
    """--now restarts every tenant through the running gateway. If it ran
    before the readiness probe, an upgrade that left the gateway down would
    report success having restarted nobody."""
    done = shelllib.run(UPGRADE, ["--now"], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    probed = next(i for i, line in enumerate(calls) if line.startswith("curl"))
    restarted = next(i for i, line in enumerate(calls) if "restart-all" in line)
    assert probed < restarted


def test_the_gateway_failing_to_answer_refuses_and_never_restarts_a_tenant(tmp_path):
    """The negative side of the ordering test above: a gateway that never
    answers must both refuse (not exit 0) and never reach restart-all, even
    with --now. `sleep` is stubbed too so the 60-attempt retry does not spend
    a real minute doing it."""
    done = shelllib.run(UPGRADE, ["--now"], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id", "sleep"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT,
                                "curl": _CURL_NEVER_READY})
    assert done.returncode != 0
    assert "the gateway did not answer after the upgrade" in done.stderr
    assert not [line for line in shelllib.calls(tmp_path) if "restart-all" in line]


def test_an_unknown_argument_is_refused(tmp_path):
    done = shelllib.run(UPGRADE, ["--force"], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode != 0
    assert "unknown argument: --force" in done.stderr


def test_a_non_root_user_is_refused_before_anything_runs(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("as root the run goes past waku_require_root")
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode != 0
    assert "run this as root" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_help_exits_zero_and_touches_nothing(tmp_path):
    done = shelllib.run(UPGRADE, ["--help"], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode == 0, done.stderr
    assert "usage: upgrade.sh" in done.stdout
    assert shelllib.calls(tmp_path) == []


def test_ref_is_passed_through_to_the_checkout(tmp_path):
    done = shelllib.run(UPGRADE, ["--ref", "v1.2.3"], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert any("checkout --detach v1.2.3" in line for line in calls)


def test_with_no_ref_the_checkout_defaults_to_origin_main(tmp_path):
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert any("checkout --detach origin/main" in line for line in calls)


def test_ref_with_no_value_is_refused_cleanly(tmp_path):
    """`--ref` at the end of the line used to expand bash's own `$2: unbound
    variable`."""
    done = shelllib.run(UPGRADE, ["--ref"], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode != 0
    assert "--ref needs a value" in done.stderr
    assert "unbound variable" not in done.stderr


def test_ref_given_empty_is_refused_rather_than_silently_defaulting(tmp_path):
    """`--ref ""` has two arguments, so the argument-count check alone lets it
    through; `${ref:-origin/main}` then treats empty the same as unset and
    upgrades to origin/main having silently ignored what was typed."""
    done = shelllib.run(UPGRADE, ["--ref", ""], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode != 0
    assert "--ref needs a value" in done.stderr


def test_a_config_name_this_script_needs_but_the_loader_never_checked_is_refused_cleanly(
        tmp_path):
    """Regression for the finding this task exists to surface: waku_load_
    install_env used to validate only the four names install.sh itself
    needs. upgrade.sh dereferences five more, and a missing one died on
    bash's own `set -u` message -- for WAKU_GATEWAY_ADDRESS specifically,
    only after the images were already rebuilt and `compose up -d` had
    already restarted the services. The loader now takes upgrade.sh's own
    list as arguments and refuses a missing name before any of that runs."""
    env = _install_env(tmp_path)
    install_env_path = Path(env["WAKU_INSTALL_ENV"])
    kept = [line for line in install_env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("WAKU_GATEWAY_ADDRESS=")]
    install_env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path, env=env,
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode != 0
    assert "install.env is missing WAKU_GATEWAY_ADDRESS" in done.stderr
    assert "unbound variable" not in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith(("docker", "build.sh"))]


# --- the caddy rebuild, which had no test at all -------------------------------


def test_the_two_word_dns_provider_survives_being_loaded(tmp_path):
    """THE SEAM, FROM THIS SIDE. Every fixture in this file wrote the one-word
    `route53`, so the value install.sh declares valid --
    `cloudflare {env.CLOUDFLARE_API_TOKEN}` -- was never loaded by this script
    in any test. Unquoted in install.env it makes `waku_load_install_env` exit
    127 with `{env.CLOUDFLARE_API_TOKEN}: command not found`, which is an
    upgrade that fails with an unrecognisable message before it does anything.
    """
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(
                            tmp_path,
                            dns_provider="cloudflare {env.CLOUDFLARE_API_TOKEN}"),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    assert "command not found" not in done.stderr


def _args(calls):
    return [line[len("ARG "):] for line in calls if line.startswith("ARG ")]


def test_xcaddy_is_handed_the_module_and_not_the_whole_directive(tmp_path):
    """install.sh splits this and says why in capitals: "The whole string used
    to go to both, so the documented Cloudflare recipe could not build at all:
    xcaddy was handed `github.com/caddy-dns/cloudflare
    {env.CLOUDFLARE_API_TOKEN}`."

    This script passed the whole string, which is that bug reproduced in the
    one command an operator runs to cross a version boundary -- and it fails
    AFTER `git checkout --detach` has moved the checkout and after both other
    images were rebuilt.
    """
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(
                            tmp_path,
                            dns_provider="cloudflare {env.CLOUDFLARE_API_TOKEN}"),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "docker": _DOCKER_PER_ARG,
                                "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    # ONE ARGUMENT, MATCHED WHOLE. The unsplit form is
    # `DNS_PROVIDER=cloudflare {env.CLOUDFLARE_API_TOKEN}` as a SINGLE argv
    # word, which no assertion on the joined command line can tell from two.
    assert "DNS_PROVIDER=cloudflare" in _args(shelllib.calls(tmp_path))


def test_the_operators_module_pin_reaches_the_rebuild(tmp_path):
    """caddy.Dockerfile's own header: "DNS_PROVIDER_VERSION IS EMPTY BY DEFAULT
    AND SHOULD NOT STAY THAT WAY on a deployment anybody depends on." This
    script passed no DNS_PROVIDER_VERSION at all, so every upgrade re-resolved
    the module's latest release while the operator's `--dns-module-version`
    pin sat unused -- and install.env did not carry it in any form."""
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path, module_version="@v1.5.0"),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN, "docker": _DOCKER_PER_ARG,
                                "id": _ID_ROOT})
    assert done.returncode == 0, done.stderr
    assert "DNS_PROVIDER_VERSION=@v1.5.0" in _args(shelllib.calls(tmp_path))


def test_an_empty_pin_is_a_note_and_an_absent_one_is_a_warning(tmp_path):
    """TWO DIFFERENT THINGS AND TWO DIFFERENT MESSAGES. Empty means the
    operator gave no --dns-module-version and accepted whatever xcaddy resolves,
    which install.sh logs a NOTE about. ABSENT means install.env was written
    before the name existed, and install.env is never rewritten -- so a `:?` in
    the load list would refuse to upgrade a VM that is otherwise fine. A script
    that read them the same way would tell an operator with a real pin nothing
    at all on the upgrade that discarded it."""
    empty = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                         env=_install_env(tmp_path, module_version=""),
                         stubs=["git", "docker", "curl", "id"],
                         bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert empty.returncode == 0, empty.stderr
    assert "NOTE: no --dns-module-version pin" in empty.stdout
    assert "WARNING" not in empty.stdout

    older = tmp_path / "older"
    older.mkdir()
    absent = shelllib.run(UPGRADE, [], tmp_path=older,
                          env=_install_env(older, omit_module_version=True),
                          stubs=["git", "docker", "curl", "id"],
                          bodies={"git": _GIT_CLEAN, "id": _ID_ROOT})
    assert absent.returncode == 0, absent.stderr
    assert "has no WAKU_DNS_MODULE_VERSION line" in absent.stdout
