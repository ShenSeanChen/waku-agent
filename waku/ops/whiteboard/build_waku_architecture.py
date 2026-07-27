"""Generate the current Waku architecture whiteboard.

Run:
    python -m waku.ops.whiteboard.build_waku_architecture

The editable Excalidraw document is the source of truth.  A small SVG proxy is
also written to /tmp so the board can be rendered and visually checked without
depending on Excalidraw's browser bundle.
"""

from __future__ import annotations

import html
import itertools
import json
from pathlib import Path

from . import style as S

ROOT = Path(__file__).resolve().parents[3]
BOARD = ROOT / "docs" / "whiteboards" / "waku-architecture.excalidraw"
SVG_PROXY = Path("/tmp/waku-architecture.svg")


def _add(elements: list[dict], item) -> None:
    elements.extend(item) if isinstance(item, list) else elements.append(item)


def build_elements() -> list[dict]:
    e: list[dict] = []
    a = lambda item: _add(e, item)

    # Title and identity.
    a(S.text(60, 36, "Waku — one turn through the whole system", size=S.FS_TITLE))
    a(S.underline(64, 112, 920, color=S.PAL["orange"][1]))
    a(S.text(64, 132, "Harness  ·  Loop  ·  Memory  ·  Eval / LLM Ops",
             size=S.FS_HEADER, color=S.PAL["grey"][1]))
    a(S.socials_block(2050, 40))

    # ------------------------------------------------------------------ Harness
    a(S.boundary(40, 190, 2440, 470, "HARNESS — waku/app.py", color="red"))

    a(S.pill_header(80, 250, 330, "1  GATEWAYS", color="grey"))
    a(S.labeled_box(80, 320, 330, 210,
                    "text in / text out\n\nCLI · Dashboard · Voice\nTelegram · Discord\n\nwaku/gateway/ + ops/dashboard.py",
                    color="grey", size=S.FS_BODY))

    a(S.labeled_box(490, 355, 230, 130,
                    "Waku.respond()\n\nassemble · run\npersist · trace",
                    color="red", size=S.FS_BODY))
    a(S.labeled_arrow(410, 420, 490, 420, "user text"))

    a(S.pill_header(800, 250, 350, "2  WORKING MEMORY", color="green"))
    a(S.labeled_box(800, 320, 350, 210,
                    "runtime/session.py\n\nSOUL.md\nmatched SKILL.md\ngated long-term memory\nlast N chat turns",
                    color="green", size=S.FS_BODY))
    a(S.labeled_arrow(720, 420, 800, 420, "build_system"))

    a(S.boundary(1230, 235, 850, 370, "3  THE LOOP — loop/agent.py", color="orange"))
    a(S.ellipse(1290, 350, 260, 130,
                "LLM\nmodels.py adapter\n2 wire formats", color="pink"))
    a(S.labeled_box(1690, 295, 330, 230,
                    "ToolRegistry\n\ncore: calendar · notes ·\nmessages · search\nself: memory · SOUL · skill\nedges: Apple · MCP\nexperimental: delegate + stubs",
                    color="orange", size=S.FS_BODY))
    a(S.labeled_arrow(1550, 365, 1690, 365, "tool calls", color=S.PAL["orange"][1]))
    a(S.labeled_arrow(1690, 475, 1550, 475, "results", color=S.PAL["orange"][1]))
    a(S.annotate(1320, 535, "stop: no tool call  OR  max_iterations"))
    a(S.labeled_arrow(1150, 420, 1290, 420, "system + history"))

    a(S.chip(2180, 370, 220, 100, "FINAL REPLY", color="green"))
    a(S.labeled_arrow(2080, 420, 2180, 420, "natural exit"))
    a(S.annotate(2145, 500, "returns through the\noriginating gateway"))

    # ------------------------------------------------------------------- Memory
    a(S.boundary(40, 710, 1490, 580, "MEMORY — waku/memory/", color="green"))

    a(S.labeled_box(80, 800, 330, 150,
                    "PROCEDURAL\nSKILL.md files\nkeyword matched — how to act",
                    color="green", size=S.FS_BODY))
    a(S.labeled_arrow(245, 800, 930, 530, "matched into prompt",
                      color=S.PAL["green"][1]))

    a(S.diamond(470, 790, 280, 170,
                "RETRIEVAL GATE\nneed memory?", color="green"))
    a(S.labeled_arrow(930, 530, 610, 790, "message first",
                      color=S.PAL["green"][1]))

    a(S.labeled_box(830, 755, 280, 130,
                    "SEMANTIC\nfacts · user profile\nSQLite FTS5 / Supabase",
                    color="green", size=S.FS_BODY))
    a(S.labeled_box(830, 925, 280, 130,
                    "EPISODIC\ndated events · past chats\nSQLite / Notion",
                    color="green", size=S.FS_BODY))
    a(S.labeled_arrow(750, 845, 830, 820, "retrieve"))
    a(S.labeled_arrow(750, 900, 830, 990, "retrieve"))

    a(S.labeled_box(1170, 755, 300, 180,
                    "state.db — source of truth\n\nchat_log · facts · episodes\ncalendar · notes\n\n.waku/MEMORY.md = mirror",
                    color="grey", size=S.FS_BODY))
    a(S.arrow(2290, 470, 1320, 755, color=S.PAL["grey"][1]))
    a(S.annotate(1180, 710, "reply → chat_log"))

    a(S.diamond(500, 1060, 280, 160,
                "CONSOLIDATE\nafter N exchanges", color="green"))
    a(S.labeled_arrow(1170, 900, 780, 1140, "unconsolidated chat"))
    a(S.labeled_arrow(780, 1110, 830, 870, "facts"))
    a(S.labeled_arrow(780, 1170, 830, 1010, "episode"))
    a(S.red_note(80, 1225,
                 "Failure invariant: gate fails open; consolidation failure never marks chat as consolidated."))

    # -------------------------------------------------------------- Eval / Ops
    a(S.boundary(1590, 710, 890, 580, "EVAL / LLM OPS — waku/ops/ + evals/", color="blue"))

    a(S.labeled_box(1640, 800, 330, 150,
                    "ONE EVENT STREAM\nobserver callbacks\nturn · gate · LLM · tool · reply",
                    color="blue", size=S.FS_BODY))
    a(S.labeled_arrow(1480, 605, 1750, 800, "every runtime event",
                      color=S.PAL["blue"][1]))

    a(S.labeled_box(2050, 775, 360, 180,
                    "OBSERVE\nDashboard — live UI\nTracer — JSONL always\nOTel — optional export\ncost · latency · errors",
                    color="blue", size=S.FS_BODY))
    a(S.labeled_arrow(1970, 875, 2050, 875, "fan out",
                      color=S.PAL["blue"][1]))

    a(S.labeled_box(1640, 1000, 270, 105,
                    "DETERMINISTIC\n0 / 1 behavior\nevals/deterministic/",
                    color="blue", size=S.FS_BODY))
    a(S.labeled_box(1640, 1140, 270, 105,
                    "LLM-AS-JUDGE\nscored quality\nevals/judge/",
                    color="blue", size=S.FS_BODY))
    a(S.diamond(2020, 1040, 190, 150, "RELEASE\nGATE", color="blue"))
    a(S.labeled_arrow(1910, 1050, 2020, 1080, "must pass",
                      color=S.PAL["blue"][1]))
    a(S.labeled_arrow(1910, 1190, 2020, 1150, "threshold",
                      color=S.PAL["blue"][1]))
    a(S.labeled_box(2280, 1055, 140, 120, "RELEASE\nnew prompt /\nmodel / tool",
                    color="blue", size=S.FS_BODY))
    a(S.labeled_arrow(2210, 1115, 2280, 1115, "pass",
                      color=S.PAL["blue"][1]))
    a(S.annotate(1980, 1220,
                 "offline feedback changes prompt / model / config / tools — then re-run the gate"))

    a(S.source_label(60, 1330,
                     "source: waku/app.py, runtime/session.py, loop/agent.py, memory/*, tools/*, ops/* — 2026-07-27"))
    a(S.watermark(2200, 1330))
    return e


