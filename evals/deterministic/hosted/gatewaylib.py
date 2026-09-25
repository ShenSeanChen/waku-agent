"""What every group E test needs, in one place.

THE OFFLINE TIER'S spawnerlib.py. Group C put its shared Docker constants and
helpers in evals/hosted_docker/spawnerlib.py rather than importing them out of
a test module, because importing a fixture into a module that also names it as
a test argument is ruff F811 -- seventeen times over, in group C's case. The
same rule applies here: FakeSpawner is needed by test_launcher.py,
test_auth.py, test_gateway.py and test_admin.py, so it lives here and is
imported as a module.

E2 writes this half. E1 step 11 appends the HTTP harness, the signing key and
the wire helpers; E3 step 9 appends the fake container. Nothing is defined
twice and nothing is moved after it is written.
"""

from __future__ import annotations

import asyncio

from hosted.core.tenant import address_for_project
from hosted.ports.runtime import RunningContainer


class Clock:
    """A clock a test moves by hand. Every Fleet, TurnWindow, SessionCache,
    HandoffCodes and Launcher in this group takes one, so no test in the group
    ever sleeps to make time pass."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class FakeSpawner:
    """Records every request and answers from a script.

    `address` and `port` exist so E3 can point a start at a real fake
    container on loopback; left None, a start answers the fixed address the
    tenant's project id derives, which is what the real spawner answers.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.running: dict[str, RunningContainer] = {}
        self.fail_start: Exception | None = None
        self.fail_provision: Exception | None = None
        self.start_delay: float = 0.0
        self.address: str | None = None
        self.port: int = 7777
        # E3 step 14 drives the refused-connection ladder by making the FIRST
        # start answer a port nothing listens on. A list of ports, consumed in
        # order, with the last one repeating.
        self.ports: list[int] = []

    def _port(self) -> int:
        if not self.ports:
            return self.port
        return self.ports.pop(0) if len(self.ports) > 1 else self.ports[0]

    async def provision(self, tenant_id: str, project_id: int) -> None:
        self.requests.append({"op": "provision", "tenant_id": tenant_id,
                              "project_id": project_id})
        if self.fail_provision is not None:
            raise self.fail_provision

    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str) -> RunningContainer:
        self.requests.append({"op": "start", "tenant_id": tenant_id,
                              "project_id": project_id, "timezone": timezone,
                              "token": token})
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.fail_start is not None:
            raise self.fail_start
        container = RunningContainer(
            tenant_id=tenant_id,
            address=self.address or address_for_project(project_id),
            port=self._port())
        self.running[tenant_id] = container
        return container

    async def stop(self, tenant_id: str) -> None:
        self.requests.append({"op": "stop", "tenant_id": tenant_id})
        self.running.pop(tenant_id, None)

    async def list(self) -> list[RunningContainer]:
        self.requests.append({"op": "list"})
        return list(self.running.values())

    async def task(self, tenant_id: str, task: str, project_id: int = 0) -> dict:
        self.requests.append({"op": "task", "tenant_id": tenant_id,
                              "task": task, "project_id": project_id})
        return {"ok": True}


def ops(spawner: FakeSpawner, op: str) -> list[dict]:
    return [request for request in spawner.requests if request["op"] == op]
