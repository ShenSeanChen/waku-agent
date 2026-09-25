"""Every deploy script parses, the set of them is pinned, and compose.yaml
renders into the spec's table.

NOT A SOURCE SCAN. `bash -n` RUNS bash's parser over the file; a syntax error
in an operator script otherwise surfaces at 03:17 in a timer unit. The pinned
name set is the other half: a script added without a test here is a script
nobody parsed, and a script deleted is a script some other script still
sources.

NOT A YAML READ EITHER. `docker compose config` resolves the file the way the
daemon will read it -- variable substitution, env_file attachment, the short
volume syntax expanded -- so what this asserts is the rendered truth and not
the text. It needs the Compose CLI and NO DAEMON, which is why it is in this
tier and not in evals/hosted_docker/: the `validate` job's runner has the CLI,
and `validate` is the required check. A machine without the CLI skips with the
reason named, the same way evals/hosted_docker/conftest.py handles a missing
daemon.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import shelllib

# F2, F3 and F4 each append their own names to this set in the same commit that
# adds the script. C3's firewall.sh joins it when C3 lands.
EXPECTED_SCRIPTS = {"backup.sh", "checks.sh", "envfiles.sh", "install.sh",
                    "lib.sh", "networks.sh", "restore.sh", "tree.sh",
                    "upgrade.sh"}

COMPOSE = shelllib.DEPLOY / "compose.yaml"
SERVICES = ("caddy", "gateway", "proxy", "spawner")


def test_the_deploy_directory_holds_exactly_the_scripts_this_group_wrote():
    found = {path.name for path in shelllib.DEPLOY.glob("*.sh")}
    assert found == EXPECTED_SCRIPTS


# The three that are SOURCED and never run, so the partition below is a closed
# set in both directions rather than a list of the ones somebody remembered.
SOURCED_SCRIPTS = {"checks.sh", "envfiles.sh", "lib.sh"}


@pytest.mark.parametrize("name", sorted(EXPECTED_SCRIPTS))
def test_every_script_is_runnable_if_and_only_if_it_is_meant_to_be_run(name):
    """backup.sh's executable bit is what waku-backup.service's ExecStart
    depends on, and unlike the firewall block install.sh does not test for it
    before installing the unit -- so a lost bit is a unit that fails at 03:17,
    inside a timer, which is the failure the @WAKU_BACKUP@ placeholder exists
    one layer out to prevent. The other direction matters too: a sourced file
    with the bit set is one somebody will eventually run, and lib.sh run rather
    than sourced defines eleven functions and exits 0 having done nothing."""
    path = shelllib.DEPLOY / name
    assert os.access(path, os.X_OK) is (name not in SOURCED_SCRIPTS)


@pytest.mark.parametrize("name", sorted(EXPECTED_SCRIPTS))
def test_every_script_parses(name):
    done = subprocess.run(["bash", "-n", str(shelllib.DEPLOY / name)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_firewall_unit_reapplies_after_a_docker_restart():
    """A DRIFT CHECK on a declarative file, and it is worth one: Docker
    rewrites its own iptables chains when it starts, so a unit without
    PartOf=docker.service leaves a VM with no tenant egress rules after a
    Docker package upgrade -- silently, and only until somebody looks.

    configparser with strict=False because systemd allows a repeated key
    (WantedBy appears twice here).
    """
    unit = configparser.ConfigParser(strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(shelllib.DEPLOY / "waku-firewall.service")
    assert unit["Unit"]["PartOf"] == "docker.service"
    assert unit["Unit"]["After"] == "docker.service"
    assert unit["Unit"]["Requires"] == "docker.service"
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["RemainAfterExit"] == "yes"


def test_the_firewall_units_execstart_is_a_placeholder_and_not_a_path():
    """THE HOLE THIS CLOSES. The unit used to carry
    `ExecStart=/srv/waku/src/hosted/deploy/firewall.sh` while install.sh's
    guard tested `"$here/firewall.sh"` -- its own directory. `--root` is a
    flag and a checkout can live anywhere, so on any VM whose source is not at
    /srv/waku/src the guard passed and the unit installed pointed at nothing:
    a unit that fails at every boot, which is the exact outcome the comment
    above that guard says it is avoiding.

    The earlier assertion was `ExecStart.endswith("/hosted/deploy/firewall.sh")`,
    which the hardcoded path satisfies. This one pins the whole value, so any
    path at all is red and install.sh has to substitute the path it tested.
    """
    unit = configparser.ConfigParser(strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(shelllib.DEPLOY / "waku-firewall.service")
    assert unit["Service"]["ExecStart"] == "@WAKU_FIREWALL@"


def test_the_backup_units_execstart_is_a_placeholder_and_not_a_path():
    """THE SAME HOLE AS THE FIREWALL UNIT'S, and worth closing before it is dug
    rather than after. A unit carrying /srv/waku/src/hosted/deploy/backup.sh
    installs cleanly on a VM whose checkout is anywhere else, and then fails at
    03:17, in a timer, where nobody is looking. install.sh substitutes the path
    of the file it is installing the unit beside."""
    unit = configparser.ConfigParser(strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(shelllib.DEPLOY / "waku-backup.service")
    assert unit["Service"]["ExecStart"] == "@WAKU_BACKUP@ --all"
    assert unit["Service"]["Type"] == "oneshot"
    # A backup that failed tonight is a backup to look at, not one to run again
    # in ten seconds against a staging slot it may itself have wedged.
    assert "Restart" not in unit["Service"]


def test_a_vm_that_was_off_at_the_backup_hour_backs_up_when_it_returns():
    """A DRIFT CHECK on a declarative file. Without Persistent=true a VM that
    was powered down at 03:17 simply skips that night, and the operator's
    evidence that backups are running -- a timer that is enabled -- says
    nothing about whether one ever ran."""
    unit = configparser.ConfigParser(strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(shelllib.DEPLOY / "waku-backup.timer")
    assert unit["Timer"]["Persistent"] == "true"
    assert unit["Timer"]["OnCalendar"] == "*-*-* 03:17:00"
    assert unit["Timer"]["RandomizedDelaySec"] == "900"
    assert unit["Install"]["WantedBy"] == "timers.target"


def test_the_caddy_dockerfile_quotes_the_module_it_builds():
    """Both ARGs are substituted by the shell the RUN starts, so an unquoted
    expansion would let a value with a space in it become extra words in the
    command. install.sh keeps both to closed sets and this is the other half,
    at the place the value is used.

    A Dockerfile has no parser to drive the way `bash -n` drives a script, so
    this reads the instruction. It is the one declarative-file read in the
    file, like the systemd unit above, and it is pinned whole rather than
    searched for a substring.
    """
    text = (shelllib.DEPLOY.parent / "image" / "caddy.Dockerfile").read_text(
        encoding="utf-8")
    runs = [line.strip() for line in text.splitlines()
            if line.startswith("RUN ")]
    expected = ('RUN xcaddy build --with "github.com/caddy-dns/'
                '${DNS_PROVIDER}${DNS_PROVIDER_VERSION}"')
    assert runs == [expected]


# --- tree.sh and networks.sh, driven with a stubbed daemon -------------------
#
# What tree.sh actually creates -- real owners, real modes -- needs root and is
# evals/hosted_docker/test_compose.py's. What is here is the refusal above it
# and the ARGUMENTS networks.sh hands the daemon, which is where the copy of
# core/tenant.py's addresses can silently drift.

TREE = shelllib.DEPLOY / "tree.sh"
NETWORKS = shelllib.DEPLOY / "networks.sh"

# A docker whose `network inspect` says "not there", so networks.sh takes its
# create path. Everything else records and succeeds.
_DOCKER_NO_NETWORKS = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$WAKU_CALLS"
if [ "$1 $2" = "network inspect" ]; then exit 1; fi
exit 0
"""

