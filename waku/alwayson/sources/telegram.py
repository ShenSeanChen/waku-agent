"""Telegram as an event source (spec 023).

The long-polling app and the allowlist are the gateway's own
(waku/gateway/telegram.py). Under `waku serve` an allowed message becomes one
row in `events` instead of a turn:

  external_id   Telegram's update_id, so a redelivered update is a no-op
  session_id    telegram:<chat id>, so each chat keeps its own history

`waku telegram` and the dashboard's poller do not come through here.
"""

from __future__ import annotations

from waku.alwayson.events import Inbox
from waku.alwayson.sources import MissingExtra
from waku.gateway import telegram as gateway


def record(inbox: Inbox, update) -> bool:
    """Write one Telegram update into the inbox. True when a row was written."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    return inbox.add(
        source="telegram",
        external_id=str(update.update_id),
        session_id=f"telegram:{chat_id}",
        payload={"text": update.message.text,
                 "sender": {"id": str(user.id), "name": getattr(user, "full_name", "") or ""},
                 "reply_to": {"chat_id": chat_id}},
    )


# Why Telegram is blocked, as the line that lists the sources says it (spec
# 024, decision 11). Telegram gives one bot's messages to one reader. The
# poller keeps asking, so this one receives again once the other one stops.
BLOCKED = ("another program is reading this bot (waku dashboard, waku telegram or a "
           "second waku serve). Stop it and this one takes over.")


def new_state() -> dict:
    """What the poller knows: whether Telegram refused it because another
    program reads the same bot."""
    return {"conflict": False}


def received(inbox: Inbox, state: dict, update) -> bool:
    """One allowed message arrived, so this program is the bot's reader."""
    state["conflict"] = False
    return record(inbox, update)


def build_app(token: str, inbox: Inbox, state: dict | None = None):
    state = state if state is not None else new_state()
    return gateway._build_app(token, on_message=lambda update: received(inbox, state, update))


class Handle:
    def __init__(self, loop, app, thread, state: dict | None = None) -> None:
        self.loop, self.app, self.thread = loop, app, thread
        self.state = state if state is not None else new_state()

    def blocked(self) -> str:
        """The reason this source receives nothing, or "" while it listens."""
        return BLOCKED if self.state["conflict"] else ""

    def stop(self) -> None:
        gateway.stop_polling(self.loop, self.app, self.thread)


def start(inbox: Inbox) -> Handle | None:
    """Start polling, or return None when Telegram is not configured.
    Raises MissingExtra when the token is set and the extra is not installed."""
    from waku.config import load_settings

    token = load_settings().telegram_token
    if not token:
        return None
    try:
        import telegram  # noqa: F401
    except ImportError:
        raise MissingExtra("TELEGRAM_BOT_TOKEN", "telegram") from None
    print("(telegram) starting:")
    print(gateway.posture())
    state = new_state()
    app = build_app(token, inbox, state)
    loop, thread = gateway.poll_in_thread(app, state, BLOCKED)
    return Handle(loop, app, thread, state)
