"""DETERMINISTIC EVAL -- which Docker API version the spawner speaks.

Found on a real VM, not here. `hosted/spawner/engine.py` pinned every path to
`/v1.43`; the VM's Docker 29.1.3 speaks 1.44..1.52 and answers every one of
them `400 client version 1.43 is too old`. The spawner's first call is
`/containers/json` from the gateway's startup resync, so the gateway crashed,
the container restart-looped, and `install.sh` timed out waiting for /login.

CI did not see it: the `hosted-docker` job runs on ubuntu-24.04 runners whose
daemon still accepts 1.43. A pin is wrong in one direction on a new daemon and
in the other on an old one, so these tests are about the NEGOTIATION and about
the refusal, not about any one number.

THERE IS NO DOCKER DAEMON ON A DEVELOPER'S MACHINE, and none is needed: the
Engine's whole contact with Docker is aiohttp's ClientSession, so the session is
a stub here and every version range is a dict. What this CANNOT check offline is
that a real daemon reports ApiVersion and MinAPIVersion at all -- that rests on
reading Docker's /version documentation and on the 400 quoted above, which names
the same two numbers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from hosted.spawner.engine import API_VERSION, Engine, EngineError, choose_api_version

SOCKET = Path("/nonexistent/there-is-no-daemon-here.sock")


class _Answer:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _Daemon:
    """Just enough aiohttp.ClientSession for Engine: request() returning an
    async context manager with .status and .read(), and close().

    Answers are keyed by the path Engine built, so a test can see whether
    /version was asked WITHOUT a version in front of it -- which is the fact
    that makes negotiating possible at all.
    """

    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self._answers = answers
        self.paths: list[str] = []
        self.closed = False

    def request(self, method, url, json=None, params=None):
        assert url.startswith("http://docker"), url
        path = url[len("http://docker"):]
        self.paths.append(path)
        return _Answer(*self._answers.get(path, (200, b"[]")))

    async def close(self) -> None:
        self.closed = True


def _version_answer(**fields) -> dict[str, tuple[int, bytes]]:
    return {"/version": (200, json.dumps(fields).encode("utf-8"))}


def _engine(monkeypatch, daemon: _Daemon) -> Engine:
    """An Engine whose aiohttp session is `daemon`, built the way __aenter__
    builds it, so the negotiation under test is the real one."""
    from hosted.spawner import engine as engine_mod

    monkeypatch.setattr(engine_mod.aiohttp, "UnixConnector", lambda **kw: None)
    monkeypatch.setattr(engine_mod.aiohttp, "ClientSession", lambda **kw: daemon)
    return engine_mod.Engine(SOCKET)


def _negotiated(monkeypatch, daemon: _Daemon) -> str | None:
    """Connect, make one ordinary call, disconnect. Returns what was chosen."""
    chosen: list[str | None] = []

    async def go() -> None:
        async with _engine(monkeypatch, daemon) as engine:
            chosen.append(engine.api_version)
            await engine.containers()

    asyncio.run(go())
    return chosen[0]


# --- the three outcomes, and the path each one produces --------------------


def test_a_daemon_whose_minimum_is_above_ours_gets_the_daemons_minimum(monkeypatch):
    """The live failure, as measured: Docker 29.1.3 reports 1.44..1.52 and
    refuses 1.43. 1.44 is the oldest thing it will answer, so 1.44 is what goes
    in the path -- and the path is asserted, because choosing right and then
    sending something else is the same outage."""
    daemon = _Daemon(_version_answer(ApiVersion="1.52", MinAPIVersion="1.44"))
    assert _negotiated(monkeypatch, daemon) == "1.44"
    assert daemon.paths == ["/version", "/v1.44/containers/json"]


def test_a_daemon_whose_maximum_is_below_ours_gets_the_daemons_maximum(monkeypatch):
    """The other direction, which the pin will meet as soon as it is bumped: an
    older daemon that has never heard of the version this code prefers. 1.41 is
    the newest it speaks, so 1.41 is what it is asked for."""
    daemon = _Daemon(_version_answer(ApiVersion="1.41", MinAPIVersion="1.24"))
    assert _negotiated(monkeypatch, daemon) == "1.41"
    assert daemon.paths == ["/version", "/v1.41/containers/json"]


def test_a_daemon_whose_range_contains_ours_gets_ours(monkeypatch):
    """The presence half. Without it every clamp above passes against a
    negotiation that always returns an end of the daemon's range, and the
    request shapes this code was written from would silently be sent under a
    version they were not written for."""
    daemon = _Daemon(_version_answer(ApiVersion="1.52", MinAPIVersion="1.24"))
    assert _negotiated(monkeypatch, daemon) == "1.43"
    assert daemon.paths == ["/version", "/v1.43/containers/json"]


def test_version_is_negotiated_once_per_connection_not_once_per_call(monkeypatch):
    """/version is one round trip at connect time. Asked again on every call it
    would double the traffic on the start hot path and give two calls in one
    operation two different versions if the daemon were upgraded between them."""
    daemon = _Daemon(_version_answer(ApiVersion="1.52", MinAPIVersion="1.44"))

    async def go() -> None:
        async with _engine(monkeypatch, daemon) as engine:
            await engine.containers()
            await engine.inspect("waku-tenant-k3fq7x2mza4b")
            await engine.logs("waku-tenant-k3fq7x2mza4b")

    asyncio.run(go())
    assert daemon.paths.count("/version") == 1
    assert daemon.paths[1:] == ["/v1.44/containers/json",
                                "/v1.44/containers/waku-tenant-k3fq7x2mza4b/json",
                                "/v1.44/containers/waku-tenant-k3fq7x2mza4b/logs"]


def test_the_chosen_version_is_dropped_when_the_connection_closes(monkeypatch):
    """A version negotiated with one daemon is not a version to reuse against
    the next one, and an Engine outside its context manager has no session to
    have negotiated with."""
    engine = _engine(monkeypatch, _Daemon(
        _version_answer(ApiVersion="1.52", MinAPIVersion="1.44")))
    assert engine.api_version is None

    async def go() -> None:
        async with engine:
            assert engine.api_version == "1.44"

    asyncio.run(go())
    assert engine.api_version is None


# --- ordering: the trap this was always going to fall into -----------------


@pytest.mark.parametrize("ours, daemon_max, daemon_min, expected", [
    # "1.9" sorts AFTER "1.10" as a string, and 1.9 is the older version.
    ("1.9", "1.52", "1.10", "1.10"),   # clamped up; compared as strings, to 1.52
    ("1.10", "1.9", "1.5", "1.9"),     # clamped down; as strings, to 1.5
    ("1.10", "1.52", "1.9", "1.10"),   # ours fits; as strings, clamped to 1.9
    # The same trap at the numbers actually in play: "1.5" > "1.43" as a string.
    ("1.43", "1.52", "1.5", "1.43"),
    ("1.43", "1.52", "1.44", "1.44"),        # the live daemon's own range
    ("1.43", "1.43", "1.43", "1.43"),        # equality at both ends
    ("1.43", "1.43.2", "1.43.1", "1.43.1"),  # more than two components
])
def test_versions_are_compared_as_numbers_not_as_strings(ours, daemon_max,
                                                         daemon_min, expected):
    """The first FOUR rows answer differently under a string comparison than
    under a numeric one -- 1.9 against 1.10 in both directions and then with
    ours between them, and the 1.43-against-1.5 pair actually in play -- so a
    `<` between two strings turns exactly those four red. The last three agree
    under either comparison and are here for the boundaries instead: the live
    daemon's range, equality at both ends, and a version with three
    components."""
    assert choose_api_version(ours, daemon_max, daemon_min) == expected


# --- refusals: a version nobody chose on purpose fails further away --------


def test_a_range_whose_minimum_exceeds_its_maximum_is_refused_naming_both():
    """Nothing to choose from, and no end of it is safe to send. The operator
    reading the refusal needs both numbers, because the pair is the bug."""
    with pytest.raises(EngineError) as caught:
        choose_api_version("1.43", "1.44", "1.50")
    message = str(caught.value)
    assert "1.44" in message and "1.50" in message


@pytest.mark.parametrize("fields", [
    {},                                                 # neither field
    {"ApiVersion": "1.52"},                             # no minimum
    {"MinAPIVersion": "1.24"},                          # no maximum
    {"ApiVersion": "1.52", "MinAPIVersion": None},      # present and null
    {"ApiVersion": 1.52, "MinAPIVersion": 1.24},        # numbers, not strings
    {"ApiVersion": "1.52", "MinAPIVersion": ""},        # empty string
    {"ApiVersion": "v1.52", "MinAPIVersion": "1.24"},   # the v belongs in the path
    {"ApiVersion": "1.52", "MinAPIVersion": "1.24-rc1"},
    {"ApiVersion": "latest", "MinAPIVersion": "1.24"},
    {"ApiVersion": "1..52", "MinAPIVersion": "1.24"},
    {"ApiVersion": "1.52 ", "MinAPIVersion": "1.24"},   # a version is not trimmed
])
def test_a_version_answer_it_cannot_read_is_refused_rather_than_guessed(fields,
                                                                       monkeypatch):
    """A closed set: a dotted decimal is the only shape read as a version. The
    alternative is a fallback to the preferred number, which is exactly the pin
    this replaces -- and it would fail at the first real call, with a message
    about a version nobody chose."""
    daemon = _Daemon(_version_answer(**fields))
    with pytest.raises(EngineError):
        asyncio.run(_open(monkeypatch, daemon))
    assert daemon.paths == ["/version"], "a versioned call went out anyway"
    assert daemon.closed, "the refused connection left its session open"


@pytest.mark.parametrize("answer", [
    (200, b""),                    # empty body
    (200, b"not json at all"),     # unparseable
    (200, b"[1, 2]"),              # JSON, but not an object
    (200, b'"1.52"'),              # JSON, but not an object either
    (500, b"boom"),                # the daemon failed the call
    (404, b""),                    # a socket that is not Docker
])
def test_a_version_endpoint_that_does_not_answer_a_range_is_refused(answer,
                                                                   monkeypatch):
    """The unix socket the spawner is handed might not be Docker's, and a
    daemon mid-restart answers 500. Either way there is no range, so there is
    no version, so there is nothing to send."""
    daemon = _Daemon({"/version": answer})
    with pytest.raises(EngineError):
        asyncio.run(_open(monkeypatch, daemon))
    assert daemon.paths == ["/version"]
    assert daemon.closed


async def _open(monkeypatch, daemon: _Daemon) -> None:
    async with _engine(monkeypatch, daemon):
        pass


# --- what the operator reads ----------------------------------------------


def test_the_negotiated_version_is_logged_once_with_both_daemon_numbers(monkeypatch,
                                                                       caplog):
    """The next version problem is someone else's, on a daemon nobody here has.
    One INFO line carrying the choice AND the range it was chosen from is the
    difference between a 400 and a diagnosis."""
    daemon = _Daemon(_version_answer(ApiVersion="1.52", MinAPIVersion="1.44"))
    with caplog.at_level(logging.INFO, logger="hosted.spawner.engine"):
        assert _negotiated(monkeypatch, daemon) == "1.44"
    lines = [record for record in caplog.records
             if record.name == "hosted.spawner.engine"]
    assert len(lines) == 1, [record.getMessage() for record in lines]
    assert lines[0].levelno == logging.INFO
    said = lines[0].getMessage()
    for number in ("1.44", "1.52", API_VERSION):
        assert number in said, said
