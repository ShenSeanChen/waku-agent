"""Discord gateway — message Waku from a direct message or a server channel.

READ THIS BEFORE PUTTING THE BOT IN A SHARED SERVER.

This gateway runs YOUR agent: your memory (`.waku/state.db`), your SOUL.md, your
tools. Anyone it answers can ask what it remembers about you, and — because the
chat log feeds consolidation — anything they say can become a permanent fact in
your memory. A bot sitting in a busy channel is therefore three problems at
once, not one:

  cost       every message it answers is a full agent turn, billed to your key
  privacy    strangers can read your personal facts back out
  integrity  their conversation gets distilled into your long-term memory

So the default posture is DENY, and it takes deliberate configuration to open
up. With nothing configured the bot answers direct messages only and ignores
every server channel completely.

Setup:
  1. Create a bot at https://discord.com/developers/applications
  2. Enable the Message Content Intent on the bot page
  3. Put DISCORD_BOT_TOKEN=... in .env and invite the bot to your server
  4. Open it up only as far as you actually need (all optional):

     DISCORD_ALLOWED_USER      comma-separated numeric user ids. Empty = anyone
                               may DM the bot. Set this if the bot is reachable
                               by people you don't know.
     DISCORD_ALLOWED_CHANNEL   comma-separated numeric channel ids. Empty means
                               NO server channel is answered. This is the switch
                               that keeps a 500-person Discord from billing you.
     DISCORD_REQUIRE_MENTION   default "1". In a server channel, only reply when
                               the bot is @-mentioned. Set "0" at your own risk.
     DISCORD_MAX_TURNS_PER_HOUR  default 30. A hard ceiling across all users; the
                               bot goes quiet once it's hit rather than spending.
     DISCORD_HOME              a SEPARATE .waku for the bot. Use this whenever
                               the bot serves anyone but you — it gets its own
                               memory, so a community bot can never read or
                               poison your personal one.
  5. make discord
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from waku.app import Waku
from waku.gateway.cli import _observer
from waku.gateway.runner import GatewayAgentRunner, run_gateway_turn
from waku.integrations import IntegrationState, IntegrationStatus


def _ids(env: str) -> set[str]:
    """Parse a comma-separated numeric id allowlist. Empty env -> empty set."""
    return {p.strip() for p in os.getenv(env, "").split(",") if p.strip()}


class TurnBudget:
    """A hard ceiling on agent turns per rolling hour, across every user.

    Not a fairness mechanism — a spend stop. Without it, one person (or one bot
    loop, or one person who thinks it's funny) can run your API bill up for as
    long as they can type. At the limit the bot goes SILENT rather than replying
    with an error, because an error reply is itself a message that invites
    another message.
    """

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._turns: list[float] = []

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._turns = [t for t in self._turns if now - t < 3600]
        if len(self._turns) >= self.per_hour:
            return False
        self._turns.append(now)
        return True


def should_answer(*, is_dm: bool, author_id: str, channel_id: str, mentioned: bool,
                  allowed_users: set[str], allowed_channels: set[str],
                  require_mention: bool) -> bool:
    """The whole access policy, as one pure function so it can be tested without
    a Discord connection. Deny is the default at every branch.

    DMs      answered unless an allowlist exists and excludes you.
    Channels answered only if the channel is explicitly allowlisted AND the bot
             was @-mentioned AND the author passes the user allowlist.

    The load-bearing line is `channel_id not in allowed_channels`: an UNSET
    channel allowlist means no channel, not every channel. The previous version
    read `if allowed and author != allowed` — with nothing configured that guard
    is skipped entirely, so the bot answered every message from every member of
    every channel it could see. An empty config must be the safe one.
    """
    if allowed_users and author_id not in allowed_users:
        return False
    if is_dm:
        return True
    if channel_id not in allowed_channels:
        return False
    return mentioned or not require_mention


def _strip_mention(content: str, bot_id: str) -> str:
    """Drop the '@Waku Agent' prefix so the model sees the question, not the ping."""
    for form in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(form, " ")
    return content.strip()


def _build_agent() -> Waku:
    """The agent behind the bot. DISCORD_HOME gives it a memory of its own —
    the difference between 'a bot in my community' and 'my private assistant,
    exposed'."""
    home = os.getenv("DISCORD_HOME", "").strip()
    if not home:
        return Waku()
    from waku.config import load_settings

    settings = load_settings()
    settings.home = Path(home)
    settings.ensure_home()
    return Waku(settings=settings)


def _new_runner() -> GatewayAgentRunner:
    return GatewayAgentRunner(
        _build_agent, session_id="discord", source="discord", observer=_observer
    )


def _build_client(runner: GatewayAgentRunner | None = None):
    """Build the Discord client and its message handler."""
    import discord

    allowed_users = _ids("DISCORD_ALLOWED_USER")
    allowed_channels = _ids("DISCORD_ALLOWED_CHANNEL")
    require_mention = os.getenv("DISCORD_REQUIRE_MENTION", "1").strip() not in ("", "0")
    budget = TurnBudget(int(os.getenv("DISCORD_MAX_TURNS_PER_HOUR", "30")))

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    runner = runner or _new_runner()

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author == client.user or not message.content:
            return
        if not should_answer(
            is_dm=message.guild is None,
            author_id=str(message.author.id),
            channel_id=str(message.channel.id),
            mentioned=client.user in message.mentions,
            allowed_users=allowed_users,
            allowed_channels=allowed_channels,
            require_mention=require_mention,
        ):
            return                      # silence, not a refusal reply
        if not budget.allow():
            print(f"(discord) hourly turn budget spent — ignoring {message.author}")
            return
        text = _strip_mention(message.content, str(client.user.id))
        if not text:
            return
        async with message.channel.typing():
            await run_gateway_turn(runner, text, message.reply)

    return client


def describe_posture() -> str:
    """Three lines on startup saying exactly who can reach this bot and whose
    memory it is using. A silent default is how a bot ends up answering a whole
    server without anyone noticing it started."""
    users = _ids("DISCORD_ALLOWED_USER")
    channels = _ids("DISCORD_ALLOWED_CHANNEL")
    who = f"{len(users)} allowlisted user(s)" if users else "anyone who can DM it"
    where = (f"{len(channels)} allowlisted channel(s), @mention required"
             if channels else "DMs only — no server channel will be answered")
    home = os.getenv("DISCORD_HOME", "").strip() or ".waku — YOUR personal memory"
    cap = os.getenv("DISCORD_MAX_TURNS_PER_HOUR", "30")
    return (f"  reachable by: {who}\n"
            f"  answers in:   {where}\n"
            f"  memory:       {home}\n"
            f"  spend cap:    {cap} turns/hour")


def main() -> None:
    try:
        import discord  # noqa: F401
    except ImportError:
        raise SystemExit("Discord extra not installed: pip install 'waku-agent[discord]'")

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN in .env (create a bot in the Discord Developer Portal).")
    runner = _new_runner()
    try:
        client = _build_client(runner)
        print("Waku is listening on Discord. Ctrl-C to stop.")
        print(describe_posture())
        client.run(token)
    finally:
        runner.close()


class DiscordHandle:
    def __init__(self, client, thread, started, runner: GatewayAgentRunner) -> None:
        self.client, self.thread, self.started = client, thread, started
        self.runner = runner

    def stop(self) -> None:
        try:
            self.started.wait(timeout=5)
            loop = getattr(self.client, "loop", None)
            try:
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.client.close(), loop).result(timeout=10)
            finally:
                if self.thread.is_alive():
                    self.thread.join(timeout=10)
        finally:
            self.runner.close()

    def status(self) -> IntegrationStatus:
        if not self.thread.is_alive() or self.client.is_closed():
            return IntegrationStatus(IntegrationState.ERROR, "gateway stopped unexpectedly")
        if error := self.runner.error_status:
            return IntegrationStatus(IntegrationState.ERROR, error)
        if self.client.is_ready():
            return IntegrationStatus(IntegrationState.CONNECTED)
        return IntegrationStatus(IntegrationState.INSTALLED_BUT_UNCONFIGURED, "starting")


def start_in_background() -> DiscordHandle | None:
    """Start Discord on a daemon thread, returning False when it is not configured."""
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        return None
    try:
        import discord  # noqa: F401
    except ImportError:
        print("(discord) DISCORD_BOT_TOKEN is set but the extra isn't installed — "
              "pip install 'waku-agent[discord]'")
        return None

    print("(discord) starting:")
    print(describe_posture())

    runner = _new_runner()
    try:
        client = _build_client(runner)
    except Exception:
        runner.close()
        raise
    started = threading.Event()

    @client.event
    async def on_ready():
        started.set()

    def run() -> None:
        try:
            client.run(token)
        except Exception as exc:  # noqa: BLE001 — isolate the dashboard from bot errors
            print(f"(discord) background client stopped: {exc}")

    thread = threading.Thread(target=run, daemon=True, name="discord-client")
    thread.start()
    return DiscordHandle(client, thread, started, runner)


if __name__ == "__main__":
    main()
