"""The admin path and the running gateway agree -- acceptance 23.

The restart-all half is E3's, because "the gateway forwards to the new
address" needs a forwarder that reads an address, and it is the one test here
that takes `wired` rather than `harness`.

EVERY TEST HERE THAT MATTERS GOES OVER THE SOCKET. admin.handle is reachable
in-process, and test_auth.py calls it that way once for brevity, but the ones
here -- the allowlist and the disable -- write a JSON line to a real Unix
socket, because the socket is what tenant.sh has and the in-process call is
not.
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest
from gatewaylib import (
    APEX,
    FakeContainer,
    cookie_value,
    ops,
    sign,
    sign_in,
    signed_in_on_the_tenant_host,
)

from hosted import jsonsock
from hosted.gateway import admin


@pytest.mark.parametrize("request_body, refusal", [
    ({"op": "nope"}, "op must be one of"),
    ({"op": "disable"}, "disable needs a tenant id or email"),
    ({"op": "disable", "tenant": "a@b.c", "force": True},
     "disable does not take ['force']"),
    ({"op": "restart-all", "tenant": "a@b.c"},
     "restart-all does not take ['tenant']"),
    # stop-all takes no tenant either, and the two tables have to be edited
    # together: an op in ADMIN_OPS with no row in ADMIN_KEYS raises KeyError
    # inside handle() rather than answering a refusal.
    ({"op": "stop-all", "tenant": "a@b.c"}, "stop-all does not take ['tenant']"),
    ({"op": "resolve"}, "resolve needs a tenant id or email"),
    ({"op": "resolve", "tenant": "a@b.c", "why": 1}, "resolve does not take ['why']"),
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


def test_disable_burns_the_hand_off_codes_that_were_already_minted(harness):
    """`end_sessions` clears the session rows, the session cache AND the
    outstanding hand-off codes. The third of those had no test: a code minted
    before a disable still bought a live session row and a cookie, and only
    the tenant host's status check then turned the request away. Two guards
    covering for each other is one guard and one liability.

    The tenant is enabled again before the code is used, so the status check
    cannot answer for this: what refuses the code has to be the code.
    """
    async def run():
        await harness.start()
        tenant_id, _apex, code = await sign_in(harness)
        host = f"{tenant_id}.agent.waku.one"
        disabled = await admin.handle(harness.gateway,
                                      {"op": "disable", "tenant": tenant_id})
        enabled = await admin.handle(harness.gateway,
                                     {"op": "enable", "tenant": tenant_id})
        stale = await harness.send("GET", f"/auth/enter?code={code}", host=host)
        landed = await harness.send(
            "GET", "/api/data", host=host,
            cookie=f"__Host-waku_tenant={cookie_value(stale[1], '__Host-waku_tenant')}")
        await harness.stop()
        return disabled, enabled, stale, landed

    disabled, enabled, stale, landed = asyncio.run(run())
    assert disabled["ok"] is True
    assert enabled["ok"] is True
    assert stale[1]["location"] == "https://agent.waku.one/login"
    assert cookie_value(stale[1], "__Host-waku_tenant") == ""
    assert landed[0] == 401


def test_restart_all_leaves_one_container_per_tenant_and_forwards_to_the_new_one(
        wired, sock_dir):
    """Acceptance 23's second half. The forwarding assertion is what makes it
    E3's: "the gateway forwards to the new address" needs a forwarder that
    reads an address, and E1 had a recording stand-in."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        before = await wired.send("GET", "/api/data", host=host, cookie=cookie)

        # upgrade.sh --now moves the container to a second port, the way a new
        # image would move it to a new container.
        second = FakeContainer()
        second.start()
        wired.spawner.port = second.port

        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, wired.gateway)
        answer = await jsonsock.ask(path, {"op": "restart-all"}, timeout=30.0)
        server.close()
        await server.wait_closed()

        after = await wired.send("GET", "/api/data", host=host, cookie=cookie)
        await wired.stop()
        second.stop()
        return tenant_id, before, answer, after, second

    tenant_id, before, answer, after, second = asyncio.run(run())
    assert before[0] == 200
    assert answer == {"restarted": [tenant_id]}
    assert after[0] == 200
    assert second.targets == ["/api/data"]        # the NEW container answered
    assert wired.container.targets == ["/api/data"]   # the old one, once, before
    starts = ops(wired.spawner, "start")
    assert len(starts) == 2
    assert starts[0]["token"] != starts[1]["token"]

