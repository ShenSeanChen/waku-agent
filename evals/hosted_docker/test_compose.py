"""What install.sh actually builds, read back from the kernel and the daemon.

FOUR THINGS ONLY ROOT AND A DAEMON CAN ANSWER:
  - a directory's real owner and mode, after a chown to a UID this machine has
    no user for;
  - whether a bridge really carries enable_icc=false and the right IPAM;
  - whether UID 10003 can open control.db (acceptance 20);
  - whether a tenant container outranks every service for the OOM killer
    (acceptance 16's last clause).

THE WHOLE INSTALL IS NOT RUN HERE. install.sh apt-installs packages, builds
three images and starts Caddy, which would reach out to Let's Encrypt with
credentials this runner does not have. Running it end to end is recorded check
F1-R1, on the VM. What runs here are the two pieces that change the machine --
tree.sh and networks.sh -- plus the two kernel facts the Compose stack's users
and oom_score_adj exist for.

WHAT MOVED OUT OF THIS FILE, and why. The plan's step 13 put the
`docker compose config` assertions here. They need the Compose CLI and no
daemon, so they live in evals/deterministic/hosted/test_deploy_scripts.py
instead: `hosted-docker` is advisory until Sean adds it to branch protection
(status.md open question 2), and the four services' users, mounts and
oom_score_adj are exactly the fields that must not be able to regress through
a required check. The one compose assertion that does need a daemon -- that a
tenant container's oom_score_adj is ABOVE the services' -500 -- is still here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import dockerlib
import pytest
import spawnerlib

from hosted.core import tenant as tenant_mod
from hosted.spawner import template

DEPLOY = Path(__file__).resolve().parents[2] / "hosted" / "deploy"

# path -> (uid, gid, mode). The spec's two tables, as literals: the socket
# directories carry the PEER's group and the setgid bit, so a socket bound
# inside one takes that group even though the serving process is not in it.
EXPECTED_TREE = {
    "config": (0, 0, 0o700),
    "control": (10002, 10002, 0o700),
    "control/backup": (10002, 10002, 0o700),
    "ledger": (10003, 10003, 0o700),
    "ledger/backup": (10003, 10003, 0o700),
    "run": (0, 0, 0o755),
    "run/gateway": (10002, 10003, 0o2750),
    "run/proxy": (10003, 10002, 0o2750),
    "run/spawner": (0, 10002, 0o2750),
    "run/admin": (10002, 10002, 0o700),
    "tenants": (0, 0, 0o700),
    "archive": (0, 0, 0o700),
    "staging": (0, 0, 0o700),
}


def _require_root():
    if os.geteuid() != 0:
        pytest.skip("this tier runs as root in the hosted-docker job; "
                    'run it with `sudo -E env "PATH=$PATH" python -m pytest`')


def test_the_tree_has_the_owners_and_modes_the_sockets_need(tmp_path):
    _require_root()
    root = tmp_path / "waku"
    root.mkdir()
    subprocess.run(["bash", str(DEPLOY / "tree.sh"), str(root)], check=True)

    seen = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            info = path.stat()
            seen[str(path.relative_to(root))] = (
                info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    # BOTH DIRECTIONS. A directory created here that the spec does not name is
    # as much a finding as one that is missing: this tree is what decides who
    # can open which socket.
    assert seen == EXPECTED_TREE


def test_tree_sh_repairs_a_directory_somebody_chmodded(tmp_path):
    """Idempotent means repaired, not skipped. And the chown-before-chmod
    order is what this catches: chown clears the set-group-ID bit, so a script
    that chmodded first would leave 0750 here and the peer would be locked out
    of the socket with EACCES."""
    _require_root()
    root = tmp_path / "waku"
    root.mkdir()
    script = str(DEPLOY / "tree.sh")
    subprocess.run(["bash", script, str(root)], check=True)
    (root / "run" / "gateway").chmod(0o777)
    subprocess.run(["bash", script, str(root)], check=True)
    info = (root / "run" / "gateway").stat()
    assert stat.S_IMODE(info.st_mode) == 0o2750
    assert (info.st_uid, info.st_gid) == (10002, 10003)


def test_networks_sh_creates_both_bridges_with_icc_off():
    """networks.sh copies core/tenant.py's addresses into shell, so this reads
    the RESULT back from the daemon and compares it with the module. The
    duplicate is checked by behaviour rather than by grepping the script.

    evals/deterministic/hosted/test_deploy_scripts.py checks the ARGUMENTS the
    script passes; this checks that the daemon did with them what the arguments
    were meant to make it do.
    """
    for name in (tenant_mod.TENANT_NETWORK, tenant_mod.INSPECT_NETWORK):
        dockerlib.network_remove(name)
    try:
        subprocess.run(["bash", str(DEPLOY / "networks.sh")], check=True)
        raw = subprocess.run(
            ["docker", "network", "inspect", tenant_mod.TENANT_NETWORK,
             tenant_mod.INSPECT_NETWORK],
            capture_output=True, text=True, check=True).stdout
        tenants, inspect = json.loads(raw)

        assert tenants["Options"]["com.docker.network.bridge.enable_icc"] == "false"
        assert tenants["Options"]["com.docker.network.bridge.name"] == tenant_mod.TENANT_BRIDGE
        config = tenants["IPAM"]["Config"][0]
        assert config["Subnet"] == str(tenant_mod.TENANT_SUBNET)
        assert config["Gateway"] == str(tenant_mod.TENANT_GATEWAY)
        assert config["IPRange"] == str(tenant_mod.DYNAMIC_RANGE)

        assert inspect["Options"]["com.docker.network.bridge.enable_icc"] == "false"
        assert inspect["Options"]["com.docker.network.bridge.name"] == tenant_mod.INSPECT_BRIDGE
        inspect_config = inspect["IPAM"]["Config"][0]
        assert inspect_config["Subnet"] == str(tenant_mod.INSPECT_SUBNET)
        assert inspect_config["Gateway"] == str(tenant_mod.INSPECT_GATEWAY)

        # Idempotent: the spec's install is rerunnable, and a second `docker
        # network create` against an existing network exits non-zero with
        # "already exists".
        again = subprocess.run(["bash", str(DEPLOY / "networks.sh")],
                               capture_output=True, text=True, check=False)
        assert again.returncode == 0, again.stderr
    finally:
        for name in (tenant_mod.TENANT_NETWORK, tenant_mod.INSPECT_NETWORK):
            dockerlib.network_remove(name)


def test_each_database_is_reachable_only_by_its_owner(tmp_path, services_image):
    """Acceptance 20, the two clauses that do not need the proxy running.

    This is what compose.yaml's `user:` lines are FOR: the gateway runs as
    10002 and the proxy as 10003 precisely so that neither can open the
    other's database. The third clause -- a revoked token stops working at the
    proxy within 10 seconds -- is group D's, and group B already proves the
    gateway's half of it offline in test_internal_api.py.
    """
    _require_root()
    root = tmp_path / "waku"
    root.mkdir()
    subprocess.run(["bash", str(DEPLOY / "tree.sh"), str(root)], check=True)
    subprocess.run(["install", "-o", "10002", "-g", "10002", "-m", "0600",
                    "/dev/null", str(root / "control" / "control.db")], check=True)
    subprocess.run(["install", "-o", "10003", "-g", "10003", "-m", "0600",
                    "/dev/null", str(root / "ledger" / "ledger.db")], check=True)

    def can_open(uid: int, path: str) -> bool:
        done = dockerlib.run_once(
            services_image, ["sh", "-c", f'cat "{path}" >/dev/null'],
            user=f"{uid}:{uid}", binds=[f"{root}:{root}"], network="none",
            check=False)
        return done.returncode == 0

    assert can_open(10002, f"{root}/control/control.db")
    assert not can_open(10003, f"{root}/control/control.db")
    assert can_open(10003, f"{root}/ledger/ledger.db")
    assert not can_open(10002, f"{root}/ledger/ledger.db")


def test_a_tenant_container_outranks_every_service_for_the_oom_killer(
        spawner, spawner_root):
    """Acceptance 16's last clause, against a real container's HostConfig.

    Both numbers are pinned as literals AND compared, because the property that
    matters is the ORDER: under memory pressure the kernel kills a tenant, never
    the gateway or the proxy. The services' -500 is the literal every service
    in compose.yaml carries, asserted in
    evals/deterministic/hosted/test_deploy_scripts.py.
    """
    del spawner_root
    tenant_id, project_id = "foomcompose4", 9100
    answer = spawnerlib.ask(spawner, {"op": "provision", "tenant_id": tenant_id,
                                      "project_id": project_id})
    assert "error" not in answer, answer
    answer = spawnerlib.ask(spawner, {"op": "start", "tenant_id": tenant_id,
                                      "project_id": project_id,
                                      "timezone": "UTC", "token": "a" * 43})
    assert "error" not in answer, answer
    try:
        info = dockerlib.inspect(
            template.container_name(tenant_id, template.KIND_TENANT))
        tenant_adj = info["HostConfig"]["OomScoreAdj"]
    finally:
        spawnerlib.ask(spawner, {"op": "stop", "tenant_id": tenant_id})
    service_adj = -500
    assert tenant_adj == 500
    assert tenant_adj > service_adj
