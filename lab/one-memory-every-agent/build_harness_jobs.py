"""Draw "What a personal agent harness is actually for".

Six jobs people give these agents, each with how Grok Bot and Muse do it, and
the five parts every one of those jobs needs. The point of the board is the
last line: four of the five parts you can swap; memory is the one you cannot
take with you.

Run:  python lab/one-memory-every-agent/build_harness_jobs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for scripts/

from scripts.whiteboard import style as S  # noqa: E402

OUT = Path(__file__).resolve().parent / "whiteboards" / "harness-jobs.excalidraw"

JOBS = [
    ("1  Inbox and daily digest",
     "read mail, chat and calendar, then write the summary\n"
     "Grok Bot:  a chief-of-staff routine\n"
     "Muse:  Gmail connector, Sentinel approves each send"),
    ("2  Research and errands on the web",
     "open sites, compare, fill forms, book\n"
     "Grok Bot:  one browser, signed in once for every Bot\n"
     "Muse:  browser sub-agent, reads text, no JavaScript"),
    ("3  Recurring work",
     "the same job every morning, or when something happens\n"
     "Grok Bot:  routines, 50 per Bot, last 20 runs kept\n"
     "Muse:  long-term goals; no routines documented"),
    ("4  Spending money",
     "checkout, subscriptions, bills\n"
     "Grok Bot:  stops before anything is sent\n"
     "Muse:  Stripe Link single-use cards, asks every time"),
    ("5  Watching things for you",
     "accounts, incidents, metrics, then tell me\n"
     "Grok Bot:  8 official use cases, all work\n"
     "Muse:  life -- bills, recipes, training plans"),
    ("6  Teaching it your way",
     "record a process once, reuse it, share it\n"
     "Grok Bot:  10 minutes of your screen becomes a skill\n"
     "Muse:  no skills exposed; it writes its own connectors"),
]

PARTS = [
    ("connectors", "your accounts"),
    ("a browser", "the rest of the web"),
    ("a schedule", "work while you sleep"),
    ("approvals", "what it may do alone"),
    ("memory", "who you are, and\nwhat happened last time"),
]


def build() -> list:
    e: list = []
    e.append(S.text(60, 40, "What a personal agent harness is actually for", size=S.FS_TITLE))
    e.append(S.underline(64, 110, 780))
    e.append(S.text(64, 130, "six jobs people give Grok Bot and Muse -- and the five parts every one of them needs",
                    size=S.FS_HEADER, color=S.PAL["grey"][1]))
    e += S.socials_block(2150, 44)

    bottoms = []
    for i, (title, body) in enumerate(JOBS):
        x = 80 + (i % 3) * 800
        y = 210 + (i // 3) * 260
        card = S.card(x, y, 740, title, body)
        e += card
        bottoms.append((x + 370, y + card[0]["height"]))

    # every job drops into the same five parts. Only the bottom row draws an
    # arrow: a line from the top row would cross the card beneath it.
    for cx, cy in bottoms[3:]:
        e.append(S.arrow(cx, cy, cx, 740, dashed=True))

    e += S.boundary(80, 740, 2300, 190, "every one of those six needs the same five parts", color="blue")
    for i, (name, note) in enumerate(PARTS):
        e += S.labeled_box(120 + i * 450, 795, 410, 110, f"{name}\n{note}",
                           color="green" if name == "memory" else "plain",
                           fill="chip" if name == "memory" else "node")

    e.append(S.red_note(80, 960, "Four of the five you can swap for another vendor. Memory is the one you cannot take with you:"))
    e.append(S.red_note(80, 990, "Grok Bot will not export it, and Muse keeps it in a file inside its own VM."))
    e += S.labeled_box(1500, 950, 880, 80, "Waku Memory: the part you can take with you",
                       color="green", fill="chip", size=S.FS_HEADER)

    e.append(S.source_label(80, 1070, "Jobs and mechanics from the vendors' own pages: docs.x.ai/grok-bot/use-cases and about.fb.com/news 2026-09-08, read 2026-09-15"))
    e.append(S.watermark(2150, 1070))
    return e


def main() -> None:
    elements = build()
    S.validate(elements)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({len(elements)} elements)")


if __name__ == "__main__":
    main()
