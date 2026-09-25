"""DETERMINISTIC EVAL -- the container template's every isolation field.

A DRIFT CHECK, not the guard. It reads the dict template.py builds. The guard
that the kernel honoured any of it is evals/hosted_docker/test_isolation.py,
which reads `docker inspect` on a RUNNING container and then tries the thing
each field is supposed to prevent.

Both exist because they catch different things at different times. This one
runs on every PR, offline, and catches a dropped CapDrop the moment it is
dropped. That one runs where there is a daemon, and catches a CapDrop that is
set and ignored.
"""

from __future__ import annotations

import ast
import asyncio
import inspect as _inspect
from pathlib import Path

import pytest

from hosted.core import tenant
from hosted.core.requests import OPERATIONS, TASKS
from hosted.spawner import docker as docker_mod
from hosted.spawner import service, template, xfsquota

CONFIG = template.SpawnerConfig(
    tenant_root=Path("/srv/waku/tenants"),
    archive_root=Path("/srv/waku/archive"),
    staging_root=Path("/srv/waku/staging"),
    tenant_image="waku-tenant:test",
    services_image="waku-services:test",
    platform_base_url="http://10.88.0.1:8788",
    platform_model="a-model",
    platform_small_model="a-model",
    tenant_disk_bytes=1073741824,
    data_device="/dev/sdb1",
    seccomp_profile='{"defaultAction":"SCMP_ACT_ERRNO"}',
)
TENANT = "k3fq7x2mza4b"
TOKEN = "x" * 43


def _tenant_body():
    return template.tenant_container(CONFIG, tenant_id=TENANT, project_id=2,
                                     timezone="Asia/Shanghai", token=TOKEN)


def _bodies() -> dict:
    return {
        "tenant": _tenant_body(),
        "task": template.task_container(CONFIG, tenant_id=TENANT, command=["true"]),
        "provision": template.task_container(CONFIG, tenant_id=TENANT, command=["true"],
                                             kind=template.KIND_PROVISION),
        "inspect": template.inspect_container(CONFIG, tenant_id=TENANT, host_port=17777),
    }


