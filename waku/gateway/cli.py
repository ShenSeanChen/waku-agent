"""CLI gateway — the zero-setup way to talk to your Waku.

The Gateway Interface box: a gateway only moves text in and out; everything
interesting happens in the loop. The Telegram gateway is the same ~60 lines
with polling instead of input().
"""

from __future__ import annotations

import sqlite3
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from waku.app import Waku

console = Console()


def make_console_unicode_safe() -> None:
    """Waku prints model output and live text that may hold ANY unicode (→, ✓,
    emoji) and arrows in its own status lines. On Windows, a redirected or
    piped stdout/stderr defaults to the console code page (e.g. cp1251) and
    dies with UnicodeEncodeError on the first such character — a backgrounded
    `waku dashboard`, a `| tee` pipe, a task-scheduler run all hit it. Swap in
    errors='replace' so no exotic char can take a gateway down; keep the code
    page so the local log stays readable. No-op on POSIX, where normal streams
    are already UTF-8-safe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def _memory_snapshot(conn: sqlite3.Connection) -> str:
    """Render a bounded, read-only view of Waku's local memory."""
    fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    facts = conn.execute("SELECT subject, content FROM facts ORDER BY id DESC LIMIT 8").fetchall()
    episode_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    episodes = conn.execute(
        "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC, id DESC LIMIT 5"
    ).fetchall()
    pending = conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated = 0").fetchone()[0]

    lines = [f"Semantic facts ({fact_count})"]
    lines.extend(f"- [{row['subject']}] {row['content']}" for row in facts)
    if not facts:
        lines.append("- none yet")

    lines.extend(["", f"Recent episodes ({episode_count})"])
    lines.extend(f"- {row['happened_at']} - {row['summary']}" for row in episodes)
    if not episodes:
        lines.append("- none yet")

    lines.extend(["", f"Unconsolidated chat messages: {pending}"])
    return "\n".join(lines)


def _observer(kind: str, event: dict) -> None:
    """Show the loop's internals live — the video's 'transparent harness' beat."""
    if kind == "tool":
        console.print(f"  [dim]tool · {event['tool']}({event['args']}) → {event['output'][:80]}[/dim]")
    elif kind == "gate":
        console.print(f"  [dim]gate · {event['decision']} — {event.get('reason','')}[/dim]")
    elif kind == "consolidation":
        console.print(f"  [dim]memory · consolidated {event['new_facts']} fact(s) from recent chats[/dim]")


def main() -> None:
    make_console_unicode_safe()
    waku = Waku()
    waku.session.session_id = "terminal"   # its own conversation thread in the inbox
    console.print(Panel.fit(
        "[bold]Waku[/bold] — local, yours, transparent.\n"
        f"home: {waku.settings.home.resolve()}   model: {waku.settings.model}\n"
        "Commands: /memory · /quit",
        border_style="cyan",
    ))
    while True:
        try:
            user_message = console.input("[bold cyan]you ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_message:
            continue
        if user_message in ("/quit", "/exit"):
            break
        if user_message == "/memory":
            console.print(
                Panel(
                    Text(_memory_snapshot(waku.conn)),
                    title="Memory snapshot",
                    border_style="cyan",
                )
            )
            continue
        result = waku.respond(user_message, observer=_observer, source="cli")
        console.print(f"[bold green]waku ›[/bold green] {result.reply}\n")
    console.print("[dim]bye — your memory stays in state.db[/dim]")


if __name__ == "__main__":
    main()
