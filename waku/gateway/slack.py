"""Slack gateway — DM Waku from Slack.

DMs only, and only from the people you list. Channels are never answered.

This gateway runs YOUR agent: your memory (`.waku/state.db`), your SOUL.md,
your tools. Every member of a Slack workspace can open a DM with any app
installed in it, so an unlisted bot is a bot the whole workspace can question
about you, bill to your key, and feed into your long-term memory. That is why
SLACK_ALLOWED_USER is required: with no list the gateway refuses to start
rather than guess who you meant.

Setup (about 5 minutes, no public URL needed: Socket Mode dials out):

  1. Create an app at https://api.slack.com/apps, "From scratch".
  2. Socket Mode: turn it on and create an app-level token with the
     connections:write scope. That token (xapp-...) is SLACK_APP_TOKEN.
  3. OAuth & Permissions: add the bot scopes chat:write and im:history, then
     install the app to your workspace. The Bot User OAuth Token (xoxb-...)
     is SLACK_BOT_TOKEN.
  4. Event Subscriptions: turn it on and subscribe to the bot event message.im.
  5. App Home: allow users to send messages from the Messages tab, or nobody
     can DM the bot.
  6. In .env:

     SLACK_BOT_TOKEN           the xoxb- token from step 3.
     SLACK_APP_TOKEN           the xapp- token from step 2.
     SLACK_ALLOWED_USER        required. Comma-separated member ids (U0123ABCD):
                               open a profile, the three-dot menu, "Copy member
                               ID". Only these people get an answer.
     SLACK_MAX_TURNS_PER_HOUR  default 30. A hard ceiling across all users; the
                               bot goes quiet once it's hit rather than spending.
     SLACK_HOME                a SEPARATE .waku for the bot. Use this whenever
                               you list anyone but yourself, so they get their
                               own memory instead of reading yours.
  7. make slack
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from waku.app import Waku
from waku.gateway.cli import _observer
from waku.gateway.discord import TurnBudget, _ids
from waku.gateway.runner import GatewayAgentRunner, run_gateway_turn, safe_exception
from waku.integrations import IntegrationState, IntegrationStatus

INSTALL_HINT = "Slack extra not installed: pip install 'waku-agent[slack]'"


def config_problem() -> str | None:
    """What is missing before the gateway may start, or None when nothing is.

    An empty allowlist is a problem, not a default: see the module docstring."""
    if not os.getenv("SLACK_BOT_TOKEN", "").strip():
        return "Set SLACK_BOT_TOKEN in .env (the xoxb- Bot User OAuth Token)."
    if not os.getenv("SLACK_APP_TOKEN", "").strip():
        return "Set SLACK_APP_TOKEN in .env (the xapp- app-level token, Socket Mode)."
    if not _ids("SLACK_ALLOWED_USER"):
        return ("Set SLACK_ALLOWED_USER in .env: the Slack member ids allowed to DM "
                "Waku. The Slack gateway will not start without it.")
    return None


def should_answer(event: dict, allowed_users: set[str]) -> bool:
    """The whole access policy, as one pure function so it can be tested without
    a Slack connection. Deny is the default at every branch.

    Answered only when it is a plain DM, typed by a person on the list. A
    `subtype` covers edits, deletions, joins and file shares; a `bot_id` covers
    every bot, this one's own replies included, so Waku never answers itself.
    """
    if not allowed_users:
        return False
    if event.get("channel_type") != "im":
        return False
    if event.get("subtype") or event.get("bot_id"):
        return False
    if event.get("user") not in allowed_users:
        return False
    return bool((event.get("text") or "").strip())


class SeenEvents:
    """The last few event ids, so an event Slack delivers twice runs once.

    Slack resends an event it thinks went unacknowledged. Without this, a
    resend is a second billed turn and a second copy of the reply. Slack only
    retries within minutes, so a short in-memory window is enough.
    """

    def __init__(self, size: int = 1000) -> None:
        self.size = size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def first_time(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._ids:
            return False
        self._ids[event_id] = None
        if len(self._ids) > self.size:
            self._ids.popitem(last=False)
        return True


def _build_agent() -> Waku:
    """The agent behind the bot. SLACK_HOME gives it a memory of its own."""
    home = os.getenv("SLACK_HOME", "").strip()
    if not home:
        return Waku()
    from waku.config import load_settings

    settings = load_settings()
    settings.home = Path(home)
    settings.ensure_home()
    return Waku(settings=settings)


def _new_runner() -> GatewayAgentRunner:
    return GatewayAgentRunner(
        _build_agent, session_id="slack", source="slack", observer=_observer
    )


def _build_app(runner: GatewayAgentRunner):
    """Build the Slack app and its message handler."""
    from slack_bolt.async_app import AsyncApp

    allowed_users = _ids("SLACK_ALLOWED_USER")
    budget = TurnBudget(int(os.getenv("SLACK_MAX_TURNS_PER_HOUR", "30")))
    seen = SeenEvents()
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"].strip())

    # Bolt acknowledges the event to Slack before this runs, so a slow agent
    # turn never trips Slack's 3-second resend.
    @app.event("message")
    async def on_message(body: dict, event: dict, say) -> None:
        if not should_answer(event, allowed_users):
            return                      # silence, not a refusal reply
        if not seen.first_time(body.get("event_id", "")):
            return
        if not budget.allow():
            print(f"(slack) hourly turn budget spent — ignoring {event.get('user')}")
            return
        thread_ts = event.get("thread_ts")

        async def send(text: str) -> None:
            await say(text=text, thread_ts=thread_ts)

        await run_gateway_turn(runner, event["text"].strip(), send)

    return app


async def _serve(runner: GatewayAgentRunner, stop: asyncio.Event,
                 on_ready: Callable[[], None] | None = None) -> None:
    """Connect, run until `stop` is set, disconnect.

    Bolt's own `start_async()` sleeps forever, which leaves nothing for the
    dashboard to stop, so this connects and waits on an event instead."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    app = _build_app(runner)
    await app.client.auth_test()        # a wrong bot token fails here, not on the first DM
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"].strip())
    await handler.connect_async()
    if on_ready:
        on_ready()
    try:
        await stop.wait()
    finally:
        await handler.close_async()


