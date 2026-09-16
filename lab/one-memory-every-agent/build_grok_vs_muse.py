"""Draw "Two personal agent harnesses, side by side" — Grok Bot vs Muse.

Solid boxes are what the vendor documents. Dashed boxes are our inference,
marked as such, because neither company publishes how memory is stored or how
the agent plans. Every claim carries a dated source label.

Run:  python lab/one-memory-every-agent/build_grok_vs_muse.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for scripts/

from scripts.whiteboard import style as S  # noqa: E402

OUT = Path(__file__).resolve().parent / "whiteboards" / "grok-vs-muse.excalidraw"


def guess_box(e, x, y, w, h, label):
    """A box we inferred: dashed edge, so nobody mistakes it for documentation."""
    box, text = S.labeled_box(x, y, w, h, label, size=S.FS_BODY)
    box["strokeStyle"] = "dashed"
    e += [box, text]


def build() -> list:
    e: list = []
    e.append(S.text(60, 40, "Two personal agent harnesses, side by side", size=S.FS_TITLE))
    e.append(S.underline(64, 110, 760))
    e.append(S.text(64, 130, "solid = the vendor documents it   ·   dashed = our inference, not documented",
                    size=S.FS_HEADER, color=S.PAL["grey"][1]))
    e += S.socials_block(2150, 44)

    # ── Grok Bot ────────────────────────────────────────────────────────────
    e += S.pill_header(80, 200, 1060, "GROK BOT (xAI)  —  a team of Bots sharing one computer", color="plain")
    e += S.boundary(80, 280, 1060, 600, "your account's cloud VM  (one, for every Bot)", color="red")

    for i, name in enumerate(("Bot: sales", "Bot: recruiting", "Bot: chief of staff")):
        e += S.ellipse(120 + i * 330, 350, 290, 110, name, fill="node")
    e.append(S.text(120, 490, "separate personalities, separate screens", size=S.FS_SMALL, color=S.PAL["grey"][1]))

    e += S.labeled_box(120, 530, 300, 120, "shared browser\nsign in once, every Bot is signed in")
    e += S.labeled_box(450, 530, 300, 120, "/workspace files\nvisible to every Bot")
    e += S.labeled_box(780, 530, 320, 120, "terminal + credentials\nshared too")
    guess_box(e, 120, 680, 300, 110, "memory per Bot\nstorage undocumented")
    e += S.labeled_box(450, 680, 300, 110, "routines\n50 per Bot, last 20 runs")
    e += S.labeled_box(780, 680, 320, 110, "skills\nrecord 10 min of your screen")

    e += S.labeled_box(120, 810, 980, 52, "connectors and custom MCP are account-wide: public URL + key header, no sign-in flow",
                       size=S.FS_SMALL)

    # ── Muse ────────────────────────────────────────────────────────────────
    e += S.pill_header(1340, 200, 1060, "MUSE (Meta)  —  one agent with a bodyguard", color="plain")
    e += S.boundary(1340, 280, 1060, 600, "Muse Secure VM  (one per person)", color="red")

    e += S.boundary(1380, 350, 620, 300, "agent container", color="orange")
    e += S.ellipse(1420, 390, 250, 110, "Muse agent\n(Spark 1.3)", fill="node")
    e += S.labeled_box(1700, 390, 260, 110, "MEMORY.md + files\nyou can read them")
    e += S.ellipse(1420, 520, 540, 100, "browser sub-agent: reads the page as text, cannot run JavaScript", fill="node")

    e += S.diamond(2060, 390, 300, 150, "Sentinel\nallow / deny / ask you", color="green")
    e.append(S.text(2040, 600, "outside the container -- it acts on your accounts",
                    size=S.FS_SMALL, color=S.PAL["grey"][1]))
    e += S.labeled_arrow(1960, 445, 2060, 460, "every action")
    e.append(S.arrow(2210, 545, 2210, 700))
    e.append(S.text(2240, 640, "real credentials swapped in here", size=S.FS_SMALL))
    e += S.labeled_box(2060, 700, 300, 110, "connectors\nGmail, Stripe Link, Instagram")
    guess_box(e, 1380, 680, 620, 130, "how it decides what to remember, and what it forgets\nundocumented")

    # ── the line that matters ───────────────────────────────────────────────
    e.append(S.red_note(80, 900, "Both remember you inside their own VM.\nNeither exports it, and neither can read the other's."))
    e += S.labeled_box(830, 980, 860, 90, "Waku Memory: one memory both can reach, that you can read and export",
                       color="green", fill="chip", size=S.FS_HEADER)
    e.append(S.arrow(700, 880, 900, 980, dashed=True))
    e.append(S.arrow(1800, 880, 1620, 980, dashed=True))

    e.append(S.source_label(80, 1090, "Grok Bot: docs.x.ai/grok-bot (computer-and-apps, bots, skills-routines, connectors), read 2026-09-15 · launch 2026-08-11"))
    e.append(S.source_label(80, 1114, "Muse: about.fb.com 2026-09-08 · research.meta.ai security post · Meta Help 'Muse memory', read 2026-09-15"))
    e.append(S.watermark(2150, 1114))
    return e


def main() -> None:
    elements = build()
    S.validate(elements)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({len(elements)} elements)")


if __name__ == "__main__":
    main()