@pytest.mark.parametrize("builder", ["tenant", "task", "provision", "inspect"])
def test_every_container_drops_every_capability_and_new_privileges(builder):
    """One function sets these for all four, so a task container cannot end up
    with privileges a tenant container does not have."""
    host = _bodies()[builder]["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert any(opt.startswith("seccomp=") for opt in host["SecurityOpt"])
    assert host["ReadonlyRootfs"] is True
    assert host["Tmpfs"] == {"/tmp": template.TMPFS_OPTIONS}
    assert host["PidsLimit"] == template.PIDS_LIMIT
    assert host["RestartPolicy"] == {"Name": "no"}
    assert host["LogConfig"] == {"Type": "local",
                                 "Config": {"max-size": "10m", "max-file": "3"}}


def test_only_the_tenant_container_reaps_itself():
    """AutoRemove is on for the tenant container and off for the three the
    spawner waits on. The daemon starts reaping an AutoRemove container the
    instant it exits, which is the instant `wait` returns, so a throwaway
    container with it on races the reaper with the `logs` read on the next
    line -- an intermittent failure of every start, since every start
    provisions."""
    bodies = _bodies()
    assert bodies["tenant"]["HostConfig"]["AutoRemove"] is True
    for name in ("task", "provision", "inspect"):
        assert bodies[name]["HostConfig"]["AutoRemove"] is False, name


def test_the_provision_kind_differs_from_the_task_kind_only_in_its_label():
    """`kind` is the one parameter this template takes that is not data, and
    the claim made for it is that it changes a label and nothing else. This is
    that claim as a test rather than as a comment."""
    task = template.task_container(CONFIG, tenant_id=TENANT, command=["true"])
    provision = template.task_container(CONFIG, tenant_id=TENANT, command=["true"],
                                        kind=template.KIND_PROVISION)
    assert task.pop("Labels")[template.LABEL_KIND] == template.KIND_TASK
    assert provision.pop("Labels")[template.LABEL_KIND] == template.KIND_PROVISION
    assert task == provision


def test_only_an_operators_container_blocks_a_start():
    """BLOCKING_KINDS is read by _refuse_if_busy. KIND_PROVISION must not be in
    it: every start creates one on its way to starting the container, so a
    tenant's own retried start would refuse itself with {"code": "busy"} and
    tell them they are under maintenance by their own first request."""
    assert template.BLOCKING_KINDS == {template.KIND_TASK, template.KIND_INSPECT}
    assert template.KIND_PROVISION not in template.BLOCKING_KINDS
    assert template.KIND_TENANT not in template.BLOCKING_KINDS
    assert template.BLOCKING_KINDS < template.KINDS


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        template.task_container(CONFIG, tenant_id=TENANT, command=["true"],
                                kind="root")


def test_the_numbers_are_the_specs_numbers():
    assert template.MEMORY_BYTES == 768 * 1024 * 1024
    assert template.NANO_CPUS == 1_000_000_000
    assert template.PIDS_LIMIT == 256
    assert "size=256m" in template.TMPFS_OPTIONS
    assert template.DASHBOARD_PORT == 7777


def test_a_tenant_container_has_a_higher_oom_score_than_every_service():
    """Acceptance 16's oom clause, as a comparison between two names. F1 writes
    SERVICE_OOM_SCORE_ADJ into compose.yaml, so the two numbers are one edit
    apart instead of two literals in two files."""
    assert template.TENANT_OOM_SCORE_ADJ > template.SERVICE_OOM_SCORE_ADJ
    assert _tenant_body()["HostConfig"]["OomScoreAdj"] == template.TENANT_OOM_SCORE_ADJ


def test_the_only_two_host_paths_are_the_tenants_own():
    binds = _tenant_body()["HostConfig"]["Binds"]
    assert binds == [f"/srv/waku/tenants/{TENANT}/home:/data",
                     f"/srv/waku/tenants/{TENANT}/env:/work"]


def test_the_address_is_the_one_the_project_id_derives():
    body = _tenant_body()
    endpoint = body["NetworkingConfig"]["EndpointsConfig"][tenant.TENANT_NETWORK]
    assert endpoint["IPAMConfig"]["IPv4Address"] == tenant.address_for_project(2)
    assert body["HostConfig"]["NetworkMode"] == tenant.TENANT_NETWORK


def test_the_environment_is_the_specs_nine_variables():
    names = [entry.split("=", 1)[0] for entry in _tenant_body()["Env"]]
    assert names == ["WAKU_HOME", "WAKU_DASHBOARD_HOST", "WAKU_DASHBOARD_PORT",
                     "TZ", "HOME", "WAKU_PLATFORM_BASE_URL", "WAKU_PLATFORM_TOKEN",
                     "WAKU_PLATFORM_MODEL", "WAKU_PLATFORM_SMALL_MODEL"]
    assert "TZ=Asia/Shanghai" in _tenant_body()["Env"]
    assert _tenant_body()["WorkingDir"] == "/work"
    assert _tenant_body()["User"] == "10001:10001"


def test_a_task_container_has_no_network_and_an_inspect_container_is_on_its_own_bridge():
    task = template.task_container(CONFIG, tenant_id=TENANT, command=["true"])
    assert task["HostConfig"]["NetworkMode"] == "none"
    assert task["Image"] == CONFIG.services_image
    inspect = template.inspect_container(CONFIG, tenant_id=TENANT, host_port=17777)
    assert inspect["HostConfig"]["NetworkMode"] == tenant.INSPECT_NETWORK
    assert inspect["Image"] == CONFIG.tenant_image


def test_an_inspect_container_gets_no_platform_token():
    """Acceptance 2, by construction: an inspect container has no platform
    variables at all, so there is no key in it to leak."""
    names = [entry.split("=", 1)[0]
             for entry in template.inspect_container(CONFIG, tenant_id=TENANT,
                                                     host_port=17777)["Env"]]
    assert not [name for name in names if name.startswith("WAKU_PLATFORM_")]


def test_an_inspect_container_publishes_on_loopback_only():
    bindings = template.inspect_container(CONFIG, tenant_id=TENANT,
                                          host_port=17777)["HostConfig"]["PortBindings"]
    assert bindings == {"7777/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17777"}]}


