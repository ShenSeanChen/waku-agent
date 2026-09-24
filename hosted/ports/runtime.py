"""Where a tenant runs. Docker now; an in-process pool if memory binds (4.4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunningContainer:
    tenant_id: str
    address: str
    port: int


class TenantRuntime(Protocol):
    async def provision(self, tenant_id: str, project_id: int) -> None: ...
    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str) -> RunningContainer: ...
    async def stop(self, tenant_id: str) -> None: ...
    async def list(self) -> list[RunningContainer]: ...
    async def task(self, tenant_id: str, task: str) -> dict: ...
