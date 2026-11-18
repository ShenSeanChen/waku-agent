"""Slack as an event source (spec 023): an @mention becomes one row in `events`.

Setup:
  1. Create a Slack app with Socket Mode on, the `app_mentions:read` and
     `chat:write` scopes (to hear a mention and to reply) and the
     `app_mention` event. Socket Mode means this machine calls Slack, so a
     laptop needs no public address.
  2. pip install 'waku-agent[slack]'
  3. In .env: SLACK_APP_TOKEN (starts xapp-), SLACK_BOT_TOKEN (starts xoxb-)
     and SLACK_ALLOWED_CHANNELS, a comma-separated list of channel ids.
  4. waku serve

Who is heard: an `app_mention`, written by a person, in the workspace the bot
token belongs to, in a channel on the allowlist. Everything else is dropped.
The allowlist is a closed set, so an empty SLACK_ALLOWED_CHANNELS hears nobody.

  external_id   Slack's event_id, so a resent event is a no-op
  session_id    slack:<channel>:<thread>, so each thread keeps its own history

Written against slack_sdk 3.47.0 (2026-11-16). It has been run against that
library's classes and against fake envelopes, never against a live workspace.
"""

from __future__ import annotations

import os

from waku.alwayson.events import Inbox


def allowed_channels() -> set[str]:
    return {c.strip() for c in os.getenv("SLACK_ALLOWED_CHANNELS", "").split(",") if c.strip()}


def record(inbox: Inbox, envelope: dict, *, team_id: str, channels: set[str]) -> bool:
    """Write one Events API envelope into the inbox when it is a mention this
    agent was asked to hear. True when a row was written."""
    event = envelope.get("event") or {}
    event_id = envelope.get("event_id") or ""
    channel, ts = event.get("channel") or "", event.get("ts") or ""
    if event.get("type") != "app_mention" or not event_id or not ts:
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


class Handle:
    def __init__(self, client) -> None:
        self.client = client

    def stop(self) -> None:
        self.client.close()


def start(inbox: Inbox) -> Handle | None:
    """Connect over Socket Mode, or return None when Slack is not configured."""
    app_token, bot_token = os.getenv("SLACK_APP_TOKEN", ""), os.getenv("SLACK_BOT_TOKEN", "")
    if not app_token or not bot_token:
        return None
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except ImportError:
        print("(slack) SLACK_APP_TOKEN is set but the extra isn't installed — "
              "pip install 'waku-agent[slack]'")
        return None

    channels = allowed_channels()
    web = WebClient(token=bot_token)
    team_id = web.auth_test()["team_id"]   # the one workspace this agent hears
    client = SocketModeClient(app_token=app_token, web_client=web)

    def listener(client, request) -> None:
        # Store, then acknowledge. Slack resends what was not acknowledged, and
        # the event_id makes that second delivery a no-op.
        if request.type == "events_api":
            record(inbox, request.payload, team_id=team_id, channels=channels)
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    print(f"(slack) listening for mentions in {len(channels)} allowed channel(s)"
          + ("" if channels else ": set SLACK_ALLOWED_CHANNELS, or nobody is heard"))
    return Handle(client)
