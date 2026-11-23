"""The two requests the gateway itself makes to a tenant container (spec 026).

  hand_over  POST /api/events: give the container the events the gateway kept
             for its tenant while the container was stopped
  next_work  GET /api/alwayson/next: ask what the container still has to do,
             before the idle loop stops it

Both go straight to the container's address on the tenant network. Neither
passes through forward.py, which carries a browser's requests and refuses
both routes (hosted/core/policy.py, GATEWAY_ONLY).

The container is stock waku and knows nothing about the gateway. Neither
function raises: a container that is slow, gone or answering something else
is an answer of False or None, and the caller's row stays as it was.
"""

from __future__ import annotations

import aiohttp

from hosted import log
from hosted.gateway.forward import listening
from hosted.ports.control import TenantEvent
from hosted.ports.runtime import RunningContainer

_LOG = log.get(__name__)

EVENTS_PATH = "/api/events"
NEXT_PATH = "/api/alwayson/next"
# One local request to a process on the same VM. A container that takes
# longer than this is not going to answer.
TIMEOUT_SECONDS = 10


def _url(running: RunningContainer, path: str) -> str:
    return f"http://{running.address}:{running.port}{path}"


class ContainerApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)

    async def listening(self, running: RunningContainer) -> bool:
        """Whether the container's dashboard accepts a connection yet. The
        spawner answers a start before the dashboard has bound its port."""
        return await listening(running)

    async def hand_over(self, running: RunningContainer,
                        events: list[TenantEvent]) -> bool:
        """True only when the container answered 200, which is when it has
        stored every event of the batch."""
        if not await self.listening(running):
            _LOG.info("tenant=%s is not listening yet; its events wait",
                      running.tenant_id)
            return False
        body = {"events": [{"source": e.source, "external_id": e.external_id,
                            "session_id": e.session_id, "payload": e.payload}
                           for e in events]}
        try:
            async with self._session.post(_url(running, EVENTS_PATH), json=body,
                                          timeout=self._timeout) as answer:
                status = answer.status
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            _LOG.warning("hand-over to tenant=%s failed: %s", running.tenant_id,
                         type(exc).__name__)
            return False
        if status != 200:
            _LOG.warning("tenant=%s answered %s to a hand-over of %s events",
                         running.tenant_id, status, len(events))
        return status == 200

    async def next_work(self, running: RunningContainer) -> dict | None:
        """The container's own report, or None when it gave none: it did not
        answer, it answered an error, or its image is older than spec 026 and
        has no such route."""
        try:
            async with self._session.get(_url(running, NEXT_PATH),
                                         timeout=self._timeout) as answer:
                if answer.status != 200:
                    return None
                report = await answer.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return None
        return report if isinstance(report, dict) else None
