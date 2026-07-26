"""Voice gateway — talk to your laptop, it talks back.

    pip install -e '.[voice]'
    make voice

Push-to-talk MVP: press Enter, speak, press Enter again. Your speech runs
through the exact same loop/memory/eval pipeline as typed text — a gateway
only moves words in and out (that's the whole point of the gateway box).

  ears   faster-whisper (local Whisper, ~74MB model downloads on first run)
  voice  macOS `say` with a British voice by default (zero setup), or the
         neural Kokoro voice if installed:  pip install kokoro soundfile
         then set WAKU_TTS=kokoro  (WAKU_VOICE=bm_george / bm_fable / ...)

Wake-word mode ("hey <name>, ...") is deliberately v2 — see docs/roadmap:
openWakeWord can train a custom wake word for whatever we name this thing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from waku.app import Waku
from waku.gateway.cli import _observer  # show gate/tool lines in voice mode too

SAMPLE_RATE = 16000


def record_until_enter():
    """Capture mic audio between two Enter presses; returns a float32 array."""
    import numpy as np
    import sounddevice as sd

    frames: list[np.ndarray] = []

    def collect(indata, frame_count, time_info, status):
        frames.append(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=collect):
        input("recording — press Enter when done… ")
    if not frames:
        return np.zeros(0, dtype="float32")
    return np.concatenate(frames)[:, 0]


class Ears:
    def __init__(self, model_size: str | None = None):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            model_size or os.getenv("WAKU_WHISPER_MODEL", "base"),
            compute_type="int8",
        )

    def transcribe(self, audio, language: str | None = None) -> str:
        segments, _ = self.model.transcribe(
            audio, language=language or os.getenv("WAKU_WHISPER_LANG")
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


def _best_say_voice() -> str:
    """Pick the nicest available macOS voice: prefer a downloaded Premium/Enhanced
    (near-Siri quality) voice, then a decent compact one. Beats hardcoding a
    robotic default — and auto-upgrades the moment you download a better voice in
    System Settings ▸ Accessibility ▸ Spoken Content ▸ System Voice."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5, check=False).stdout
    except Exception:
        return "Daniel"
    # each line: "Name (variant)      en_US    # sample" — split name from the locale column
    english = []
    for ln in out.splitlines():
        m = re.match(r"^(.+?)\s{2,}([a-z]{2})_", ln)
        if m and m.group(2) == "en":
            english.append(m.group(1).strip())
    # first: a downloaded high-quality voice (Premium > Enhanced)
    for tag in ("(Premium)", "(Enhanced)"):
        hit = next((n for n in english if tag in n), None)
        if hit:
            return hit
    # else a reasonable compact voice, if present
    for pref in ("Serena", "Kate", "Daniel", "Samantha"):
        if pref in english:
            return pref
    return english[0] if english else "Daniel"


# Emoji and pictographs — a TTS engine reads these out loud ("rocket", "sparkles"),
# which sounds ridiculous. Strip them (plus leftover markdown bullets) before speaking.
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff"  # symbols, pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027bf"    # misc symbols + dingbats
    "\U0001f1e6-\U0001f1ff"    # regional-indicator flag letters
    "\U00002190-\U000021ff"    # arrows
    "\U00002b00-\U00002bff"    # stars, misc symbols-and-arrows
    "\U0000fe00-\U0000fe0f"    # variation selectors
    "\U0000200d\U000020e3\U0000fe0f]+"  # ZWJ + keycap/variation joiners
)


def _speakable(text: str) -> str:
    """What actually gets voiced: no emoji, no stray markdown markers, tidy spaces."""
    if not text:
        return ""
    text = _EMOJI.sub("", text)
    text = re.sub(r"[*_`#>]", "", text)      # markdown emphasis/heading/quote/code marks
    text = re.sub(r"[ \t]{2,}", " ", text)   # collapse gaps left by removed glyphs
    return "\n".join(ln.strip() for ln in text.splitlines()).strip()