# The same, with the create refused. `set -e` must stop the script on the first
# one rather than carry on and report success having made one bridge.
_DOCKER_CREATE_FAILS = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$WAKU_CALLS"
if [ "$1 $2" = "network inspect" ]; then exit 1; fi
if [ "$1 $2" = "network create" ]; then echo "boom" >&2; exit 1; fi
exit 0
"""


# The spec's two tables, as literals: path -> (mode, owner, group). Written
# out again here rather than shared with evals/hosted_docker/test_compose.py,
# which asserts the same table against the KERNEL. Two independent copies
# checked two different ways is the point; one shared constant compared with
# itself would hold for any values at all.
EXPECTED_TREE = {
    "config": ("0700", "0", "0"),
    "control": ("0700", "10002", "10002"),
    "control/backup": ("0700", "10002", "10002"),
    "ledger": ("0700", "10003", "10003"),
    "ledger/backup": ("0700", "10003", "10003"),
    "run": ("0755", "0", "0"),
    "run/gateway": ("2750", "10002", "10003"),
    "run/proxy": ("2750", "10003", "10002"),
    "run/spawner": ("2750", "0", "10002"),
    "run/admin": ("0700", "10002", "10002"),
    "tenants": ("0700", "0", "0"),
    "archive": ("0700", "0", "0"),
    "staging": ("0700", "0", "0"),
}


def test_tree_sh_refuses_to_run_without_a_root(tmp_path):
    """It would otherwise chown and chmod thirteen directories under a path
    that expanded to nothing."""
    done = shelllib.run(TREE, [], tmp_path=tmp_path, stubs=["docker"])
    assert done.returncode != 0
    assert "usage: tree.sh <root>" in done.stderr


def _tree_run(tmp_path):
    """tree.sh with chown and chmod recorded instead of applied.

    A real chown to UID 10002 needs root, which the required check does not
    have -- so what is asserted here is the ARGUMENTS and their order. That
    the kernel then honours them is
    evals/hosted_docker/test_compose.py::test_the_tree_has_the_owners_and_modes_the_sockets_need.
    """
    root = tmp_path / "waku"
    done = shelllib.run(TREE, [str(root)], tmp_path=tmp_path,
                        stubs=["chown", "chmod"])
    assert done.returncode == 0, done.stderr
    return root, shelllib.calls(tmp_path)


def test_tree_sh_makes_every_directory_the_spec_names_and_no_other(tmp_path):
    root, _log = _tree_run(tmp_path)
    made = {str(path.relative_to(root))
            for path in root.rglob("*") if path.is_dir()}
    assert made == set(EXPECTED_TREE)


def test_tree_sh_gives_each_directory_the_owner_and_mode_the_spec_gives_it(tmp_path):
    """BOTH DIRECTIONS: the set of (path, mode, owner, group) the script asked
    for is compared whole with the spec's table, so an extra directory is as
    much a finding as a wrong mode."""
    root, log = _tree_run(tmp_path)
    asked = {}
    for line in log:
        words = line.split()
        path = str(Path(words[-1]).relative_to(root))
        entry = asked.setdefault(path, {})
        entry[words[0]] = words[1]
    assert asked == {
        path: {"chown": f"{owner}:{group}", "chmod": mode}
        for path, (mode, owner, group) in EXPECTED_TREE.items()}


def test_tree_sh_chowns_each_directory_before_it_chmods_it(tmp_path):
    """LOAD-BEARING ORDER. chown clears the set-group-ID bit, so a chmod 2750
    followed by a chown leaves a 0750 directory -- and a socket bound inside it
    then takes the serving process's group instead of the peer's, which locks
    the peer out with EACCES and no explanation. The three 2750 directories are
    where it bites, and all thirteen are checked because the next directory to
    take the setgid bit should not have to remember this.
    """
    _root, log = _tree_run(tmp_path)
    seen_chmod = set()
    for line in log:
        words = line.split()
        target = words[-1]
        if words[0] == "chmod":
            seen_chmod.add(target)
        else:
            assert target not in seen_chmod, (
                f"tree.sh chmodded {target} before it chowned it; the chown "
                "clears the setgid bit the chmod just set")


def _creates(tmp_path) -> dict[str, list[str]]:
    """Every `docker network create` networks.sh ran, keyed by the network
    name, which is the command's last argument."""
    made = {}
    for line in shelllib.calls(tmp_path):
        words = line.split()
        if words[:3] == ["docker", "network", "create"]:
            made[words[-1]] = words[3:-1]
    return made