def test_every_builder_refuses_a_path_that_is_not_a_tenant_id():
    for bad in ("../../etc", "k3fq7x2mza4", "K3FQ7X2MZA4B", "", "a/b"):
        with pytest.raises(ValueError):
            template.task_container(CONFIG, tenant_id=bad, command=["true"])


def test_a_tenant_container_refuses_a_zone_or_a_token_it_should_not_carry():
    """The four checks tenant_container's docstring claims, all four driven.
    An earlier draft checked the id and the project id and left `timezone` and
    `token` to the wire, while the docstring claimed a guard for the in-process
    path that did not exist for two of the four values that reach the
    container's environment."""
    for bad_zone in ("../../etc/localtime", "Mars/Olympus", "", None):
        with pytest.raises(ValueError):
            template.tenant_container(CONFIG, tenant_id=TENANT, project_id=2,
                                      timezone=bad_zone, token=TOKEN)
    for bad_token in ("", "short", "x" * 44, "x" * 42 + "/"):
        with pytest.raises(ValueError):
            template.tenant_container(CONFIG, tenant_id=TENANT, project_id=2,
                                      timezone="UTC", token=bad_token)
    for bad_project in (0, 1, 65280, True):
        with pytest.raises(ValueError):
            template.tenant_container(CONFIG, tenant_id=TENANT, project_id=bad_project,
                                      timezone="UTC", token=TOKEN)


def test_the_eleven_variable_names_are_pinned_for_group_f():
    """Both directions. A name in the example file that ENV_NAMES does not read
    is a value the operator set and the spawner ignored, which looks configured
    and is not."""
    example = (Path(__file__).resolve().parents[3]
               / "hosted" / "deploy" / "spawner.env.example").read_text(encoding="utf-8")
    in_file = {line.split("=", 1)[0] for line in example.splitlines()
               if line.strip() and not line.startswith("#")}
    assert in_file == set(template.ENV_NAMES)


def test_config_from_env_refuses_an_empty_value_and_accepts_the_typed_escape(tmp_path):
    """NO_QUOTA_DEVICE has to be TYPED. An empty WAKU_DATA_DEVICE is a hard
    refusal, so a misspelling in install.sh cannot turn every tenant's disk
    limit off by accident; the word `none` is the only way to mean it."""
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}', encoding="utf-8")
    env = {name: "x" for name in template.ENV_NAMES}
    env["WAKU_TENANT_DISK_BYTES"] = "1073741824"
    env["WAKU_SECCOMP_PROFILE"] = str(profile)

    env["WAKU_DATA_DEVICE"] = ""
    with pytest.raises(ValueError, match="WAKU_DATA_DEVICE"):
        template.config_from_env(env)

    env["WAKU_DATA_DEVICE"] = docker_mod.NO_QUOTA_DEVICE
    config = template.config_from_env(env)
    assert config.data_device == docker_mod.NO_QUOTA_DEVICE
    assert config.seccomp_profile == '{"defaultAction":"SCMP_ACT_ERRNO"}', (
        "the profile is read as TEXT at startup; a path here would reach the "
        "daemon as a literal filename and be rejected on every create")


def test_the_xfs_commands_are_what_they_claim_to_be():
    """The argv, pinned offline, because no maintainer machine has XFS. The
    guard that the limit BINDS is test_isolation.py on the loop-mounted XFS the
    hosted-docker job makes."""
    assert xfsquota.project_argv("/dev/sdb1", "/srv/waku/tenants/x/home", 7) == [
        "xfs_quota", "-x", "-c", "project -s -p /srv/waku/tenants/x/home 7", "/dev/sdb1"]
    assert xfsquota.limit_argv("/dev/sdb1", 7, 1073741824) == [
        "xfs_quota", "-x", "-c", "limit -p bhard=1073741824 7", "/dev/sdb1"]


