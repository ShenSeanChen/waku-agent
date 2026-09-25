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
import shutil
import subprocess
from pathlib import Path

import pytest
import shelllib

# F2, F3 and F4 each append their own names to this set in the same commit that
# adds the script. C3's firewall.sh joins it when C3 lands.
EXPECTED_SCRIPTS = {"checks.sh", "install.sh", "lib.sh", "networks.sh", "tree.sh"}

COMPOSE = shelllib.DEPLOY / "compose.yaml"
SERVICES = ("caddy", "gateway", "proxy", "spawner")


def test_the_deploy_directory_holds_exactly_the_scripts_this_group_wrote():
    found = {path.name for path in shelllib.DEPLOY.glob("*.sh")}
    assert found == EXPECTED_SCRIPTS


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
    assert unit["Service"]["ExecStart"].endswith("/hosted/deploy/firewall.sh")


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


def test_networks_sh_hands_the_daemon_the_addresses_core_tenant_py_holds(tmp_path):
    """networks.sh copies hosted/core/tenant.py's addresses into shell,
    because a shell script cannot import a Python constant. THE DUPLICATE IS
    CHECKED BY BEHAVIOUR: the script is run against a stub daemon and the
    arguments it actually passed are compared with the module. Nothing here
    reads the script's text.

    The flags are compared as a whole dict, not looked up one at a time: an
    extra `--opt` nobody reviewed is as much a finding as a missing one.
    """
    from hosted.core import tenant

    done = shelllib.run(NETWORKS, [], tmp_path=tmp_path, stubs=["docker"],
                        bodies={"docker": _DOCKER_NO_NETWORKS})
    assert done.returncode == 0, done.stderr
    made = _creates(tmp_path)
    assert set(made) == {tenant.TENANT_NETWORK, tenant.INSPECT_NETWORK}

    tenants = made[tenant.TENANT_NETWORK]
    assert tenants == [
        "--driver", "bridge",
        "--subnet", str(tenant.TENANT_SUBNET),
        "--gateway", str(tenant.TENANT_GATEWAY),
        "--opt", f"com.docker.network.bridge.name={tenant.TENANT_BRIDGE}",
        "--opt", "com.docker.network.bridge.enable_icc=false",
        "--ip-range", str(tenant.DYNAMIC_RANGE)]

    inspect = made[tenant.INSPECT_NETWORK]
    assert inspect == [
        "--driver", "bridge",
        "--subnet", str(tenant.INSPECT_SUBNET),
        "--gateway", str(tenant.INSPECT_GATEWAY),
        "--opt", f"com.docker.network.bridge.name={tenant.INSPECT_BRIDGE}",
        "--opt", "com.docker.network.bridge.enable_icc=false"]
    # The inspect bridge takes no --ip-range: an inspect container is given a
    # dynamic address, which is the whole reason it is not on the tenant
    # bridge.
    assert "--ip-range" not in inspect


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


def test_every_service_outranks_a_tenant_for_the_oom_killer(rendered):
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