def _split_opts(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """A `docker network create` argument list, split into its `--opt key=value`
    pairs and everything else in order."""
    rest: list[str] = []
    options: dict[str, str] = {}
    remaining = list(args)
    while remaining:
        word = remaining.pop(0)
        if word == "--opt":
            key, _, setting = remaining.pop(0).partition("=")
            options[key] = setting
        else:
            rest.append(word)
    return rest, options


def test_networks_sh_hands_the_daemon_the_addresses_core_tenant_py_holds(tmp_path):
    """networks.sh copies hosted/core/tenant.py's addresses into shell,
    because a shell script cannot import a Python constant. THE DUPLICATE IS
    CHECKED BY BEHAVIOUR: the script is run against a stub daemon and the
    arguments it actually passed are compared with the module. Nothing here
    reads the script's text.

    THE OPTIONS ARE COMPARED WITH core/tenant.BRIDGE_OPTIONS AS A WHOLE DICT,
    not looked up one at a time and not spelt out here. An earlier version
    pinned `enable_icc=false` as a string literal, so a third option added to
    the module would land on the test bridges that
    evals/hosted_docker/conftest.py builds FROM that dict and never on the
    real ones this script creates, with nothing going red. enable_icc is the
    tenant-to-tenant isolation pulled forward out of C3, so a silent drift
    there is the worst kind available.
    """
    from hosted.core import tenant

    done = shelllib.run(NETWORKS, [], tmp_path=tmp_path, stubs=["docker"],
                        bodies={"docker": _DOCKER_NO_NETWORKS})
    assert done.returncode == 0, done.stderr
    made = _creates(tmp_path)
    assert set(made) == {tenant.TENANT_NETWORK, tenant.INSPECT_NETWORK}

    tenant_flags, tenant_options = _split_opts(made[tenant.TENANT_NETWORK])
    assert tenant_flags == [
        "--driver", "bridge",
        "--subnet", str(tenant.TENANT_SUBNET),
        "--gateway", str(tenant.TENANT_GATEWAY),
        "--ip-range", str(tenant.DYNAMIC_RANGE)]
    assert tenant_options == tenant.BRIDGE_OPTIONS[tenant.TENANT_NETWORK]

    inspect_flags, inspect_options = _split_opts(made[tenant.INSPECT_NETWORK])
    # The inspect bridge takes no --ip-range: an inspect container is given a
    # dynamic address, which is the whole reason it is not on the tenant
    # bridge.
    assert inspect_flags == [
        "--driver", "bridge",
        "--subnet", str(tenant.INSPECT_SUBNET),
        "--gateway", str(tenant.INSPECT_GATEWAY)]
    assert inspect_options == tenant.BRIDGE_OPTIONS[tenant.INSPECT_NETWORK]


def test_networks_sh_creates_nothing_the_second_time(tmp_path):
    """The spec's install is rerunnable. With both bridges already there, the
    default recording stub answers `network inspect` with success and the
    script must reach the end having created nothing."""
    done = shelllib.run(NETWORKS, [], tmp_path=tmp_path, stubs=["docker"])
    assert done.returncode == 0, done.stderr
    assert _creates(tmp_path) == {}
    assert "already exists" in done.stdout


def test_networks_sh_stops_on_the_first_bridge_it_could_not_create(tmp_path):
    """`set -e`, proved rather than asserted from the text. A script that
    carried on would leave the VM with one bridge, and the proxy binds the
    other one's gateway address."""
    done = shelllib.run(NETWORKS, [], tmp_path=tmp_path, stubs=["docker"],
                        bodies={"docker": _DOCKER_CREATE_FAILS})
    assert done.returncode != 0
    assert list(_creates(tmp_path)) == ["waku-tenants"]


# --- lib.sh's waku_write_config ----------------------------------------------
#
# It was inside install.sh, where no test in any tier could reach it, and it is
# the sole implementation of the spec's "a rerun never overwrites existing
# config" as well as the function that writes both of this deployment's
# secrets. Moving it into lib.sh is what makes these six assertions possible.

LIB = shelllib.DEPLOY / "lib.sh"


def _write_config(tmp_path, target, body: str, *, name="call.sh"):
    script = tmp_path / name
    script.write_text(
        f'set -euo pipefail\n. "{LIB}"\n'
        f'printf %s "$2" | waku_write_config "$1"\n', encoding="utf-8")
    return shelllib.run(script, [str(target), body], tmp_path=tmp_path)


def test_write_config_writes_the_body_it_was_given(tmp_path):
    target = tmp_path / "config" / "proxy.env"
    target.parent.mkdir()
    done = _write_config(tmp_path, target, "WAKU_PLATFORM_KEY=sk-ant-abc\n")
    assert done.returncode == 0, done.stderr
    assert target.read_text(encoding="utf-8") == "WAKU_PLATFORM_KEY=sk-ant-abc\n"
    assert f"wrote {target}" in done.stdout


def test_write_config_makes_the_file_readable_only_by_its_owner(tmp_path):
    """config/proxy.env holds the platform's model key and config/caddy.env
    holds a credential that can rewrite the DNS zone. 0600 is asserted as a
    literal, and it is set by the umask that CREATES the file rather than by a
    chmod afterwards -- a file that is briefly 0644 is a file that was briefly
    readable."""
    target = tmp_path / "config" / "caddy.env"
    target.parent.mkdir()
    done = _write_config(tmp_path, target, "AWS_SECRET_ACCESS_KEY=x\n")
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_config_never_overwrites_and_says_which_file_it_kept(tmp_path):
    """The spec's "a rerun skips finished steps and never overwrites existing
    config". The running services were started from the file that is there."""
    target = tmp_path / "config" / "gateway.env"
    target.parent.mkdir()
    target.write_text("WAKU_MAX_RUNNING=7\n", encoding="utf-8")
    done = _write_config(tmp_path, target, "WAKU_MAX_RUNNING=99\n")
    assert done.returncode == 0, done.stderr
    assert target.read_text(encoding="utf-8") == "WAKU_MAX_RUNNING=7\n"
    assert f"keeping the existing {target}" in done.stdout


def test_write_config_drains_stdin_when_it_keeps_a_file(tmp_path):
    """The caller's body is a heredoc on the other end of a pipe. A function
    that returned without reading it would leave the writer blocked, or --
    worse, and only on a body short enough to fit the pipe buffer -- would
    work in testing and hang on a longer file."""
    target = tmp_path / "config" / "gateway.env"
    target.parent.mkdir()
    target.write_text("kept\n", encoding="utf-8")
    done = _write_config(tmp_path, target, "X=1\n" * 20000)
    assert done.returncode == 0, done.stderr
    assert target.read_text(encoding="utf-8") == "kept\n"


def test_a_killed_run_leaves_nothing_at_the_targets_name(tmp_path):
    """THE DEFECT waku_write_config's RENAME EXISTS FOR. The earlier shape
    created the target and then catted into it, so a run that stopped half way
    left a TRUNCATED env file -- and the next run's "never overwrite" kept it,
    logging "keeping the existing ..." over a half-written gateway.env.
    Rerunning could not repair the one state rerunning is for.

    THE SIGNAL MATTERS AND A FAILING `cat` WOULD NOT DO. An earlier version of
    this test stubbed `cat` to exit 1, and it passed with the rename deleted:
    the `|| rm -f "$tmp"` cleanup removes the target too when the temporary IS
    the target. It was carried by the cleanup branch, not by the guard it was
    named for. SIGKILL is the one path where neither that branch nor an EXIT
    trap runs, so it is the rename alone that decides -- and it is also what a
    reboot or an OOM kill looks like from inside the function.

    The leftover temporary is asserted too: it is the evidence that the body
    was being written somewhere other than the target's name.
    """
    target = tmp_path / "config" / "gateway.env"
    target.parent.mkdir()
    script = tmp_path / "killed.sh"
    script.write_text(
        f'set -euo pipefail\n. "{LIB}"\n'
        f'waku_write_config "$1" </dev/null\n', encoding="utf-8")
    done = shelllib.run(
        script, [str(target)], tmp_path=tmp_path, stubs=["cat"],
        bodies={"cat": "#!/bin/sh\nprintf 'WAKU_MAX_RU'\nkill -9 $PPID\n"})
    assert done.returncode != 0
    assert not target.exists(), (
        "a killed run left an env file at the target's name; the next run's "
        '"never overwrite" would keep it and nothing could repair it')
    left = [path.name for path in target.parent.iterdir()]
    assert left and all(".tmp." in name for name in left), left


def test_a_stale_temporary_does_not_lend_the_target_its_mode(tmp_path):
    """`rm -f "$tmp"` before the create, and it had no fixture at all.

    Truncating a file that is already there keeps whatever mode it already
    had, so the umask that is supposed to make the file 0600 at birth does
    nothing and the target inherits the stale file's mode. A `$$` that repeats
    across a reboot is how a stale temporary comes to exist.

    The script makes the stale file itself, because only the shell that runs
    waku_write_config knows the `$$` the name is built from.
    """
    target = tmp_path / "config" / "proxy.env"
    target.parent.mkdir()
    script = tmp_path / "stale.sh"
    script.write_text(
        f'set -euo pipefail\n. "{LIB}"\n'
        'stale=$1.tmp.$$\n'
        ': >"$stale"\nchmod 0666 "$stale"\n'
        'printf %s "$2" | waku_write_config "$1"\n', encoding="utf-8")
    done = shelllib.run(script, [str(target), "WAKU_PLATFORM_KEY=sk-ant-abc\n"],
                        tmp_path=tmp_path)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_callers_exit_trap_can_find_the_temporary_to_remove(tmp_path):
    """`WAKU_WRITE_TMP=$tmp`, which also had no fixture.

    SIGTERM, not SIGKILL: a catchable signal is the case where the caller's
    EXIT trap runs, and the trap can only remove the temporary if
    waku_write_config published its name. install.sh's trap line itself sits
    below waku_require_root, so what is proved here is the mechanism, with the
    test supplying its own caller exactly as every other waku_write_config
    test does.

    The pair is complementary: the SIGKILL test asserts the temporary IS left
    -- evidence the body went somewhere other than the target's name -- and
    this one asserts a caller that traps can then take it away, rather than
    leaving a 0600 file holding part of a platform key.
    """
    target = tmp_path / "config" / "proxy.env"
    target.parent.mkdir()
    script = tmp_path / "termed.sh"
    script.write_text(
        f'set -euo pipefail\n. "{LIB}"\n'
        'trap \'rm -f ${WAKU_WRITE_TMP:+"$WAKU_WRITE_TMP"}\' EXIT\n'
        'waku_write_config "$1" </dev/null\n', encoding="utf-8")
    done = shelllib.run(
        script, [str(target)], tmp_path=tmp_path, stubs=["cat"],
        bodies={"cat": "#!/bin/sh\nprintf 'WAKU_PLATFORM_KEY=sk-ant-par'\n"
                       "kill -TERM $PPID\n"})
    assert done.returncode != 0
    left = [path.name for path in target.parent.iterdir()]
    assert left == [], (
        f"a terminated run left {left}; the caller's trap could not find the "
        "temporary, which holds part of the platform key at 0600")


def test_a_failed_write_takes_its_partial_secret_with_it(tmp_path):
    """The temporary is 0600 and holds PART OF A SECRET between the create and
    the rename. A failed `cat` used to leave it there under a name nothing
    would ever clean up or look at again, beside the target it was going to
    become."""
    target = tmp_path / "config" / "proxy.env"
    target.parent.mkdir()
    script = tmp_path / "failing.sh"
    script.write_text(
        f'set -euo pipefail\n. "{LIB}"\nwaku_write_config "$1" </dev/null\n',
        encoding="utf-8")
    done = shelllib.run(
        script, [str(target)], tmp_path=tmp_path, stubs=["cat"],
        bodies={"cat": "#!/bin/sh\nprintf 'WAKU_PLATFORM_KEY=sk-ant-par'\nexit 1\n"})
    assert done.returncode != 0
    left = sorted(path.name for path in target.parent.iterdir())
    assert left == [], f"a failed write left {left} holding part of a secret"


def test_write_config_leaves_no_temporary_file_behind(tmp_path):
    """The write is atomic: the body goes to a temporary file beside the
    target, in the same directory so the rename cannot cross a filesystem, and
    appears at its name complete or not at all. The earlier shape created the
    target and then catted into it, so a run killed between the two left a
    TRUNCATED env file -- which the next run's "never overwrite" then kept,
    logging "keeping the existing ...". That is the one state rerunning cannot
    repair."""
    target = tmp_path / "config" / "install.env"
    target.parent.mkdir()
    done = _write_config(tmp_path, target, "WAKU_ROOT=/srv/waku\n")
    assert done.returncode == 0, done.stderr
    assert {path.name for path in target.parent.iterdir()} == {"install.env"}


def test_write_config_refuses_a_target_whose_directory_is_not_there(tmp_path):
    target = tmp_path / "nothing" / "here" / "proxy.env"
    done = _write_config(tmp_path, target, "X=1\n")
    assert done.returncode != 0
    assert "is not a directory" in done.stderr
    assert not target.exists()


# --- envfiles.sh: the names install.sh writes, pinned against the modules ----
#
# The brief's own comment says it: "a name missing here raises at startup; a
# name MISSPELT here sets nothing and raises nothing". gateway.env.example and
# spawner.env.example are pinned against their modules in both directions, and
# install.sh -- the thing that writes the file the services actually read --
# was pinned against neither, because its heredocs sat inside a script that
# needs root, an Ubuntu release and a Docker daemon before it reaches them.

ENVFILES = shelllib.DEPLOY / "envfiles.sh"

# Every global the four functions read, with a value whose shape is plausible
# but obviously a fixture. `set -u` in the caller means a global nobody set is
# an error rather than an empty line, which is the property that matters.
_ENV_GLOBALS = {
    "root": "/srv/waku",
    "src": "/srv/waku/src",
    "domain": "agent.example.test",
    "dns_provider": "route53",
    "acme_email": "ops@example.test",
    "gateway_address": "127.0.0.1:8787",
    "max_running": "95",
    "supabase_url": "https://p.supabase.co",
    "supabase_audience": "https://api.waku.one/mcp",
    "supabase_publishable_key": "sb_publishable_x",
    "tenant_image": "waku-tenant:current",
    "services_image": "waku-services:current",
    "caddy_image": "waku-caddy:current",
    "free_model": "claude-haiku-4-5",
    "tenant_disk_bytes": "1073741824",
    "data_device": "/dev/sdb1",
    "platform_key": "sk-ant-envfiles-fixture",
    "restic_repository": "s3:s3.example.test/waku-backups-fixture",
    "restic_password_file": "/srv/waku/config/restic-password",
}


# A git that answers `rev-parse HEAD`, so waku_install_env's commit line is
# the shape it has on a real checkout rather than the empty string a recording
# stub would leave.
_GIT_WITH_A_HEAD = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$WAKU_CALLS"
echo 0123456789abcdef0123456789abcdef01234567
"""

# A git that is there and cannot answer -- a tarball with no .git, which is a
# real way to deploy.
_GIT_WITHOUT_A_HEAD = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$WAKU_CALLS"
echo "fatal: not a git repository" >&2
exit 128
"""


def _env_body(tmp_path, function: str, *, git=_GIT_WITH_A_HEAD,
              extra: dict[str, str] | None = None) -> dict[str, str]:
    """Run one envfiles.sh function and parse what it printed.

    `extra` overrides a global for one call -- used where the VALUE has to be
    a path that exists on the machine running the test, rather than the
    /srv/waku fixture the others use.
    """
    assignments = "\n".join(f"{name}={value!r}"
                            for name, value in {**_ENV_GLOBALS, **(extra or {})}.items())
    script = tmp_path / f"{function}.sh"
    script.write_text(
        f"set -euo pipefail\n{assignments}\n. \"{ENVFILES}\"\n{function}\n",
        encoding="utf-8")
    done = shelllib.run(script, [], tmp_path=tmp_path, stubs=["git"],
                        bodies={"git": git})
    assert done.returncode == 0, done.stderr
    parsed = {}
    for line in done.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        assert separator == "=", f"{function} printed a line with no '=': {line!r}"
        assert name not in parsed, f"{function} printed {name} twice"
        parsed[name] = value
    return parsed


def test_the_gateway_env_install_writes_carries_every_name_the_gateway_requires(tmp_path):
    """BOTH DIRECTIONS and in the module's own ORDER. A missing name raises at
    startup, which is survivable; a misspelt one sets nothing and raises
    nothing, which is a setting the operator believes they made and the
    gateway never saw."""
    from hosted.gateway.config import REQUIRED_ENV_NAMES

    written = _env_body(tmp_path, "waku_gateway_env")
    assert list(written) == list(REQUIRED_ENV_NAMES)


def test_the_spawner_env_install_writes_carries_every_name_the_spawner_requires(tmp_path):
    """The 11 required names in the module's order, plus WAKU_SPAWNER_SOCKET,
    which is one of the two in OPTIONAL_ENV_NAMES -- written anyway so the
    socket the spawner binds and the one the gateway is told about come from
    one line of one file. Anything else is a finding in either direction."""
    from hosted.spawner.template import ENV_NAMES, OPTIONAL_ENV_NAMES

    written = _env_body(tmp_path, "waku_spawner_env")
    assert list(written)[:len(ENV_NAMES)] == list(ENV_NAMES)
    extra = set(written) - set(ENV_NAMES)
    assert extra == {"WAKU_SPAWNER_SOCKET"}
    assert extra <= set(OPTIONAL_ENV_NAMES)


def test_the_free_model_reaches_both_the_spawner_and_the_proxy(tmp_path):
    """The spec's container-template table: --free-model is written into BOTH
    files "so the two can never disagree". The retrieval gate uses the small
    model, and a small model outside the proxy's allowlist would be refused on
    every turn and silently fail open."""
    spawner = _env_body(tmp_path, "waku_spawner_env")
    proxy = _env_body(tmp_path, "waku_proxy_env")
    assert spawner["WAKU_PLATFORM_MODEL"] == "claude-haiku-4-5"
    assert spawner["WAKU_PLATFORM_SMALL_MODEL"] == "claude-haiku-4-5"
    assert proxy["WAKU_FREE_MODELS"] == "claude-haiku-4-5"


def test_the_proxy_env_carries_the_platform_key_and_the_specs_free_tier_numbers(tmp_path):
    """Group D is deferred, so no module pins these names; they are group F's
    and proxy.env.example carries the same warning. The numbers are the
    spec's, as literals."""
    written = _env_body(tmp_path, "waku_proxy_env")
    assert written["WAKU_PLATFORM_KEY"] == "sk-ant-envfiles-fixture"
    assert written["WAKU_FREE_MONTHLY_CAP_USD"] == "1"
    assert written["WAKU_FREE_CONCURRENT_CALLS"] == "4"
    assert written["WAKU_FREE_REQUESTS_PER_MINUTE"] == "60"
    assert written["WAKU_MAX_TOKENS_CEILING"] == "4096"
    assert written["WAKU_MAX_BODY_BYTES"] == "4194304"
    assert written["WAKU_PROXY_BIND"] == "10.88.0.1"


def test_the_backup_env_install_writes_is_accepted_by_the_loader_that_reads_it(tmp_path):
    """THE WRITER AND THE READER, RUN AGAINST EACH OTHER. config/backup.env is
    the one config file no service reads -- backup.sh and restore.sh source it
    and hand it to restic through the ENVIRONMENT -- so the property that
    matters is not what the file looks like but that the names install.sh
    writes are the names waku_load_backup_env requires and that they come out
    the other side EXPORTED. A child process is how "exported" is visible:
    without `set -a` in the loader, restic sees nothing and the variable is
    still perfectly present in the shell that checked it.
    """
    password = tmp_path / "restic-password"
    password.write_text("a-restic-password\n", encoding="utf-8")
    written = _env_body(tmp_path, "waku_backup_env",
                        extra={"restic_password_file": str(password)})
    assert list(written) == ["RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE"]

    env_file = tmp_path / "backup.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in written.items()),
                        encoding="utf-8")
    done = shelllib.call_function(
        shelllib.DEPLOY / "lib.sh",
        f'waku_load_backup_env "{env_file}" RESTIC_REPOSITORY RESTIC_PASSWORD_FILE; '
        "sh -c 'printf %s \"$RESTIC_REPOSITORY\"'")
    assert done.returncode == 0, done.stderr
    assert done.stdout == written["RESTIC_REPOSITORY"]


