"""The two internal sockets, and the 10-second bound on a revoked token.

asyncio.run around each coroutine: the repo has no pytest-asyncio and does not
need one for two sockets. No aiohttp either -- these are stdlib Unix sockets
carrying one JSON line each way.
"""

from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import time
from pathlib import Path

import pytest

from hosted import jsonsock
from hosted.core.quota import utc_month
from hosted.gateway.internal import GATEWAY_SOCKET_MODE, serve_token_lookup
from hosted.gateway.proxy_client import read_spend
from hosted.gateway.store import ControlDb
from hosted.proxy.gateway_client import TOKEN_CACHE_SECONDS, TokenCache
from hosted.proxy.internal import PROXY_SOCKET_MODE, serve_spend
from hosted.proxy.ledger import Ledger


async def _closing(server):
    server.close()
    await server.wait_closed()


@pytest.fixture
def sock_dir():
    """A short directory for the sockets. The databases stay on tmp_path.

    An AF_UNIX path is capped at 104 bytes on macOS and 108 on Linux, and
    pytest spells the test's own name into tmp_path -- long enough here to go
    past the cap. Binding there fails with "AF_UNIX path too long", and, worse,
    the two absent-socket tests would then pass for the wrong reason: a path
    nobody can bind looks exactly like a gateway that is down.
    """
    with tempfile.TemporaryDirectory(prefix="waku") as short:
        yield Path(short)


def test_the_proxy_resolves_a_token_it_was_given(tmp_path, sock_dir):
    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
        token = store.issue_token(tenant.id)
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            cache = TokenCache(sock)
            assert await cache.resolve(token) == (tenant.id, "active")
            assert await cache.resolve("T" * 43) is None
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_the_socket_carries_the_mode_install_sh_expects(tmp_path, sock_dir):
    async def run():
        store = ControlDb(tmp_path / "control.db")
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            assert stat.S_IMODE(sock.stat().st_mode) == GATEWAY_SOCKET_MODE == 0o660
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_the_proxy_socket_carries_the_mode_install_sh_expects(tmp_path, sock_dir):
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger)
        try:
            assert stat.S_IMODE(sock.stat().st_mode) == PROXY_SOCKET_MODE == 0o660
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())


def test_the_gateway_socket_answers_nothing_but_token_lookups(tmp_path, sock_dir):
    """The proxy is the one service tenant code can reach. It must not be able
    to enumerate tenants, read a session, or issue anything."""
    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            for request in ({"op": "tenant", "id": tenant.id},
                            {"op": "issue", "tenant": tenant.id},
                            {"op": "session", "value": "c"},
                            {}):
                answer = await jsonsock.ask(sock, request)
                assert "error" in answer, request
                assert tenant.id not in str(answer)
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_a_revoked_token_is_refused_at_the_proxy_within_ten_seconds(tmp_path, sock_dir):
    """Acceptance 13 and 20. Nothing pushes a revocation, so the cache's TTL
    is the bound, and the bound is what the spec names."""
    clock = {"t": 0.0}

    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
        token = store.issue_token(tenant.id)
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            cache = TokenCache(sock, now=lambda: clock["t"])
            assert await cache.resolve(token) == (tenant.id, "active")
            store.revoke_tokens(tenant.id)

            clock["t"] = TOKEN_CACHE_SECONDS - 0.1
            assert await cache.resolve(token) == (tenant.id, "active"), "still cached"

            clock["t"] = TOKEN_CACHE_SECONDS + 0.1
            assert await cache.resolve(token) is None, "past the cache, must be refused"
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())
    assert TOKEN_CACHE_SECONDS == 10.0


