"""Slack as an event source (spec 023): an @mention becomes one row in `events`.

Setup:
  1. Create a Slack app from the manifest beside this file,
     slack-app-manifest.yaml (spec 025): Socket Mode on, the `app_mentions:read`
     and `chat:write` scopes (to hear a mention and to reply) and the
     `app_mention` event. Socket Mode means this machine calls Slack, so a
     laptop needs no public address.
  2. pip install 'waku-agent[slack]'
  3. In .env: SLACK_APP_TOKEN (starts xapp-), SLACK_BOT_TOKEN (starts xoxb-)
     and SLACK_ALLOWED_CHANNELS, a comma-separated list of channel ids.
  4. waku serve

`waku connections` checks the two tokens and names a scope that is missing.

Who is heard: an `app_mention`, written by a person, in the workspace the bot
token belongs to, in a channel on the allowlist. Everything else is dropped.
The allowlist is a closed set, so an empty SLACK_ALLOWED_CHANNELS hears nobody.

A click on Approve or Reject (spec 035) in an allowed channel of the same
workspace becomes the same "yes <code>" or "no <code>" a person types in
Telegram, marked as a click. Whether that person may approve is decided when
the worker reads it, not here.

  external_id   Slack's event_id, so a resent event is a no-op
  session_id    slack:<channel>:<thread>, so each thread keeps its own history

Written against slack_sdk 3.47.0 (2026-11-16). It has been run against that
library's classes and against fake envelopes, never against a live workspace.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from waku.alwayson.events import Inbox
from waku.alwayson.senders.slack import APPROVE_ACTION, REJECT_ACTION
from waku.alwayson.sources import MissingExtra

# The app manifest a person creates their Slack app from. It ships in the
# package, so an install from PyPI has it too.
MANIFEST = Path(__file__).with_name("slack-app-manifest.yaml")
# What the bot token must be allowed to do, and the one event the app is
# sent. The manifest grants exactly these, and `waku connections` checks them.
BOT_SCOPES = ("app_mentions:read", "chat:write")
BOT_EVENT = "app_mention"
# What the app-level token (xapp-) needs to open a Socket Mode connection.
APP_SCOPE = "connections:write"
# A button says yes or no, and its value is the request's 8-character code.
CHOICES = {APPROVE_ACTION: "yes", REJECT_ACTION: "no"}
_CODE_RE = re.compile(r"^[0-9a-f]{8}$")


def allowed_channels() -> set[str]:
    return {c.strip() for c in os.getenv("SLACK_ALLOWED_CHANNELS", "").split(",") if c.strip()}


def record(inbox: Inbox, envelope: dict, *, team_id: str, channels: set[str]) -> bool:
    """Write one Events API envelope into the inbox when it is a mention this
    agent was asked to hear. True when a row was written."""
    event = envelope.get("event") or {}
    event_id = envelope.get("event_id") or ""
    channel, ts = event.get("channel") or "", event.get("ts") or ""
    if event.get("type") != BOT_EVENT or not event_id or not ts:
        return False
    if event.get("bot_id"):
        return False
    # A shared channel carries people from other workspaces; `user_team` names
    # the sender's. Without it the event's own team is the sender's.
    if envelope.get("team_id") != team_id or event.get("user_team", team_id) != team_id:
        return False
    if channel not in channels:
        return False
    thread = event.get("thread_ts") or ts
    return inbox.add(
        source="slack",
        external_id=event_id,
        session_id=f"slack:{channel}:{thread}",
        payload={"text": event.get("text") or "",
                 "sender": {"id": event.get("user") or ""},
                 "reply_to": {"channel": channel, "thread_ts": thread}},
    )


def record_click(inbox: Inbox, payload: dict, *, team_id: str, channels: set[str]) -> bool:
    """Write one Approve or Reject click into the inbox (spec 035, decision
    4). The click becomes `yes <code>` or `no <code>`, the row a typed
    Telegram answer writes, so the worker resolves it unchanged. `clicked`
    marks it, because a typed yes in a channel never counts. True when a row
    was written."""
    if payload.get("type") != "block_actions":
        return False
    actions = payload.get("actions") or []
    action = actions[0] if actions and isinstance(actions[0], dict) else {}
    choice, code = CHOICES.get(action.get("action_id")), str(action.get("value") or "")
    user = payload.get("user") or {}
    channel = (payload.get("channel") or {}).get("id") or ""
    message = payload.get("message") or {}
    ts = message.get("ts") or ""
    if not choice or not _CODE_RE.match(code) or not user.get("id") or not ts:
        return False
    if (payload.get("team") or {}).get("id") != team_id or user.get("team_id", team_id) != team_id:
        return False
    if channel not in channels:
        return False
    thread = message.get("thread_ts") or ts
    return inbox.add(
        source="slack",
        external_id=f"click:{channel}:{ts}:{user['id']}:{action.get('action_ts') or ''}",
        session_id=f"slack:{channel}:{thread}",
        payload={"text": f"{choice} {code}", "clicked": choice, "sender": {"id": user["id"]},
                 "reply_to": {"channel": channel, "thread_ts": thread}})


class Handle:
    def __init__(self, client) -> None:
        self.client = client

    def stop(self) -> None:
        self.client.close()


def start(inbox: Inbox) -> Handle | None:
    """Connect over Socket Mode, or return None when Slack is not configured.
    Raises MissingExtra when the tokens are set and the extra is not installed."""
    app_token, bot_token = os.getenv("SLACK_APP_TOKEN", ""), os.getenv("SLACK_BOT_TOKEN", "")
    if not app_token or not bot_token:
        return None
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except ImportError:
        raise MissingExtra("SLACK_APP_TOKEN", "slack") from None

    channels = allowed_channels()
    web = WebClient(token=bot_token)
    team_id = web.auth_test()["team_id"]   # the one workspace this agent hears
    client = SocketModeClient(app_token=app_token, web_client=web)

    def listener(client, request) -> None:
        # Store, then acknowledge. Slack resends what was not acknowledged, and
        # the event_id, or the click's own id, makes that second delivery a no-op.
        if request.type == "events_api":
            record(inbox, request.payload, team_id=team_id, channels=channels)
        elif request.type == "interactive":
            record_click(inbox, request.payload, team_id=team_id, channels=channels)
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    print(f"(slack) listening for mentions in {len(channels)} allowed channel(s)"
          + ("" if channels else ": set SLACK_ALLOWED_CHANNELS, or nobody is heard"))
    return Handle(client)