def test_the_install_env_carries_exactly_what_every_other_script_reads(tmp_path):
    """lib.sh's waku_load_install_env refuses without the first four, and
    `docker compose --env-file` needs the rest to resolve compose.yaml. A
    closed set in both directions."""
    written = _env_body(tmp_path, "waku_install_env")
    assert set(written) == {
        "WAKU_ROOT", "WAKU_SRC", "WAKU_COMPOSE", "WAKU_DOMAIN",
        "WAKU_DNS_PROVIDER", "WAKU_ACME_EMAIL", "WAKU_GATEWAY_ADDRESS",
        "WAKU_TENANT_IMAGE", "WAKU_SERVICES_IMAGE", "WAKU_CADDY_IMAGE",
        "WAKU_DATA_DEVICE", "WAKU_INSTALLED_COMMIT"}
    assert written["WAKU_INSTALLED_COMMIT"] == (
        "0123456789abcdef0123456789abcdef01234567")


def test_a_checkout_with_no_git_history_records_the_commit_as_unknown(tmp_path):
    """A tarball deploy is a real way to install, and `git rev-parse` there
    exits 128. The value must be the word `unknown` and never the empty
    string: an env_file line with nothing after the `=` is a variable Compose
    passes through as empty, and "" is not distinguishable from "nobody wrote
    this" when somebody is reading the file to find out what is deployed."""
    written = _env_body(tmp_path, "waku_install_env", git=_GIT_WITHOUT_A_HEAD)
    assert written["WAKU_INSTALLED_COMMIT"] == "unknown"


