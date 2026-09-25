"""The one shape every container the spawner creates has.

NO BRANCHES AND NO OPTIONS, on purpose. The spec calls this "one template", and
a template with a flag is two templates that agree today. Every isolation
property a tenant container has -- the read-only root, CapDrop ALL,
no-new-privileges, the seccomp profile, the pids limit, the memory and CPU
caps, the oom_score_adj, the log driver, AutoRemove -- is set here, once, in a
function whose output a test can read without a Docker daemon.

WHAT THE OFFLINE TEST PROVES AND WHAT IT DOES NOT.
test_container_template.py reads the dict this builds. That is a DRIFT CHECK:
it catches a dropped CapDrop the moment somebody drops it, offline, on every
PR. It does NOT prove the kernel honoured any of it. The guard is
evals/hosted_docker/test_isolation.py, which reads `docker inspect` on a
RUNNING container and then tries the thing the field is supposed to prevent.
Both exist and neither is the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hosted.core.tenant import (
    INSPECT_NETWORK,
    TENANT_NETWORK,
    address_for_project,
    is_known_timezone,
    is_project_id,
    is_proxy_token,
    is_tenant_id,
    tenant_dirs,
)

DASHBOARD_PORT = 7777

# Spec, the container template table. Every number is the spec's, spelled with
# its unit beside it so the next reader does not have to divide.
MEMORY_BYTES = 768 * 1024 * 1024        # 768 MB
NANO_CPUS = 1_000_000_000               # 1 CPU
PIDS_LIMIT = 256                        # a ulimit would not work: every tenant shares UID 10001
TMPFS_OPTIONS = "rw,noexec,nosuid,nodev,size=256m"
LOG_DRIVER = "local"
LOG_MAX_SIZE = "10m"
LOG_MAX_FILE = "3"
TENANT_UID = 10001

# Under memory pressure the kernel kills a tenant, never the gateway or the
# proxy. F1 sets the services to SERVICE_OOM_SCORE_ADJ in compose.yaml; the
# constant lives here so the two numbers are one edit apart and acceptance 16's
# "a tenant container has a higher oom_score_adj than every service" is a
# comparison between two names rather than two literals in two files.
TENANT_OOM_SCORE_ADJ = 500
SERVICE_OOM_SCORE_ADJ = -500

# The label that makes the maintenance mark survive a gateway restart: the
# spawner refuses `start` for a tenant while a task or inspect container with
# their id exists, so two dashboards never run on one state.db even if the
# gateway's memory was lost.
LABEL_TENANT = "waku.tenant"
LABEL_KIND = "waku.kind"
KIND_TENANT = "tenant"
KIND_TASK = "task"
KIND_INSPECT = "inspect"

# KIND_PROVISION is the spawner's OWN bookkeeping, and it is a separate kind
# for one reason: _refuse_if_busy must not see it. The maintenance mark exists
# to stop a tenant's dashboard starting while an OPERATOR is backing up,
# restoring, archiving or inspecting their data. Provisioning is something
# `start` does to itself, every time, on its way to starting the container --
# so labelling it KIND_TASK makes a tenant's own start refuse a concurrent or
# retried start of the same tenant with {"code": "busy"}. The spec's start path
# retries: "a start that does not answer within 15 seconds returns 'Your
# assistant is taking too long to start. Try again.'", and a container that
# refuses the connection is "looked up again ... and only if it is gone does
# the gateway start it once more". Both retries would land on Busy, and the
# tenant would be told they are under maintenance by their own first request.
KIND_PROVISION = "provision"

# Every kind a container the spawner creates can carry. _refuse_if_busy reads
# BLOCKING_KINDS, so adding a kind is a decision about whether it blocks a
# start rather than something that falls out of a label string.
KINDS = frozenset({KIND_TENANT, KIND_TASK, KIND_INSPECT, KIND_PROVISION})
BLOCKING_KINDS = frozenset({KIND_TASK, KIND_INSPECT})

# Every variable config/spawner.env carries. Pinned in both directions by
# test_container_template.py, because without the pin a misspelling in F1's
# install.sh is silent: config_from_env raises on a missing name, but a name
# nobody reads is a value the operator set and the spawner ignored.
ENV_NAMES = (
    "WAKU_TENANT_ROOT",
    "WAKU_ARCHIVE_ROOT",
    "WAKU_STAGING_ROOT",
    "WAKU_TENANT_IMAGE",
    "WAKU_SERVICES_IMAGE",
    "WAKU_PLATFORM_BASE_URL",
    "WAKU_PLATFORM_MODEL",
    "WAKU_PLATFORM_SMALL_MODEL",
    "WAKU_TENANT_DISK_BYTES",
    "WAKU_DATA_DEVICE",
    "WAKU_SECCOMP_PROFILE",
)

# ONE literal for this path. provision_main.py imports it rather than
# hardcoding the same string, because a plan that says "setting any of them in
# two places is how the two drift" cannot then ship two copies of a path.
SOUL_TEMPLATE_IN_IMAGE = Path("/app/hosted/templates/SOUL.md")


@dataclass(frozen=True)
class SpawnerConfig:
    tenant_root: Path
    archive_root: Path
    staging_root: Path
    tenant_image: str
    services_image: str
    platform_base_url: str
    platform_model: str
    platform_small_model: str
    tenant_disk_bytes: int
    data_device: str
    seccomp_profile: str        # the JSON TEXT, read once at startup


def config_from_env(env: Mapping[str, str]) -> SpawnerConfig:
    missing = [name for name in ENV_NAMES if not env.get(name)]
    if missing:
        raise ValueError(
            f"config/spawner.env is missing {missing}. install.sh writes this "
            "file; every name in template.ENV_NAMES must be in it.")
    return SpawnerConfig(
        tenant_root=Path(env["WAKU_TENANT_ROOT"]),
        archive_root=Path(env["WAKU_ARCHIVE_ROOT"]),
        staging_root=Path(env["WAKU_STAGING_ROOT"]),
        tenant_image=env["WAKU_TENANT_IMAGE"],
        services_image=env["WAKU_SERVICES_IMAGE"],
        platform_base_url=env["WAKU_PLATFORM_BASE_URL"],
        platform_model=env["WAKU_PLATFORM_MODEL"],
        platform_small_model=env["WAKU_PLATFORM_SMALL_MODEL"],
        tenant_disk_bytes=int(env["WAKU_TENANT_DISK_BYTES"]),
        data_device=env["WAKU_DATA_DEVICE"],
        seccomp_profile=Path(env["WAKU_SECCOMP_PROFILE"]).read_text(encoding="utf-8"),
    )


def container_name(tenant_id: str, kind: str) -> str:
    return f"waku-{kind}-{tenant_id}"


def _isolation(config: SpawnerConfig) -> dict:
    """The fields that are the same on every container the spawner creates.

    One function, so a tenant container and a throwaway backup container cannot
    end up with different privileges because somebody edited one call site.
    """
    return {
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": TMPFS_OPTIONS},
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true", f"seccomp={config.seccomp_profile}"],
        "PidsLimit": PIDS_LIMIT,
        "Memory": MEMORY_BYTES,
        "NanoCpus": NANO_CPUS,
        "OomScoreAdj": TENANT_OOM_SCORE_ADJ,
        "RestartPolicy": {"Name": "no"},
        "LogConfig": {"Type": LOG_DRIVER,
                      "Config": {"max-size": LOG_MAX_SIZE, "max-file": LOG_MAX_FILE}},
    }

# AutoRemove is NOT in _isolation(), and the split is deliberate.
#
# A tenant container gets it: the spec says "each start creates a new container
# and each stop removes it (AutoRemove)", and nobody is waiting on its exit.
#
# A throwaway container does NOT get it, because the spawner waits for it and
# then reads its logs. The daemon begins reaping an AutoRemove container the
# instant it exits -- which is the instant POST /containers/{id}/wait returns --
# so GET /containers/{id}/logs races the reaper and intermittently answers 404.
# Every start goes through provision goes through _run_to_completion, so that
# race is an intermittent failure of the platform's hot path, surfacing to the
# tenant as a start that failed for no stated reason and to the operator as
# jsonsock's opaque "the handler failed". _run_to_completion's `finally: remove`
# does the cleanup instead, where nothing is racing it.


def tenant_container(config: SpawnerConfig, *, tenant_id: str, project_id: int,
                     timezone: str, token: str) -> dict:
    """The create body for one tenant's dashboard.

    The four checks at the top are not belt and braces over core/requests.py:
    they are the second half of the same allowlist. requests.parse guards the
    WIRE; this guards the one call the gateway could ever make in-process if
    someone wired DockerRuntime up directly, and it costs four comparisons.

    All four, and not two: an earlier draft checked the id and the project id
    and left `timezone` and `token` to the wire, while the docstring claimed a
    guard for the in-process path that did not exist for two of the four values
    that reach the container's environment.
    """
    if not is_tenant_id(tenant_id):
        raise ValueError(f"not a tenant id: {tenant_id!r}")
    if not is_project_id(project_id):
        raise ValueError(f"not a project id: {project_id!r}")
    if not is_known_timezone(timezone):
        # TZ is the one genuinely tenant-authored string that reaches a
        # container. zoneinfo is what constrains it to a name and not a path.
        raise ValueError(f"not a zone Python knows: {timezone!r}")
    if not is_proxy_token(token):
        raise ValueError("not a proxy token")
    dirs = tenant_dirs(config.tenant_root, tenant_id)
    address = address_for_project(project_id)
    host = _isolation(config)
    host.update({
        # The ONLY two host paths a tenant container ever sees.
        "Binds": [f"{dirs.home}:/data", f"{dirs.env}:/work"],
        "NetworkMode": TENANT_NETWORK,
        # See the note under _isolation: the tenant container is the one kind
        # nothing waits on, so it is the one kind that reaps itself.
        "AutoRemove": True,
    })
    return {
        "Image": config.tenant_image,
        "User": f"{TENANT_UID}:{TENANT_UID}",
        "WorkingDir": "/work",
        "Env": [
            "WAKU_HOME=/data",
            "WAKU_DASHBOARD_HOST=0.0.0.0",
            f"WAKU_DASHBOARD_PORT={DASHBOARD_PORT}",
            f"TZ={timezone}",
            "HOME=/tmp",
            f"WAKU_PLATFORM_BASE_URL={config.platform_base_url}",
            f"WAKU_PLATFORM_TOKEN={token}",
            f"WAKU_PLATFORM_MODEL={config.platform_model}",
            f"WAKU_PLATFORM_SMALL_MODEL={config.platform_small_model}",
        ],
        "Labels": {LABEL_TENANT: tenant_id, LABEL_KIND: KIND_TENANT},
        "ExposedPorts": {f"{DASHBOARD_PORT}/tcp": {}},
        "HostConfig": host,
        "NetworkingConfig": {
            "EndpointsConfig": {
                # The fixed address, every start, derived from the project id.
                TENANT_NETWORK: {"IPAMConfig": {"IPv4Address": address}},
            },
        },
    }


def task_container(config: SpawnerConfig, *, tenant_id: str, command: list[str],
                   extra_binds: tuple[str, ...] = (),
                   kind: str = KIND_TASK) -> dict:
    """A throwaway container for one operation on one tenant's files.

    NetworkMode "none", because none of provision, backup, restore or archive
    talks to anything. The services image, because it holds core/provision.py,
    sqlite3 and zstd. UID 10001, because a tenant can plant symlinks in their
    own mounts and a planted symlink must resolve inside the container.

    `kind` IS THE ONE PARAMETER THIS TEMPLATE TAKES THAT IS NOT DATA, and it
    changes a LABEL and nothing else: every privilege field comes from
    _isolation() and is identical for KIND_TASK and KIND_PROVISION.
    test_every_container_drops_every_capability_and_new_privileges parametrises
    over all four kinds, and
    test_the_provision_kind_differs_from_the_task_kind_only_in_its_label
    asserts the two bodies are equal once the label is removed -- so "it only
    changes a label" is a test, not a comment. See KIND_PROVISION's own note
    for why the distinction has to exist at all.
    """
    if not is_tenant_id(tenant_id):
        raise ValueError(f"not a tenant id: {tenant_id!r}")
    if kind not in KINDS:
        raise ValueError(f"not a container kind: {kind!r}")
    dirs = tenant_dirs(config.tenant_root, tenant_id)
    host = _isolation(config)
    host.update({
        "Binds": [f"{dirs.home}:/data", f"{dirs.env}:/work", *extra_binds],
        "NetworkMode": "none",
        # NOT AutoRemove: the spawner waits for this container and then reads
        # its logs. See the note under _isolation().
        "AutoRemove": False,
    })
    return {
        "Image": config.services_image,
        "User": f"{TENANT_UID}:{TENANT_UID}",
        "WorkingDir": "/work",
        "Env": ["HOME=/tmp", "PYTHONPATH=/app"],
        "Labels": {LABEL_TENANT: tenant_id, LABEL_KIND: kind},
        "Cmd": command,
        "HostConfig": host,
    }


def inspect_container(config: SpawnerConfig, *, tenant_id: str, host_port: int) -> dict:
    """A stock `waku dashboard` on a stopped tenant's data, for an operator.

    The TENANT image, because it runs waku. The INSPECT bridge and not the
    tenant bridge, because it takes a dynamic address and the one rule the
    fixed-address scheme rests on is that nothing but a tenant container is
    ever on a tenant address. Published on the host's LOOPBACK only; the
    operator reaches it over an SSH tunnel (spec, "Work inside a tenant's
    directories").
    """
    if not is_tenant_id(tenant_id):
        raise ValueError(f"not a tenant id: {tenant_id!r}")
    dirs = tenant_dirs(config.tenant_root, tenant_id)
    host = _isolation(config)
    host.update({
        "Binds": [f"{dirs.home}:/data", f"{dirs.env}:/work"],
        "NetworkMode": INSPECT_NETWORK,
        "PortBindings": {f"{DASHBOARD_PORT}/tcp": [{"HostIp": "127.0.0.1",
                                                    "HostPort": str(host_port)}]},
        # NOT AutoRemove: `inspect-stop` removes it, and a container that
        # reaped itself the moment the operator's dashboard crashed would leave
        # the tenant in maintenance with nothing to explain it.
        "AutoRemove": False,
    })
    return {
        "Image": config.tenant_image,
        "User": f"{TENANT_UID}:{TENANT_UID}",
        "WorkingDir": "/work",
        "Env": [
            "WAKU_HOME=/data",
            "WAKU_DASHBOARD_HOST=0.0.0.0",
            f"WAKU_DASHBOARD_PORT={DASHBOARD_PORT}",
            "HOME=/tmp",
        ],
        "Labels": {LABEL_TENANT: tenant_id, LABEL_KIND: KIND_INSPECT},
        "ExposedPorts": {f"{DASHBOARD_PORT}/tcp": {}},
        "HostConfig": host,
    }
