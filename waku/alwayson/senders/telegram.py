"""Send one outbox row to a Telegram chat: POST sendMessage with the bot token
`waku serve` already polls with. The standard library is enough, so sending
needs no extra."""

from __future__ import annotations

from waku.alwayson.outbox import Message
from waku.alwayson.senders import Post, SendFailed, post_json

# Telegram refuses a longer text, so a long reply goes out in parts.
MAX_CHARS = 4096


def make(token: str, post: Post = post_json):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(message: Message) -> None:
        body = message.body
        for start in range(0, len(body), MAX_CHARS):
            answer = post(url, {"chat_id": message.target.get("chat_id"),
                                "text": body[start:start + MAX_CHARS]}, {})
            if not answer.get("ok"):
                raise SendFailed(f"Telegram refused: {answer.get('description') or 'no reason'}")

    return send
