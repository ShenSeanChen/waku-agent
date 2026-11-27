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
answers, never against a live workspace.

With your own app, a row whose target holds a "request" (spec 035) is drawn
as Block Kit: the title, the reason and the link, the exact text to be
posted, and two buttons, Approve and Reject. Each button's value is the
request's 8-character code. `make` returns the posted message's ts, so the
dispatcher can keep it. Once the request is answered, a row with "update_ts"
edits that same message: the buttons become one line, "Approved by <login>"
or "Rejected by <login>", and the rest of the request stays. The hosted
sender skips that edit (spec 041)."""

from __future__ import annotations

from waku.alwayson.outbox import Message
from waku.alwayson.senders import Post, SendFailed, post_json
from waku.alwayson.senders.format import slack_mrkdwn

URL = "https://slack.com/api/chat.postMessage"
# A row whose target holds "update_ts" edits that message instead. The bot
# may edit its own messages with the `chat:write` scope it already has.
UPDATE_URL = "https://slack.com/api/chat.update"
HOSTED_PATH = "/slack/send"
# Slack asks for messages under 4,000 characters, and the hosted relay takes
# at most 8,000, so a hosted reply longer than this goes out in parts.
HOSTED_MAX_CHARS = 4000
# Slack refuses a section's text over 3,000 characters.
SECTION_MAX_CHARS = 3000
# The two buttons of an approval request. sources/slack.py reads a click by
# these ids.
APPROVE_ACTION = "waku_approve"
REJECT_ACTION = "waku_reject"


def request_blocks(request: dict) -> list[dict]:
    """An approval request (approvals.Request.fields()) as Block Kit. The
    body is plain text, so the maintainer reads the exact characters that
    will be posted, with no Markdown drawn over them."""
    link = str(request.get("link") or "")
    head = "\n".join(part for part in (f"*{request.get('title', '')}*",
                                       str(request.get("why") or ""),
                                       f"<{link}>" if link else "") if part)
    body = str(request.get("body") or "")
    code = str(request.get("code") or "")
    approve, reject = ([str(c) for c in request.get("choices") or []] + ["Approve", "Reject"])[:2]
    blocks: list[dict] = [{"type": "section",
                           "text": {"type": "mrkdwn", "text": head[:SECTION_MAX_CHARS]}}]
    for start in range(0, len(body), SECTION_MAX_CHARS):
        blocks.append({"type": "section", "text": {
            "type": "plain_text", "text": body[start:start + SECTION_MAX_CHARS]}})
    if decided := str(request.get("decided") or ""):
        # Answered: one line says who decided, in place of the buttons.
        blocks.append({"type": "context",
                       "elements": [{"type": "plain_text", "text": decided}]})
        return blocks
    blocks.append({"type": "actions", "block_id": f"waku_approval_{code}", "elements": [
        {"type": "button", "action_id": APPROVE_ACTION, "style": "primary",
         "text": {"type": "plain_text", "text": approve}, "value": code},
        {"type": "button", "action_id": REJECT_ACTION, "style": "danger",
         "text": {"type": "plain_text", "text": reject}, "value": code}]})
    return blocks


def _payload(message: Message, text: str) -> dict:
    payload = {"channel": message.target.get("channel"), "text": text}
    if thread := message.target.get("thread_ts"):
        payload["thread_ts"] = thread
    return payload


def make(bot_token: str, post: Post = post_json):
    def send(message: Message) -> str:
        if ts := message.target.get("update_ts"):
            payload = {"channel": message.target.get("channel"), "ts": ts,
                       "text": slack_mrkdwn(message.body),
                       "blocks": request_blocks(message.target.get("request") or {})}
            answer = post(UPDATE_URL, payload, {"Authorization": f"Bearer {bot_token}"})
            if not answer.get("ok"):
                raise SendFailed(f"Slack refused: {answer.get('error') or 'no reason'}")
            return str(answer.get("ts") or ts)
        payload = _payload(message, slack_mrkdwn(message.body))
        if isinstance(request := message.target.get("request"), dict):
            payload["blocks"] = request_blocks(request)
        answer = post(URL, payload, {"Authorization": f"Bearer {bot_token}"})
        if not answer.get("ok"):   # Slack answers 200 and says no in the body
            raise SendFailed(f"Slack refused: {answer.get('error') or 'no reason'}")
        return str(answer.get("ts") or "")

    return send


def make_hosted(base_url: str, platform_token: str, post: Post = post_json):
    """The sender for a hosted container. The platform answers in Slack's own
    shape: {"ok": true}, or {"ok": false, "error": <code>}."""
    url = base_url.rstrip("/") + HOSTED_PATH

    def send(message: Message) -> None:
        if message.target.get("update_ts"):
            return   # the platform has no edit route yet (spec 041)
        body = slack_mrkdwn(message.body)
        for start in range(0, len(body), HOSTED_MAX_CHARS):
            answer = post(url, _payload(message, body[start:start + HOSTED_MAX_CHARS]),
                          {"Authorization": f"Bearer {platform_token}"})
            if not answer.get("ok"):
                error = answer.get("error")
                raise SendFailed(
                    f"Slack refused: {error if isinstance(error, str) and error else 'no reason'}")

    return send
