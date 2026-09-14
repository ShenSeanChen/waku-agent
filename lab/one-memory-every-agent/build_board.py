"""Draw the "one memory, five agents" board in Sean's style.

Waku Memory sits in the middle. Each agent saves to it (solid arrow, labelled
with how it connects) and recalls from it (dashed arrow back). Skills travel
separately, as SKILL.md folders, with a plugin folder for Grok Bot.

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
    e.append(S.text(64, 130, "Waku Memory is one hosted MCP server: save in any agent, recall in every other",
                    size=S.FS_HEADER, color=S.PAL["grey"][1]))
    e += S.socials_block(2150, 44)

    mx, my, mw, mh = MEM
    e += S.labeled_box(mx, my, mw, mh, "Waku Memory\nhosted MCP server\napi.waku.one/mcp",
                       color="green", fill="chip", size=S.FS_HEADER)

    # Grok Bot, inside the cloud it runs in
    e += S.boundary(1030, 150, 440, 230, "runs in xAI's cloud", color="blue")
    _agent(e, 1100, 215, "Grok Bot\n(Grok 4.6)")
    e += S.labeled_arrow(1220, 345, 1220, my, "connector + API key")
    e.append(S.arrow(1330, my, 1330, 345, dashed=True))

    # left: the Waku agent and Claude Code; right: Codex and Muse Code
    sides = [
        (120, 250, "Waku agent\n(your laptop)", "waku connect waku-memory", mx, my + 25, my + 90),
        (120, 700, "Claude Code", "npx waku-memory setup", mx, my + 105, my + 160),
        (2080, 250, "Codex", "capture enable", mx + mw, my + 25, my + 90),
        (2080, 700, "Muse Code\n(Muse Spark)", "MCP in settings.json", mx + mw, my + 105, my + 160),
    ]
    for ax, ay, label, how, edge_x, save_y, recall_y in sides:
        _agent(e, ax, ay, label)
        near_x = ax + AGENT_W if ax < mx else ax
        e += S.labeled_arrow(near_x, ay + 35, edge_x, save_y, how)
        e.append(S.arrow(edge_x, recall_y, near_x, ay + 100, dashed=True))

    e.append(S.annotate(150, 460, "solid = save\ndashed = recall"))

    # skills travel as files, not through memory (yet)
    sx, sy, sw, sh = 1050, 900, 400, 120
    e += S.labeled_box(sx, sy, sw, sh, "Waku skills\nSKILL.md folders")
    e += S.labeled_arrow(sx, sy + 40, 420, 800, "waku skill export")
    e += S.labeled_arrow(sx + sw, sy + 40, 2080, 800, "--project (.claude/skills)")
    e += S.labeled_box(1560, 190, 330, 110, "Grok plugin folder\nskills/ + .mcp.json")
    e.append(S.arrow(sx + sw - 40, sy, 1740, 300, dashed=True))
    e += S.labeled_arrow(1560, 245, 1400, 265, "marketplace")

    e.append(S.red_note(120, 930, "local memory is not Waku Memory:\nnothing syncs up on its own"))
    e.append(S.source_label(120, 1030, "Grok Bot connectors take a URL + header, no OAuth sign-in (posterly guide, 2026-09)"))
    e.append(S.source_label(120, 1054, "Muse Code reads .claude/skills (codersera guide, 2026-09); Waku Memory MCP: waku.one/docs, 2026-09-14"))
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
