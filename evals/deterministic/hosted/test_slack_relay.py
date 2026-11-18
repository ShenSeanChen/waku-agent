"""DETERMINISTIC EVAL -- the Slack send relay (spec 026 task C3, acceptance 7).

A hosted container holds no Slack token. Its Slack sender posts the message
to the metering proxy, the one service on the tenant network, with the
container's own platform token. The proxy finds the tenant and asks the
gateway over gateway.sock, and the gateway posts to Slack with the bot token
of the workspace that tenant connected.

  container's sender --HTTP--> proxy --gateway.sock--> gateway --HTTPS--> Slack
  (waku/, MIT)                 (hosted/, Elastic)      (hosted/, Elastic)

Every piece is the real one: the MIT sender from waku/, MeteringProxy, the
gateway's socket server, SlackRelay and ControlDb. Slack is the FakeSlack on
loopback.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
import pytest
from aiohttp import web
from gatewaylib import (
    SLACK_BOT_TOKEN,
    SLACK_TOKEN_KEY,
    FakeSlack,
)

from hosted import jsonsock
from hosted.gateway.internal import serve_token_lookup
from hosted.gateway.secretbox import SecretBox
from hosted.gateway.slack import SlackApi, SlackRelay, seal_binding
from hosted.gateway.store import ControlDb
from hosted.proxy.app import MeteringProxy
from hosted.proxy.config import ProxyConfig
from hosted.proxy.gateway_client import TokenCache
from hosted.proxy.ledger import Ledger
from hosted.proxy.slack import MAX_TEXT_CHARS, SEND_PATH, SENDS_PER_MINUTE, SlackSend
from waku.alwayson.outbox import Message
from waku.alwayson.senders import SendFailed, post_json
from waku.alwayson.senders import slack as slack_sender

BOB_BOT_TOKEN = "xoxb-fake-token-for-bob"
TARGET = {"channel": "C0DEMO", "thread_ts": "1700000000.000100"}


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.start()
    yield fake
    fake.stop()


def _message(body: str = "The demo is at 10.", target: dict | None = None) -> Message:
    return Message(id=1, run_id="r1", channel="slack",
                   target=TARGET if target is None else target, body=body,
                   idempotency_key="e1:0", status="pending", attempts=0,
                   next_attempt_at="2026-11-20T09:00:00+00:00", last_error="")


class Rig:
    """The gateway's half and the proxy's half, joined by a real socket."""

    def __init__(self, tmp_path, sock_dir, slack: FakeSlack) -> None:
        self.tmp_path, self.sock, self.slack = tmp_path, sock_dir / "gateway.sock", slack
        self.box = SecretBox.from_hex(SLACK_TOKEN_KEY)
        # Everything the container's side was sent back, for acceptance 7.
        self.container_saw: list[str] = []
        # The proxy's clock for its per-minute limit. A test moves it by hand.
        self.proxy_clock = 1000.0

    async def __aenter__(self):
        self.store = ControlDb(self.tmp_path / "control.db")
        self.session = aiohttp.ClientSession()
        self.relay = SlackRelay(store=self.store, box=self.box,
                                api=SlackApi(self.session, base_url=self.slack.url))
        self.server = await serve_token_lookup(self.sock, self.store,
                                               slack_send=self.relay.send)
        config = ProxyConfig(
            bind_host="127.0.0.1", port=0, ledger_db=self.tmp_path / "ledger.db",
            gateway_socket=self.sock, proxy_socket=self.tmp_path / "proxy.sock",
            platform_key="sk-ant-platform", free_models=("claude-sonnet-5-5",),
            monthly_cap_usd=1.0, concurrent_calls=4, requests_per_minute=60,
            global_concurrent_calls=16, max_tokens_ceiling=8192,
            max_body_bytes=4 * 1024 * 1024, upstream_base_url="https://api.anthropic.com")
        self.ledger = Ledger(config.ledger_db)
        route = SlackSend(resolve=TokenCache(self.sock).resolve, gateway_socket=self.sock,
                          monotonic=lambda: self.proxy_clock)
        proxy = MeteringProxy(config=config, ledger=self.ledger,
                              resolve=TokenCache(self.sock).resolve, upstream=None,
                              slack=route.handle)
        self.runner = web.AppRunner(proxy.build())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.runner.cleanup()
        self.server.close()
        await self.server.wait_closed()
        await self.session.close()
        self.ledger.close()
        self.store.close()

    def tenant(self, sub: str, team: str | None, bot_token: str = SLACK_BOT_TOKEN):
        """A tenant and its container's platform token, with a workspace
        connected when `team` names one."""
        tenant = self.store.create_tenant(sub=sub, email=f"{sub}@example.com",
                                          timezone="UTC")
        if team:
            assert self.store.set_slack_install(
                tenant.id, team_id=team, team_name=team, bot_user_id="U0BOT",
                token_sealed=self.box.seal(bot_token, bound_to=seal_binding(team)))
        return tenant, self.store.issue_token(tenant.id)

    async def send(self, platform_token: str, message: Message) -> None:
        """What a hosted container does: the MIT sender, over real HTTP."""
        def post(url, payload, headers):
            answer = post_json(url, payload, headers)
            self.container_saw.append(json.dumps(answer))
            return answer

        sender = slack_sender.make_hosted(self.base, platform_token, post=post)
        await asyncio.to_thread(sender, message)

    async def raw(self, platform_token: str, body, *, method: str = "POST"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        async with self.session.request(
                method, self.base + SEND_PATH, data=data,
                headers={"Authorization": f"Bearer {platform_token}",
                         "Content-Type": "application/json"}) as reply:
            text = await reply.text()
            self.container_saw.append(text)
            return reply.status, json.loads(text)


def _run(tmp_path, sock_dir, slack, body):
    async def run():
        async with Rig(tmp_path, sock_dir, slack) as rig:
            return await body(rig)

    return asyncio.run(run())


def test_a_containers_message_is_posted_with_the_gateways_token(tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, platform_token = rig.tenant("sub-ana", "T0ANA")
        await rig.send(platform_token, _message())
        return platform_token, rig.container_saw

    platform_token, container_saw = _run(tmp_path, sock_dir, slack, body)
    [posted] = slack.calls("/chat.postMessage")
    assert posted["authorization"] == f"Bearer {SLACK_BOT_TOKEN}"
    assert posted["body"] == {"channel": "C0DEMO", "text": "The demo is at 10.",
                              "thread_ts": "1700000000.000100"}
    # The platform token stopped at the proxy, and the bot token never left
    # the gateway: the container was told "ok" and nothing else.
    assert platform_token not in json.dumps(slack.requests)
    assert [json.loads(seen) for seen in container_saw] == [{"ok": True}]


def test_a_message_with_no_thread_goes_to_the_channel(tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, platform_token = rig.tenant("sub-ana", "T0ANA")
        await rig.send(platform_token, _message(target={"channel": "C0DEMO"}))

    _run(tmp_path, sock_dir, slack, body)
    assert slack.calls("/chat.postMessage")[0]["body"] == {
        "channel": "C0DEMO", "text": "The demo is at 10."}


def test_each_tenant_posts_only_to_the_workspace_it_connected(tmp_path, sock_dir, slack):
    """The token is the workspace: Slack posts where the token's workspace
    is. Each container's request is answered with its own tenant's token, and
    nothing in a request can name another tenant or another workspace."""
    async def body(rig):
        _ana, ana_token = rig.tenant("sub-ana", "T0ANA")
        _bob, bob_token = rig.tenant("sub-bob", "T0BOB", BOB_BOT_TOKEN)
        await rig.send(bob_token, _message("from Bob's agent"))
        await rig.send(ana_token, _message("from Ana's agent"))
        named = [await rig.raw(bob_token, {"channel": "C0DEMO", "text": "x", **extra})
                 for extra in ({"tenant": _ana.id}, {"team": "T0ANA"},
                               {"team_id": "T0ANA"}, {"token": SLACK_BOT_TOKEN})]
        return named

    named = _run(tmp_path, sock_dir, slack, body)
    assert [(c["authorization"], c["body"]["text"]) for c in slack.calls("/chat.postMessage")] \
        == [(f"Bearer {BOB_BOT_TOKEN}", "from Bob's agent"),
            (f"Bearer {SLACK_BOT_TOKEN}", "from Ana's agent")]
    assert [status for status, _ in named] == [400, 400, 400, 400]


def test_a_tenant_with_no_workspace_posts_nothing(tmp_path, sock_dir, slack):
    async def body(rig):
        rig.tenant("sub-ana", "T0ANA")
        _carol, carol_token = rig.tenant("sub-carol", None)
        with pytest.raises(SendFailed) as refused:
            await rig.send(carol_token, _message())
        return str(refused.value)

    assert "not_installed" in _run(tmp_path, sock_dir, slack, body)
    assert slack.calls("/chat.postMessage") == []


def test_a_request_without_a_live_platform_token_posts_nothing(tmp_path, sock_dir, slack):
    async def body(rig):
        ana, ana_token = rig.tenant("sub-ana", "T0ANA")
        unknown = await rig.raw("x" * 43, {"channel": "C0DEMO", "text": "x"})
        empty = await rig.raw("", {"channel": "C0DEMO", "text": "x"})
        rig.store.revoke_tokens(ana.id)
        fresh = rig.store.issue_token(ana.id)
        revoked = await rig.raw(ana_token, {"channel": "C0DEMO", "text": "x"})
        rig.store.set_status(ana.id, "disabled")
        disabled = await rig.raw(fresh, {"channel": "C0DEMO", "text": "x"})
        return unknown, empty, revoked, disabled

    answers = _run(tmp_path, sock_dir, slack, body)
    assert [status for status, _ in answers] == [401, 401, 401, 401]
    assert all(answer == {"ok": False, "error": "not_signed_in"} for _, answer in answers)
    assert slack.calls("/chat.postMessage") == []


@pytest.mark.parametrize("body", [
    {"text": "x"},                                           # no channel
    {"channel": "C0DEMO"},                                   # no text
    {"channel": "C0DEMO", "text": ""},
    {"channel": "C0DEMO", "text": 7},
    {"channel": "#general", "text": "x"},                    # a name, not an id
    {"channel": "c0demo", "text": "x"},
    {"channel": ["C0DEMO"], "text": "x"},
    {"channel": "C0DEMO", "text": "x", "thread_ts": "yesterday"},
    {"channel": "C0DEMO", "text": "x", "thread_ts": 1700000000.0001},
    {"channel": "C0DEMO", "text": "x" * (MAX_TEXT_CHARS + 1)},
    {"channel": "C0DEMO", "text": "x", "blocks": []},        # a field the relay does not take
    ["C0DEMO", "x"],
    b"not json",
])
def test_a_request_that_is_not_one_message_is_refused_before_slack(tmp_path, sock_dir,
                                                                   slack, body):
    async def run(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        return await rig.raw(token, body)

    status, answer = _run(tmp_path, sock_dir, slack, run)
    assert status == 400 and answer == {"ok": False, "error": "bad_request"}
    assert slack.calls("/chat.postMessage") == []


def test_the_longest_message_the_relay_takes_fits_the_socket(tmp_path, sock_dir, slack):
    """gateway.sock carries one line of at most jsonsock.MAX_LINE bytes. A
    message at the relay's limit, written in characters JSON escapes to six
    bytes each, still arrives whole."""
    text = "é" * MAX_TEXT_CHARS

    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        return await rig.raw(token, {"channel": "C0DEMO", "text": text})

    assert _run(tmp_path, sock_dir, slack, body) == (200, {"ok": True})
    assert slack.calls("/chat.postMessage")[0]["body"]["text"] == text
    assert MAX_TEXT_CHARS * 6 < jsonsock.MAX_LINE - 1024


def test_only_a_post_is_a_send(tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        return [(await rig.raw(token, {"channel": "C0DEMO", "text": "x"}, method=m))[0]
                for m in ("GET", "PUT", "DELETE")]

    assert _run(tmp_path, sock_dir, slack, body) == [405, 405, 405]
    assert slack.calls("/chat.postMessage") == []


def test_slacks_refusal_reaches_the_sender_as_its_error_code(tmp_path, sock_dir, slack,
                                                             caplog):
    slack.post_answer = {"ok": False, "error": "channel_not_found"}

    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        with pytest.raises(SendFailed) as refused:
            await rig.send(token, _message())
        return str(refused.value), rig.container_saw

    with caplog.at_level(logging.DEBUG):
        message, saw = _run(tmp_path, sock_dir, slack, body)
    assert "channel_not_found" in message
    assert SLACK_BOT_TOKEN not in message + "".join(saw) + caplog.text


def test_an_answer_from_slack_is_never_passed_through(tmp_path, sock_dir, slack):
    """Whatever Slack answers, the container gets `ok` and a short error
    code. An answer that echoed the token would stop at the gateway."""
    slack.post_answer = {"ok": False, "error": f"leaked {SLACK_BOT_TOKEN}",
                         "token": SLACK_BOT_TOKEN, "response_metadata": {"x": 1}}

    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        return await rig.raw(token, {"channel": "C0DEMO", "text": "x"})

    assert _run(tmp_path, sock_dir, slack, body) == (200, {"ok": False, "error": "refused"})


@pytest.mark.parametrize("code", ["token_revoked", "account_inactive", "invalid_auth"])
def test_a_token_slack_no_longer_accepts_disconnects_the_workspace(tmp_path, sock_dir,
                                                                   slack, code):
    slack.post_answer = {"ok": False, "error": code}

    async def body(rig):
        ana, token = rig.tenant("sub-ana", "T0ANA")
        bob, _ = rig.tenant("sub-bob", "T0BOB", BOB_BOT_TOKEN)
        answer = await rig.raw(token, {"channel": "C0DEMO", "text": "x"})
        return answer, rig.store.slack_install(ana.id), rig.store.slack_install(bob.id)

    answer, ana_install, bob_install = _run(tmp_path, sock_dir, slack, body)
    assert answer == (200, {"ok": False, "error": code})
    assert ana_install is None and bob_install is not None


def test_a_token_sealed_with_another_key_is_not_sent(tmp_path, sock_dir, slack):
    """The key in config/gateway.env changed, or a row was copied from
    another VM. Nothing is posted, and the sender is told why."""
    async def body(rig):
        ana, token = rig.tenant("sub-ana", None)
        other = SecretBox.from_hex("1e" * 32)
        rig.store.set_slack_install(
            ana.id, team_id="T0ANA", team_name="x", bot_user_id="U0BOT",
            token_sealed=other.seal(SLACK_BOT_TOKEN, bound_to=seal_binding("T0ANA")))
        return await rig.raw(token, {"channel": "C0DEMO", "text": "x"})

    assert _run(tmp_path, sock_dir, slack, body) == (
        200, {"ok": False, "error": "token_unreadable"})
    assert slack.calls("/chat.postMessage") == []


def test_slack_being_down_is_a_failure_the_dispatcher_retries(tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        slack.stop()
        with pytest.raises(SendFailed) as refused:
            await rig.send(token, _message())
        return str(refused.value)

    assert "slack_unreachable" in _run(tmp_path, sock_dir, slack, body)


def test_the_gateway_being_down_is_a_503_and_not_a_bad_token(tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        # Resolve once, so the proxy's token cache answers the next request
        # by itself and the send is what finds the socket gone.
        await rig.raw(token, {"channel": "C0DEMO", "text": "first"})
        rig.server.close()
        await rig.server.wait_closed()
        rig.sock.unlink(missing_ok=True)
        return await rig.raw(token, {"channel": "C0DEMO", "text": "second"})

    assert _run(tmp_path, sock_dir, slack, body) == (
        503, {"ok": False, "error": "relay_unreachable"})
    assert len(slack.calls("/chat.postMessage")) == 1


# --- the limit: 30 sends a tenant a minute ---------------------------------

def test_a_tenant_may_send_thirty_messages_a_minute_and_no_more(tmp_path, sock_dir, slack):
    assert SENDS_PER_MINUTE == 30

    async def body(rig):
        _ana, ana_token = rig.tenant("sub-ana", "T0ANA")
        _bob, bob_token = rig.tenant("sub-bob", "T0BOB", BOB_BOT_TOKEN)
        message = {"channel": "C0DEMO", "text": "x"}
        ana = [await rig.raw(ana_token, message) for _ in range(32)]
        # Ana at her limit does not hold Bob back.
        bob = await rig.raw(bob_token, message)
        # The window is the last 60 seconds, so her first send leaves it then.
        rig.proxy_clock += 59
        early = await rig.raw(ana_token, message)
        rig.proxy_clock += 1
        later = [await rig.raw(ana_token, message) for _ in range(31)]
        return ana, bob, early, later

    ana, bob, early, later = _run(tmp_path, sock_dir, slack, body)
    refused = (429, {"ok": False, "error": "rate_limited"})
    assert ana[:30] == [(200, {"ok": True})] * 30
    assert ana[30:] == [refused, refused]
    assert bob == (200, {"ok": True})
    assert early == refused
    assert later == [(200, {"ok": True})] * 30 + [refused]
    posts = [call["authorization"] for call in slack.calls("/chat.postMessage")]
    assert posts.count(f"Bearer {SLACK_BOT_TOKEN}") == 60
    assert posts.count(f"Bearer {BOB_BOT_TOKEN}") == 1


def test_a_request_that_is_refused_for_another_reason_does_not_use_the_limit(
        tmp_path, sock_dir, slack):
    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        for _ in range(40):
            await rig.raw(token, {"channel": "#general"})        # no text: 400
            await rig.raw("x" * 43, {"channel": "C0DEMO", "text": "x"})     # 401
        return await rig.raw(token, {"channel": "C0DEMO", "text": "x"})

    assert _run(tmp_path, sock_dir, slack, body) == (200, {"ok": True})


def test_a_send_refused_at_the_limit_is_retried_and_delivered_once(tmp_path, sock_dir,
                                                                    slack):
    """The container's side, whole: the outbox, the dispatcher and the hosted
    sender from waku/. The 31st message of the minute is refused, the
    dispatcher keeps the row and tries it again ten seconds later, and after
    the minute has passed it is delivered. Slack receives it once."""
    from datetime import UTC, datetime, timedelta

    from waku.alwayson import outbox
    from waku.alwayson.dispatcher import Dispatcher
    from waku.db import connect

    home = tmp_path / "home"
    home.mkdir()
    at = [datetime(2026, 11, 20, 9, 0, tzinfo=UTC)]

    async def body(rig):
        _ana, token = rig.tenant("sub-ana", "T0ANA")
        for _ in range(30):
            await rig.raw(token, {"channel": "C0DEMO", "text": "earlier"})

        def container():
            conn = connect(home, check_same_thread=False)
            outbox.insert(conn, run_id="r1", channel="slack", target=TARGET,
                          body="The demo is at 10.", idempotency_key="e1:0", now=at[0])
            dispatcher = Dispatcher(
                conn, {"slack": slack_sender.make_hosted(rig.base, token)},
                now=lambda: at[0], log=lambda line: None)
            statuses = []
            for seconds in (0, 10, 60):       # at once, after 10 s, after 1 min
                at[0] += timedelta(seconds=seconds)
                rig.proxy_clock += seconds
                dispatcher.step()
                statuses.append(tuple(conn.execute(
                    "SELECT status, attempts, last_error FROM outbox").fetchone()))
            assert dispatcher.step() == 0     # sent: nothing is left to try
            conn.close()
            return statuses

        return await asyncio.to_thread(container)

    statuses = _run(tmp_path, sock_dir, slack, body)
    assert [(status, attempts) for status, attempts, _ in statuses] == [
        ("pending", 1), ("pending", 2), ("sent", 2)]
    assert "rate_limited" in statuses[0][2]
    texts = [call["body"]["text"] for call in slack.calls("/chat.postMessage")]
    assert texts.count("The demo is at 10.") == 1 and len(texts) == 31


# --- gateway.sock ---------------------------------------------------------

def test_the_socket_sends_only_when_the_gateway_was_given_a_relay(tmp_path, sock_dir):
    """A gateway with Slack off serves the same socket, and on it slack_send
    is one more operation it does not have."""
    async def run():
        store = ControlDb(tmp_path / "control.db")
        tenant = store.create_tenant(sub="s", email="a@example.com", timezone="UTC")
        server = await serve_token_lookup(sock_dir / "g.sock", store)
        try:
            return await jsonsock.ask(sock_dir / "g.sock", {
                "op": "slack_send", "tenant": tenant.id, "channel": "C0DEMO", "text": "x"})
        finally:
            server.close()
            await server.wait_closed()
            store.close()

    assert asyncio.run(run()) == {"ok": False, "error": "slack_not_configured"}


def test_the_socket_refuses_a_send_with_a_field_it_does_not_take(tmp_path, sock_dir, slack):
    async def body(rig):
        ana, _ = rig.tenant("sub-ana", "T0ANA")
        return await jsonsock.ask(rig.sock, {
            "op": "slack_send", "tenant": ana.id, "channel": "C0DEMO", "text": "x",
            "token": "anything"})

    answer = _run(tmp_path, sock_dir, slack, body)
    assert "error" in answer and "token" in answer["error"]
    assert slack.calls("/chat.postMessage") == []


# --- acceptance 7: the token stays out -------------------------------------

def test_nothing_a_container_is_given_holds_the_bot_token(tmp_path, slack, monkeypatch):
    """Ana connects her workspace, mentions the agent, and her container is
    started and handed the event. What the spawner was asked to start the
    container with, what the container was handed, and every answer the
    gateway gave are searched for the token. The stored value still opens, so
    the search is not empty because the install failed."""
    import hashlib
    import hmac
    import urllib.parse

    from gatewaylib import (
        APEX,
        SLACK_SIGNING_SECRET,
        AlwaysOnContainer,
        FakeSpawner,
        Harness,
        sign_in,
    )

    from hosted.core import idle
    from hosted.gateway.container_api import ContainerApi
    from hosted.gateway.scheduler import Scheduler

    monkeypatch.setattr(idle, "READY_TIMEOUT_SECONDS", 0)
    container = AlwaysOnContainer()
    container.start()
    harness = Harness(tmp_path, FakeSpawner())
    harness.use_slack(slack)
    harness.spawner.address, harness.spawner.port = "127.0.0.1", container.port
    answers: list[bytes] = []

    async def send(method, target, **kwargs):
        status, headers, body = await harness.send(method, target, host=APEX, **kwargs)
        answers.extend([json.dumps(headers).encode(), body])
        return status, headers

    async def run():
        await harness.start()
        try:
            tenant_id, cookie, _ = await sign_in(harness)
            signed = f"__Host-waku_session={cookie}"
            _, headers = await send("GET", "/slack/install", cookie=signed)
            state = urllib.parse.parse_qs(
                urllib.parse.urlsplit(headers["location"]).query)["state"][0]
            await send("GET", f"/slack/oauth/callback?code=c&state={state}", cookie=signed)
            await harness.launcher.stop(tenant_id)      # asleep, as at night
            body = json.dumps({
                "type": "event_callback", "team_id": "T0ANA", "event_id": "Ev1",
                "event": {"type": "app_mention", "user": "U0ANA", "channel": "C0DEMO",
                          "text": "<@U0BOT> hello", "ts": "1700000000.000100"}}).encode()
            at = str(int(harness.clock.t))
            signature = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(),
                                         b"v0:" + at.encode() + b":" + body,
                                         hashlib.sha256).hexdigest()
            await send("POST", "/v1/triggers/slack", body=body, headers={
                "Content-Type": "application/json", "X-Slack-Request-Timestamp": at,
                "X-Slack-Signature": signature})
            async with aiohttp.ClientSession() as session:
                await Scheduler(store=harness.store, launcher=harness.launcher,
                                containers=ContainerApi(session), now=harness.clock).tick()
            return tenant_id, harness.store.slack_install(tenant_id)
        finally:
            await harness.stop()

    try:
        _tenant_id, install = asyncio.run(run())
    finally:
        container.stop()

    assert SecretBox.from_hex(SLACK_TOKEN_KEY).open(
        install.token_sealed, bound_to=seal_binding("T0ANA")) == SLACK_BOT_TOKEN
    assert len([r for r in harness.spawner.requests if r["op"] == "start"]) == 2
    assert [[e["external_id"] for e in batch] for batch in container.handed] == [["Ev1"]]
    given = json.dumps(harness.spawner.requests) + json.dumps(container.handed)
    assert SLACK_BOT_TOKEN not in given
    assert all(SLACK_BOT_TOKEN.encode() not in answer for answer in answers)
