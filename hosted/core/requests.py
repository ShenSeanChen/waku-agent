"""Validate every spawner request before the spawner acts on it.

The spawner is the most privileged process in the deployment: root, with
CAP_SYS_ADMIN and the data disk's block device. Only the gateway can reach it,
so this is not a guard against a hostile caller -- it is the line that keeps a
bug in the gateway from becoming a bug in the spawner.

Allowlists all the way down: the operation, the task, and the keys each
operation takes. A key an operation does not take is a refusal, not something
to ignore, because an ignored key is how a caller comes to believe it asked
for something it did not get.
"""

from __future__ import annotations

from dataclasses import dataclass

from hosted.core.tenant import is_known_timezone, is_project_id, is_proxy_token, is_tenant_id

OPERATIONS = frozenset({"provision", "start", "stop", "list", "tenants", "task"})
TASKS = frozenset({"backup", "restore", "archive", "inspect", "inspect-stop"})

_KEYS: dict[str, frozenset[str]] = {
    "provision": frozenset({"op", "tenant_id", "project_id"}),
    "start": frozenset({"op", "tenant_id", "project_id", "timezone", "token"}),
    "stop": frozenset({"op", "tenant_id"}),
    "list": frozenset({"op"}),
    "tenants": frozenset({"op"}),
    # project_id is OPTIONAL here and required only for `restore`, which is a
    # per-TASK requirement and _REQUIRED is per-OPERATION, so service.handle
    # enforces it rather than parse(). restore recreates the tenant's two
    # directories empty and has to give them back their own project id; the
    # spawner cannot look one up, because the directories have just been
    # removed and "The spawner opens neither database" (spec, The two
    # databases). The gateway already sends this value on `provision` and on
    # `start`, so sending it here adds no new source of truth -- it closes a
    # gap where the spawner would otherwise have had to invent one.
    "task": frozenset({"op", "tenant_id", "task", "project_id"}),
}
_REQUIRED: dict[str, tuple[str, ...]] = {
    "provision": ("tenant_id", "project_id"),
    "start": ("tenant_id", "project_id", "timezone", "token"),
    "stop": ("tenant_id",),
    "list": (),
    "tenants": (),
    "task": ("tenant_id", "task"),
}


class Invalid(ValueError):
    """The request is not one the spawner will act on."""


@dataclass(frozen=True)
class SpawnerRequest:
    op: str
    tenant_id: str = ""
    project_id: int = 0
    timezone: str = "UTC"
    token: str = ""
    task: str = ""


def parse(payload: object) -> SpawnerRequest:
    if not isinstance(payload, dict):
        raise Invalid(f"a request is a JSON object, not {type(payload).__name__}")
    op = payload.get("op")
    if op not in OPERATIONS:
        raise Invalid(f"op must be one of {sorted(OPERATIONS)}, not {op!r}")

    unknown = sorted(set(payload) - _KEYS[op])
    if unknown:
        raise Invalid(f"{op} does not take {unknown}")
    missing = [name for name in _REQUIRED[op] if name not in payload]
    if missing:
        raise Invalid(f"{op} needs {missing}")

    tenant_id = payload.get("tenant_id", "")
    if "tenant_id" in payload and not is_tenant_id(tenant_id):
        raise Invalid(f"tenant_id is not a tenant id: {tenant_id!r}")

    project_id = payload.get("project_id", 0)
    if "project_id" in payload and not is_project_id(project_id):
        raise Invalid(f"project_id is not a project id: {project_id!r}")

    timezone = payload.get("timezone", "UTC")
    if "timezone" in payload and not is_known_timezone(timezone):
        raise Invalid(
            f"timezone is not a zone Python knows: {timezone!r}. The gateway "
            "normalises an unknown zone to UTC before it stores one, so this "
            "means the gateway has a bug.")

    token = payload.get("token", "")
    if "token" in payload and not is_proxy_token(token):
        raise Invalid("token is not a proxy token")

    task = payload.get("task", "")
    if "task" in payload and task not in TASKS:
        raise Invalid(f"task must be one of {sorted(TASKS)}, not {task!r}")

    return SpawnerRequest(op=op, tenant_id=tenant_id, project_id=project_id,
                          timezone=timezone, token=token, task=task)