def test_no_env_file_install_writes_holds_a_value_compose_would_misread(tmp_path):
    """Every value goes into an env_file that Compose parses line by line. A
    value with a newline in it writes a second variable; one with a trailing
    space carries that space into whatever reads it. The fixtures above are
    the shapes install.sh actually produces."""
    for function in ("waku_install_env", "waku_gateway_env",
                     "waku_spawner_env", "waku_proxy_env"):
        for name, value in _env_body(tmp_path, function).items():
            assert value == value.strip(), f"{function}: {name} has edge whitespace"
            assert value, f"{function}: {name} is empty"


# --- compose.yaml, rendered ---------------------------------------------------


@pytest.fixture
def rendered(tmp_path):
    """`docker compose config` over compose.yaml with a fake install.env.

    The four config/*.env files must exist -- Compose refuses to render a
    service whose env_file is missing -- so each is written with ONE marker
    naming itself. That marker is what proves each service is handed its own
    file and not another's: a proxy pointed at gateway.env renders with the
    gateway's marker in its environment, which no amount of reading the text
    would catch as reliably.
    """
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI: `docker compose config` is what renders "
                    "this file. It needs the CLI and no daemon; the validate "
                    "job's runner has it.")
    root = tmp_path / "waku"
    (root / "config").mkdir(parents=True)
    for service in SERVICES:
        (root / "config" / f"{service}.env").write_text(
            f"WAKU_ENV_FILE_MARKER={service}\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    env_file = tmp_path / "install.env"
    env_file.write_text(
        f"WAKU_ROOT={root}\n"
        f"WAKU_SRC={src}\n"
        f"WAKU_COMPOSE={COMPOSE}\n"
        "WAKU_DOMAIN=example.test\n"
        "WAKU_DNS_PROVIDER=route53\n"
        "WAKU_ACME_EMAIL=a@b.test\n"
        "WAKU_GATEWAY_ADDRESS=127.0.0.1:8787\n"
        "WAKU_TENANT_IMAGE=waku-tenant:rendertest\n"
        "WAKU_SERVICES_IMAGE=waku-services:rendertest\n"
        "WAKU_CADDY_IMAGE=waku-caddy:rendertest\n"
        "WAKU_DATA_DEVICE=/dev/null\n",
        encoding="utf-8")
    done = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE),
         "config", "--format", "json"],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        msg = done.stderr.strip()
        if "docker compose" in msg and "is not a docker command" in msg:
            pytest.skip("the Compose plugin is not installed: " + msg)
        raise AssertionError(f"compose.yaml does not render: {msg}")
    return json.loads(done.stdout), root, src


