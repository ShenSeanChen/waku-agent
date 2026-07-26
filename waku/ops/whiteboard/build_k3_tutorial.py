"""Generate the two K3 tutorial whiteboards in Sean's style.

  Act 1  — the concept LADDER: bag-of-words -> Kimi K3 (intro to hero, 10 min)
  Act 2  — the GRADING sheet: test each claim live on Waku (10 min)

Sean's idiom: mostly-WHITE nodes with colored strokes, agents as ellipses,
labeled arrows carry the logic, blue floating side-notes, colored-stroke
subsystem boundaries, solid fill only on user-prompt / reply chips.

Run:  python -m waku.ops.whiteboard.build_k3_tutorial
"""
from __future__ import annotations

import itertools
import json
import pathlib

from . import style as S

OUT = pathlib.Path(__file__).resolve().parents[3] / "docs" / "whiteboards"


def _add(e, x):
    e.extend(x) if isinstance(x, list) else e.append(x)


def act1():
    e = []
    a = lambda x: _add(e, x)

    a(S.text(60, 40, "From Bag-of-Words to Kimi K3", size=S.FS_TITLE))
    a(S.underline(64, 120, 900, color=S.PAL["orange"][1]))
    a(S.text(64, 140, "the one question every era answers better:  predict the next word",
             size=S.FS_HEADER, color=S.PAL["grey"][1]))
    a(S.socials_block(1980, 44))

    # rising chain of WHITE nodes, colored strokes, labeled arrows
    rungs = [
        ("A3  Bag-of-words\ncount words, ignore order\n\"not good\" == \"good not\"", "grey"),
        ("A4  LSTM  (what you built)\nreads left->right, keeps memory\nbut slow + forgets far back", "blue"),
        ("A5  ATTENTION\nlook at ALL words at once,\nweigh what matters", "green"),
        ("A6  Transformer\nattention, stacked deep", "grey"),
    ]
    bw, bh, gap, x0, ybase, step = 300, 130, 60, 80, 820, 130
    prev = None
    for i, (label, color) in enumerate(rungs):
        x, y = x0 + i * (bw + gap), ybase - i * step
        a(S.labeled_box(x, y, bw, bh, label, color=color, size=S.FS_BODY))
        if prev:
            a(S.labeled_arrow(prev[0], prev[1], x, y + bh / 2, "gets better at"))
        prev = (x + bw, y + bh / 2)

    # A5 turning-point annotation (blue side-note)
    a5x = x0 + 2 * (bw + gap)
    a(S.annotate(a5x, ybase - 2 * step - 60,
                 "* THE turning point (2017) --\n  the only atom they must keep"))

    # A7 LLM as an ellipse (agent-ish node), pink
    llx, lly = x0 + 4 * (bw + gap), ybase - 4 * step
    a(S.ellipse(llx, lly, 300, 150, "A7  LLM\nTransformer x huge scale\nx the whole internet",
                color="pink"))
    a(S.labeled_arrow(prev[0], prev[1], llx, lly + 75, "gets better at"))

    # A8 cost wall — red subsystem boundary with white sub-nodes inside
    wx = llx + 300 + gap
    a(S.boundary(wx, 200, 320, 620, "A8  THE COST WALL", color="red"))
    a(S.labeled_box(wx + 30, 300, 260, 110, "too many\nparameters", color="red"))
    a(S.labeled_box(wx + 30, 470, 260, 130, "attention costs\nn squared\n(A5's hidden price)", color="red"))
    a(S.labeled_arrow(llx + 300, lly + 75, wx, 500, "hits"))

    # A9 Kimi K3 — green boundary with three trick chips
    hx = wx + 320 + gap
    a(S.boundary(hx, 180, 430, 640, "A9  KIMI K3", color="green"))
    a(S.annotate(hx + 26, 230, "the hero that beats the wall"))
    a(S.chip(hx + 40, 300, 350, 110, "MoE:  own 2.8T, use ~2%\n(16 of 896 experts)", color="green"))
    a(S.chip(hx + 40, 440, 350, 110, "KDA:  linear attention,\nkills the n squared", color="green"))
    a(S.chip(hx + 40, 580, 350, 90, "FP4:  shrink each number 4x", color="green"))
    a(S.labeled_arrow(wx + 320, 510, hx, 500, "breaks through"))
    a(S.source_label(hx, 830, "specs per MarkTechPost / Moonshot, Jul 16 2026"))

    a(S.red_note(80, 980,
                 "A10  honest asterisk:  K3's 6.3x / 25% / 2.5x are VENDOR claims "
                 "(open weights Jul 27).  Opus & Fable internals are secret -- we can only draw K3."))
    a(S.watermark(80, 1030))
    return e


