"""The Docker Engine API over its Unix socket, with aiohttp.

No Docker SDK (spec, "The spawner"). The API surface the spawner needs is nine
calls, `GET /version` included, and a library for nine calls is a dependency, a
version to track and a second way to express the container template.

aiohttp appears here and nowhere else in hosted/spawner/: the spec keeps it in
"the outermost HTTP layer", and for the spawner this is that layer -- the
service's own front door is a Unix socket speaking one JSON object per line
(hosted/jsonsock.py), not HTTP.

THE API VERSION IS NEGOTIATED, NOT PINNED. Every versioned path carries a
version the daemon has to accept, and a pinned one is wrong in one direction or
the other for every daemon but the ones it was written beside: Docker 29 refuses
1.43 as "too old" (minimum 1.44) and a daemon from two years ago refuses 1.52 as
too new. `GET /version` is served UNVERSIONED, so it can be asked before a
version is chosen, and it reports the daemon's own range. __aenter__ asks, then
picks -- see choose_api_version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Self

import aiohttp

from hosted import log

# The version this code is WRITTEN AGAINST: the one whose request and response
# shapes hosted/spawner/ was built from. It is a preference, not a pin --
# choose_api_version moves off it when the daemon's range does not contain it.
API_VERSION = "1.43"
DOCKER_SOCKET = Path("/var/run/docker.sock")

_LOG = log.get(__name__)

# Docker API versions are dotted decimals ("1.43", "1.52"). A closed set: this
# is the only shape read as a version, and anything else is refused rather than
# guessed at, because a version this cannot compare is one it cannot choose by.
_VERSION = re.compile(r"[0-9]+(\.[0-9]+)*\Z")


class EngineError(RuntimeError):
    """Docker answered, and the answer was not what was asked for."""


def _components(version: str) -> tuple[int, ...]:
    """A version as the numbers it is made of, for comparing.

    NOT comparable as strings: "1.9" sorts after "1.10", and "1.43" after
    "1.5". Both pairs are real Docker API versions, and either one compared the
    string way sends the daemon a version it rejects.
    """
    if not _VERSION.match(version):
        raise ValueError(f"not a Docker API version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def choose_api_version(ours: str, daemon_max: object, daemon_min: object) -> str:
    """The version to put in every path: ours if the daemon takes it, else the
    nearest end of the daemon's own range.

    `daemon_max` is ApiVersion, the newest the daemon speaks; `daemon_min` is
    MinAPIVersion, the oldest. Both arrive from a JSON body, so both are
    `object` here and are checked rather than trusted.

    When the two cannot be read, or describe an empty range, this REFUSES.
    Falling back to a guess would move the failure to the first real call,
    where the message is a 400 about a version nobody chose on purpose.
    """
    if not isinstance(daemon_max, str) or not isinstance(daemon_min, str):
        raise EngineError(
            f"Docker /version did not report both of its API versions: "
            f"ApiVersion={daemon_max!r}, MinAPIVersion={daemon_min!r}; "
            f"this code is written against {ours}")
    try:
        mine = _components(ours)
        newest = _components(daemon_max)
        oldest = _components(daemon_min)
    except ValueError as exc:
        raise EngineError(
            f"Docker /version reported an API version this cannot read: "
            f"ApiVersion={daemon_max!r}, MinAPIVersion={daemon_min!r}; "
            f"this code is written against {ours} ({exc})") from exc
    if oldest > newest:
        raise EngineError(
            f"Docker reports MinAPIVersion={daemon_min} above ApiVersion="
            f"{daemon_max}, which is not a range to choose a version from; "
            f"this code is written against {ours}")
    if oldest > mine:
        return daemon_min
    if newest < mine:
        return daemon_max
    return ours


# One stream byte, three zero bytes, a big-endian uint32 length.
_FRAME_HEADER = 8


def demultiplex(payload: bytes) -> str:
    """Docker's framed log stream, as plain text. Idempotent on unframed input.

    A container started WITH a TTY gets an unframed stream, and so does a
    daemon that decides not to frame; this cannot tell the two apart by asking,
    so it checks the shape of each frame before trusting it -- a valid header
    is a stream byte in 0..2, three zero bytes, and a length that does not run
    off the end. The first byte that fails that test ends the framed reading
    and the rest is returned as-is, so unframed output is never mangled and a
    truncated stream is never silently dropped.

    Both streams are interleaved in the order the daemon sent them, because
    that is the order they happened; nothing here needs to tell stdout from
    stderr, and a caller that did would want the frames, not this.
    """
    if not payload:
        return ""
    out: list[bytes] = []
    at = 0
    while at + _FRAME_HEADER <= len(payload):
        stream = payload[at]
        if stream > 2 or payload[at + 1:at + 4] != b"\x00\x00\x00":
            break
        size = int.from_bytes(payload[at + 4:at + _FRAME_HEADER], "big")
        end = at + _FRAME_HEADER + size
        if end > len(payload):
            break
        out.append(payload[at + _FRAME_HEADER:end])
        at = end
    out.append(payload[at:])
    return b"".join(out).decode("utf-8", "replace")


class Engine:
    def __init__(self, socket_path: Path = DOCKER_SOCKET, *, timeout: float = 120.0) -> None:
        self._socket_path = socket_path
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._api_version: str | None = None

    @property
    def api_version(self) -> str | None:
        """The version negotiated at connect time; None before __aenter__."""
        return self._api_version

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(self._socket_path)),
            timeout=self._timeout)
        try:
            self._api_version = await self._negotiate_api_version()
        except BaseException:
            # __aexit__ does not run when __aenter__ raises, and an unclosed
            # ClientSession is a warning on a path that is already failing.
            await self._session.close()
            self._session = None
            raise
        return self

    async def _negotiate_api_version(self) -> str:
        """Ask the daemon its range, then choose. Logged once, with both ends.

        The operator who meets the next version problem reads this line: what
        was chosen, what the daemon offered, and what this code was written
        against are the three numbers that turn a 400 into a diagnosis.
        """
        answer = await self._request("GET", "/version", expect=(200,))
        if not isinstance(answer, dict):
            raise EngineError(
                "Docker GET /version did not answer a JSON object: "
                f"{repr(answer)[:200]}")
        daemon_max = answer.get("ApiVersion")
        daemon_min = answer.get("MinAPIVersion")
        chosen = choose_api_version(API_VERSION, daemon_max, daemon_min)
        _LOG.info("docker api version %s: the daemon speaks %s..%s and this code "
                  "is written against %s", chosen, daemon_min, daemon_max, API_VERSION)
        return chosen

    async def __aexit__(self, *exc) -> None:
        self._api_version = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, path: str, **kwargs):
        """One request under the NEGOTIATED version, which every path but
        /version needs in front of it."""
        assert self._api_version is not None, (
            "use Engine as an async context manager: no API version negotiated")
        return await self._request(method, f"/v{self._api_version}{path}", **kwargs)

    async def _request(self, method: str, path: str, *, body: dict | None = None,
                       params: dict | None = None,
                       expect: tuple[int, ...] = (200, 201, 204),
                       raw: bool = False):
        """One request at `path` EXACTLY as given -- no version is added here,
        because /version is asked before there is a version to add."""
        assert self._session is not None, "use Engine as an async context manager"
        # The host is ignored for a Unix socket and must still be syntactically
        # valid; "docker" is the convention and appears in no request.
        url = f"http://docker{path}"
        async with self._session.request(method, url, json=body, params=params) as response:
            payload = await response.read()
            if response.status not in expect:
                raise EngineError(
                    f"{method} {path} -> {response.status}: "
                    f"{payload[:1000].decode('utf-8', 'replace')}")
            if raw:
                return payload
            if not payload:
                return None
            text = payload.decode("utf-8", "replace")
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
        """204 removed, 404 already gone, 409 REMOVAL ALREADY IN PROGRESS.

        All three mean the caller's intent holds. 409 is the AutoRemove
        reaper: a tenant container that has just exited is being removed by
        the daemon, and a DELETE that arrives during that window answers
        "removal of container ... is already in progress". `stop()` calls this
        on the start hot path precisely to clear a name the reaper may be
        mid-way through, so treating that as an error turns the race it exists
        to absorb into an EngineError on every affected start.
        """
        await self._call("DELETE", f"/containers/{container}",
                         params={"force": "true" if force else "false", "v": "true"},
                         expect=(204, 404, 409))

    async def wait(self, container: str) -> int:
        answer = await self._call("POST", f"/containers/{container}/wait", expect=(200,))
        return int(answer["StatusCode"])

    async def inspect(self, container: str) -> dict:
        return await self._call("GET", f"/containers/{container}/json", expect=(200,))

    async def logs(self, container: str) -> str:
        """The container's output, DEMULTIPLEXED, as text an operator can read.

        A non-TTY container's log stream is FRAMED: each chunk carries an
        8-byte header -- one stream byte (0 stdin, 1 stdout, 2 stderr), three
        zero bytes, then a big-endian uint32 length. Returned verbatim, those
        headers land in the middle of the RuntimeError _run_to_completion
        raises, which is the string an operator reads when a backup or a
        provision fails on the VM. They decode as control characters rather
        than raising, so the old shape was unreadable rather than broken --
        which is worse, because nothing said so.
        """
        payload = await self._call(
            "GET", f"/containers/{container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": "200"},
            expect=(200,), raw=True)
        return demultiplex(payload if isinstance(payload, bytes) else b"")

    async def containers(self, *, label: str | None = None,
                         all_states: bool = False) -> list[dict]:
        params = {"all": "true" if all_states else "false"}
        if label:
            params["filters"] = json.dumps({"label": [label]})
        return await self._call("GET", "/containers/json", params=params, expect=(200,))
