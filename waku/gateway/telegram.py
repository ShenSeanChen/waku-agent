"""Telegram gateway — message your laptop from your phone.

Setup (2 minutes, free):
  1. In Telegram, message @BotFather → /newbot → copy the token
  2. Put TELEGRAM_BOT_TOKEN=... in .env
  3. Set TELEGRAM_ALLOWED_USER=<your numeric id> (message @userinfobot to get
     it) so ONLY you can talk to your Waku. Comma-separate for several people.
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
import re
import tempfile
from pathlib import Path

from waku.app import Waku
from waku.gateway.cli import make_console_unicode_safe

make_console_unicode_safe()  # any unicode (→, ✓, emoji) must never kill a backgrounded bot


def _observer(kind: str, event: dict) -> None:
    """Mirror the loop's internals to the laptop terminal — plain prints, no
    Rich, so a redirected cp1251 console cannot kill message handling on an
    em-dash or arrow (the CLI's Rich observer is too fragile off a TTY)."""
    if kind == "tool":
        print(f"  tool - {event['tool']}({event['args']}) -> {event['output'][:80]}")
    elif kind == "gate":
        print(f"  gate - {event['decision']} - {event.get('reason', '')}")
    elif kind == "consolidation":
        print(f"  memory - consolidated {event['new_facts']} fact(s) from recent chats")

# Emoji and pictographs: a TTS engine reads these aloud ("rocket", "sparkles"),
# which sounds absurd in a voice reply. Strip them and stray markdown markers
# before synthesizing — same hygiene as the voice gateway's Mouth.
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f]+"
)


def _speakable(text: str) -> str:
    text = _EMOJI.sub("", text or "")
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


_whisper = None
_whisper_lock = None
_pipeline = None
_RU_DIR = Path(os.getenv("WAKU_RU_VOICE_DIR", "")) if os.getenv("WAKU_RU_VOICE_DIR") else None
if _RU_DIR is None:
    home = os.getenv("WAKU_HOME", ".waku")
    _RU_DIR = Path(home) / "kokoro-ru"
_RU_DIR = _RU_DIR if _RU_DIR.is_dir() else None


def _get_whisper():
    """One shared WhisperModel across messages (loading takes seconds) — created
    lazily on the first voice note so telegram-only installs never pay for it."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(
            os.getenv("WAKU_WHISPER_MODEL", "base"),
            compute_type="int8",
        )
    return _whisper


def _transcribe(data: bytes) -> str:
    """Whisper an OGG voice note. The note is binary (opus in ogg), so write it
    to a temp file and let faster-whisper's decoder (libav) handle it."""
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        segments, _ = _get_whisper().transcribe(path, language=os.getenv("WAKU_WHISPER_LANG"))
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _speech_wav(text: str) -> bytes:
    """Kokoro reply as a sendable WAV (mono 24k, like the local Mouth).

    Russian by default via the kokoro-ru port (zaakirio/kokoro-ru, three
    cons a party on consented studio voices). Falls back to British English
    if the ru model files are missing, so a plain voice install still works.
    """
    import io

    import numpy as np
    from soundfile import write as sf_write

    audio = _synth_ru(text) if _RU_DIR.is_dir() else _synth_en(text)
    buf = io.BytesIO()
    sf_write(buf, np.concatenate(audio), 24000, format="WAV")
    return buf.getvalue()


def _synth_en(text: str) -> list:
    from kokoro import KPipeline

    global _pipeline
    if _pipeline is None:
        _pipeline = KPipeline(lang_code="b")
    return [audio for _, _, audio in _pipeline(text, voice=os.getenv("WAKU_VOICE", "bm_george"))]