def act2(standalone=True):
    e = []
    a = lambda x: _add(e, x)

    a(S.text(60, 40, "Testing the Hero -- live, on Waku", size=S.FS_TITLE))
    a(S.underline(64, 120, 760, color=S.PAL["orange"][1]))
    if standalone:
        a(S.socials_block(1640, 44))

    # B1 the rig: prompt chip -> Waku ellipse -> three provider chips
    a(S.text(80, 170, "B1   The rig = Waku", size=S.FS_HEADER, color=S.PAL["blue"][1]))
    a(S.chip(80, 250, 200, 90, "one prompt", color="green"))
    a(S.ellipse(360, 235, 220, 120, "Waku\n(swap the brain)", color="pink"))
    a(S.labeled_arrow(280, 295, 360, 295, "send"))
    provs = [("Kimi K3", "green"), ("Opus 4.8", "blue"), ("Fable 5", "orange")]
    for i, (name, c) in enumerate(provs):
        py = 200 + i * 75
        a(S.chip(680, py, 220, 60, name, color=c))
        a(S.arrow(580, 295, 680, py + 30, color=S.INK))
    a(S.annotate(940, 250, "same prompt,\nevery provider"))
    a(S.text(80, 400, "unfair advantage: nobody else demos on their own multi-provider agent",
             size=S.FS_SMALL, color=S.PAL["grey"][1]))

    # three test nodes (white, colored stroke), each with a blue "grades" note
    cards = [
        ("B2  Pelican", "SVG of a pelican\non a bicycle --\nvisual, instant to judge",
         "grades A7  (raw capability)", "green", "Willison benchmark, Jul 16 2026"),
        ("B3  Vibecode", "one real \"build me X\"\nthrough Waku,\nK3 vs Opus vs Fable",
         "grades their world", "pink", ""),
        ("B4  1M context", "drop a huge file,\nask a needle-deep\nquestion",
         "grades A9  (the KDA claim)", "orange", ""),
    ]
    cw, ch, gap, y = 520, 230, 80, 520
    rig = (470, 355)
    for i, (title, body, grades, color, src) in enumerate(cards):
        x = 80 + i * (cw + gap)
        a(S.text(x + 10, y - 40, title, size=S.FS_HEADER, color=S.PAL[color][1]))
        a(S.labeled_box(x, y, cw, ch, body, color=color, size=S.FS_BOX))
        a(S.annotate(x + 10, y + ch + 12, grades))
        if src:
            a(S.source_label(x + 10, y + ch + 42, src))
        a(S.arrow(rig[0] + i * 20, rig[1], x + cw / 2, y, color=S.INK))

    # B5 verdict — red subsystem boundary
    vy = 880
    a(S.boundary(80, vy, 1900, 190, "B5  The verdict", color="red"))
    a(S.text(120, vy + 80,
             "where each wins / loses -- then loop back to A10:  claims  vs  what you just saw on camera.",
             size=S.FS_HEADER, color=S.INK))
    a(S.text(120, vy + 125, "honesty = the brand.", size=S.FS_HEADER, color=S.PAL["red"][1]))

    a(S.text(80, vy + 210,
             "structure:  Act 1 is a LADDER (each rung enables the next).  "
             "Act 2 is a GRADING SHEET (each test grades one atom).",
             size=S.FS_BODY, color=S.PAL["grey"][1]))
    if standalone:
        a(S.watermark(80, vy + 245))
    return e


def _shift(els, dy):
    """Move every element down by dy (points are relative, so only anchors move)."""
    for el in els:
        el["y"] += dy
    return els


def combined():
    """Both acts on one canvas: Act 1 (ladder) above, Act 2 (grading) below."""
    e = act1()
    dy = 1180
    a1_bottom = 1080
    # dashed divider between the two acts
    e.append(S.hand_line(60, a1_bottom, [[0, 0], [2620, 0]],
                         color=S.PAL["grey"][1], sw=S.SW_DETAIL))
    e[-1]["strokeStyle"] = "dashed"
    e += _shift(act2(standalone=False), dy)
    return e


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    S._ids = itertools.count(1)
    els = combined()
    S.validate(els)
    (OUT / "k3-tutorial.excalidraw").write_text(json.dumps(S.document(els), indent=2))
    print(f"wrote k3-tutorial.excalidraw  ({len(els)} elements)")


if __name__ == "__main__":
    build()
