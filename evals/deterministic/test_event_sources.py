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