async def _signed_in(harness, *, sub: str, email: str) -> str:
    """One tenant, signed in over the real POST /auth/session, and the id it
    was given.

    gatewaylib.sign_in always presents sub-mei, and `stop-all` is the one verb
    whose whole subject is more than one tenant at once. The pre-warm inside
    /auth/session is what puts the container in the fleet AND in the fake
    spawner's running set, so both halves of the union below have something
    real behind them.
    """
    token = sign(harness.private, now=harness.clock.t, sub=sub, email=email)
    status, _headers, body = await harness.json_post(
        "/auth/session", {"access_token": token, "timezone": "UTC"}, host=APEX)
    assert status == 200, body
    return json.loads(body)["enter"].split("//", 1)[1].split(".", 1)[0]


def test_stop_all_stops_every_running_container_and_disables_nobody(harness):
    """The verb restore.sh --all needs. Two tenants running, both stopped, both
    still active: a restore is not a punishment."""
    async def run():
        await harness.start()
        first = await _signed_in(harness, sub="sub-one", email="one@example.test")
        second = await _signed_in(harness, sub="sub-two", email="two@example.test")
        answer = await admin.handle(harness.gateway, {"op": "stop-all"})
        statuses = sorted(harness.store.tenant_by_id(one).status
                          for one in (first, second))
        still = sorted(harness.gateway.launcher.fleet.running())
        stops = [request["tenant_id"] for request in ops(harness.spawner, "stop")]
        await harness.stop()
        return first, second, answer, statuses, still, stops

    first, second, answer, statuses, still, stops = asyncio.run(run())
    assert first != second, "both sign-ins landed on one tenant"
    assert answer == {"stopped": sorted([first, second])}
    assert sorted(stops) == sorted([first, second])
    assert statuses == ["active", "active"]
    assert still == []


def test_stop_all_stops_a_container_the_gateway_had_forgotten(harness):
    """The half `fleet.running()` alone cannot see.

    A gateway that restarted knows nothing until it has resynced, and the
    container that outlives a restart is exactly the one a restore must not
    leave running: its bind mount is inside the tree the restore removes. The
    fleet is emptied here and the spawner still holds the container, which is
    what a fresh gateway process looks like from inside this function.
    """
    async def run():
        await harness.start()
        tenant_id = await _signed_in(harness, sub="sub-one", email="one@example.test")
        harness.gateway.launcher.fleet.forget(tenant_id)
        assert harness.gateway.launcher.fleet.running() == [], (
            "the fleet still names the tenant, so this test would pass on the "
            "fleet alone and prove nothing about the spawner's list")
        answer = await admin.handle(harness.gateway, {"op": "stop-all"})
        stops = [request["tenant_id"] for request in ops(harness.spawner, "stop")]
        await harness.stop()
        return tenant_id, answer, stops

    tenant_id, answer, stops = asyncio.run(run())
    assert answer == {"stopped": [tenant_id]}
    assert stops == [tenant_id]


def test_stop_all_stops_a_container_the_spawner_does_not_list(harness):
    """The other half, and the reason this is a union rather than a swap.

    `resync` believes the spawner and forgets everything the spawner does not
    name -- a container whose address does not match the one its project id
    derives, a tenant control.db has lost, a start still in flight. The fleet
    is the only record of those, and a restore that skipped them would delete
    the directories under a live container.
    """
    async def run():
        await harness.start()
        tenant_id = await _signed_in(harness, sub="sub-one", email="one@example.test")
        # The spawner lists nothing; the gateway still believes it is running.
        harness.spawner.running.clear()
        assert harness.gateway.launcher.fleet.running() == [tenant_id]
        answer = await admin.handle(harness.gateway, {"op": "stop-all"})
        stops = [request["tenant_id"] for request in ops(harness.spawner, "stop")]
        await harness.stop()
        return tenant_id, answer, stops

    tenant_id, answer, stops = asyncio.run(run())
    assert answer == {"stopped": [tenant_id]}
    assert stops == [tenant_id]