def test_only_the_create_path_ever_issues_a_recursive_project_walk(monkeypatch):
    """C-1. `project -s` is a RECURSIVE DESCENT, so it may run only on a
    directory this process has just created and nothing has yet written to.
    Provisioning runs before EVERY start, so running it unconditionally would
    put a root, CAP_SYS_ADMIN walk over a tenant-written tree on the hot path
    of every start of every tenant.

    This drives DockerRuntime._apply_quota with a recording xfsquota, once with
    created=True and once with created=False, and asserts which argv each
    issues. It is the offline half; the Docker half is
    test_tenant_files.py::test_a_second_start_issues_no_recursive_walk.
    """
    issued: list[str] = []

    async def fake_claim(device, path, project_id, hard_bytes):
        issued.append("claim")

    async def fake_set_limit(device, project_id, hard_bytes):
        issued.append("set_limit")

    monkeypatch.setattr(docker_mod.xfsquota, "claim", fake_claim)
    monkeypatch.setattr(docker_mod.xfsquota, "set_limit", fake_set_limit)
    runtime = docker_mod.DockerRuntime(CONFIG, engine=None)

    asyncio.run(runtime._apply_quota(TENANT, Path("/srv/waku/tenants/x/home"), 7,
                                     created=True))
    assert issued == ["claim"]

    issued.clear()
    asyncio.run(runtime._apply_quota(TENANT, Path("/srv/waku/tenants/x/home"), 7,
                                     created=False))
    assert issued == ["set_limit"], (
        "provisioning an EXISTING directory issued a recursive project walk. "
        "That directory is full of whatever the tenant wrote, and this process "
        "is root with CAP_SYS_ADMIN.")


def test_the_typed_escape_sets_no_quota_at_all(monkeypatch, caplog):
    """WAKU_DATA_DEVICE=none issues neither argv, and says so at WARNING on
    every provision -- not once at startup, because a deployment that has
    drifted into this state has no disk limits and should say so in every line
    an operator greps for a tenant id."""
    import logging

    issued: list[str] = []

    async def fake_claim(device, path, project_id, hard_bytes):
        issued.append("claim")

    async def fake_set_limit(device, project_id, hard_bytes):
        issued.append("set_limit")

    monkeypatch.setattr(docker_mod.xfsquota, "claim", fake_claim)
    monkeypatch.setattr(docker_mod.xfsquota, "set_limit", fake_set_limit)
    no_quota = template.SpawnerConfig(**{**CONFIG.__dict__,
                                         "data_device": docker_mod.NO_QUOTA_DEVICE})
    runtime = docker_mod.DockerRuntime(no_quota, engine=None)
    with caplog.at_level(logging.WARNING, logger="hosted.spawner.docker"):
        asyncio.run(runtime._apply_quota(TENANT, Path("/srv/waku/tenants/x/home"), 7,
                                         created=True))
    assert issued == []
    assert TENANT in caplog.text and "NO DISK QUOTA" in caplog.text


def test_set_limit_names_no_path():
    """The reason the repeat path is safe: there is nothing in the argv for
    xfs_quota to walk."""
    argv = xfsquota.limit_argv("/dev/sdb1", 7, 1073741824)
    assert not any("/srv/waku/tenants" in part for part in argv)


def test_the_repair_walk_is_not_a_spawner_verb():
    """xfsquota.repair re-applies a project id to a POPULATED tree, which is
    the walk everything above exists to keep off the hot path. An operator runs
    it knowingly. There must be no socket message that reaches it.

    READ AS CODE, NOT AS TEXT. The brief wrote this as `"xfsquota.repair" not
    in getsource(module)`, and that is red on the module this task actually
    ships: docker.py's provision() docstring says, correctly, that a lost
    project id is an operator's xfsquota.repair. A substring test cannot tell
    a sentence about a function from a call to it. This walks the AST instead,
    so a mention in prose passes and an `xfsquota.repair(...)` or a
    `from ... import repair` fails -- which is the distinction the check is
    for.
    """
    assert "repair" not in OPERATIONS and "repair" not in TASKS
    for module in (docker_mod, service):
        tree = ast.parse(_inspect.getsource(module))
        for node in ast.walk(tree):
            reached = (isinstance(node, ast.Attribute) and node.attr == "repair"
                       and isinstance(node.value, ast.Name)
                       and node.value.id == "xfsquota")
            imported = (isinstance(node, ast.ImportFrom)
                        and (node.module or "").endswith("xfsquota")
                        and any(alias.name == "repair" for alias in node.names))
            assert not (reached or imported), (
                f"{module.__name__} reaches xfsquota.repair at line "
                f"{node.lineno}. It is an operator command on the host, not "
                "something a request can trigger.")
    assert callable(xfsquota.repair)