def test_a_disabled_tenants_status_reaches_the_proxy(tmp_path, sock_dir):
    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
        token = store.issue_token(tenant.id)
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            store.set_status(tenant.id, "disabled")
            cache = TokenCache(sock, now=lambda: 0.0, ttl=0.0)
            assert await cache.resolve(token) == (tenant.id, "disabled")
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_an_absent_gateway_socket_raises_unreachable_rather_than_denying(sock_dir):
    """During an upgrade the gateway is briefly gone. A 401 there would read
    to the SDK, and then to the tenant, as a bad key; the proxy answers 529
    overloaded_error instead, which the SDK retries."""
    async def run():
        cache = TokenCache(sock_dir / "not-there.sock")
        with pytest.raises(jsonsock.Unreachable):
            await cache.resolve("T" * 43)

    asyncio.run(run())


def test_the_gateway_reads_spend_over_the_proxys_socket(tmp_path, sock_dir):
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        ledger.settle("abcdefghijkl", "2026-09", reserved=0.0, actual=0.25)
        ledger.record_platform_call("abcdefghijkl", 1_789_459_200.0)
        sock = sock_dir / "proxy.sock"
        # 1789905600 is 2026-09-20T12:00:00Z.
        server = await serve_spend(sock, ledger, now=lambda: 1_789_905_600.0)
        try:
            spend = await read_spend(sock, "abcdefghijkl")
            assert spend.settled == 0.25 and spend.reserved == 0.0
            assert spend.last_platform_call == 1_789_459_200.0
            assert spend.month == "2026-09"
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())


def test_the_proxy_socket_answers_nothing_but_spend_reads(tmp_path, sock_dir):
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger)
        try:
            for request in ({"op": "settle", "tenant": "abcdefghijkl", "actual": 0.0},
                            {"op": "reserve", "tenant": "abcdefghijkl"},
                            {}):
                assert "error" in await jsonsock.ask(sock, request), request
            assert ledger.spend("abcdefghijkl", utc_month(time.time())) == (0.0, 0.0)
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())


@pytest.mark.parametrize("line,expected", [
    (b"[1, 2]\n", "not an object"),
    (b'"a string"\n', "not an object"),
    (b"null\n", "not an object"),
    (b"{not json\n", "not JSON"),
    (b"\n", "not JSON"),
])
def test_a_request_that_is_not_an_object_is_refused_not_ignored(tmp_path, sock_dir,
                                                                line, expected):
    """Refused, not dropped. A dropped connection reaches the caller as
    Unreachable, which the proxy turns into 529 overloaded_error and the SDK
    retries -- so silence on a malformed request would be retried forever
    instead of reported once.
    """
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger)
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(line)
            await writer.drain()
            answer = await reader.readline()
            writer.close()
            assert answer != b"", "dropped instead of refused"
            assert json.loads(answer) == {"error": expected}
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())


def test_an_absent_proxy_socket_reads_as_no_answer(sock_dir):
    """The gateway then applies free's turn limit and /account says the spend
    is unavailable. It never guesses a number."""
    async def run():
        assert await read_spend(sock_dir / "not-there.sock", "abcdefghijkl") is None

    asyncio.run(run())


def test_a_peer_that_never_sends_a_newline_cannot_grow_the_server(tmp_path, sock_dir):
    """Both sides of MAX_LINE, because one side alone pins nothing.

    Over the bound the peer is dropped without an answer; under it the same
    connection is served. A bound raised past MAX_LINE fails the first half --
    the server buffers the flood to EOF and answers it -- and a bound lowered
    below MAX_LINE fails the second.
    """
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger, now=lambda: 1_789_905_600.0)
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(b"x" * (jsonsock.MAX_LINE + 10))
            writer.write_eof()
            await writer.drain()
            assert await reader.readline() == b""      # dropped, not answered
            writer.close()

            # A line that fits is served, so the bound is at MAX_LINE and not
            # somewhere short of it.
            request = {"op": "spend", "tenant": "abcdefghijkl"}
            pad = jsonsock.MAX_LINE - len(json.dumps(request).encode()) - len(', "pad": ""') - 1
            request["pad"] = "x" * pad
            assert len(json.dumps(request).encode()) + 1 == jsonsock.MAX_LINE
            answer = await jsonsock.ask(sock, request)
            assert answer["month"] == "2026-09"
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())