class Mouth:
    """TTS with a boring, reliable default (macOS `say`) and a neural upgrade
    (Kokoro-82M, Apache-2.0 — its bm_* voices are the proper British butler)."""

    def __init__(self):
        self.engine = os.getenv("WAKU_TTS", "").strip().lower()
        self.voice = os.getenv("WAKU_VOICE", "")
        if not self.engine:
            # Auto: use the nicer neural voice (Kokoro) if it's installed, else
            # fall back to macOS `say`. So `pip install kokoro soundfile` alone
            # upgrades the voice — no env var needed.
            try:
                import kokoro  # noqa: F401

                self.engine = "kokoro"
            except ImportError:
                self.engine = "say"
        if self.engine == "kokoro":
            from kokoro import KPipeline

            self.pipeline = KPipeline(lang_code="b")  # b = British English
            self.voice = self.voice or "bm_george"
        elif self.engine == "say" and not self.voice:
            self.voice = _best_say_voice()  # auto-upgrade to a Premium/Enhanced voice

    def speak(self, text: str) -> None:
        text = _speakable(text)
        if not text:
            return
        if self.engine == "kokoro":
            import sounddevice as sd

            for _, _, audio in self.pipeline(text, voice=self.voice):
                sd.play(audio, 24000)
                sd.wait()
        elif sys.platform == "darwin":
            subprocess.run(["say", "-v", self.voice or "Daniel", text], check=False)
        else:
            print("(no TTS engine on this platform — set WAKU_TTS=kokoro)")


def matches_wake(text: str, wake_word: str) -> bool:
    """Does a transcript contain the (customizable) wake word?

    Fuzzy on purpose: Whisper hears "waku waku" as "wakuwaku", "Waku, waku!",
    "walku waku" — or transcribes it as Japanese kana わくわく (the first live
    test!). So: `wake_word` accepts comma-separated variants across scripts
    ("waku waku,わくわく"), normalization keeps kana AND CJK, and matching is
    substring + sliding-window similarity. Pure function → deterministic evals.
    """
    import difflib
    import re

    def norm(s: str) -> str:
        # keep latin, digits, hiragana/katakana (぀-ヿ), CJK ideographs (一-鿿)
        return re.sub(r"[^a-z0-9぀-ヿ一-鿿 ]", "", s.lower()).strip()

    heard = norm(text)
    if not heard:
        return False

    for variant in (v for v in (norm(v) for v in wake_word.split(",")) if v):
        if variant in heard or variant.replace(" ", "") in heard.replace(" ", ""):
            return True
        words, n = heard.split(), len(variant.split())
        if any(
            difflib.SequenceMatcher(None, " ".join(words[i : i + n]), variant).ratio() >= 0.7
            for i in range(max(0, len(words) - n + 1))
        ):
            return True
    return False


def _mic_threshold() -> float:
    """RMS below this = silence. Mics vary wildly — tune with
    WAKU_MIC_THRESHOLD (lower if it never hears you, higher if it
    wakes on room noise)."""
    return float(os.getenv("WAKU_MIC_THRESHOLD", "0.005"))


def record_command(stream, max_seconds: float = 15.0, silence_after: float = 1.2):
    """After the wake word: keep reading the SAME stream until the speaker
    goes quiet. Reusing the stream matters — opening a fresh macOS audio
    stream per phase is how the first version froze."""
    import numpy as np

    block = SAMPLE_RATE // 10  # 100ms blocks
    frames, quiet, spoke = [], 0, False
    for _ in range(int(max_seconds * 10)):
        data, _ = stream.read(block)
        frames.append(data.copy())
        loud = float(np.sqrt((data**2).mean())) > _mic_threshold() * 2
        spoke = spoke or loud
        quiet = 0 if loud else quiet + 1
        if spoke and quiet >= int(silence_after * 10):
            break
    return np.concatenate(frames)[:, 0]


def wait_for_speech(stream, timeout: float) -> bool:
    """Poll the SAME stream for the onset of speech, up to `timeout` seconds.
    Returns True the moment the mic goes loud, False if it stays quiet — lets a
    conversation stay open for follow-ups (Siri-style) without the wake word."""
    import numpy as np

    block = SAMPLE_RATE // 10
    for _ in range(int(timeout * 10)):
        data, _ = stream.read(block)
        if float(np.sqrt((data**2).mean())) > _mic_threshold() * 2:
            return True
    return False


