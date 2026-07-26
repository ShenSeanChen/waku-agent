"""`python -m waku.ops.show_trace` — read a JSONL trace as a terminal timeline.

Pass a trace file directly, or omit it to show the most recent trace in WAKU_HOME
(the current directory's .waku/ by default). Trace records are printed one at a
time, so long-running sessions do not need to fit in memory.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from rich.console import Console

from waku.config import load_settings


def _short(value: object, limit: int = 100) -> str:
    """Make trace fields readable without letting one event consume the screen."""
    if isinstance(value, str):
        text = value.replace("\n", " ")
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(", ", ": "))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _event_summary(event: dict) -> str:
    """Describe the event fields emitted by Tracer and the loop observers."""
    kind = event.get("type", "event")
    if kind == "turn_start":
        return f"turn start · {_short(event.get('user_message', ''))}"
    if kind == "turn_end":
        reply = _short(event.get("reply", ""))
        return f"turn end · {reply} · {event.get('iterations', 0)} iteration(s)"
    if kind == "llm":
        usage = event.get("usage", {})
        return (
            f"llm · iteration {event.get('iteration', '?')} · "
            f"{usage.get('in', 0)} in / {usage.get('out', 0)} out"
        )
    if kind == "tool":
        return f"tool · {event.get('tool', '?')}({_short(event.get('args', {}))}) → {_short(event.get('output', ''))}"
    if kind == "gate":
        reason = event.get("reason")
        return f"gate · {event.get('decision', '?')}" + (f" — {_short(reason)}" if reason else "")
    if kind == "consolidation":
        return f"memory · consolidated {event.get('new_facts', 0)} fact(s)"

    fields = {key: value for key, value in event.items() if key not in {"type", "ts"}}
    return str(kind) + (f" · {_short(fields)}" if fields else "")


def render_trace(path: Path, console: Console | None = None) -> int:
    """Render one trace file and return the number of valid events printed."""
    console = console or Console()
    console.print(f"[bold]Trace[/bold] {path}")
    events = 0
    turn_depth = 0

    try:
        lines: TextIO = path.open(encoding="utf-8")
    except FileNotFoundError:
        console.print(f"[dim]No trace file found: {path}[/dim]")
        return 0

    with lines:
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                console.print(f"[yellow]Skipping invalid JSON on line {line_number}.[/yellow]")
                continue
            if not isinstance(event, dict):
                console.print(f"[yellow]Skipping non-object JSON on line {line_number}.[/yellow]")
                continue

            kind = event.get("type", "event")
            # Turns never nest, so depth is binary: the turn_start/turn_end pair
            # always sits flush left and the events between them are indented one
            # level. Assigning the depth rather than adding to it keeps a turn that
            # crashed before writing its turn_end from pushing the rest of the day
            # steadily to the right.
            if kind in {"turn_start", "turn_end"}:
                turn_depth = 0
            timestamp = str(event.get("ts", ""))
            stamp = timestamp[11:19] if len(timestamp) >= 19 else timestamp
            indent = "  " * turn_depth
            console.print(f"{indent}[dim]{stamp}[/dim] {_event_summary(event)}")
            if kind == "turn_start":
                turn_depth = 1
            events += 1

    if not events:
        console.print("[dim]Trace is empty.[/dim]")
    return events


def _render_trace_section(label: str, path: Path, console: Console) -> int:
    console.print(f"\n[bold]=== {label} ===[/bold]")
    return render_trace(path, console)


def render_traces(paths: list[Path], console: Console | None = None) -> int:
    console = console or Console()
    events = 0
    for path in paths:
        events += _render_trace_section(path.stem, path, console)
    return events


def render_recent_days(
    traces: Path,
    days: int,
    today: date | None = None,
    console: Console | None = None,
) -> int:
    console = console or Console()
    end = today or date.today()
    events = 0
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        label = day.isoformat()
        path = traces / f"{label}.jsonl"
        if path.exists():
            events += _render_trace_section(label, path, console)
    return events


def latest_trace(traces: Path) -> Path | None:
    """Return the most recent daily trace without reading all of its contents."""
    if not traces.is_dir():
        return None
    files = (path for path in traces.glob("*.jsonl") if path.is_file())
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a Waku JSONL trace as a terminal timeline."
    )
    parser.add_argument(
        "traces", nargs="*", type=Path, help="trace JSONL file(s) (defaults to latest)"
    )
    parser.add_argument("--days", type=int, metavar="N", help="render the last N daily traces")
    args = parser.parse_args()
    console = Console()

    if args.days is not None:
        if args.days < 1:
            parser.error("--days must be at least 1")
        if args.traces:
            parser.error("--days cannot be combined with trace paths")
        render_recent_days(load_settings().home / "traces", args.days, console=console)
        return

    if args.traces:
        if len(args.traces) == 1:
            render_trace(args.traces[0], console)
            return
        render_traces(args.traces, console)
        return

    traces = load_settings().home / "traces"
    path = latest_trace(traces)
    if path is None:
        console.print(f"[dim]No traces found in {traces}.[/dim]")
        return
    render_trace(path, console)


if __name__ == "__main__":
    main()