def describe_posture() -> str:
    """Four lines on startup saying exactly who can reach this bot and whose
    memory it is using."""
    users = _ids("SLACK_ALLOWED_USER")
    home = os.getenv("SLACK_HOME", "").strip() or ".waku — YOUR personal memory"
    cap = os.getenv("SLACK_MAX_TURNS_PER_HOUR", "30")
    return (f"  reachable by: {len(users)} allowlisted user(s)\n"
            f"  answers in:   DMs only — no channel will be answered\n"
            f"  memory:       {home}\n"
            f"  spend cap:    {cap} turns/hour")


def main() -> None:
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        raise SystemExit(INSTALL_HINT)
    if problem := config_problem():
        raise SystemExit(problem)

    runner = _new_runner()
    print("Waku is listening on Slack. Ctrl-C to stop.")
    print(describe_posture())
    try:
        asyncio.run(_serve(runner, asyncio.Event()))
    except KeyboardInterrupt:
        pass
    finally:
        runner.close()


class SlackHandle:
    """Runs the Slack client on its own thread and event loop for the dashboard."""

    def __init__(self, runner: GatewayAgentRunner) -> None:
        self.runner = runner
        self.ready = threading.Event()
        self.error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._loop_started = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="slack-client")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._loop_started.set()
        await _serve(self.runner, self._stop, on_ready=self.ready.set)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 — isolate the dashboard from bot errors
            self.error = safe_exception(exc)
            print(f"(slack) background client stopped: {self.error}")
        finally:
            self._loop_started.set()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        try:
            self._loop_started.wait(timeout=5)
            if self._loop and self._stop:
                try:
                    self._loop.call_soon_threadsafe(self._stop.set)
                except RuntimeError:
                    pass                # loop already closed: the client stopped on its own
            if self.thread.is_alive():
                self.thread.join(timeout=10)
        finally:
            self.runner.close()

    def status(self) -> IntegrationStatus:
        if self.error:
            return IntegrationStatus(IntegrationState.ERROR, self.error)
        if not self.thread.is_alive():
            return IntegrationStatus(IntegrationState.ERROR, "gateway stopped unexpectedly")
        if error := self.runner.error_status:
            return IntegrationStatus(IntegrationState.ERROR, error)
        if self.ready.is_set():
            return IntegrationStatus(IntegrationState.CONNECTED)
        return IntegrationStatus(IntegrationState.INSTALLED_BUT_UNCONFIGURED, "starting")


def start_in_background() -> SlackHandle | None:
    """Start Slack on a daemon thread; None when no bot token is set.

    A token with something else missing raises, so the dashboard card shows
    what to fix instead of a generic failure."""
    if not os.getenv("SLACK_BOT_TOKEN", "").strip():
        return None
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        raise RuntimeError(INSTALL_HINT) from None
    if problem := config_problem():
        raise RuntimeError(problem)

    print("(slack) starting:")
    print(describe_posture())
    handle = SlackHandle(_new_runner())
    handle.start()
    return handle


if __name__ == "__main__":
    main()
