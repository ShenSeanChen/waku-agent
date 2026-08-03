"""Background gateways keep each Waku on one dedicated worker thread."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.gateway import discord as discord_gateway
from waku.gateway import telegram as telegram_gateway
from waku.gateway.discord import DiscordHandle
from waku.gateway.runner import (
    GATEWAY_ERROR_REPLY,
    GatewayAgentRunner,
    run_gateway_turn,
)
from waku.gateway.telegram import TelegramHandle
from waku.integrations import IntegrationState


class _Connection:
    def __init__(self, closed_on: list[int]) -> None:
        self.closed_on = closed_on

    def close(self) -> None:
        self.closed_on.append(threading.get_ident())


def test_runner_owns_agent_and_connection_on_one_worker_thread():
    created_on: list[int] = []
    responded_on: list[int] = []
    closed_on: list[int] = []

    class Agent:
        def __init__(self) -> None:
            created_on.append(threading.get_ident())
            self.session = SimpleNamespace(session_id="")
            self.conn = _Connection(closed_on)

        def respond(self, text, *, observer, source):
            responded_on.append(threading.get_ident())
            return SimpleNamespace(reply=f"reply:{text}")

        def close(self) -> None:
            closed_on.append(threading.get_ident())

    caller = threading.get_ident()
    runner = GatewayAgentRunner(Agent, session_id="telegram", source="telegram")

    async def turns():
        first, second = await asyncio.gather(runner.respond("one"), runner.respond("two"))
        return first.reply, second.reply

    assert asyncio.run(turns()) == ("reply:one", "reply:two")
    runner.close()

    assert created_on == [responded_on[0]]
    assert len(set(responded_on)) == 1
    assert responded_on[0] != caller
    assert closed_on == [responded_on[0], responded_on[0]]


def test_runner_serializes_concurrent_turns():
    active = 0
    max_active = 0
    order: list[str] = []

    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            order.append(f"start:{text}")
            time.sleep(0.02)
            order.append(f"end:{text}")
            active -= 1
            return SimpleNamespace(reply=text)

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="discord", source="discord")

    async def turns():
        await asyncio.gather(runner.respond("one"), runner.respond("two"))

    asyncio.run(turns())
    runner.close()

    assert max_active == 1
    assert order == ["start:one", "end:one", "start:two", "end:two"]


def test_real_waku_persists_two_telegram_turns_from_worker(tmp_path):
    home = tmp_path / "home"
    client = ScriptedClient([
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([text_block("hello one")]),
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([text_block("hello two")]),
    ])
    runner = GatewayAgentRunner(
        lambda: make_waku(home, client=client, episodic_store="sqlite"),
        session_id="telegram",
        source="telegram",
    )

    async def turns():
        first = await runner.respond("one")
        second = await runner.respond("two")
        return first.reply, second.reply

    assert asyncio.run(turns()) == ("hello one", "hello two")
    runner.close()

    conn = sqlite3.connect(home / "state.db")
    rows = conn.execute(
        "SELECT source, role, content FROM chat_log ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [
        ("telegram", "user", "one"),
        ("telegram", "assistant", "hello one"),
        ("telegram", "user", "two"),
        ("telegram", "assistant", "hello two"),
    ]


def test_gateway_turn_replies_safely_marks_error_and_recovers(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "secret-value")
    attempts = 0

    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("provider rejected secret-value")
            return SimpleNamespace(reply="recovered")

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="telegram", source="telegram")
    sent: list[str] = []
    logs: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    async def turns():
        failed = await run_gateway_turn(runner, "first", send, logger=logs.append)
        assert runner.error_status == "message handling failed (RuntimeError) — see dashboard terminal"
        succeeded = await run_gateway_turn(runner, "second", send, logger=logs.append)
        return failed, succeeded

    failed, succeeded = asyncio.run(turns())
    runner.close()

    assert failed is None
    assert succeeded.reply == "recovered"
    assert sent == [GATEWAY_ERROR_REPLY, "recovered"]
    assert runner.error_status is None
    assert any("RuntimeError" in line for line in logs)
    assert all("secret-value" not in line for line in logs)


def test_gateway_turn_marks_delivery_failure():
    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            return SimpleNamespace(reply="ready")

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="discord", source="discord")
    logs: list[str] = []

    async def fail_send(text: str) -> None:
        raise ConnectionError("network down")

    result = asyncio.run(run_gateway_turn(runner, "hello", fail_send, logger=logs.append))
    runner.close()

    assert result is None
    assert runner.error_status == "reply delivery failed (ConnectionError) — see dashboard terminal"
    assert any("reply delivery failed" in line for line in logs)


def test_runner_rejects_turns_after_close():
    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            return SimpleNamespace(reply=text)

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="telegram", source="telegram")
    runner.close()

    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(runner.respond("late"))


@pytest.mark.parametrize("handle_kind", ["telegram", "discord"])
def test_gateway_handle_surfaces_and_recovers_runner_error(handle_kind):
    runner = SimpleNamespace(error_status="message handling failed — see dashboard terminal")
    thread = SimpleNamespace(is_alive=lambda: True)
    if handle_kind == "telegram":
        handle = TelegramHandle(None, None, thread, {"conflict": False}, runner)
    else:
        client = SimpleNamespace(is_closed=lambda: False, is_ready=lambda: True)
        handle = DiscordHandle(client, thread, SimpleNamespace(), runner)

    status = handle.status()
    assert status.state is IntegrationState.ERROR
    assert status.message == runner.error_status

    runner.error_status = None
    assert handle.status().state is IntegrationState.CONNECTED


@pytest.mark.parametrize("handle_kind", ["telegram", "discord"])
def test_gateway_handle_closes_runner_even_if_gateway_thread_stopped(handle_kind):
    closed = []
    runner = SimpleNamespace(error_status=None, close=lambda: closed.append(True))
    thread = SimpleNamespace(is_alive=lambda: False, join=lambda timeout: None)
    if handle_kind == "telegram":
        handle = TelegramHandle(None, None, thread, {"conflict": False}, runner)
    else:
        client = SimpleNamespace(loop=None)
        started = SimpleNamespace(wait=lambda timeout: None)
        handle = DiscordHandle(client, thread, started, runner)

    handle.stop()

    assert closed == [True]


def test_telegram_handler_uses_runner_and_returns_safe_error(monkeypatch):
    callbacks = []
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)

    class Filter:
        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    class MessageHandler:
        def __init__(self, pattern, callback):
            callbacks.append(callback)

    app = SimpleNamespace(add_handler=lambda handler: None)
    builder = SimpleNamespace(token=lambda token: builder, build=lambda: app)
    application = SimpleNamespace(builder=lambda: builder)
    monkeypatch.setitem(sys.modules, "telegram", SimpleNamespace(Update=object))
    monkeypatch.setitem(sys.modules, "telegram.ext", SimpleNamespace(
        Application=application,
        ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),
        MessageHandler=MessageHandler,
        filters=SimpleNamespace(TEXT=Filter(), COMMAND=Filter()),
    ))

    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            raise RuntimeError("boom")

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="telegram", source="telegram")
    telegram_gateway._build_app("fake-token", runner=runner)
    sent = []

    async def reply_text(text):
        sent.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(text="hello", reply_text=reply_text),
    )
    asyncio.run(callbacks[0](update, SimpleNamespace()))
    runner.close()

    assert sent == [GATEWAY_ERROR_REPLY]
    assert runner.error_status == "message handling failed (RuntimeError) — see dashboard terminal"


def test_discord_handler_uses_runner_and_delivers_reply(monkeypatch):
    # Isolate from developer .env so the allowlist/budget defaults under test are predictable.
    for key in (
        "DISCORD_ALLOWED_USER",
        "DISCORD_ALLOWED_CHANNEL",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_MAX_TURNS_PER_HOUR",
        "DISCORD_HOME",
    ):
        monkeypatch.delenv(key, raising=False)

    callbacks = {}

    class Intents:
        message_content = False

        @classmethod
        def default(cls):
            return cls()

    class Client:
        def __init__(self, *, intents):
            self.intents = intents
            self.user = SimpleNamespace(id=99)

        def event(self, callback):
            callbacks[callback.__name__] = callback
            return callback

    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(Intents=Intents, Client=Client,
                                                                  Message=object))

    class Agent:
        session = SimpleNamespace(session_id="")
        conn = None

        def respond(self, text, *, observer, source):
            return SimpleNamespace(reply="discord reply")

        def close(self) -> None:
            pass

    runner = GatewayAgentRunner(Agent, session_id="discord", source="discord")
    client = discord_gateway._build_client(runner)
    sent = []

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    async def reply(text):
        sent.append(text)

    message = SimpleNamespace(
        author=SimpleNamespace(id=123),
        content="hello",
        guild=None,
        channel=SimpleNamespace(id=456, typing=lambda: Typing()),
        mentions=[],
        reply=reply,
    )
    asyncio.run(callbacks["on_message"](message))
    runner.close()

    assert client.intents.message_content is True
    assert sent == ["discord reply"]
