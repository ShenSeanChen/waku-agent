"""`python -m waku brief` — a morning briefing that runs through the normal
harness (so it traces and animates like any turn), composing your real calendar,
mail, and memory into a focus-first summary. Cron it for a daily greeting:

    30 7 * * *  cd ~/waku-agent && make brief

The heavy lifting lives in skills/weekly-brief/SKILL.md — this just kicks off
the turn and saves the result to the outbox.
"""

from __future__ import annotations

from datetime import date

from rich.console import Console

from waku.app import Waku

PROMPT = "Brief me on my week: what's on my calendar, what's in my mail that needs attention, and what I should focus on today."


def main() -> None:
    console = Console()
    waku = Waku()
    if not waku.settings.apple_tools:
        console.print("[dim]Tip: set WAKU_APPLE_TOOLS=1 to brief from your real Calendar and Mail.[/dim]")
    result = waku.respond(PROMPT, source="brief")
    console.print(result.reply)
    out = waku.settings.home / "outbox" / f"brief-{date.today().isoformat()}.txt"
    out.write_text(result.reply + "\n", encoding="utf-8")
    console.print(f"[dim]saved to {out}[/dim]")


if __name__ == "__main__":
    main()