def test_the_stack_is_exactly_four_services(rendered):
    """A closed set in both directions. A fifth service here is something
    nobody reviewed running as root beside the spawner; a missing one is a
    stack that comes up half-installed and says nothing."""
    config, _root, _src = rendered
    assert set(config["services"]) == set(SERVICES)
    assert config["name"] == "waku"


def test_each_service_runs_as_the_user_the_socket_modes_need(rendered):
    """UID:GID and never a bare UID. A bare UID runs the process with group 0,
    and the setgid socket directories then hand the socket group 0 instead of
    the peer's group, which locks the peer out with EACCES."""
    config, _root, _src = rendered
    assert config["services"]["gateway"]["user"] == "10002:10002"
    assert config["services"]["proxy"]["user"] == "10003:10003"
    assert config["services"]["spawner"]["user"] == "0:0"
    # Caddy keeps its image's own user (spec's table), so the rendered service
    # must carry no `user` at all -- not a user that happens to look right.
    assert "user" not in config["services"]["caddy"]


def test_every_service_carries_the_oom_score_the_spec_gives_it(rendered):
    """-500 as a LITERAL. Comparing the rendered value with
    template.SERVICE_OOM_SCORE_ADJ would hold for every value of that
    constant, zero included. The relation to the tenant's +500 is asserted
    against a real container in
    evals/hosted_docker/test_compose.py::test_a_tenant_container_outranks_every_service_for_the_oom_killer.
    """
    config, _root, _src = rendered
    for name in SERVICES:
        assert config["services"][name]["oom_score_adj"] == -500
        assert config["services"][name]["network_mode"] == "host"


