"""Reset .milli to a clean, curated state for a demo / recording.

    python scripts/demo_seed.py                 # clean slate, KEEPS the spend ledger
    python scripts/demo_seed.py --reset-spend   # also wipe usage.jsonl (money/tokens)

What it does (your old state is backed up first, never just deleted):
  1. moves the current .milli aside to .milli.bak-<timestamp>
  2. creates a fresh state.db + calendar.ics
  3. seeds a small, clean memory (a few facts + one episode) and ONE calendar
     event — Sergey's standing Saturday 5 PM swim
  4. clears the loop/tool traces AND the Ops eval history, so the Loop, Tools and
     Ops tabs start empty and fill up live in front of the viewer as you type

The money/token spend ledger (usage.jsonl) is treated as a permanent record and
is KEPT by default — it's only wiped when you explicitly pass --reset-spend.

Everything it writes is the same data the app writes — open state.db afterwards
and it looks exactly like real use, just tidy.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime

from milli.config import load_settings
from milli.db import connect
from milli.memory.episodic.store import SqliteEpisodeStore
from milli.memory.semantic.store import SqliteFactStore
from milli.tools.calendar import make_tool

# Curated seed — clean, no duplicates. Edit these to taste before recording.
FACTS = [
    # The parentheses around each multi-line string are load-bearing, not style:
    # inside a collection, a MISSING COMMA silently glues two entries into one
    # instead of erroring. ruff's ISC004 flags exactly that shape.
    ("user", ("The user runs the YouTube channel 'Sean's AI Stories' and films implementation "
              "walkthroughs. His X account is @ShenSeanChen. All of his Chinese social media "
              "accounts are called 肖恩君Sean.")),
    ("raj", ("Raj is a close friend who plays really great tennis and always teaches me great "
             "British slangs!")),
    ("sergey", "Sergey is the close friend who loves swimming and often cooks delicious food!"),
]
EPISODE = ("2026-07-11", "Confirmed the standing Saturday 5 PM swim with Sergey.")
EVENT = {"title": "Swim with Sergey", "start": "2026-07-11T17:00",
         "end": "2026-07-11T18:00", "attendees": "Sergey"}


def main(reset_spend: bool = False) -> None:
    settings = load_settings()
    home = settings.home

    if home.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = home.with_name(f"{home.name}.bak-{stamp}")
        shutil.copytree(home, backup)
        print(f"backed up {home} -> {backup}")
        # calendar.ics + these dirs are plain files no process holds open.
        # traces/ = the Loop & Tools history; clear it so those tabs start empty.
        (home / "calendar.ics").unlink(missing_ok=True)
        for sub in ("outbox", "skills", "traces"):
            d = home / sub
            if d.exists():
                shutil.rmtree(d)
        # Ops eval history — start empty so a live `make gate` adds a visible row.
        (home / "eval_runs.jsonl").unlink(missing_ok=True)
        (home / "eval_report.json").unlink(missing_ok=True)
        # The spend ledger is a permanent record — only wiped on explicit request.
        if reset_spend:
            (home / "usage.jsonl").unlink(missing_ok=True)

    settings.ensure_home()
    conn = connect(home)

    # Clear the DB rows IN PLACE — never delete state.db. Deleting the file
    # would leave any live gateway (a running `make telegram`, the dashboard,
    # an open CLI) holding a broken, read-only connection to the old inode.
    for table in ("chat_log", "calendar_events", "facts", "episodes"):
        conn.execute(f"DELETE FROM {table}")   # triggers keep the FTS index in sync
    conn.commit()

    facts, episodes = SqliteFactStore(conn), SqliteEpisodeStore(conn)
    for subject, content in FACTS:
        facts.add(subject, content, source="user")
    episodes.add(EPISODE[1], happened_at=EPISODE[0])

    create_event = make_tool(conn, home).fn
    print(create_event(**EVENT))

    # regenerate the human-readable MEMORY.md mirror for the fresh state
    from milli.memory import Memory

    Memory(conn, settings, None).export_markdown()

    print(f"\nclean demo state ready in {home}")
    print(f"  facts: {len(FACTS)}  ·  episodes: 1  ·  events: 1  ·  chat log: cleared")
    print("  CLEARED: loop/tool traces, Ops eval history, outbox, skills.")
    if reset_spend:
        print("  CLEARED: usage.jsonl (money/token spend) — you approved this.")
    else:
        print("  KEPT: SOUL.md and usage.jsonl (your real spend — pass --reset-spend to wipe).")
    print("  Run `milli dashboard` and start filming.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset .milli to a clean demo state.")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="required confirmation: yes, wipe .milli (it is backed up first)")
    parser.add_argument("--reset-spend", action="store_true",
                        help="also wipe usage.jsonl (the money/token spend ledger)")
    args = parser.parse_args()
    if not args.yes:
        # Safety gate: this destroys live memory/calendar/traces. Refuse unless the
        # human explicitly confirms with --yes. See CLAUDE.md ("Never wipe runtime
        # data without asking first"). It backs up, but restoring is a hassle.
        print("REFUSING to run: demo_seed clears .milli (memory, calendar, chat, traces"
              + (", AND spend" if args.reset_spend else "") + ").")
        print("This is destructive. If you truly mean it, re-run with --yes:")
        print("    python scripts/demo_seed.py --yes"
              + (" --reset-spend" if args.reset_spend else ""))
        raise SystemExit(2)
    main(reset_spend=args.reset_spend)