def test_nothing_in_the_spawner_frees_or_reuses_a_project_id():
    """The retired-project-id tombstone is never pruned.

    next_project_id is monotonic and hosted/core/tenant.py's docstring gives
    the reason: a directory that is moved or deleted keeps its XFS project id,
    so a reused id bills a new tenant for what the old one left behind. A
    tenant's BRIDGE ADDRESS derives from the same id, so a reused id also hands
    a new tenant a deleted tenant's fixed address -- and the fixed-address
    scheme's whole safety claim is that a stale address in the gateway's memory
    "can only reach nothing or the same tenant".

    The spawner is where a "reclaim unused ids" helper would be written,
    because it is the process that sees the directories. So this reads the
    spawner package and refuses the vocabulary. It is a shape check, not a
    proof -- named as such -- and it is here because the defect it guards
    against is silent, permanent and cross-tenant.
    """
    spawner = Path(__file__).resolve().parents[3] / "hosted" / "spawner"
    forbidden = {"free_project_id", "release_project_id", "reclaim_project_id",
                 "reuse_project_id", "compact_project_ids", "prune_project_ids"}
    offenders = {}
    for py in sorted(spawner.rglob("*.py")):
        names = {node.name for node in ast.walk(ast.parse(py.read_text(encoding="utf-8")))
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if found := sorted(names & forbidden):
            offenders[py.name] = found
    assert not offenders, (
        f"the spawner defines {offenders}. Project ids are monotonic and a "
        "freed one is never reused: it carries both an XFS quota and a bridge "
        "address, and reusing it gives a new tenant the old tenant's disk "
        "accounting and the old tenant's address.")


def test_the_committed_seccomp_profile_makes_exactly_the_one_edit():
    """DRIFT CHECK on hosted/image/seccomp.json.

    The GUARD is test_isolation.py::test_changing_the_project_id_of_ones_own_file_fails,
    which runs the two ioctls one value apart inside a real container and has a
    seccomp=unconfined control beside it. This is the offline half: it reads
    the committed profile and asserts the shape make_seccomp.py produces.

    WHY THE SHAPE MATTERS RATHER THAN THE COUNT. The profile's defaultAction is
    SCMP_ACT_ERRNO, so its rules are ALLOWANCES: an added deny for ioctl would
    lose to the unconditional allow it was meant to override. So `ioctl` must
    appear exactly once, in an allow carrying the condition -- not twice, and
    not once unconditionally with a deny somewhere below it.

    IT ASSERTS THE STATED GUARD AND NOT A STRONGER ONE. SCMP_CMP_NE compares
    the full 64-bit register while the kernel reads ioctl's request as a 32-bit
    unsigned int, so an aliased 0x1_401c5820 passes the filter and truncates
    back. seccomp has no masked-not-equal; the residual is named in
    make_seccomp.py's docstring and on G4's checklist, and this test does not
    pretend it is closed.
    """
    import json

    profile = json.loads((Path(__file__).resolve().parents[3] / "hosted" / "image"
                          / "seccomp.json").read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO", (
        "the profile's default is no longer a refusal, so every rule in it "
        "became decoration and `ioctl`'s condition stops meaning anything")

    carrying = [entry for entry in profile["syscalls"]
                if "ioctl" in entry.get("names", [])]
    assert len(carrying) == 1, (
        f"`ioctl` appears in {len(carrying)} rule blocks. make_seccomp.py takes "
        "it out of the unconditional allow and puts it back once, with a "
        "condition; two blocks means one of them is unconditional and wins.")
    only = carrying[0]
    assert only["names"] == ["ioctl"], only["names"]
    assert only["action"] == "SCMP_ACT_ALLOW"
    assert only["args"] == [{"index": 1, "value": 0x401C5820, "op": "SCMP_CMP_NE"}], (
        f"the condition is {only['args']}, not 'argument 1 is not "
        "FS_IOC_FSSETXATTR'. Without exactly this, a tenant can move their own "
        "file into another XFS project and write past their disk limit.")
