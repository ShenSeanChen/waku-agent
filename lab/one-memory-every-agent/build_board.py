"""Draw the "one memory, five agents" board in Sean's style.

Grok Bot and Muse sit on top, each inside the cloud computer it runs in, and
reach Waku Memory differently: Grok Bot over MCP with a key header, Muse
through a connector it builds to the web API. Each agent saves (solid arrow,
labelled with how it connects) and recalls (dashed arrow back). Skills travel
separately, as SKILL.md folders.

Run:  python lab/one-memory-every-agent/build_board.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for scripts/

from scripts.whiteboard import style as S  # noqa: E402

OUT = Path(__file__).resolve().parent / "whiteboards" / "one-memory-every-agent.excalidraw"

MEM = (1050, 470, 400, 170)          # x, y, w, h of Waku Memory
AGENT_W, AGENT_H = 300, 130


def _agent(e, x, y, label):
    e += S.ellipse(x, y, AGENT_W, AGENT_H, label, fill="node")


def build() -> list:
    e: list = []
    e.append(S.text(60, 40, "One memory, five agents", size=S.FS_TITLE))
    e.append(S.underline(64, 110, 520))
    e.append(S.text(64, 130, "Grok Bot and Muse keep your memory in their own VM. One memory every agent can reach:",
                    size=S.FS_HEADER, color=S.PAL["grey"][1]))
    e += S.socials_block(2150, 44)

    mx, my, mw, mh = MEM
    e += S.labeled_box(mx, my, mw, mh, "Waku Memory\nhosted, yours to see and export\napi.waku.one/mcp",
                       color="green", fill="chip", size=S.FS_HEADER)

    # Grok Bot: many Bots, one shared computer
    e += S.boundary(700, 180, 440, 200, "one VM, shared by every Bot", color="blue")
    _agent(e, 770, 235, "Grok Bot\n(xAI)")
    e.append(S.arrow(990, 365, 1100, my))
    e.append(S.text(830, 410, "MCP + key header", size=S.FS_SMALL))
    e.append(S.arrow(1160, my, 1050, 365, dashed=True))

    # Muse: one agent, guarded by Sentinel
    e += S.boundary(1320, 180, 460, 200, "Muse VM + Sentinel", color="blue")
    _agent(e, 1400, 235, "Muse\n(Meta, muse.ai)")
    e.append(S.arrow(1470, 365, 1360, my))
    e.append(S.text(1250, 400, "POST /imports", size=S.FS_SMALL))
    e.append(S.arrow(1420, my, 1580, 365, dashed=True))
    e.append(S.text(1530, 425, "GET /memories?q=", size=S.FS_SMALL))

    # the three that sign in through the browser
    sides = [
        (120, 250, "Waku agent\n(your laptop)", "waku connect waku-memory", mx, my + 25, my + 90),
        (120, 700, "Claude Code", "npx waku-memory setup", mx, my + 105, my + 160),
        (2080, 700, "Codex", "capture enable", mx + mw, my + 105, my + 160),
    ]
    for ax, ay, label, how, edge_x, save_y, recall_y in sides:
        _agent(e, ax, ay, label)
        near_x = ax + AGENT_W if ax < mx else ax
        e += S.labeled_arrow(near_x, ay + 35, edge_x, save_y, how)
        e.append(S.arrow(edge_x, recall_y, near_x, ay + 100, dashed=True))

    e.append(S.annotate(150, 460, "solid = save\ndashed = recall"))

    # skills travel as files to the agents that read SKILL.md
    sx, sy, sw, sh = 1050, 900, 400, 120
    e += S.labeled_box(sx, sy, sw, sh, "Waku skills\nSKILL.md folders")
    e += S.labeled_arrow(sx, sy + 40, 420, 800, "waku skill export --to claude")
    e += S.labeled_arrow(sx + sw, sy + 40, 2080, 800, "--to codex")

    e.append(S.red_note(120, 930, "neither Grok Bot nor Muse exports your memory:\nit stays in the vendor's VM"))
    e.append(S.source_label(120, 1030, "Grok Bot: one VM for all Bots, custom MCP needs a public URL (docs.x.ai, 2026-09); MCP sign-in never runs (imogen-server #27, 2026-09-07)"))
    e.append(S.source_label(120, 1054, "Muse: MEMORY.md in its VM, no MCP, Sentinel approves actions (Meta, 2026-09-08)"))
    e.append(S.watermark(120, 1090))
    return e


def main() -> None:
    elements = build()
    S.validate(elements)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({len(elements)} elements)")


if __name__ == "__main__":
    main()
