"""GET /setup on the apex: the three steps, and which are done (spec 026 C4).

  1. Sign in          done when the browser holds an active tenant's session
  2. Add to Slack     done when that tenant connected a workspace
  3. Mention @Waku    done with step 2: nothing is left to set up

The gateway renders the page for the person asking, so the page has no
script and its policy allows none. Each step's state is a `data-state` on its
list item and a word beside it: done, todo, or off when this server has no
Slack app configured.

EVERYTHING THAT IS NOT THIS FILE'S OWN TEXT IS ESCAPED. The workspace's name
comes from Slack, and the outcome in `?slack=` comes from a URL anybody can
write, so the outcome is looked up in a closed set and never shown itself.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiohttp import web

from hosted.gateway import answers, slack
from hosted.ports.control import SlackInstall

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "setup.html"

# How an install ended, as the callback reports it in /setup?slack=<outcome>.
OUTCOMES = {
    slack.CONNECTED: "Slack is connected.",
    slack.DENIED: "Slack was not connected, because the request was cancelled in Slack.",
    slack.EXPIRED: "That link expired before Slack answered. Press Add to Slack again.",
    slack.FAILED: "Slack did not finish the install. Press Add to Slack again.",
    slack.TAKEN: "That workspace is already connected to another Waku account.",
}
SLACK_OFF = "Slack is not available on this server yet."
STATE_WORDS = {"done": "Done", "todo": "To do", "off": "Not available"}

# No script-src, so default-src 'none' is what a script gets.
POLICY = ("default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; "
          "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")


def _button(href: str, label: str, *, secondary: bool = False) -> str:
    level = ' class="secondary"' if secondary else ""
    return (f'<p><a data-slot="button"{level} href="{html.escape(href, quote=True)}">'
            f"{html.escape(label)}</a></p>")


def _step(number: int, name: str, state: str, title: str, detail: str,
          action: str = "") -> str:
    return (f'    <li data-step="{name}" data-state="{state}">'
            f'<span class="number">{number}</span>'
            f"<div><h2>{html.escape(title)}</h2>"
            f'<p class="detail">{html.escape(detail)}</p>{action}</div>'
            f'<span class="state">{STATE_WORDS[state]}</span></li>\n')


def render(*, signed_in: bool, slack_on: bool, install: SlackInstall | None,
           outcome: str, login_url: str, dashboard_url: str) -> web.Response:
    connected = signed_in and install is not None
    steps = _step(
        1, "sign-in", "done" if signed_in else "todo", "Sign in",
        "You are signed in." if signed_in
        else "Sign in with a link sent to your email.",
        "" if signed_in else _button(login_url, "Sign in"))
    if not slack_on:
        steps += _step(2, "slack", "off", "Add to Slack", SLACK_OFF)
    elif connected:
        steps += _step(
            2, "slack", "done", "Add to Slack",
            f"Connected to {install.team_name}.",
            _button(slack.INSTALL_PATH, "Connect a different workspace", secondary=True))
    else:
        steps += _step(
            2, "slack", "todo", "Add to Slack",
            "Choose your workspace and approve Waku. No token is copied."
            if signed_in else "Sign in first.",
            _button(slack.INSTALL_PATH, "Add to Slack") if signed_in else "")
    steps += _step(
        3, "mention", "done" if connected else "todo", "Mention @Waku",
        "Invite @Waku to a channel and mention it. It answers in the thread."
        if connected else "After Slack is connected, mention @Waku in a channel.",
        _button(dashboard_url, "Open your agent", secondary=True) if connected else "")
    sentence = OUTCOMES.get(outcome, "") if signed_in else ""
    notice = (f'  <p id="notice" role="status" data-outcome="{outcome}">'
              f"{html.escape(sentence)}</p>\n" if sentence else "")
    body = (TEMPLATE.read_text(encoding="utf-8")
            .replace("@@NOTICE@@", notice).replace("@@STEPS@@", steps))
    response = web.Response(text=body, content_type="text/html", charset="utf-8")
    response.headers["Content-Security-Policy"] = POLICY
    return answers.harden(response)