def test_each_service_is_handed_its_own_config_file(rendered):
    """The marker written into config/<service>.env comes back in that
    service's environment and in no other's."""
    config, _root, _src = rendered
    for name in SERVICES:
        environment = config["services"][name].get("environment") or {}
        assert environment.get("WAKU_ENV_FILE_MARKER") == name, (
            f"{name} was handed "
            f"{environment.get('WAKU_ENV_FILE_MARKER')!r}'s config file")


def test_the_two_images_go_where_the_install_builds_them(rendered):
    config, _root, _src = rendered
    assert config["services"]["caddy"]["image"] == "waku-caddy:rendertest"
    for name in ("gateway", "proxy", "spawner"):
        assert config["services"][name]["image"] == "waku-services:rendertest"


def test_only_the_spawner_gets_cap_sys_admin_and_the_data_device(rendered):
    """xfs_quota needs both from inside a container. Nothing else does, and a
    second service with CAP_SYS_ADMIN is a second service that can mount."""
    config, _root, _src = rendered
    assert config["services"]["spawner"]["cap_add"] == ["SYS_ADMIN"]
    assert [d["source"] for d in config["services"]["spawner"]["devices"]] == ["/dev/null"]
    for name in ("caddy", "gateway", "proxy"):
        assert "cap_add" not in config["services"][name]
        assert "devices" not in config["services"][name]


