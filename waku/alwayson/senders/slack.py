"""Send one outbox row to Slack: POST chat.postMessage with the bot token. A
row with a thread goes to that thread. The bot needs the `chat:write` scope.
The model writes Markdown, so the text is converted to Slack's mrkdwn
(format.py).

This is one HTTPS request through the standard library, so sending does not
need the extra [slack] that receiving does. It has been run against fake
answers, never against a live workspace."""

from __future__ import annotations

from waku.alwayson.outbox import Message
from waku.alwayson.senders import Post, SendFailed, post_json
from waku.alwayson.senders.format import slack_mrkdwn

URL = "https://slack.com/api/chat.postMessage"


def make(bot_token: str, post: Post = post_json):
    def send(message: Message) -> None:
        payload = {"channel": message.target.get("channel"), "text": slack_mrkdwn(message.body)}
        if thread := message.target.get("thread_ts"):
            payload["thread_ts"] = thread
        answer = post(URL, payload, {"Authorization": f"Bearer {bot_token}"})
        if not answer.get("ok"):   # Slack answers 200 and says no in the body
            raise SendFailed(f"Slack refused: {answer.get('error') or 'no reason'}")

    return send
