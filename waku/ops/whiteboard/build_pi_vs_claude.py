"""Build docs/whiteboards/pi-vs-claude-code.excalidraw — the comparison board.

One poster-tight landscape frame: two harness boundaries side by side, eight
aligned rows each, then the waku punchline. Facts are sourced on the board
itself (standing rule); the architecture deep-dive lives in
pi-architecture.excalidraw — this board only argues the philosophy split.

Run:  python -m waku.ops.whiteboard.build_pi_vs_claude
"""

from __future__ import annotations

import json
from pathlib import Path

from waku.ops.whiteboard import style as S

OUT = Path(__file__).resolve().parents[3] / "docs" / "whiteboards" / "pi-vs-claude-code.excalidraw"

# One row per harness decision; (category, claude code cell, pi cell, pi color).
ROWS = [
    ("TOOLS",
     "15+ built-ins — Read / Write / Edit / Bash /\nGrep / Glob / Task / WebFetch / Todo ...",
     "four — read / write / edit / bash.\neverything else is an extension, not a built-in",
     "plain"),
    ("CONTEXT",
     "big system prompt + injected reminders,\nhooks, tool schemas — the vendor curates it",
     "system prompt under 1k tokens.\n\"nothing enters context you didn't put there\"",
     "green"),
    ("SUB-AGENTS",
     "built-in Task tool spawns workers\n(black box within a black box)",
     "refused — spawn pi yourself: tmux,\nor waku's delegate_task (that's us)",
     "plain"),
    ("MCP",
     "first-class: servers, connectors,\nschemas loaded into context",
     "refused — a CLI + README, read only when\nneeded (Playwright MCP alone = 13.7k tokens)",
     "plain"),
    ("PLAN / TODOS",
     "plan mode + TodoWrite,\nstate lives inside the harness",
     "PLAN.md / TODO.md — files you can see\n(\"built-in to-dos confuse models\")",
     "plain"),
    ("PERMISSIONS",
     "popups, permission modes,\nsandboxed tool calls",
     "none — runs with YOUR user's powers.\nwant safety? run it in a container",
     "plain"),
    ("SESSIONS",
     "linear history + compaction\nwhen context fills up",
     "a TREE in one JSONL {id, parentId} —\n/fork /tree /clone time-travel",
     "green"),
    ("PROVIDERS + LICENSE",
     "Anthropic models,\nclosed source",
     "37 providers via pi-ai (Kimi, DeepSeek,\nGLM, ...) - MIT - ~72k stars",
     "plain"),
]

ROW_H, ROW_GAP = 78, 8
TOP = 250


def build() -> list:
    e = []
    e.append(S.text(60, 40, "pi vs Claude Code — two bets on the harness", size=S.FS_TITLE))
    e.append(S.underline(64, 84, 980, color=S.PAL["orange"][1]))
    e.append(S.text(60, 108,
                    "same job: turn a model into a coding agent. opposite answers to one question —\n"
                    "WHO owns the context window: the vendor, or you?",
                    size=S.FS_BODY))

    rows_h = len(ROWS) * (ROW_H + ROW_GAP) + 70
    e += S.boundary(80, 190, 1150, rows_h, "Claude Code — batteries included")
    e += S.boundary(1340, 190, 1150, rows_h, "pi — you are the batteries")

    y = TOP
    for cat, cc, ppi, pcol in ROWS:
        e += S.labeled_box(110, y, 1090, ROW_H, f"{cat}:  {cc}", color="plain")
        e += S.labeled_box(1370, y, 1090, ROW_H, f"{cat}:  {ppi}", color=pcol)
        y += ROW_H + ROW_GAP

    band = 190 + rows_h + 40
    e += S.labeled_box(80, band, 2410, 96,
                       "waku uses BOTH: Claude Code writes waku's code - pi is waku's delegate_task sub-agent,\n"
                       "on whatever model the loop is running. pi refused to build sub-agents — which is exactly "
                       "what makes it embeddable as one.",
                       color="green")

    e.append(S.red_note(80, band + 128,
                        "honest ink: \"minimal\" doesn't delete complexity — pi relocates it to 2,100+ community "
                        "packages. trust moves to third parties, it doesn't vanish."))
    e.append(S.source_label(80, band + 176,
                            "per mariozechner.at 'what i learned building a minimal coding agent' (2025-11-30) - "
                            "earendil-works/pi docs + source (read 2026-07-24) - implicator.ai (2026-04)"))
    e += S.socials_block(2160, 40)
    e.append(S.watermark(80, band + 216))
    return e


def main() -> None:
    elements = build()
    S.validate(elements)
    OUT.write_text(json.dumps(S.document(elements), indent=2), encoding="utf-8")
    print(f"wrote {OUT} ({len(elements)} elements)")


if __name__ == "__main__":
    main()