def _svg_text(el: dict) -> str:
    size = el.get("fontSize", 16)
    x = el["x"]
    y = el["y"] + size
    anchor = "middle" if el.get("textAlign") == "center" else "start"
    if anchor == "middle":
        x += el.get("width", 0) / 2
    lines = el.get("text", "").split("\n")
    spans = []
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else size * el.get("lineHeight", 1.25)
        spans.append(
            f'<tspan x="{x:.1f}" dy="{dy:.1f}">{html.escape(line)}</tspan>'
        )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
        f'font-family="Excalifont, Comic Sans MS, cursive" font-size="{size}" '
        f'fill="{el.get("strokeColor", S.INK)}">{"".join(spans)}</text>'
    )


def write_svg_proxy(elements: list[dict], path: Path) -> None:
    """Render a layout-faithful SVG used only for visual QA and PNG export."""
    parts = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" width="2520" height="1390" '
            'viewBox="0 0 2520 1390">'
        ),
        '<rect width="2520" height="1390" fill="white"/>',
        (
            '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" '
            'orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="context-stroke"/></marker></defs>'
        ),
    ]
    for el in elements:
        kind = el["type"]
        stroke = el.get("strokeColor", S.INK)
        fill = el.get("backgroundColor", "transparent")
        fill = "none" if fill == "transparent" else fill
        width = el.get("strokeWidth", 1)
        dash = ' stroke-dasharray="10 8"' if el.get("strokeStyle") == "dashed" else ""
        if kind == "rectangle":
            rx = 18 if el.get("roundness") else 0
            parts.append(
                f'<rect x="{el["x"]}" y="{el["y"]}" width="{el["width"]}" '
                f'height="{el["height"]}" rx="{rx}" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="{width}"{dash}/>'
            )
        elif kind == "ellipse":
            parts.append(
                f'<ellipse cx="{el["x"] + el["width"] / 2}" '
                f'cy="{el["y"] + el["height"] / 2}" rx="{el["width"] / 2}" '
                f'ry="{el["height"] / 2}" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="{width}"{dash}/>'
            )
        elif kind == "diamond":
            x, y, w, h = el["x"], el["y"], el["width"], el["height"]
            points = f"{x + w / 2},{y} {x + w},{y + h / 2} {x + w / 2},{y + h} {x},{y + h / 2}"
            parts.append(
                f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="{width}"{dash}/>'
            )
        elif kind in {"arrow", "line"}:
            pts = " ".join(f"{el['x'] + px},{el['y'] + py}" for px, py in el["points"])
            marker = ' marker-end="url(#arrow)"' if kind == "arrow" else ""
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
                f'stroke-width="{width}"{dash}{marker}/>'
            )
        elif kind == "text":
            parts.append(_svg_text(el))
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def build() -> None:
    BOARD.parent.mkdir(parents=True, exist_ok=True)
    S._ids = itertools.count(1)
    S._FILES.clear()
    elements = build_elements()
    S.validate(elements)
    BOARD.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    write_svg_proxy(elements, SVG_PROXY)
    print(f"wrote {BOARD.relative_to(ROOT)} ({len(elements)} elements)")
    print(f"wrote {SVG_PROXY} (visual QA proxy)")


if __name__ == "__main__":
    build()