def _synth_ru(text: str, voice: str = "sveta") -> list:
    """Route text -> kokoro-ru (ru_g2p -> KModel). Paths live in $WAKU_HOME/
    kokoro-ru; create them by running scripts/setup_ru_voice.py."""
    import sys

    import torch

    # _RU_DIR is non-None here — the caller checked it before choosing this path.
    sys.path.insert(0, str(_RU_DIR))
    from kokoro import KModel  # noqa: E402
    from ru_g2p import RuG2P  # noqa: E402

    g2p = RuG2P(espeak_data=str(_RU_DIR / "espeak-data"), vocab_path=str(_RU_DIR / "kokoro-config.json"))
    model = KModel(model=str(_RU_DIR / "kokoro-ru-v2-base.pth")).eval()
    pack = torch.load(str(_RU_DIR / f"voices/{voice}.pt"), map_location="cpu", weights_only=True)
    ipa, _ = g2p(text)
    with torch.no_grad():
        return [model(ipa, pack[len(ipa) - 1], 1.0, return_output=True).audio.numpy()]


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
    gateway and the background poller `waku dashboard` starts.

    `allowed` is accepted for backwards compatibility; the allowlist is read
    from the environment so a single id and a comma-separated list behave the
    same way."""
    allowed_ids = _allowed_ids() | ({allowed.strip()} if allowed.strip() else set())
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    waku = Waku()
    waku.session.session_id = "telegram"   # its own conversation thread in the inbox

    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if allowed_ids and str(update.effective_user.id) not in allowed_ids:
            await update.message.reply_text("This Waku serves someone else. Run your own!")
            return
        print(f"you › {update.message.text}")
        result = waku.respond(update.message.text, observer=_observer, source="telegram")
        print(f"waku › {result.reply}")
        await update.message.reply_text(result.reply or "(no reply)")

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if allowed_ids and str(update.effective_user.id) not in allowed_ids:
            await update.message.reply_text("This Waku serves someone else. Run your own!")
            return
        try:
            import asyncio
            import io

            note = update.message.voice or getattr(update.message, "audio", None)
            if note is None:
                return
            from telegram.constants import ChatAction

            await update.message.chat.send_action(ChatAction.TYPING)
            tg_file = await note.get_file()
            data = await tg_file.download_as_bytearray()
            print(f"you › [voice {len(data)//1024} KiB]")
            heard = await asyncio.to_thread(_transcribe, bytes(data))
            if not heard:
                await update.message.reply_text("(didn't catch that — try again?)")
                return
            print(f"you › {heard}")
            result = waku.respond(heard, observer=_observer, source="telegram")
            print(f"waku › {result.reply}")
            text = result.reply or "(no reply)"
            await update.message.reply_text(text)
            # Voice back to them if the neural voice is installed — optional, so a
            # telegram-only install (no torch) still works as plain text.
            try:
                import kokoro  # noqa: F401
            except ImportError:
                return
            if os.getenv("WAKU_TG_VOICE", "0") != "1":
                return
            wav = await asyncio.to_thread(_speech_wav, _speakable(text))
            await update.message.reply_voice(io.BytesIO(wav))
        except Exception as exc:  # BLE001: a voice hiccup must never kill the bot
            print(f"(telegram) voice handling failed: {exc}")
            try:
                await update.message.reply_text("(voice processing failed — try again?)")
            except Exception:
                pass

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    return app


def main() -> None:
    try:
        import telegram  # noqa: F401
    except ImportError:
        raise SystemExit("Telegram extra not installed: pip install 'waku-agent[telegram]'")

    from waku.config import load_settings

    token = load_settings().telegram_token
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (message @BotFather to create a bot).")
    app = _build_app(token)
    print("Waku is listening on Telegram — message your bot. Ctrl-C to stop.")
    print(posture())
    app.run_polling()


def start_in_background() -> bool:
    """Start the Telegram poller on a daemon thread — so `waku dashboard` runs
    the browser cockpit AND Telegram from one command. Returns True if started,
    False (quietly) if there's no token or the extra isn't installed. Never
    raises: a gateway problem must not take down the dashboard."""
    from waku.config import load_settings
    from waku.gateway.cli import make_console_unicode_safe

    make_console_unicode_safe()  # model replies can hold → ✓ emoji; a pipe must not kill the bot
    token = load_settings().telegram_token
    if not token:
        return False
    try:
        import telegram  # noqa: F401
    except ImportError:
        print("(telegram) TELEGRAM_BOT_TOKEN is set but the extra isn't installed — "
              "pip install 'waku-agent[telegram]'")
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
                  "Telegram stays idle. Stop the other `waku telegram` and restart to use it here.")

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
