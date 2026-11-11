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
from waku.tools.waku_memory import connect, is_waku_memory

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


def card(settings: Settings) -> dict:
    """What the dashboard's Overview needs to show the question."""
    return {"pending": pending(settings), "what": WHAT_LEAVES}
