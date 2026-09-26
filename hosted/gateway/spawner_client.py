"""The gateway's side of run/spawner/spawner.sock.

The spawner is root with CAP_SYS_ADMIN and the data disk's block device, and
this is the only thing that speaks to it. hosted/core/requests.py already
allowlists the six operations, the five tasks and the exact key set of each,
so this module's job is narrow: send exactly those keys, give each call a
timeout that matches the work it asks for, and turn the spawner's two answer
shapes into a value or an exception.

WHY THE THREE TIMEOUTS ARE DIFFERENT NUMBERS. jsonsock.ask defaults to two
seconds, which is right for the token lookup it was written for and wrong for
every call here. `start` runs a provisioning container and then creates and
starts a tenant container; `task restore` unpacks a tenant's whole tree. A
single timeout would have to be the largest, and then a spawner that has died
holds a browser request open for fifteen minutes.

WHY `busy` IS ITS OWN EXCEPTION. The spawner answers {"error": ..., "code":
"busy"} when a task or inspect container holds the tenant. That is not a
failure: it is the maintenance mark, enforced by the spawner so it survives a
gateway restart (spec, "Maintenance"), and the gateway answers it with the
maintenance sentence rather than with a five-hundred.
"""

from __future__ import annotations

from pathlib import Path

from hosted import jsonsock
from hosted.core.tenant import is_tenant_id
from hosted.ports.runtime import RunningContainer

# Long enough that the gateway's own 15-second start budget is what expires
# first, so the tenant is told "taking too long to start" rather than being
# handed a socket error the sentence for does not exist.
START_ASK_TIMEOUT = 30.0
# An operator task: a backup, a restore, an archive. Nobody is watching a
# browser tab on these; the admin command is.
TASK_ASK_TIMEOUT = 900.0
# provision, stop and list are one Docker API call each.
QUICK_ASK_TIMEOUT = 15.0

BUSY_CODE = "busy"


class SpawnerError(RuntimeError):
    """The spawner answered with an error object."""


class SpawnerBusy(SpawnerError):
    """A task or inspect container holds this tenant: the maintenance mark."""


class SpawnerClient:
    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    async def _ask(self, payload: dict, *, timeout: float) -> dict:
        answer = await jsonsock.ask(self._socket_path, payload, timeout=timeout)
        if "error" in answer:
            message = str(answer["error"])
            if answer.get("code") == BUSY_CODE:
                raise SpawnerBusy(message)
            raise SpawnerError(message)
        return answer

    async def provision(self, tenant_id: str, project_id: int) -> None:
        await self._ask({"op": "provision", "tenant_id": tenant_id,
                         "project_id": project_id}, timeout=QUICK_ASK_TIMEOUT)

    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str) -> RunningContainer:
        answer = await self._ask(
            {"op": "start", "tenant_id": tenant_id, "project_id": project_id,
             "timezone": timezone, "token": token}, timeout=START_ASK_TIMEOUT)
        return self._container(tenant_id, answer)

    async def stop(self, tenant_id: str) -> None:
        await self._ask({"op": "stop", "tenant_id": tenant_id},
                        timeout=QUICK_ASK_TIMEOUT)

    async def list(self) -> list[RunningContainer]:
        answer = await self._ask({"op": "list"}, timeout=QUICK_ASK_TIMEOUT)
        entries = answer.get("containers")
        if not isinstance(entries, list):
            raise SpawnerError(f"list answered {type(entries).__name__}, not a list")
        for entry in entries:
            # Each ELEMENT too, not just the list. An entry that is not a dict
            # makes entry.get raise AttributeError, which is outside the
            # (SpawnerError, OSError) every caller of resync catches -- so a
            # malformed answer would surface as a 500 on some tenant's request
            # instead of the refusal _container's docstring promises.
            if not isinstance(entry, dict):
                raise SpawnerError(
                    f"list answered an entry that is {type(entry).__name__}, "
                    "not an object")
        return [self._container(str(entry.get("tenant_id", "")), entry)
                for entry in entries]

    async def tenant_ids(self) -> list[str]:
        """Every tenant container the spawner labelled, by id.

        Validated the same way `list`'s answer is, and for the same reason:
        what comes back is joined to a container name and stopped, so a shape
        that is not a tenant id is a refusal rather than a thing to act on.
        """
        answer = await self._ask({"op": "tenants"}, timeout=QUICK_ASK_TIMEOUT)
        entries = answer.get("tenant_ids")
        if not isinstance(entries, list):
            raise SpawnerError(
                f"tenants answered {type(entries).__name__}, not a list")
        for entry in entries:
            if not isinstance(entry, str) or not is_tenant_id(entry):
                raise SpawnerError(f"tenants answered {entry!r}, not a tenant id")
        return list(entries)

    async def task(self, tenant_id: str, task: str, project_id: int = 0) -> dict:
        payload = {"op": "task", "tenant_id": tenant_id, "task": task}
        if project_id:
            # core/requests.py admits project_id on `task` and service.handle
            # requires it for `restore` only. Sending a zero would be refused
            # by is_project_id, so it is omitted rather than defaulted.
            payload["project_id"] = project_id
        return await self._ask(payload, timeout=TASK_ASK_TIMEOUT)

    @staticmethod
    def _container(tenant_id: str, answer: dict) -> RunningContainer:
        """An answer that is not the shape the spec's table promises is an
        error, not a container with empty fields. The gateway FORWARDS to
        whatever comes out of here, so a blank address would become a request
        to `http://:7777`, which aiohttp resolves to the gateway's own host."""
        address = answer.get("address")
        port = answer.get("port")
        if not isinstance(address, str) or not address:
            raise SpawnerError(f"no address in the spawner's answer: {answer!r}")
        if not isinstance(port, int) or port <= 0:
            raise SpawnerError(f"no port in the spawner's answer: {answer!r}")
        return RunningContainer(tenant_id=tenant_id, address=address, port=port)
