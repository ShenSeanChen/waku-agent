"""Whiteboard: loop vs graph engineering.

The companion picture to docs/loop-vs-graph.md.

COMPOSITION — two mirrored FLOWS on top, their measured timelines underneath.

The first version of this board led with the timelines, because timelines are
what actually prove concurrency. That produced an infographic: 27 boxes, one
arrow, nothing moving. Measured against the masters in ~/Developer/Excalidraw
it was 0.07 arrows per shape against their 0.88 — a table pretending to be a
whiteboard.

So the flow is the hero and the timeline is the receipt beneath it. The two
flows are drawn to be read against each other:

  * the LOOP is a cycle. Its signature is the arrow that goes BACK — the model
    decides, calls a tool, looks again, decides again. You cannot draw where it
    ends because it does not know either.
  * the GRAPH is a fan. Its signature is one arrow becoming four and four
    becoming one. Every edge is on the page before anything runs.

COLOUR — the masters are 96% black strokes (354 of 368 shapes) with transparent
or white fills; colour lands maybe fifteen times on a whole board. The first
version of this one was 3% black. Ink by default; a fill has to earn its place.

    python -m waku.ops.whiteboard.build_loop_vs_graph
"""

from __future__ import annotations

import json
from pathlib import Path

from waku.ops.whiteboard import style as S

OUT = Path(__file__).resolve().parents[3] / "docs" / "whiteboards" / "loop-vs-graph.excalidraw"

# One pixel per 12ms, so the real measurements set the bar lengths. The timeline
# is evidence; scaling it by hand would make it a drawing of an argument.
MS = 1 / 12.0
BAR_H = 30


def _box(x, y, w, h, label, *, fill=None, size=S.FS_BOX):
    """Black-stroked container; `fill` tints the background only."""
    out = S.labeled_box(x, y, w, h, label, color="plain", size=size)
    if fill:
        out[0]["backgroundColor"] = S.PAL[fill][0]
    return out


def _pill(x, y, w, title, *, stroke=None):
    out = S.pill_header(x, y, w, title, color="plain")
    if stroke:
        out[0]["strokeColor"] = S.PAL[stroke][1]
    return out


def _bar(x, y, ms, label, *, fill=None, show_ms=True):
    """A timeline bar whose LENGTH is the measured duration.

    Name above, duration to the right. Both were inside once and collided the
    moment a bar was short — a 1ms scan is 6px wide, and "memory" written into
    6px rendered as "memory1ms". A bar this chart exists to show as tiny must
    stay tiny.
    """
    w = max(int(ms * MS), 7)
    out = [S.text(x, y - 21, label, size=S.FS_BOX)]
    out += _box(x, y, w, BAR_H, "", fill=fill)
    out[1]["strokeWidth"] = S.SW_DETAIL
    if show_ms:
        out.append(S.text(x + w + 10, y + 5, f"{ms}ms", size=S.FS_BOX,
                          color=S.PAL["grey"][1]))
    return out


def _curve(pts, *, color=S.INK, dashed=False, sw=S.SW_BOX):
    """A multi-point arrow — the loop's back-edge needs to go around, not through."""
    x, y = pts[0]
    a = S.arrow(x, y, pts[-1][0], pts[-1][1], color=color, dashed=dashed, sw=sw)
    a["points"] = [[float(px - x), float(py - y)] for px, py in pts]
    a["width"] = max(p[0] for p in pts) - min(p[0] for p in pts)
    a["height"] = max(p[1] for p in pts) - min(p[1] for p in pts)
    return a


