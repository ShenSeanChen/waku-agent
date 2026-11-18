"""Send one outbox row to a Telegram chat: POST sendMessage with the bot token
`waku serve` already polls with. The standard library is enough, so sending
needs no extra.

The model writes Markdown, so each part is converted to Telegram's HTML and
sent with `parse_mode: HTML` (format.py). When Telegram refuses a part for its
markup, the part goes out again as plain text with the marks removed: a
formatting error never loses a reply."""

from __future__ import annotations

from waku.alwayson.outbox import Message
from waku.alwayson.senders import Post, SendFailed, post_json
from waku.alwayson.senders.format import parts, plain, telegram_html

# Telegram refuses a longer text, so a long reply goes out in parts. Telegram
# counts the text after it has read the markup, so the tags of a converted
# part are free and an escaped < counts as one character.
MAX_CHARS = 4096
# How Telegram's refusal of a message's markup begins.
_MARKUP_REFUSALS = ("can't parse entities", "can't find end")


def make(token: str, post: Post = post_json):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(message: Message) -> None:
        chat_id = message.target.get("chat_id")
        for part in parts(message.body, MAX_CHARS):
            answer = post(url, {"chat_id": chat_id, "text": telegram_html(part),
                                "parse_mode": "HTML"}, {})
            said = str(answer.get("description") or "").lower()
            if not answer.get("ok") and any(refusal in said for refusal in _MARKUP_REFUSALS):
                answer = post(url, {"chat_id": chat_id, "text": plain(part)}, {})
            if not answer.get("ok"):
                raise SendFailed(f"Telegram refused: {answer.get('description') or 'no reason'}")

    return send
