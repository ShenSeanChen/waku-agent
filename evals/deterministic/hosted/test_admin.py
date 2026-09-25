"""The admin path and the running gateway agree -- acceptance 23.

The restart-all half is E3's, because "the gateway forwards to the new
address" needs a forwarder that reads an address.

EVERY TEST HERE THAT MATTERS GOES OVER THE SOCKET. admin.handle is reachable
in-process, and test_auth.py calls it that way once for brevity, but the ones
here -- the allowlist and the disable -- write a JSON line to a real Unix
socket, because the socket is what tenant.sh has and the in-process call is
not.
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

import pytest
from gatewaylib import sign, signed_in_on_the_tenant_host

from hosted import jsonsock
from hosted.gateway import admin


@pytest.fixture
def sock_dir():
    """A short directory: AF_UNIX caps a path at 104 bytes on macOS, and
    pytest spells the test's name into tmp_path. test_internal_api.py has the
    same fixture for the same reason."""
    with tempfile.TemporaryDirectory(prefix="waku") as short:
        yield Path(short)


@pytest.mark.parametrize("request_body, refusal", [
    ({"op": "nope"}, "op must be one of"),
    ({"op": "disable"}, "disable needs a tenant id or email"),
    ({"op": "disable", "tenant": "a@b.c", "force": True},
     "disable does not take ['force']"),
    ({"op": "restart-all", "tenant": "a@b.c"},
     "restart-all does not take ['tenant']"),
    ({"op": "status", "path": "/etc/passwd"}, "status does not take ['path']"),
])
def test_the_admin_socket_refuses_anything_outside_its_allowlist(
        harness, sock_dir, request_body, refusal):
    """The REFUSAL is read, not merely the presence of an error.

    Three of these five name a tenant nobody has, so "some error came back"
    would stay true with the key allowlist deleted -- the not-found branch
    would answer instead, and the test would pass while the guard it is
    named after was gone. The sentence says which branch answered.
    """
    async def run():
        await harness.start()
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        answer = await jsonsock.ask(path, request_body)
        server.close()
        await server.wait_closed()
        await harness.stop()
        return answer

    answer = asyncio.run(run())
    assert refusal in answer.get("error", "")
    assert harness.spawner.requests == []


def test_the_admin_socket_is_reachable_only_by_its_owner(harness, sock_dir):
    async def run():
        await harness.start()
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        mode = stat.S_IMODE(path.stat().st_mode)
        server.close()
        await server.wait_closed()
        await harness.stop()
        return mode

    assert asyncio.run(run()) == 0o600


def test_disable_over_the_socket_refuses_the_tenants_next_request_at_once(
        harness, sock_dir):
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        tenant_id = host.split(".", 1)[0]
        before = await harness.send("GET", "/api/data", host=host, cookie=cookie)

        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        answer = await jsonsock.ask(path, {"op": "disable", "tenant": tenant_id})
        server.close()
        await server.wait_closed()

        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        relogin = await harness.json_post(
            "/auth/session", {"access_token": sign(harness.private,
                                                   now=harness.clock.t)},
            host="agent.waku.one")
        await harness.stop()
        return tenant_id, before, answer, after, relogin

    tenant_id, before, answer, after, relogin = asyncio.run(run())
    assert before[0] == 200
    assert answer == {"ok": True, "tenant": tenant_id}
    assert after[0] == 401             # the 60-second cache did NOT delay it
    assert relogin[0] == 403


def test_disable_by_email_reaches_the_same_tenant(harness, sock_dir):
    """tenant.sh takes either. An email that matches nobody is an error, not
    a silent no-op, and neither shape reaches the spawner twice."""
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        tenant_id = host.split(".", 1)[0]
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        nobody = await jsonsock.ask(path, {"op": "disable",
                                           "tenant": "nobody@example.com"})
        answer = await jsonsock.ask(path, {"op": "disable",
                                           "tenant": "mei@example.com"})
        server.close()
        await server.wait_closed()
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        await harness.stop()
        return tenant_id, nobody, answer, after

    tenant_id, nobody, answer, after = asyncio.run(run())
    assert "error" in nobody
    assert answer == {"ok": True, "tenant": tenant_id}
    assert after[0] == 401


def test_status_over_the_socket_names_the_running_tenants(harness, sock_dir):
    """`status` is the one op with no tenant, and it reads the fleet the
    sign-in's pre-warm filled."""
    async def run():
        await harness.start()
        host, _cookie = await signed_in_on_the_tenant_host(harness)
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        answer = await jsonsock.ask(path, {"op": "status"})
        disabled = await jsonsock.ask(path, {"op": "disable",
                                             "tenant": host.split(".", 1)[0]})
        after = await jsonsock.ask(path, {"op": "status"})
        server.close()
        await server.wait_closed()
        await harness.stop()
        return host.split(".", 1)[0], answer, disabled, after

    tenant_id, answer, disabled, after = asyncio.run(run())
    assert answer == {"running": [tenant_id]}
    assert disabled == {"ok": True, "tenant": tenant_id}
    assert after == {"running": []}


def test_enabling_a_tenant_again_does_not_bring_their_old_session_back(harness):
    """`disable` DELETES the session rows; it does not merely outrank them.

    The status check alone makes a disabled tenant's next request a 401, so
    nothing else in this file can see whether the rows were deleted. This can:
    after `enable`, the status no longer refuses anything, and the cookie from
    before the disable must still be dead -- the browser that held it may be
    the reason the tenant was disabled.
    """
    async def run():
        await harness.start()
        host, cookie = await signed_in_on_the_tenant_host(harness)
        tenant_id = host.split(".", 1)[0]
        before = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        disabled = await admin.handle(harness.gateway,
                                      {"op": "disable", "tenant": tenant_id})
        enabled = await admin.handle(harness.gateway,
                                     {"op": "enable", "tenant": tenant_id})
        after = await harness.send("GET", "/api/data", host=host, cookie=cookie)
        # And a fresh sign-in works, so the tenant is usable again.
        fresh_host, fresh_cookie = await signed_in_on_the_tenant_host(harness)
        fresh = await harness.send("GET", "/api/data", host=fresh_host,
                                   cookie=fresh_cookie)
        await harness.stop()
        return before, disabled, enabled, after, fresh

    before, disabled, enabled, after, fresh = asyncio.run(run())
    assert before[0] == 200
    assert disabled["ok"] is True
    assert enabled["ok"] is True
    assert after[0] == 401
    assert fresh[0] == 200