def build() -> list:
    e: list = []

    e.append(S.text(70, 44, "Loop vs Graph Engineering", size=S.FS_TITLE))
    e.append(S.underline(74, 90, 560, color=S.PAL["orange"][1]))
    e.append(S.text(70, 110,
                    "A loop DISCOVERS what to do next.   A graph PRE-DETERMINES what happens next.",
                    size=S.FS_HEADER))

    # ======================= LEFT: THE LOOP, as a cycle =====================
    e += _pill(70, 178, 1010, "THE LOOP  —  it comes back")

    e += S.chip(70, 350, 130, 56, "you", color="green")
    e += S.ellipse(270, 336, 210, 84, "the model", color="plain", fill="node")
    e += S.diamond(560, 326, 200, 104, "tool\nneeded?", color="plain")
    e += _box(850, 346, 200, 64, "run a tool")
    e += S.chip(560, 560, 200, 56, "reply", color="green")

    e.append(S.arrow(200, 378, 268, 378))
    e.append(S.arrow(480, 378, 558, 378))
    e += S.labeled_arrow(760, 378, 848, 378, "yes")
    e += S.labeled_arrow(660, 430, 660, 558, "no")
    # THE back-edge: the loop's whole identity in one arrow. Routed ABOVE the
    # row — sending it underneath put it straight through the "no" branch, and
    # the one arrow that has to be unmissable cannot be the one crossing others.
    e.append(_curve([(950, 344), (950, 272), (375, 272), (375, 334)], sw=S.SW_EMPHASIS))
    e.append(S.text(520, 240, "and look again... and again", size=S.FS_BODY))
    e.append(S.text(70, 624,
                    "you cannot draw where this ends, because it does not know either.",
                    size=S.FS_BODY, color=S.PAL["red"][1]))

    # ======================= RIGHT: THE GRAPH, as a fan =====================
    e += _pill(1190, 178, 1140, "THE GRAPH  —  it fans out")

    e += S.chip(1190, 380, 120, 56, "START", color="green")
    scans = ["github", "web", "calendar", "memory"]
    for i, name in enumerate(scans):
        y = 262 + i * 78
        e += _box(1420, y, 200, 58, name, fill="green")
        e.append(_curve([(1312, 408), (1370, 408), (1370, y + 29), (1418, y + 29)]))
        e.append(_curve([(1622, y + 29), (1670, y + 29), (1670, 408), (1718, 408)]))
    e += S.ellipse(1720, 366, 220, 84, "synthesize", color="plain", fill="node")
    e += S.diamond(2010, 356, 190, 104, "anything\nto do?", color="plain")
    e.append(S.arrow(1942, 408, 2008, 408))
    e += S.labeled_arrow(2105, 460, 2105, 546, "yes")
    e += S.chip(2010, 548, 190, 56, "draft it", color="green")
    e += S.labeled_arrow(2200, 408, 2262, 408, "no")
    e += _box(2264, 380, 66, 56, "END")
    e.append(S.text(1190, 624,
                    "every edge is on the page before anything runs.",
                    size=S.FS_BODY, color=S.PAL["green"][1]))

    # ======================= THE RECEIPTS: two timelines ====================
    e += _pill(70, 660, 2260, "THE SAME JOB, MEASURED ON ONE MACHINE")

    X0 = 110
    e.append(S.text(X0, 730, "THE LOOP", size=S.FS_HEADER, color=S.PAL["orange"][1]))
    t = 0
    for ms, label in [(5701, "decide"), (377, "gh"), (662, "gh"),
                      (1200, "calendar"), (7357, "synthesize")]:
        e += _bar(X0 + int(t * MS), 790, ms, label, show_ms=(ms > 3000))
        t += ms
    e.append(S.text(X0, 844,
                    "3 sources.  it never searched the web or memory — it decided it had enough.",
                    size=S.FS_BODY, color=S.PAL["red"][1]))

    e.append(S.text(X0, 916, "THE GRAPH", size=S.FS_HEADER, color=S.PAL["green"][1]))
    for i, (ms, label) in enumerate([(1506, "github"), (1887, "web"),
                                     (2, "calendar"), (1, "memory")]):
        e += _bar(X0, 976 + i * 54, ms, label, fill="green")
    barrier_x = X0 + int(1890 * MS)
    e += _bar(barrier_x, 1192, 11279, "synthesize")
    e.append(S.text(X0, 1254,
                    "4 sources, every time.  the shared left edge is the whole point.",
                    size=S.FS_BODY, color=S.PAL["green"][1]))
    e.append(S.arrow(barrier_x, 962, barrier_x, 1188,
                     color=S.PAL["red"][1], dashed=True, sw=S.SW_DETAIL))
    e.append(S.text(barrier_x + 14, 1090, "the barrier — a wave costs its SLOWEST branch,",
                    size=S.FS_BODY, color=S.PAL["red"][1]))
    e.append(S.text(barrier_x + 14, 1114, "so calendar and memory idle here for 1.9s",
                    size=S.FS_BODY, color=S.PAL["red"][1]))

    # scoreboard, parked right of the timelines
    e += _box(1500, 780, 380, 86, "gathering\n6.7s  ->  1.9s   (3.5x)", fill="green")
    e += _box(1920, 780, 380, 86, "whole run\n15.3s  ->  13.2s   (14%)")
    e.append(S.red_note(1500, 900,
                        "a graph only speeds up what can be parallel. an 11s model\n"
                        "call dominates both runs — if your workflow is one big call\n"
                        "wearing a hat, a graph buys almost nothing.\n\n"
                        "THE REAL WIN IS THAT A GRAPH CAN'T FORGET."))
    e += _box(1500, 1080, 800, 74,
              "a graph does NOT replace the loop — agent_node runs one whole loop turn AS a node")

    # ======================= the ladder + the skeptic =======================
    e += _pill(70, 1330, 1010, "THE LADDER  —  each rung is something you stopped leaving to chance")
    rungs = [("prompt", "the words"), ("context", "what's in the window"),
             ("skill", "the procedure"), ("loop", "between the calls"),
             ("graph", "the shape of the calls")]
    for i, (name, what) in enumerate(rungs):
        x = 70 + i * 205
        e += _box(x, 1400, 190, 76, f"{name}\n{what}", fill="green" if i == 4 else None)
        if i:
            e.append(S.arrow(x - 14, 1438, x - 2, 1438))
    e.append(S.text(70, 1500,
                    "a SKILL.md is a procedure the model MAY follow.  a graph is the same procedure "
                    "it CANNOT NOT follow.", size=S.FS_BODY))
    e.append(S.text(70, 1526,
                    "so: write the skill first. harden it into a graph only once it stops changing.",
                    size=S.FS_BODY))

    e += _pill(1190, 1330, 1140, "\"ISN'T THIS JUST DETERMINISTIC WORKFLOWS FROM 2023?\"", stroke="red")
    e.append(S.text(1190, 1396, "Mostly yes. Airflow has run conditional parallel DAGs for a decade.",
                    size=S.FS_BODY))
    e.append(S.text(1190, 1424, "What is new is narrow:", size=S.FS_BODY))
    for i, s in enumerate([
        "1.  the nodes are non-deterministic — a node is a model call",
        "2.  a model can pick the edge — routing is data-dependent",
        "3.  so you need guards a DAG scheduler never did",
    ]):
        e.append(S.text(1210, 1456 + i * 30, s, size=S.FS_BODY))
    e.append(S.text(1190, 1552, "Old shape. New contents.", size=S.FS_HEADER,
                    color=S.PAL["red"][1]))

    e.append(S.source_label(70, 1600, "measured on waku-agent, 2026-07-31 — docs/loop-vs-graph.md"))
    e += S.socials_block(1980, 44)
    e.append(S.watermark(70, 1640))
    return e


def main() -> None:
    elements = build()
    S.validate(elements)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({len(elements)} elements)")


if __name__ == "__main__":
    main()
