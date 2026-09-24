"""The two internal sockets, and the 10-second bound on a revoked token.

asyncio.run around each coroutine: the repo has no pytest-asyncio and does not
need one for two sockets. No aiohttp either -- these are stdlib Unix sockets
carrying one JSON line each way.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
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
from hosted.proxy.gateway_client import (
    TOKEN_CACHE_MAX_ENTRIES,
    TOKEN_CACHE_SECONDS,
    TokenCache,
)
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


class _SickStore:
    """A control.db that is locked, full or corrupt. sqlite3 errors are the
    only things that raise inside the gateway's handler."""

    def tenant_for_token_hash(self, digest):
        raise sqlite3.OperationalError("database is locked")


def test_a_handler_that_raises_answers_an_error_rather_than_going_quiet(tmp_path, sock_dir):
    """Silence reaches the caller as Unreachable, which the proxy answers 529
    overloaded_error and the SDK retries. A gateway whose database is sick
    would then answer every tenant with an endless retry loop while its own log
    filled with one traceback per request -- one reportable error is better.
    """
    async def run():
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, _SickStore())
        try:
            answer = await jsonsock.ask(sock, {"op": "token", "hash": "a" * 64})
            assert answer == {"error": jsonsock.HANDLER_FAILED}
            assert "locked" not in str(answer), "the tenant does not get our internals"
        finally:
            await _closing(server)

    asyncio.run(run())


def test_a_sick_gateway_is_unreachable_at_the_proxy_not_a_bad_key(tmp_path, sock_dir):
    """The other half of the same failure: the proxy must not read "the lookup
    failed" as "this token is unknown", which would be a 401 reading to the
    tenant as a bad key."""
    async def run():
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, _SickStore())
        try:
            cache = TokenCache(sock)
            with pytest.raises(jsonsock.Unreachable):
                await cache.resolve("T" * 43)
            assert len(cache) == 0, "and a failure is not cached"
        finally:
            await _closing(server)

    asyncio.run(run())


def test_a_truncated_answer_is_unreachable_and_not_a_decode_error(tmp_path, sock_dir):
    """A gateway killed between its write and its newline. This is the case
    Unreachable exists for -- "during an upgrade the gateway is briefly gone"
    is how a process dies mid-write -- and a JSONDecodeError escaping here
    would land in a caller prepared only for Unreachable."""
    async def run():
        sock = sock_dir / "gateway.sock"

        async def half_written(reader, writer):
            await reader.readline()
            writer.write(b'{"tenant": "abcdefgh')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(half_written, path=str(sock))
        try:
            cache = TokenCache(sock)
            with pytest.raises(jsonsock.Unreachable):
                await cache.resolve("T" * 43)
            assert await read_spend(sock, "abcdefghijkl") is None, "and the sibling agrees"
        finally:
            await _closing(server)

    asyncio.run(run())


