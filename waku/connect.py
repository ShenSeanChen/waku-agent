"""`waku connect <name>` — sign in to an integration that needs your browser.

Signing in opens a browser window and waits for you to click Allow, so it is
the one step a turn must never take on its own. It has exactly two doors, both
driven by you: `waku connect google` in a terminal, and `/connect google` in the
dashboard chat, which runs it in-process because the dashboard is on your
machine. A tool that finds an integration unconnected names those two doors,
and evals/deterministic/test_connect_command.py checks that both exist.
"""

from __future__ import annotations

import importlib
from pathlib import Path

# name -> the function that opens the browser, gets consent, and caches the token
CONNECTORS = {
    "google": "waku.tools.google_calendar:connect",
    "waku-memory": "waku.tools.waku_memory:connect",
}


def usage() -> str:
    return "Usage: waku connect <name>  —  available: " + ", ".join(sorted(CONNECTORS)) + "."


def connect(name: str, home: Path) -> str:
    target = CONNECTORS.get(name.strip().lower())
    if target is None:
        return f"Nothing called '{name}' to connect. " + usage()
    module_name, _, fn_name = target.partition(":")
    return getattr(importlib.import_module(module_name), fn_name)(home)


def cli_main(argv: list[str]) -> int:
    if not argv or argv[0].strip().lower() not in CONNECTORS:
        print(usage() if not argv else connect(argv[0], Path()))
        return 1
    from waku.config import load_settings

    settings = load_settings()
    settings.ensure_home()
    print(connect(argv[0], settings.home))
    return 0
