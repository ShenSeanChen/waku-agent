"""Send one outbox row to Slack. A row with a thread goes to that thread.
The model writes Markdown, so the text is converted to Slack's mrkdwn
(format.py) in both setups.

There are two setups, and `senders.configured` picks one:

  make         your own Slack app. POST chat.postMessage with the bot token in
               SLACK_BOT_TOKEN. The bot needs the `chat:write` scope.
  make_hosted  hosted Waku (WAKU_HOSTED=1). This process has no Slack token.
               It posts the message to the platform it already sends model
               calls to, at WAKU_PLATFORM_BASE_URL, with WAKU_PLATFORM_TOKEN,
               and the platform posts it to the workspace this person
               connected with "Add to Slack".

Each is one HTTP request through the standard library, so sending does not
need the extra [slack] that receiving does. Both have been run against fake
answers, never against a live workspace."""

from __future__ import annotations

from waku.alwayson.outbox import Message
from waku.alwayson.senders import Post, SendFailed, post_json
from waku.alwayson.senders.format import slack_mrkdwn

URL = "https://slack.com/api/chat.postMessage"
HOSTED_PATH = "/slack/send"
# Slack asks for messages under 4,000 characters, and the hosted relay takes
# at most 8,000, so a hosted reply longer than this goes out in parts.
HOSTED_MAX_CHARS = 4000


def _payload(message: Message, text: str) -> dict:
    payload = {"channel": message.target.get("channel"), "text": text}
    if thread := message.target.get("thread_ts"):
        payload["thread_ts"] = thread
    return payload


def make(bot_token: str, post: Post = post_json):
    def send(message: Message) -> None:
        answer = post(URL, _payload(message, slack_mrkdwn(message.body)),
                      {"Authorization": f"Bearer {bot_token}"})
        if not answer.get("ok"):   # Slack answers 200 and says no in the body
            raise SendFailed(f"Slack refused: {answer.get('error') or 'no reason'}")

    return send


def make_hosted(base_url: str, platform_token: str, post: Post = post_json):
    """The sender for a hosted container. The platform answers in Slack's own
    shape: {"ok": true}, or {"ok": false, "error": <code>}."""
    url = base_url.rstrip("/") + HOSTED_PATH

    def send(message: Message) -> None:
        body = slack_mrkdwn(message.body)
        for start in range(0, len(body), HOSTED_MAX_CHARS):
            answer = post(url, _payload(message, body[start:start + HOSTED_MAX_CHARS]),
                          {"Authorization": f"Bearer {platform_token}"})
            if not answer.get("ok"):
                error = answer.get("error")
                raise SendFailed(
                    f"Slack refused: {error if isinstance(error, str) and error else 'no reason'}")

    return send