def test_stop_all_over_the_socket_answers_an_empty_fleet(harness, sock_dir):
    """Nothing running is not an error. restore.sh --all runs this first on a
    VM that may have been rebuilt an hour ago, and a refusal there would stop
    the restore before it began."""
    async def run():
        await harness.start()
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        answer = await jsonsock.ask(path, {"op": "stop-all"})
        server.close()
        await server.wait_closed()
        await harness.stop()
        return answer

    assert asyncio.run(run()) == {"stopped": []}


def test_resolve_turns_an_address_into_the_id_the_rest_of_the_platform_uses(
        harness, sock_dir):
    """The verb restore.sh needs so it does not carry its own SQL.

    `_find` is `tenant_by_id` or `tenant_by_email`, and the second pins its
    answer with `ORDER BY created_at, id LIMIT 1` because an address is not
    unique. A shell copy of that query in restore.sh had neither clause -- a
    second copy of a contract, already drifted. This is the one implementation.
    """
    async def run():
        await harness.start()
        tenant_id = await _signed_in(harness, sub="sub-mei", email="mei@example.com")
        path = sock_dir / "admin.sock"
        server = await admin.serve_admin(path, harness.gateway)
        by_email = await jsonsock.ask(path, {"op": "resolve",
                                             "tenant": "mei@example.com"})
        by_id = await jsonsock.ask(path, {"op": "resolve", "tenant": tenant_id})
        nobody = await jsonsock.ask(path, {"op": "resolve",
                                           "tenant": "nobody@example.com"})
        server.close()
        await server.wait_closed()
        await harness.stop()
        return tenant_id, by_email, by_id, nobody

    tenant_id, by_email, by_id, nobody = asyncio.run(run())
    assert by_email == {"ok": True, "tenant": tenant_id}
    assert by_id == {"ok": True, "tenant": tenant_id}
    assert "error" in nobody


def test_resolve_changes_nothing(harness):
    """Read-only, and that is the whole reason restore.sh may call it while
    the platform is mid-disaster. It must not stop a container, touch a
    status, or reach the spawner at all."""
    async def run():
        await harness.start()
        tenant_id = await _signed_in(harness, sub="sub-mei", email="mei@example.com")
        before = list(harness.spawner.requests)
        answer = await admin.handle(harness.gateway,
                                    {"op": "resolve", "tenant": "mei@example.com"})
        after = list(harness.spawner.requests)
        status = harness.store.tenant_by_id(tenant_id).status
        running = sorted(harness.gateway.launcher.fleet.running())
        await harness.stop()
        return tenant_id, answer, before, after, status, running

    tenant_id, answer, before, after, status, running = asyncio.run(run())
    assert answer == {"ok": True, "tenant": tenant_id}
    assert after == before
    assert status == "active"
    assert running == [tenant_id]


def test_stop_all_stops_a_container_the_gateway_would_refuse_to_forward_to(harness):
    """The correction this verb needed, and the reason it does not ask
    `resync`.

    `resync` and the spawner's `list` answer "may the gateway forward to this
    container?", so they drop one at an address its project id does not
    derive. Stopping asks "is this container ours?" -- and that container is
    precisely the one whose bind mount a restore is about to delete, leaving
    it on a dead inode. `labelled_only` is a container the spawner has and
    `list` would never report.
    """
    async def run():
        await harness.start()
        tenant_id = await _signed_in(harness, sub="sub-one", email="one@example.test")
        # The gateway forgets it -- a wrong address, or a tenant the restored
        # control.db does not know, both end here.
        harness.gateway.launcher.fleet.forget(tenant_id)
        harness.spawner.running.clear()
        harness.spawner.labelled_only = [tenant_id]
        listed = await harness.spawner.list()
        answer = await admin.handle(harness.gateway, {"op": "stop-all"})
        stops = [request["tenant_id"] for request in ops(harness.spawner, "stop")]
        await harness.stop()
        return tenant_id, listed, answer, stops

    tenant_id, listed, answer, stops = asyncio.run(run())
    assert listed == [], (
        "the spawner's list reports it, so this test would pass on resync "
        "alone and prove nothing about the wider enumeration")
    assert answer == {"stopped": [tenant_id]}
    assert stops == [tenant_id]
