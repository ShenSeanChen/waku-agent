"""The first-run question: keep your memory on Waku Memory, or on this machine?

Spec 020 A. A person who has not chosen is asked once, in the terminal (`waku`)
or on the dashboard's Overview page. Yes records `agent` and signs in; "Keep
local only" records `off`. Both are saved as WAKU_MEMORY_MODE in the same
.env a pasted model key goes to, and neither is asked again. Nothing is sent
before the answer.

A person whose mcp.json already names Waku Memory is not asked: they chose it
before this question existed, and Waku keeps behaving as it did for them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from waku.config import MEMORY_MODES, Settings, env_write_path
from waku.tools.waku_memory import connect, is_waku_memory, status

WHAT_LEAVES = (
    "Waku Memory keeps what Waku learns on waku.one, so your other agents "
    "(Claude Code, Codex, the Waku mobile app and the Waku Agent tab) know it too. "
    "It sends each fact Waku keeps, each research report and each note you save, "
    "signed in as you. Your chat history and your model keys are not sent."
)


def already_connected(home: Path) -> bool:
    """Whether mcp.json already names Waku Memory. Reads one file, opens nothing."""
    try:
        servers = json.loads((home / "mcp.json").read_text(encoding="utf-8")).get("servers", [])
    except (OSError, ValueError):
        return False
    return any(is_waku_memory(s) for s in servers)


def pending(settings: Settings) -> bool:
    """True while this person has not been asked."""
    return settings.memory_mode == "" and not already_connected(settings.home)


def save(mode: str) -> None:
    """Record the choice where a pasted key goes, and in this process."""
    if mode not in MEMORY_MODES:
        raise ValueError(f"memory mode must be one of {MEMORY_MODES}, not {mode!r}")
    from dotenv import set_key

    set_key(str(env_write_path()), "WAKU_MEMORY_MODE", mode)
    os.environ["WAKU_MEMORY_MODE"] = mode


def apply(mode: str, home: Path) -> str:
    """Record the answer and act on it. Returns one message for the person.
    `agent` signs in, which opens a browser and waits for the click."""
    if mode == "off":
        save("off")
        return ("Waku Memory stays off. Your memory stays on this machine. "
                "To connect later: waku connect waku-memory.")
    if mode == "agent":
        save("agent")
        return connect(home)
    raise ValueError("choose agent or off")


def ask(settings: Settings, input_fn=input, out=print) -> str | None:
    """The terminal question. Returns the mode recorded, or None when the
    person had nothing to answer. Enter means yes."""
    if not pending(settings):
        return None
    out(WHAT_LEAVES)
    answer = input_fn("Sign in to Waku Memory now? [Y/n] ").strip().lower()
    mode = "off" if answer in ("n", "no") else "agent"
    out(apply(mode, settings.home))
    return mode


def maybe_ask(settings: Settings) -> str | None:
    """ask(), only where a person is there to answer: a terminal on both ends.
    A gateway, a pipe, a test and a hosted container all skip it, and
    WAKU_MEMORY_PROMPT=0 skips it on purpose."""
    if os.getenv("WAKU_MEMORY_PROMPT", "") == "0":
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    return ask(settings)


def status_line(settings: Settings, waiting: int = 0) -> str:
    """One honest line for the Overview: where this person's memory goes.
    "Connected" says a sign-in exists, which is all Waku can know without
    sending; a turn's receipt says which facts Waku Memory actually took.
    `waiting` is how many kept facts have not reached it yet. "" while the
    question has not been answered."""
    if pending(settings):
        return ""
    if settings.memory_mode == "off":
        return "Local only. Your memory stays on this machine."
    line = status(settings.home)
    waits = f" {waiting} fact(s) waiting to send." if waiting else ""
    if line.startswith("connected · "):
        return f"Connected to Waku Memory as {line.split(' · ', 1)[1]}.{waits}"
    if line.startswith("configured · API key"):
        return f"Connected to Waku Memory with your API key.{waits}"
    if line.startswith("configured · not signed in"):
        return "Waku Memory is set up but you are not signed in. Run: waku connect waku-memory"
    return f"Waku Memory is not connected ({line})."


def card(settings: Settings, conn=None) -> dict:
    """What the dashboard's Overview needs: the question while it is pending,
    and a status line after."""
    waiting = 0
    if conn is not None:
        try:
            waiting = conn.execute("SELECT COUNT(*) FROM facts WHERE synced = 0").fetchone()[0]
        except Exception:
            waiting = 0
    return {"pending": pending(settings), "what": WHAT_LEAVES,
            "status": status_line(settings, waiting)}
