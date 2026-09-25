"""The Docker Engine API over its Unix socket, with aiohttp.

No Docker SDK (spec, "The spawner"). The API surface the spawner needs is nine
calls, and a library for nine calls is a dependency, a version to track and a
second way to express the container template.

aiohttp appears here and nowhere else in hosted/spawner/: the spec keeps it in
"the outermost HTTP layer", and for the spawner this is that layer -- the
service's own front door is a Unix socket speaking one JSON object per line
(hosted/jsonsock.py), not HTTP.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import aiohttp

from hosted import log

API_VERSION = "v1.43"
DOCKER_SOCKET = Path("/var/run/docker.sock")

_LOG = log.get(__name__)


class EngineError(RuntimeError):
    """Docker answered, and the answer was not what was asked for."""


class Engine:
    def __init__(self, socket_path: Path = DOCKER_SOCKET, *, timeout: float = 120.0) -> None:
        self._socket_path = socket_path
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(self._socket_path)),
            timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, path: str, *, body: dict | None = None,
                    params: dict | None = None, expect: tuple[int, ...] = (200, 201, 204)):
        assert self._session is not None, "use Engine as an async context manager"
        # The host is ignored for a Unix socket and must still be syntactically
        # valid; "docker" is the convention and appears in no request.
        url = f"http://docker/{API_VERSION}{path}"
        async with self._session.request(method, url, json=body, params=params) as response:
            text = await response.text()
            if response.status not in expect:
                raise EngineError(
                    f"{method} {path} -> {response.status}: {text[:1000]}")
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text

    async def create(self, name: str, body: dict) -> str:
        answer = await self._call("POST", "/containers/create",
                                  body=body, params={"name": name}, expect=(201,))
        return answer["Id"]

    async def start(self, container: str) -> None:
        # 304 is "already started", which a retry after a timeout reaches and
        # which is not an error: the container the caller wanted is running.
        await self._call("POST", f"/containers/{container}/start", expect=(204, 304))

    async def stop(self, container: str, *, timeout: int = 10) -> None:
        # 304 already stopped, 404 already gone (AutoRemove). Both mean the
        # caller's intent holds, and `stop` must be idempotent because the
        # gateway calls it before every start.
        await self._call("POST", f"/containers/{container}/stop",
                         params={"t": str(timeout)}, expect=(204, 304, 404))

    async def remove(self, container: str, *, force: bool = True) -> None:
        await self._call("DELETE", f"/containers/{container}",
                         params={"force": "true" if force else "false", "v": "true"},
                         expect=(204, 404))

    async def wait(self, container: str) -> int:
        answer = await self._call("POST", f"/containers/{container}/wait", expect=(200,))
        return int(answer["StatusCode"])

    async def inspect(self, container: str) -> dict:
        return await self._call("GET", f"/containers/{container}/json", expect=(200,))

    async def logs(self, container: str) -> str:
        answer = await self._call("GET", f"/containers/{container}/logs",
                                  params={"stdout": "true", "stderr": "true", "tail": "200"},
                                  expect=(200,))
        return answer if isinstance(answer, str) else str(answer)

    async def containers(self, *, label: str | None = None,
                         all_states: bool = False) -> list[dict]:
        params = {"all": "true" if all_states else "false"}
        if label:
            params["filters"] = json.dumps({"label": [label]})
        return await self._call("GET", "/containers/json", params=params, expect=(200,))
