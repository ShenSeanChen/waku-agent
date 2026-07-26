"""Generate the pi system-design whiteboard in Sean's style.

One tight landscape teaching board (companion to docs/whiteboards/pi-chart-prompt.md):

  TOP SPINE   faces -> context -> the loop -> two exits
  BAND        make pi strong: skills / extensions / packages / any CLI
  FOOTERS     refused-on-purpose (red)  ·  waku x pi (blue)

Design rule (why the earlier draft read wrong): every box is a TITLE + 2-3 short
bullets, <=4 words each. Anything longer is something you SAY, not something you
draw. Plugin family = yellow (the engine palette has no purple; legend states it).

Run:  python -m waku.ops.whiteboard.build_pi_system
"""
from __future__ import annotations

import json
import pathlib

from . import style as S
from .style import INK, PAL

OUT = pathlib.Path(__file__).resolve().parents[3] / "docs" / "whiteboards"


def _add(e, x):
    e.extend(x) if isinstance(x, list) else e.append(x)


def build():
    e = []
    a = lambda x: _add(e, x)

    # ---- title -------------------------------------------------------------
    a(S.text(60, 44, "pi — one small loop, made strong", size=S.FS_TITLE))
    a(S.underline(64, 120, 700, color=PAL["orange"][1]))
    a(S.text(64, 140, "MIT  ·  agent-loop.ts = 792 lines  ·  4 tools  ·  37 providers",
             size=S.FS_HEADER, color=PAL["grey"][1]))
    a(S.socials_logos(1720, 40))

    # ============ TOP SPINE ===============================================
    # Boxes are BLACK by default (like the masters). Colour is spent only on the
    # 3 container boundaries (loop / refused / waku), the pink model, the green
    # reply chip, and a few one-line highlight notes. Widths hug the content.

    a(S.card(60, 250, 355, "FACES · one brain, 4 costumes",
             "pi              → TUI\n"
             "pi -p           → run once\n"
             "pi --mode json  → event stream\n"
             "pi --mode rpc   → live pipe"))
    a(S.text(78, 408, "↑ waku plugs in here", size=S.FS_SMALL, color=PAL["green"][1]))

    a(S.labeled_arrow(415, 320, 455, 320, "feeds"))

    a(S.card(455, 250, 300, "CONTEXT = whole state",
             "prompt + session\n"
             "+ AGENTS.md\n"
             "+ system prompt  (< 1k)\n"
             "= plain JSON, forkable"))
    a(S.text(473, 408, "nothing enters behind your back",
             size=S.FS_SMALL, color=PAL["orange"][1]))

    a(S.labeled_arrow(755, 320, 800, 320, "to model"))

    # THE LOOP — the one container that earns an orange boundary ------------
    a(S.boundary(800, 225, 760, 345, "THE LOOP · agent-loop.ts · 792 lines", color="orange"))
    a(S.ellipse(835, 295, 185, 95, "LLM · 37 brains", color="pink"))
    a(S.diamond(1080, 298, 135, 88, "tools?", color="plain"))
    a(S.diamond(1080, 435, 145, 88, "allowed?", color="plain"))
    a(S.labeled_box(1300, 435, 225, 88, "read · write\nedit · bash", color="plain"))
    a(S.text(875, 398, "blocked → returned\nas a tool result", size=S.FS_SMALL, color=PAL["red"][1]))

    a(S.arrow(1020, 343, 1080, 342))
    a(S.labeled_arrow(1147, 386, 1152, 435, "yes"))
    a(S.labeled_arrow(1225, 479, 1300, 479, "yes"))
    a(S.labeled_arrow(1190, 312, 1565, 318, "no → reply", color=PAL["green"][1]))
    a(S.arrow(1412, 523, 1412, 558))
    a(S.labeled_arrow(1412, 558, 855, 558, "results → loop"))
    a(S.arrow(855, 558, 855, 390))

    # TWO EXITS -------------------------------------------------------------
    a(S.chip(1580, 300, 165, 72, "REPLY", color="green"))
    a(S.card(1580, 405, 300, "SESSION = a tree",
             "one JSONL, {id, parentId}\n"
             "/fork · /tree · switch model\n"
             "branches live in place"))
    a(S.arrow(1510, 470, 1580, 450))
    a(S.text(1580, 545, "~/.pi/agent/sessions/…jsonl", size=S.FS_SMALL, color=PAL["grey"][1]))

    # ============ BAND: make pi strong ====================================
    a(S.pill_header(60, 660, 540, "MAKE pi STRONG — cheapest context first", color="plain"))

    band = [
        (60, "SKILLS", "folder + SKILL.md\nmarkdown, no code\nloads on demand"),
        (375, "EXTENSIONS", "one .ts file\nadd a tool / block a call\n10 lines = permissions"),
        (690, "PACKAGES", "bundle of the above\n\"pi\" key in package.json\npi install npm: / git:"),
        (1005, "ANY CLI", "bash already runs it\nREADME, read on demand\nreplaces MCP"),
    ]
    for x, title, body in band:
        a(S.card(x, 740, 285, title, body))

    a(S.labeled_arrow(600, 740, 1152, 523, "powers this", color=PAL["grey"][1], dashed=True))
    a(S.text(60, 882, "← cheapest context first", size=S.FS_SMALL, color=PAL["green"][1]))
    a(S.text(380, 882, "lives in  ~/.pi/agent/ (you)  ·  <repo>/.pi/ (team)  ·  a package (world)",
             size=S.FS_SMALL, color=PAL["grey"][1]))

    # ============ FOOTERS ==================================================
    a(S.boundary(60, 925, 560, 205, "REFUSED — on purpose", color="red"))
    a(S.text(88, 988,
             "MCP          → a CLI + README\n"
             "sub-agents   → spawn more pi's\n"
             "plan / todos → PLAN.md, TODO.md\n"
             "permissions  → a container",
             size=S.FS_BODY, color=INK))

    a(S.boundary(660, 925, 900, 205, "waku × pi", color="blue"))
    a(S.text(688, 985,
             "waku → delegate_task → pi -p --mode json\n"
             "events → arena card\n"
             "tokens → usage ledger",
             size=S.FS_BODY, color=INK))
    a(S.text(688, 1088, "memory + evals = orchestrator  ·  tools = specialist",
             size=S.FS_BODY, color=PAL["green"][1]))

    a(S.source_label(60, 1160, "verified live · pi 0.82.0 · 2026-07-25"))
    a(S.watermark(60, 1198))

    S.validate(e)
    return S.document(e)


def main():
    doc = build()
    path = OUT / "pi-system-design.excalidraw"
    path.write_text(json.dumps(doc, indent=2))
    print(f"wrote {path}  ({len(doc['elements'])} elements)")


if __name__ == "__main__":
    main()