def wake_loop(waku: Waku, mouth: Mouth, wake_word: str) -> None:
    """Always-listening mode: scan the mic in ~2.5s windows with the tiny
    Whisper model until the wake word shows up, then hand off to the big one.

    This is the transparent, zero-training way to make ANY phrase a wake word.
    Trade-off vs a real wake-word engine (openWakeWord): a bit more CPU and a
    chunk boundary can occasionally split the phrase — say it with intent.

    Engineering notes from the first live freeze:
    - ONE persistent InputStream for everything. sd.rec()+sd.wait() per chunk
      re-opens the device every 2.5s and can block forever when macOS audio
      routing changes (say/AirPods/etc).
    - The scanner always shows a heartbeat, so "listening" never looks "dead".
    - The mic buffer is drained after Waku speaks, so it doesn't wake on
      the tail of its own voice (the "mm-hmm" self-trigger in the trace).
    """
    import numpy as np
    import sounddevice as sd

    scout = Ears(model_size="tiny")  # cheap, always on
    ears = Ears()                    # accurate, only after wake
    ack = os.getenv("WAKU_WAKE_ACK", "Yes?")
    followup = float(os.getenv("WAKU_FOLLOWUP_SECONDS", "8"))  # stay open, Siri-style
    block = SAMPLE_RATE // 10
    # Pin the scout's transcription language to match the wake word's script —
    # otherwise Whisper hears "waku waku" and helpfully writes わくわく, which
    # a latin wake word never matches. Commands after wake still auto-detect.
    wake_lang = os.getenv("WAKU_WAKE_LANG") or ("en" if wake_word.isascii() else None)
    print(f'Listening for "{wake_word}" — Ctrl-C to quit.')

    def status(msg: str) -> None:
        sys.stdout.write(f"\r\x1b[2m{msg[:72]:<72}\x1b[0m")
        sys.stdout.flush()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block) as stream:

        def drain() -> None:
            while stream.read_available >= block:
                stream.read(block)

        window: list = []
        while True:
            data, _ = stream.read(block)
            window.append(data.copy())
            if len(window) < 25:  # gather 2.5s
                continue
            chunk = np.concatenate(window)[:, 0]
            window = window[-5:]  # keep a 0.5s tail so the phrase can straddle chunks

            if float(np.sqrt((chunk**2).mean())) < _mic_threshold():
                status("· listening…")
                continue
            heard_scan = scout.transcribe(chunk, language=wake_lang)
            if not matches_wake(heard_scan, wake_word):
                status(f'· heard: "{heard_scan}"' if heard_scan else "· listening…")
                if heard_scan:  # near-misses belong in the trace (wake tuning!)
                    waku.tracer.event("wake_scan", {"heard": heard_scan, "matched": False})
                continue

            print("\n[wake word]")
            mouth.speak(ack)
            drain()  # don't transcribe the ack playing over the mic

            # Stay in the conversation after waking: answer, then keep listening
            # for a follow-up for `followup` seconds — no need to say "waku waku"
            # again (like Siri). A quiet stretch drops back to wake-word mode.
            while True:
                heard = ears.transcribe(record_command(stream))
                if heard:
                    print(f"you › {heard}")
                    result = waku.respond(heard, observer=_observer, source="voice")
                    print(f"waku › {result.reply}")
                    mouth.speak(result.reply)
                else:
                    print("(didn't catch that)")
                drain()  # ...and don't wake on the tail of the reply
                status(f"· still here — just talk, or I'll rest in {followup:.0f}s")
                if not wait_for_speech(stream, followup):
                    break  # quiet → back to listening for the wake word
            window = []


def main() -> None:
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        raise SystemExit("Voice extra not installed: pip install -e '.[voice]'")

    waku = Waku()
    waku.session.session_id = "voice"   # its own conversation thread in the inbox
    mouth = Mouth()

    # Hands-free by default: always-listening for "waku waku". The default packs
    # in the ways the tiny scanner mis-hears it (wakuwaku / waka waka / kana), so
    # it triggers reliably. Set WAKU_WAKE_WORD="" for push-to-talk instead.
    wake_word = os.getenv(
        "WAKU_WAKE_WORD", "waku waku,wakuwaku,waku,waka waka,wako wako,walk walk,わくわく"
    ).strip()
    if wake_word:
        try:
            wake_loop(waku, mouth, wake_word)
        except KeyboardInterrupt:
            pass
        print("\nbye — your memory stays in state.db")
        return

    ears = Ears()
    print("Voice Waku ready. Press Enter to talk, Ctrl-C to quit.")
    while True:
        try:
            input("\npress Enter to talk… ")
            audio = record_until_enter()
        except (EOFError, KeyboardInterrupt):
            break
        if audio.size < SAMPLE_RATE // 4:  # under 250ms — probably a slip
            print("(too short, try again)")
            continue

        heard = ears.transcribe(audio)
        if not heard:
            print("(didn't catch that)")
            continue
        print(f"you › {heard}")

        result = waku.respond(heard, observer=_observer, source="voice")
        print(f"waku › {result.reply}")
        mouth.speak(result.reply)

    print("bye — your memory stays in state.db")


if __name__ == "__main__":
    main()