def test_half_an_answer_is_no_answer_at_all(tmp_path, sock_dir):
    """/account shows a figure the tenant is billed on. An answer missing a
    field, or carrying one that is not a number, is not a spend reading, and
    the gateway says the spend is unavailable rather than showing part of it."""
    async def run():
        sock = sock_dir / "proxy.sock"

        async def partial(reader, writer):
            await reader.readline()
            writer.write(json.dumps({"month": "2026-09", "reserved": 0.0}).encode() + b"\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(partial, path=str(sock))
        try:
            assert await read_spend(sock, "abcdefghijkl") is None
        finally:
            await _closing(server)

    asyncio.run(run())


def test_the_client_reads_to_its_own_bound_and_not_to_asyncios(sock_dir, monkeypatch):
    """MAX_LINE is moved for this test, and that is the point.

    The server's bound was made explicit and the client's was left riding
    asyncio's default, which merely happens to be the same 65536 -- so deleting
    `limit=MAX_LINE` from `ask` lands on an identical number and no test at the
    real value can tell the two apart. Moving MAX_LINE separates them: with the
    argument the client refuses at the new bound, without it the client keeps
    reading to 65536 and parses an answer it should have refused.

    Unpinned, raising MAX_LINE would have the server write answers the client's
    own readline rejects, as a ValueError -- which is why `ask` turns that into
    Unreachable rather than letting it escape.
    """
    monkeypatch.setattr(jsonsock, "MAX_LINE", 1024)

    async def run():
        sock = sock_dir / "gateway.sock"

        async def too_much(reader, writer):
            await reader.readline()
            writer.write(json.dumps({"tenant": "a" * 4096}).encode() + b"\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(too_much, path=str(sock), limit=65536)
        try:
            with pytest.raises(jsonsock.Unreachable):
                await jsonsock.ask(sock, {"op": "token", "hash": "a" * 64})
        finally:
            await _closing(server)

    asyncio.run(run())


def test_the_token_cache_is_bounded_however_many_bad_keys_arrive(tmp_path, sock_dir):
    """The cache is keyed on the hash of whatever arrived, so a tenant looping
    bad keys chooses how many keys it has. The proxy is the one service tenant
    code reaches directly; an unbounded dict inside it is a tenant-driven
    allocation with no cap."""
    async def run():
        store = ControlDb(tmp_path / "control.db")
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            cache = TokenCache(sock, now=lambda: 0.0, max_entries=8)
            for n in range(500):
                assert await cache.resolve(f"bogus-token-{n}") is None
            assert len(cache) <= 8, f"{len(cache)} entries held after 500 bad keys"
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_the_shipped_cache_bound_is_four_thousand_and_ninety_six(tmp_path, sock_dir):
    """The test above passes max_entries=8, so it pins the mechanism and not
    the number the proxy actually runs with. This floods past the real default
    with the real constructor: a little under a second, which is worth paying
    for a bound whose whole job is to hold when a tenant is hostile.
    """
    assert TOKEN_CACHE_MAX_ENTRIES == 4096

    async def run():
        store = ControlDb(tmp_path / "control.db")
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            cache = TokenCache(sock, now=lambda: 0.0)       # the shipped bound
            for n in range(TOKEN_CACHE_MAX_ENTRIES + 10):
                await cache.resolve(f"bogus-token-{n}")
            assert len(cache) == TOKEN_CACHE_MAX_ENTRIES
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_an_expired_entry_does_not_hold_a_cache_slot(tmp_path, sock_dir):
    """Every entry expires within the TTL, so pruning the retired ones is what
    normally clears an overflow and the cap is only the backstop."""
    clock = {"t": 0.0}

    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
        token = store.issue_token(tenant.id)
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            cache = TokenCache(sock, now=lambda: clock["t"], max_entries=4)
            for n in range(4):
                await cache.resolve(f"bogus-{n}")
            assert len(cache) == 4

            clock["t"] = TOKEN_CACHE_SECONDS + 1        # everything held is now stale
            assert await cache.resolve(token) == (tenant.id, "active")
            assert len(cache) == 1, "the stale four went, the fresh one stayed"
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("request_,expected", [
    ({"op": "token", "hash": "a" * 64, "extra": 1}, "token does not take ['extra']"),
    ({"op": "token", "hash": "a" * 64, "tenant": "abcdefghijkl"},
     "token does not take ['tenant']"),
])
def test_the_gateway_socket_refuses_a_field_it_does_not_take(tmp_path, sock_dir,
                                                             request_, expected):
    """The shape core.requests already uses for the spawner. An extra field is
    a caller that believes this socket takes something it does not, and
    answering it anyway teaches that it does."""
    async def run():
        store = ControlDb(tmp_path / "control.db")
        sock = sock_dir / "gateway.sock"
        server = await serve_token_lookup(sock, store)
        try:
            assert await jsonsock.ask(sock, request_) == {"error": expected}
        finally:
            await _closing(server)
            store.close()

    asyncio.run(run())


def test_the_proxy_socket_refuses_a_field_it_does_not_take(tmp_path, sock_dir):
    """`month` is the tempting one: the month is the proxy's to choose, and a
    caller that could name it could read another month's spend."""
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger)
        try:
            answer = await jsonsock.ask(
                sock, {"op": "spend", "tenant": "abcdefghijkl", "month": "2026-01"})
            assert answer == {"error": "spend does not take ['month']"}
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())


def test_a_connection_answers_one_request_and_closes(tmp_path, sock_dir):
    """Written down because it is invisible: ask opens a fresh connection per
    call, so nothing in the platform pipelines today. A peer that did would
    have its second question silently ignored."""
    async def run():
        ledger = Ledger(tmp_path / "ledger.db")
        sock = sock_dir / "proxy.sock"
        server = await serve_spend(sock, ledger, now=lambda: 1_789_905_600.0)
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            line = json.dumps({"op": "spend", "tenant": "abcdefghijkl"}).encode() + b"\n"
            writer.write(line + line)
            await writer.drain()
            assert json.loads(await reader.readline())["month"] == "2026-09"
            assert await reader.readline() == b"", "the second question is not answered"
            writer.close()
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
            # somewhere short of it. The padding goes inside the tenant field,
            # because an extra field is refused on its own account.
            empty = len(json.dumps({"op": "spend", "tenant": ""}).encode())
            request = {"op": "spend", "tenant": "a" * (jsonsock.MAX_LINE - empty - 1)}
            assert len(json.dumps(request).encode()) + 1 == jsonsock.MAX_LINE
            answer = await jsonsock.ask(sock, request)
            assert answer["month"] == "2026-09"
        finally:
            await _closing(server)
            ledger.close()

    asyncio.run(run())
