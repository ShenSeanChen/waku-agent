"""The spawner's front door: run/spawner/spawner.sock.

Only the gateway mounts run/spawner/ (spec, "How the services run"), and the
directory is root:10002 mode 2750, so the filesystem already decides who may
connect. core/requests.parse then decides what they may ask for, and it is an
allowlist: five operations, five tasks, and an exact key set per operation.
Neither is widened here.
"""

from __future__ import annotations

import os
from pathlib import Path

from hosted import jsonsock, log
from hosted.core import requests as spawner_requests
from hosted.core.tenant import is_project_id
from hosted.spawner import template
from hosted.spawner.docker import Busy, DockerRuntime
from hosted.spawner.engine import Engine

_LOG = log.get(__name__)

SOCKET_PATH = Path(os.environ.get("WAKU_SPAWNER_SOCKET",
                                  "/srv/waku/run/spawner/spawner.sock"))
SOCKET_MODE = 0o660


async def handle(runtime: DockerRuntime, payload: dict) -> dict:
    """One request in, one answer out, in the spec's table's shape."""
    try:
        request = spawner_requests.parse(payload)
    except spawner_requests.Invalid as exc:
        # The gateway is the only caller, so this means the gateway has a bug.
        # It is logged at WARNING and answered plainly: the caller is a service
        # the platform controls, and telling it what was wrong is how the bug
        # gets fixed rather than retried.
        _LOG.warning("refused a request: %s", exc)
        return {"error": str(exc)}

    try:
        if request.op == "provision":
            await runtime.provision(request.tenant_id, request.project_id)
            return {"ok": True}
        if request.op == "start":
            running = await runtime.start(request.tenant_id, request.project_id,
                                          request.timezone, request.token)
            return {"address": running.address, "port": running.port}
        if request.op == "stop":
            await runtime.stop(request.tenant_id)
            return {"ok": True}
        if request.op == "list":
            return {"containers": [
                {"tenant_id": c.tenant_id, "address": c.address, "port": c.port}
                for c in await runtime.list()]}
        if request.op == "task":
            if request.task == "restore" and not is_project_id(request.project_id):
                # Per-TASK requirement, so it lives here and not in
                # _REQUIRED, which is per-operation. Refused before the
                # privileged process does anything.
                return {"error": "task restore needs the tenant's project_id"}
            return await runtime.task(request.tenant_id, request.task,
                                      request.project_id)
    except Busy as exc:
        return {"error": str(exc), "code": "busy"}

    # Unreachable: parse() refuses an op outside OPERATIONS. Kept as a refusal
    # rather than an assert, because "the allowlist and this dispatch disagree"
    # is a real thing to say and a crashed spawner is not.
    _LOG.error("parse accepted op=%r and dispatch has no branch for it", request.op)
    return {"error": f"no handler for {request.op}"}


async def main() -> None:
    log.configure()
    config = template.config_from_env(os.environ)
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with Engine() as engine:
        runtime = DockerRuntime(config, engine)
        server = await jsonsock.serve(
            SOCKET_PATH, lambda payload: handle(runtime, payload), mode=SOCKET_MODE)
        _LOG.info("spawner listening on %s as uid=%s", SOCKET_PATH, os.getuid())
        async with server:
            await server.serve_forever()

