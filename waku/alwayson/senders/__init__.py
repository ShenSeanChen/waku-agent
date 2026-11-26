"""One sender per channel. A sender delivers one outbox row and returns, or
raises SendFailed. Only the dispatcher calls a sender (spec 023, decision 5).
The Slack sender returns the posted message's ts, and the dispatcher keeps it
as the row's receipt (spec 035).

Telegram and Slack are one HTTP request each through the standard library.
GitHub is one `gh issue comment`, or one `gh pr review --comment` for a review. On hosted Waku the Slack request goes to
the platform, which holds the workspace's token (senders/slack.py).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from waku.alwayson.outbox import Message

TIMEOUT_SECONDS = 15

# (url, JSON payload, extra headers) -> the JSON object the service answered.
Post = Callable[[str, dict, Mapping[str, str]], dict]


class SendFailed(RuntimeError):
    """A delivery did not happen. The message never holds a URL or a token."""


def post_json(url: str, payload: dict, headers: Mapping[str, str]) -> dict:
    """POST JSON and return the JSON answer. Telegram's address contains the
    bot token, so no error raised here quotes the address."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
            raw = reply.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()   # Telegram explains a refusal in the body
        try:
            answer = json.loads(raw)
        except ValueError:
            answer = None
        if isinstance(answer, dict):
            return answer
        raise SendFailed(f"HTTP {exc.code}") from None
    except OSError as exc:   # URLError, a timeout, a dropped connection
        raise SendFailed(f"could not connect ({type(exc).__name__})") from None
    try:
        answer = json.loads(raw)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise SendFailed("the answer was not JSON")
    return answer


def configured(settings) -> dict[str, Callable[[Message], None]]:
    """The senders this machine's settings turn on. A row for a channel that
    is not here fails, and after five tries it is dead."""
    from waku.alwayson.senders import github, slack, telegram

    senders: dict[str, Callable[[Message], None]] = {"github": github.make()}
    if settings.telegram_token:
        senders["telegram"] = telegram.make(settings.telegram_token)
    base_url = os.getenv("WAKU_PLATFORM_BASE_URL", "")
    platform_token = os.getenv("WAKU_PLATFORM_TOKEN", "")
    if os.getenv("WAKU_HOSTED", "") == "1" and base_url and platform_token:
        # Hosted Waku: the platform holds the workspace's Slack token and
        # posts for this container (senders/slack.py, make_hosted).
        senders["slack"] = slack.make_hosted(base_url, platform_token)
    elif bot_token := os.getenv("SLACK_BOT_TOKEN", ""):
        senders["slack"] = slack.make(bot_token)
    return senders
