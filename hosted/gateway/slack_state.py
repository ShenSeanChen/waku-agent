"""GET /hosted/slack on a tenant's host: what the dashboard's Slack card shows
(waku-agent spec 030).

A tenant's container holds no Slack token and does not know whether its
person connected a workspace: the gateway does (slack.py). The dashboard's
Connections page asks here, and the gateway answers from `slack_install`
without starting the container.

READ-ONLY. It reads one row and changes nothing. The answer holds the
workspace's name and two apex addresses and never a token: connecting and
reconnecting stay the links they were, `/slack/install` and `/setup`.

The workspace's name is Slack's text. It leaves here as JSON, and the page
escapes it where it draws it.
"""

from __future__ import annotations

from hosted.gateway import slack
from hosted.ports.control import SlackInstall

PATH = "/hosted/slack"
GET_ONLY = "That route only takes a GET."


def facts(*, slack_on: bool, install: SlackInstall | None, apex_host: str) -> dict:
    """`available` is False when this server has no Slack app, and the page
    then shows no card. `connected` is False for a workspace whose stored
    token cannot be opened: it hears mentions and cannot answer, and
    `needs_reconnect` says so."""
    if not slack_on:
        return {"available": False, "connected": False, "needs_reconnect": False,
                "team_name": "", "install_url": "", "setup_url": ""}
    lost = install is not None and install.needs_reconnect
    return {
        "available": True,
        "connected": install is not None and not lost,
        "needs_reconnect": lost,
        "team_name": install.team_name if install is not None else "",
        "install_url": f"https://{apex_host}{slack.INSTALL_PATH}",
        "setup_url": f"https://{apex_host}{slack.SETUP_PATH}",
    }