def test_each_service_mounts_exactly_the_directories_the_spec_gives_it(rendered):
    """BOTH DIRECTIONS, as a set. The proxy is the one service tenant code can
    reach, so an extra mount there is a path tenant-driven code can reach; a
    missing one is a service that starts and then fails on its first write."""
    config, root, _src = rendered

    def sources(service):
        return {volume["source"]
                for volume in config["services"][service]["volumes"]}

    assert sources("gateway") == {
        f"{root}/control", f"{root}/run/gateway", f"{root}/run/proxy",
        f"{root}/run/spawner", f"{root}/run/admin"}
    assert sources("proxy") == {
        f"{root}/ledger", f"{root}/run/gateway", f"{root}/run/proxy"}
    assert sources("spawner") == {
        "/var/run/docker.sock", f"{root}/tenants", f"{root}/archive",
        f"{root}/staging", f"{root}/run/spawner"}


def test_the_host_path_and_the_container_path_are_the_same_string(rendered):
    """A requirement for the spawner, not a style: it hands host paths to the
    Docker daemon for a tenant container's binds AND opens the same paths
    itself, so a container path that differed would produce binds pointing at
    nothing. The other two follow the same rule so one env value reads the
    same from the host and from inside a container."""
    config, _root, _src = rendered
    for name in ("gateway", "proxy", "spawner"):
        for volume in config["services"][name]["volumes"]:
            assert volume["source"] == volume["target"], (
                f"{name} mounts {volume['source']} at {volume['target']}")


def test_caddy_gets_the_caddyfile_read_only_and_its_own_two_volumes(rendered):
    """The Caddyfile comes from the checkout and Caddy must not be able to
    rewrite it; /data holds the account key and the issued certificate, so it
    is a named volume that survives a rebuild rather than a bind into the
    source tree."""
    config, _root, src = rendered
    volumes = {volume["source"]: volume for volume in config["services"]["caddy"]["volumes"]}
    caddyfile = volumes[f"{src}/hosted/deploy/Caddyfile"]
    assert caddyfile["target"] == "/etc/caddy/Caddyfile"
    assert caddyfile["read_only"] is True
    assert volumes["waku-caddy-data"]["target"] == "/data"
    assert volumes["waku-caddy-config"]["target"] == "/config"
    assert set(config["volumes"]) == {"waku-caddy-data", "waku-caddy-config"}


def test_caddy_is_told_the_four_values_its_caddyfile_substitutes(rendered):
    """The Caddyfile is one file for every operator because {$VAR} is
    substituted from Caddy's own environment. A name missing here renders an
    empty site address or an empty upstream, which Caddy accepts and then
    serves nothing on."""
    config, _root, _src = rendered
    environment = config["services"]["caddy"]["environment"]
    assert environment["WAKU_DOMAIN"] == "example.test"
    assert environment["WAKU_DNS_PROVIDER"] == "route53"
    assert environment["WAKU_ACME_EMAIL"] == "a@b.test"
    assert environment["WAKU_GATEWAY_ADDRESS"] == "127.0.0.1:8787"
