"""DETERMINISTIC EVAL -- the chat API every gateway calls (spec 004 group B).

`POST https://agent.waku.one/v1/chat` is how the waku.one Waku Agent tab (and
later Slack) reaches a person's own container. It is called server to server
with the person's Supabase token as `Authorization: Bearer`; it ignores
cookies, so the Origin half of the CSRF pair does not apply to it, and it
still requires a JSON body. The container sees `/api/chat/stream`, never the
person's token.
"""

from __future__ import annotations

import asyncio
import json

from gatewaylib import FakeSpawner, Harness, sign

APEX = "agent.waku.one"
SUB = "sub-mei"


def _harness(tmp_path) -> Harness:
    return Harness(tmp_path, FakeSpawner())


async def _chat(harness: Harness, token: str | None, *, body: dict | None = None,
                content_type: str = "application/json", origin: str | None = None):
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    payload = json.dumps(body if body is not None else {"message": "hi"}).encode()
    return await harness.send("POST", "/v1/chat", host=APEX, body=payload, headers=headers)


def test_a_valid_token_reaches_the_persons_own_chat_stream(tmp_path):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        status, _, body = await _chat(harness, sign(harness.private, now=harness.clock.t))
        tenant = harness.store.tenant_by_sub(SUB)
        await harness.stop()
        return status, body, tenant

    status, body, tenant = asyncio.run(run())
    assert status == 200 and body == b"forwarded"
    assert harness.forwarder.calls == [(tenant.id, "/api/chat/stream")]


def test_a_first_call_creates_the_persons_tenant(tmp_path):
    """A waku.one user who never opened agent.waku.one still gets an agent."""
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        before = harness.store.tenant_by_sub(SUB)
        await _chat(harness, sign(harness.private, now=harness.clock.t))
        after = harness.store.tenant_by_sub(SUB)
        await harness.stop()
        return before, after

    before, after = asyncio.run(run())
    assert before is None and after is not None


def test_no_token_a_bad_token_or_an_expired_token_is_refused(tmp_path):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        none = await _chat(harness, None)
        bad = await _chat(harness, "not-a-jwt")
        expired = await _chat(harness, sign(harness.private, now=harness.clock.t - 7200))
        await harness.stop()
        return none[0], bad[0], expired[0]

    assert asyncio.run(run()) == (401, 401, 401)
    assert harness.forwarder.calls == []


def test_it_needs_no_origin_but_still_needs_json(tmp_path):
    """Server to server carries no Origin. CSRF needs a credential the browser
    attaches on its own; this route reads only the Authorization header, which
    a browser never attaches by itself, so Origin is not checked here. JSON is."""
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        plain = await _chat(harness, token, content_type="text/plain")
        foreign = await _chat(harness, token, origin="https://evil.example")
        await harness.stop()
        return plain[0], foreign[0]

    plain, foreign = asyncio.run(run())
    assert plain == 415
    assert foreign == 200, "a foreign Origin proves nothing either way on a bearer route"


def test_a_disabled_tenant_is_refused(tmp_path):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        await _chat(harness, token)
        harness.store.set_status(harness.store.tenant_by_sub(SUB).id, "disabled")
        status = (await _chat(harness, token))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) == 403


def test_only_post_is_served(tmp_path):
    harness = _harness(tmp_path)

    async def run():
        await harness.start()
        status = (await harness.send("GET", "/v1/chat", host=APEX))[0]
        await harness.stop()
        return status

    assert asyncio.run(run()) != 200
    assert harness.forwarder.calls == []


def test_the_real_forwarder_delivers_the_message_and_never_the_token(wired):
    """Through the real ContainerForwarder to a stock-shaped container: the
    container gets `/api/chat/stream` with the body, and no Authorization."""
    async def run():
        await wired.start()
        token = sign(wired.private, now=wired.clock.t)
        status, _, _ = await _chat(wired, token, body={"message": "what did my competitors ship?"})
        await wired.stop()
        return status, token

    status, token = asyncio.run(run())
    assert status == 200
    assert wired.container.targets[-1] == "/api/chat/stream"
    assert json.loads(wired.container.bodies[-1]) == {"message": "what did my competitors ship?"}
    sent = {k.lower(): v for k, v in wired.container.headers[-1].items()}
    assert "authorization" not in sent
    assert token not in json.dumps(sent)
