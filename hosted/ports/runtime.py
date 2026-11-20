"""Where a tenant runs. Docker now; an in-process pool if memory binds (4.4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunningContainer:
    tenant_id: str
    address: str
    port: int
    # True when the container was created from an older build of the tenant
    # image than the one the tag names now. Only `list` sets it, because only
    # the spawner can see image ids. An upgrade rebuilds the image under a
    # running container, and the gateway stops a stale container between
    # turns (Launcher.stop_stale) so the next message runs the new code.
    stale: bool = False


class TenantRuntime(Protocol):
    # `connectors` names the connectors the tenant signed in to (spec 029 B2).
    async def provision(self, tenant_id: str, project_id: int,
                        connectors: tuple[str, ...] = ()) -> None: ...
    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str, memory_key: str = "",
                    connectors: tuple[str, ...] = ()) -> RunningContainer: ...
    async def stop(self, tenant_id: str) -> None: ...
    async def list(self) -> list[RunningContainer]: ...
    # EVERY TENANT CONTAINER, and it answers a different question from `list`.
    # `list` answers "may the gateway forward to this container?", so it drops
    # one with no address on the tenant network and one at an address its
    # project id does not derive. Stopping asks "is this container ours?",
    # which is strictly wider -- and the difference is exactly the container
    # whose bind mount a restore is about to delete. Ids only: there is
    # nothing to forward to, so there is nothing to check an address for.
    async def tenant_ids(self) -> list[str]: ...
    # project_id is required for `restore` and ignored by the other four
    # tasks: restore recreates the tenant's two directories empty and has to
    # give them back their own id, and the spawner opens no database to look
    # one up. The default keeps every other call site unchanged.
    async def task(self, tenant_id: str, task: str, project_id: int = 0) -> dict: ...
