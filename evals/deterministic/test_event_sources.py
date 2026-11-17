"""DETERMINISTIC EVAL: each source only inserts a row (spec 023, decision 1).

A handler under `waku serve` checks who sent the message, writes one row into
`events` and returns. It never calls waku.respond(). Telegram, Slack and GitHub
are driven here with fake updates, envelopes and `gh` output: no network.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from waku.alwayson.events import Inbox
from waku.db import connect


@pytest.fixture
def inbox(tmp_path):
    return Inbox(tmp_path)


def _rows(inbox):
    import json

    conn = connect(inbox.home)
    try:
        return [(r["source"], r["external_id"], r["session_id"], json.loads(r["payload"]),
                 r["status"]) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    finally:
        conn.close()


# ---- Telegram --------------------------------------------------------------

def _telegram_update(update_id=900, chat_id=42, user_id=42, text="hello", sent=None):
    async def reply_text(message):
        (sent if sent is not None else []).append(message)

    return SimpleNamespace(
        update_id=update_id,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name="Sean Chen"),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    )


def _fake_telegram(monkeypatch):
    """The parts of python-telegram-bot that _build_app touches. Returns the
    list the message callback lands in."""
    callbacks = []

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
    monkeypatch.setitem(sys.modules, "telegram", SimpleNamespace(Update=object))
    monkeypatch.setitem(sys.modules, "telegram.ext", SimpleNamespace(
        Application=SimpleNamespace(builder=lambda: builder),
        ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),
        MessageHandler=MessageHandler,
        filters=SimpleNamespace(TEXT=Filter(), COMMAND=Filter()),
    ))
    return callbacks


def _telegram_handler(monkeypatch, inbox):
    """The serve-mode handler, built the way sources.telegram.start builds it."""
    from waku.alwayson.sources import telegram as source
    from waku.gateway import telegram as gateway

    def no_runner():
        raise AssertionError("waku serve must not build a gateway runner")

    monkeypatch.setattr(gateway, "_new_runner", no_runner)
    callbacks = _fake_telegram(monkeypatch)
    source.build_app("fake-token", inbox)
    return callbacks[0]


def test_telegram_message_becomes_one_event_in_its_chats_session(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)
    sent = []

    asyncio.run(handle(_telegram_update(update_id=900, chat_id=42, text="hello", sent=sent),
                       SimpleNamespace()))

    assert _rows(inbox) == [(
        "telegram", "900", "telegram:42",
        {"text": "hello", "sender": {"id": "42", "name": "Sean Chen"},
         "reply_to": {"chat_id": 42}},
        "pending",
    )]
    assert sent == [], "the handler stores the message and answers nothing itself"


def test_telegram_chats_do_not_share_a_session(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)

    asyncio.run(handle(_telegram_update(update_id=1, chat_id=42), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=2, chat_id=-10077), SimpleNamespace()))

    assert [row[2] for row in _rows(inbox)] == ["telegram:42", "telegram:-10077"]


def test_telegram_redelivered_update_is_one_event(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)

    asyncio.run(handle(_telegram_update(update_id=900), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=900), SimpleNamespace()))

    assert len(_rows(inbox)) == 1


def test_telegram_allowlist_still_refuses_a_stranger(monkeypatch, inbox):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER", "42, 43")
    handle = _telegram_handler(monkeypatch, inbox)
    sent = []

    asyncio.run(handle(_telegram_update(update_id=1, user_id=666, sent=sent), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=2, user_id=43, sent=sent), SimpleNamespace()))

    assert sent == ["This Waku serves someone else. Run your own!"]
    assert [row[1] for row in _rows(inbox)] == ["2"], "only the allowlisted sender is stored"


def test_telegram_source_is_off_without_a_token(monkeypatch, inbox):
    from waku.alwayson.sources import telegram as source

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert source.start(inbox) is None


def test_telegram_source_names_the_extra_when_it_is_missing(monkeypatch, inbox, capsys):
    from waku.alwayson.sources import telegram as source

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setitem(sys.modules, "telegram", None)   # import telegram -> ImportError
    assert source.start(inbox) is None
    assert "waku-agent[telegram]" in capsys.readouterr().out


# ---- Slack -----------------------------------------------------------------

def _slack_envelope(event_id="Ev900", team="T1", channel="C1", ts="1700000000.000100",
                    thread_ts=None, text="<@UBOT> is issue 12 still open?", **event):
    body = {"type": "app_mention", "user": "UANA", "text": text, "channel": channel, "ts": ts}
    if thread_ts:
        body["thread_ts"] = thread_ts
    body.update(event)
    return {"team_id": team, "event_id": event_id, "type": "event_callback", "event": body}


def _slack_record(inbox, envelope, team_id="T1", channels=("C1",)):
    from waku.alwayson.sources import slack

    return slack.record(inbox, envelope, team_id=team_id, channels=set(channels))


def test_slack_mention_becomes_one_event_in_its_threads_session(inbox):
    assert _slack_record(inbox, _slack_envelope(thread_ts="1699999999.000001")) is True

    assert _rows(inbox) == [(
        "slack", "Ev900", "slack:C1:1699999999.000001",
        {"text": "<@UBOT> is issue 12 still open?", "sender": {"id": "UANA"},
         "reply_to": {"channel": "C1", "thread_ts": "1699999999.000001"}},
        "pending",
    )]


def test_slack_mention_outside_a_thread_starts_one(inbox):
    """A reply goes to the thread, so a top-level mention is its own thread."""
    _slack_record(inbox, _slack_envelope(ts="1700000000.000100"))

    _, _, session_id, payload, _ = _rows(inbox)[0]
    assert session_id == "slack:C1:1700000000.000100"
    assert payload["reply_to"] == {"channel": "C1", "thread_ts": "1700000000.000100"}


def test_slack_redelivered_event_is_one_event(inbox):
    assert _slack_record(inbox, _slack_envelope(event_id="Ev900")) is True
    assert _slack_record(inbox, _slack_envelope(event_id="Ev900")) is False
    assert len(_rows(inbox)) == 1


@pytest.mark.parametrize("envelope, why", [
    (_slack_envelope(type="message"), "only app_mention events are taken"),
    (_slack_envelope(channel="C2"), "the channel is not in SLACK_ALLOWED_CHANNELS"),
    (_slack_envelope(team="T2"), "the event is from another workspace"),
    (_slack_envelope(user_team="T2"), "the sender belongs to another workspace"),
    (_slack_envelope(bot_id="B1"), "a bot wrote it"),
    (_slack_envelope(event_id=""), "it has no event id to deduplicate on"),
    ({"team_id": "T1", "event_id": "Ev1"}, "it has no event"),
])
def test_slack_ignores_what_it_was_not_asked_to_take(inbox, envelope, why):
    assert _slack_record(inbox, envelope) is False, why
    assert _rows(inbox) == []


def test_slack_with_no_allowed_channel_takes_nothing(inbox):
    """The allowlist is a closed set: an empty one allows no channel."""
    assert _slack_record(inbox, _slack_envelope(), channels=()) is False
    assert _rows(inbox) == []


def test_slack_allowed_channels_come_from_the_environment(monkeypatch):
    from waku.alwayson.sources import slack

    monkeypatch.setenv("SLACK_ALLOWED_CHANNELS", " C1, C2 ,,")
    assert slack.allowed_channels() == {"C1", "C2"}
    monkeypatch.delenv("SLACK_ALLOWED_CHANNELS")
    assert slack.allowed_channels() == set()


def _fake_slack_sdk(monkeypatch, order):
    """The parts of slack_sdk that sources.slack.start touches."""
    clients = []

    class WebClient:
        def __init__(self, token):
            self.token = token

        def auth_test(self):
            return {"team_id": "T1", "user_id": "UBOT"}

    class SocketModeClient:
        def __init__(self, app_token, web_client):
            self.app_token, self.web_client = app_token, web_client
            self.socket_mode_request_listeners = []
            self.connected = self.closed = False
            clients.append(self)

        def connect(self):
            self.connected = True

        def close(self):
            self.closed = True

        def send_socket_mode_response(self, response):
            order.append(("ack", response.envelope_id))

    class SocketModeResponse:
        def __init__(self, envelope_id):
            self.envelope_id = envelope_id

    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode",
                        SimpleNamespace(SocketModeClient=SocketModeClient))
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response",
                        SimpleNamespace(SocketModeResponse=SocketModeResponse))
    monkeypatch.setitem(sys.modules, "slack_sdk.web", SimpleNamespace(WebClient=WebClient))
    return clients


def test_slack_listener_stores_the_mention_before_it_acknowledges(monkeypatch, inbox):
    """Slack resends an event that was not acknowledged. Storing first means a
    crash between the two costs a redelivery, never a message."""
    from waku.alwayson.sources import slack

    order = []
    clients = _fake_slack_sdk(monkeypatch, order)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_ALLOWED_CHANNELS", "C1")
    real_add = inbox.add
    monkeypatch.setattr(inbox, "add", lambda **row: order.append(("stored", row["external_id"]))
                        or real_add(**row))

    handle = slack.start(inbox)
    client = clients[0]
    assert client.connected
    listener = client.socket_mode_request_listeners[0]
    listener(client, SimpleNamespace(type="events_api", envelope_id="env-1",
                                     payload=_slack_envelope(event_id="Ev900")))
    listener(client, SimpleNamespace(type="interactive", envelope_id="env-2", payload={}))

    assert order == [("stored", "Ev900"), ("ack", "env-1"), ("ack", "env-2")]
    assert [row[1] for row in _rows(inbox)] == ["Ev900"]
    handle.stop()
    assert client.closed


def test_slack_source_is_off_without_both_tokens(monkeypatch, inbox):
    from waku.alwayson.sources import slack

    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    assert slack.start(inbox) is None


def test_slack_source_names_the_extra_when_it_is_missing(monkeypatch, inbox, capsys):
    from waku.alwayson.sources import slack

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setitem(sys.modules, "slack_sdk", None)   # import slack_sdk -> ImportError
    assert slack.start(inbox) is None
    assert "waku-agent[slack]" in capsys.readouterr().out


def test_slack_sdk_is_not_needed_to_import_waku(tmp_path):
    """The import guard: slack_sdk is behind the extra [slack], so `waku`,
    `waku serve` and the Slack source itself import without it."""
    import subprocess

    code = ("import sys; sys.modules['slack_sdk'] = None\n"
            "import waku, waku.__main__, waku.alwayson.serve, waku.alwayson.sources.slack\n"
            "from waku.config import Settings\n"
            "sources, routes = waku.alwayson.serve.configured(Settings())\n"
            "assert 'slack' in sources\n"
            "print('imported')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=tmp_path, check=False,
                          env={"PATH": "", "HOME": str(tmp_path), "WAKU_HOME": str(tmp_path),
                               "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[2])})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "imported"
