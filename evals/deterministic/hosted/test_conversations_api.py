"""DETERMINISTIC EVAL -- chat history over the front door (spec 007 D).

waku.one shows a person's past conversations by asking the gateway, with the
same bearer token as `POST /v1/chat`:

    GET  /v1/conversations        the list            -> GET  /api/session?action=list
    GET  /v1/conversations/<id>   one conversation    -> GET  /api/session?action=history&id=<id>
    POST /v1/conversations        {"action": "new"} or {"action": "switch", "id": ...}
                                                      -> POST /api/session, the body as sent

The container keeps the one copy (its chat_log); the gateway checks the
person, finds their tenant and forwards. These pin that every route refuses a
missing, bad or expired token, forwards a good one to the right container
target, and never hands the container the person's token.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from gatewaylib import FakeSpawner, Harness, sign

APEX = "agent.waku.one"
SUB = "sub-mei"

# (method, gateway target, body, the container target it becomes)
ROUTES = [
    ("GET", "/v1/conversations", None, "/api/session?action=list"),
    ("GET", "/v1/conversations/s-20261003-091500", None,
     "/api/session?action=history&id=s-20261003-091500"),
    ("POST", "/v1/conversations", {"action": "new"}, "/api/session"),
    ("POST", "/v1/conversations", {"action": "switch", "id": "telegram-42"}, "/api/session"),
]


def _harness(tmp_path) -> Harness:
    return Harness(tmp_path, FakeSpawner())


async def _call(harness: Harness, method: str, target: str, token: str | None, *,
                body: dict | bytes | None = None,
                content_type: str = "application/json"):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if method == "POST":
        headers["Content-Type"] = content_type
        payload = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
    return await harness.send(method, target, host=APEX, body=payload, headers=headers)


@pytest.mark.parametrize(("method", "target", "body", "container"), ROUTES)
def test_a_valid_token_is_forwarded_to_the_containers_session_route(
        tmp_path, method, target, body, container):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        status, _, answer = await _call(harness, method, target,
                                        sign(harness.private, now=harness.clock.t), body=body)
        tenant = harness.store.tenant_by_sub(SUB)
        await harness.stop()
        return status, answer, tenant

    status, answer, tenant = asyncio.run(run())
    assert status == 200 and answer == b"forwarded"
    assert harness.forwarder.calls == [(tenant.id, container)]


@pytest.mark.parametrize(("method", "target", "body", "_container"), ROUTES)
def test_no_token_a_bad_token_or_an_expired_token_is_refused(
        tmp_path, method, target, body, _container):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        none = await _call(harness, method, target, None, body=body)
        bad = await _call(harness, method, target, "not-a-jwt", body=body)
        expired = await _call(harness, method, target,
                              sign(harness.private, now=harness.clock.t - 7200), body=body)
        tenant = harness.store.tenant_by_sub(SUB)
        await harness.stop()
        return [none, bad, expired], tenant

    answers, tenant = asyncio.run(run())
    assert [status for status, _, _ in answers] == [401, 401, 401]
    for _status, headers, body_bytes in answers:
        assert headers["content-type"].startswith("application/json")
        assert "error" in json.loads(body_bytes), "a refusal is the gateway's JSON shape"
    assert harness.forwarder.calls == []
    assert tenant is None, "a refused caller creates no tenant"


@pytest.mark.parametrize("body", [
    {"action": "history", "id": "s-1"},       # a read: it has its own GET route
    {"action": "delete"},
    {"action": "switch"},                     # switch to nothing
    {"action": "switch", "id": "__all__"},   # the dock's timeline, not a conversation
    {"action": "switch", "id": 7},
    [],
])
def test_only_new_and_switch_to_a_real_id_are_written(tmp_path, body):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        status, _, answer = await _call(harness, "POST", "/v1/conversations",
                                        sign(harness.private, now=harness.clock.t), body=body)
        await harness.stop()
        return status, answer

    status, answer = asyncio.run(run())
    assert status in (400, 415) and "error" in json.loads(answer)
    assert harness.forwarder.calls == []


def test_the_shape_of_the_request_is_checked(tmp_path):
    """Wrong method, wrong content type, a malformed id: refused before
    anything is forwarded, in the same JSON shape."""
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        out = [
            await _call(harness, "DELETE", "/v1/conversations", token),
            await _call(harness, "POST", "/v1/conversations/s-1", token, body={"action": "new"}),
            await _call(harness, "POST", "/v1/conversations", token, body={"action": "new"},
                        content_type="text/plain"),
            await _call(harness, "POST", "/v1/conversations", token, body=b"{not json"),
            await _call(harness, "GET", "/v1/conversations/", token),
            await _call(harness, "GET", "/v1/conversations/__all__", token),
            await _call(harness, "GET", "/v1/conversations/a/b", token),
        ]
        await harness.stop()
        return [status for status, _, _ in out]

    assert asyncio.run(run()) == [405, 405, 415, 415, 404, 404, 404]
    assert harness.forwarder.calls == []


def test_a_disabled_tenant_is_refused(tmp_path):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        await _call(harness, "GET", "/v1/conversations", token)
        harness.store.set_status(harness.store.tenant_by_sub(SUB).id, "disabled")
        status = (await _call(harness, "GET", "/v1/conversations", token))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) == 403
    assert len(harness.forwarder.calls) == 1


@pytest.mark.parametrize(("method", "target", "body", "container"), ROUTES)
def test_the_real_forwarder_delivers_it_and_never_the_token(
        wired, method, target, body, container):
    """Through the real ContainerForwarder to a stock-shaped container: the
    container gets the session route, the POST body byte for byte (it was read
    by the gateway to check the action, and must still arrive), and no
    Authorization."""
    async def run():
        await wired.start()
        token = sign(wired.private, now=wired.clock.t)
        status, _, _ = await _call(wired, method, target, token, body=body)
        await wired.stop()
        return status, token

    status, token = asyncio.run(run())
    assert status == 200
    assert wired.container.targets[-1] == container
    if body is not None:
        assert json.loads(wired.container.bodies[-1]) == body
    sent = {k.lower(): v for k, v in wired.container.headers[-1].items()}
    assert "authorization" not in sent
    assert token not in json.dumps(sent)
