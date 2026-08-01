"""Telegram gateway — message your laptop from your phone.

Setup (2 minutes, free):
  1. In Telegram, message @BotFather → /newbot → copy the token
  2. Put TELEGRAM_BOT_TOKEN=... in .env
  3. Set TELEGRAM_ALLOWED_USER=<your numeric id> (message @userinfobot to get
     it) so ONLY you can talk to your Milli. Comma-separate for several people.
     LEAVING THIS UNSET MEANS ANYONE WHO FINDS YOUR BOT CAN USE IT — and this
     bot answers out of YOUR memory, with YOUR tools, on YOUR API key. The
     startup banner tells you which posture you are in; read it.
  4. make telegram

Long-polling: your laptop calls Telegram's API — no public URL, no webhook,
no server. This is why hobbyist assistants pick Telegram over WhatsApp
(Meta's Cloud API needs business verification and a public HTTPS endpoint).
"""

from __future__ import annotations

import os

from milli.app import Milli
from milli.gateway.cli import _observer  # mirror gate/tool activity on the laptop terminal


def _allowed_ids() -> set[str]:
    """Parse TELEGRAM_ALLOWED_USER into a set. Empty means no restriction —
    which is why `posture()` says so out loud on every start."""
    return {p.strip() for p in os.getenv("TELEGRAM_ALLOWED_USER", "").split(",") if p.strip()}


def posture() -> str:
    """Who can reach this bot, printed at startup. A silent default is how an
    assistant ends up serving strangers without its owner noticing."""
    ids = _allowed_ids()
    if ids:
        return f"  reachable by: {len(ids)} allowlisted user(s)"
    return ("  reachable by: ANYONE who finds this bot — it will answer from your\n"
            "                personal memory. Set TELEGRAM_ALLOWED_USER to lock it.")


def _build_app(token: str, allowed: str = ""):
    """Build the polling app + message handler. Shared by the standalone
    gateway and the background poller `milli dashboard` starts.

    `allowed` is accepted for backwards compatibility; the allowlist is read
    from the environment so a single id and a comma-separated list behave the
    same way."""
    allowed_ids = _allowed_ids() | ({allowed.strip()} if allowed.strip() else set())
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    milli = Milli()
    milli.session.session_id = "telegram"   # its own conversation thread in the inbox

    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if allowed_ids and str(update.effective_user.id) not in allowed_ids:
            await update.message.reply_text("This Milli serves someone else. Run your own!")
            return
        print(f"you › {update.message.text}")
        result = milli.respond(update.message.text, observer=_observer, source="telegram")
        print(f"milli › {result.reply}")
        await update.message.reply_text(result.reply or "(no reply)")

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    return app


def main() -> None:
    try:
        import telegram  # noqa: F401
    except ImportError:
        raise SystemExit("Telegram extra not installed: pip install 'milli-agent[telegram]'")

    from milli.config import load_settings

    token = load_settings().telegram_token
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (message @BotFather to create a bot).")
    app = _build_app(token)
    print("Milli is listening on Telegram — message your bot. Ctrl-C to stop.")
    print(posture())
    app.run_polling()


def start_in_background() -> bool:
    """Start the Telegram poller on a daemon thread — so `milli dashboard` runs
    the browser cockpit AND Telegram from one command. Returns True if started,
    False (quietly) if there's no token or the extra isn't installed. Never
    raises: a gateway problem must not take down the dashboard."""
    from milli.config import load_settings

    token = load_settings().telegram_token
    if not token:
        return False
    try:
        import telegram  # noqa: F401
    except ImportError:
        print("(telegram) TELEGRAM_BOT_TOKEN is set but the extra isn't installed — "
              "pip install 'milli-agent[telegram]'")
        return False

    import asyncio
    import threading

    print("(telegram) starting:")
    print(posture())

    import logging

    warned = {"conflict": False}

    def on_poll_error(exc: Exception) -> None:
        # Runs on every polling error. The common one is Conflict: another bot
        # instance is already polling this token. Say it ONCE, plainly, and never
        # dump a traceback into the dashboard terminal.
        from telegram.error import Conflict

        if isinstance(exc, Conflict) and not warned["conflict"]:
            warned["conflict"] = True
            print("(telegram) another instance is already running this bot — the dashboard's "
                  "Telegram stays idle. Stop the other `milli telegram` and restart to use it here.")

    def run() -> None:
        # keep PTB's own error logging out of the dashboard terminal; we report
        # the one error that matters (Conflict) cleanly via on_poll_error.
        logging.getLogger("telegram").setLevel(logging.CRITICAL)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        # its own event loop on this thread; start_polling is non-blocking, then
        # run_forever keeps it alive until the process (a daemon thread) exits.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = _build_app(token)
            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())
            loop.run_until_complete(app.updater.start_polling(error_callback=on_poll_error))
            loop.run_forever()
        except Exception as exc:
            print(f"(telegram) background poller stopped: {exc}")

    threading.Thread(target=run, daemon=True, name="telegram-poll").start()
    return True


if __name__ == "__main__":
    main()
