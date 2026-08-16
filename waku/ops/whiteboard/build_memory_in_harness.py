"""26.08.15 Where The Memory Layers Plug In — the companion to the anatomy board.

The anatomy board argues the shape in the abstract. This one drops that shape
onto the harness chart the audience already knows, and answers the question the
abstract board cannot: WHERE would mem0 or Zep actually go?

The answer is the point of the whole video. They go in ONE box. Swap
WAKU_SEMANTIC_STORE and the semantic store changes; the loop, the gate, the
skills, the episodic log, the consolidator and the harness are all untouched.
Three vendors compete over one box of a seven-box diagram.

    .venv/bin/python -m waku.ops.whiteboard.build_memory_in_harness
"""

from __future__ import annotations

import json
import pathlib

from waku.ops.whiteboard import style as S

OUT = pathlib.Path("docs/whiteboards/memory-in-harness.excalidraw")
SVG = pathlib.Path("/tmp/memory-in-harness.svg")


def build() -> list:
    e: list = []

    e.append(S.text(60, 40, "26.08.15  Where The Memory Layers Plug In", size=S.FS_TITLE))
    e.append(S.underline(64, 96, 800, color=S.PAL["red"][1]))
    e.append(S.text(60, 116,
                    "the same harness you already know — with the STORE and the MANAGER named,\n"
                    "and the one box the whole memory-layer market is competing over.",
                    size=S.FS_BODY))
    e += S.socials_block(2180, 44)

    # ---- the harness, compressed: this board is about what is UNDER it -----
    e += S.boundary(60, 200, 1560, 300, "HARNESS  —  waku/app.py", color="red")
    e += S.chip(110, 285, 210, 70, "a user turn")
    e += S.labeled_box(390, 275, 300, 90, "working memory")
    e += S.boundary(760, 245, 500, 200, "THE LOOP", color="orange")
    e += S.ellipse(820, 300, 260, 100, "LLM  ·  tools", color="pink")
    e += S.chip(1350, 285, 190, 70, "reply")

    e.append(S.arrow(325, 320, 385, 320))
    e.append(S.arrow(695, 320, 815, 325))
    e.append(S.arrow(1085, 345, 1345, 325))

    # the retrieval gate — the reason waku can get away with no embeddings
    e += S.diamond(400, 560, 280, 130, "retrieval\ngate?", color="green")
    e += S.labeled_arrow(540, 555, 540, 370, "facts, if it decided to look")
    e.append(S.text(700, 570, "the gate REWRITES the question into keywords first —\n"
                              "which is why 发布会 can find \"product launch\" in an\n"
                              "index that has no embeddings at all.", size=S.FS_SMALL))

    # ---- THE STORE ---------------------------------------------------------
    e += S.boundary(60, 730, 1560, 330, "THE STORE   ·   Q1 shape   ·   Q2 retrieval",
                    color="plain")

    e += S.labeled_box(110, 810, 400, 120, "Procedural\nSKILL.md · how to act")
    e += S.labeled_box(590, 810, 400, 120, "Semantic\ndurable facts")
    e += S.labeled_box(1070, 810, 400, 120, "Episodic\ndated events")

    e.append(S.text(115, 950, "FILE — loaded by trigger,\nnot by similarity", size=S.FS_SMALL))
    e.append(S.text(595, 950, "ROWS + FTS5 — keyword,\nZERO embeddings", size=S.FS_SMALL))
    e.append(S.text(1075, 950, "ROWS — SQL over\nstate.db chat_log", size=S.FS_SMALL))

    e.append(S.arrow(300, 800, 480, 700))
    e.append(S.arrow(720, 800, 580, 700))
    e.append(S.arrow(1200, 800, 640, 700))

    # ---- THE MANAGER -------------------------------------------------------
    e += S.boundary(60, 1100, 1560, 260,
                    "THE MANAGER   ·   Q3 what happens to what is no longer true",
                    color="plain")

    e += S.diamond(120, 1160, 300, 160, "consolidate\nafter N?", color="green")
    e += S.ellipse(520, 1180, 300, 120, "auxiliary model\n(cheap)", color="pink")
    e += S.labeled_arrow(430, 1240, 510, 1240, "the tail of the log")
    e += S.labeled_arrow(670, 1175, 670, 940, "distil into facts")

    e += S.card(900, 1150, 690, "waku's four jobs, honestly",
                "DECIDE     adds only — never updates, never deletes\n"
                "RETIRE     nothing automatic\n"
                "ATTRIBUTE  has a source field, no link back to the turn\n"
                "REFLECT    per-turn, on the critical path")

    # ---- THE ONE BOX ------------------------------------------------------
    e += S.pill_header(1680, 200, 940, "THE BOX EVERYONE IS COMPETING OVER", color="plain")

    e += S.card(1680, 275, 940, "WAKU_SEMANTIC_STORE = ...",
                "sqlite      rows + FTS5 keyword          (default)\n"
                "mem0        rows + vector, LLM decides add/update/delete\n"
                "zep         temporal graph, edges carry validity\n"
                "langmem     whatever store YOU bring; the manager is the product\n"
                "supabase    rows + pgvector, textbook RAG, no manager at all",
                color="red")

    e += S.card(1680, 490, 940, "what a swap does NOT change",
                "the loop · the retrieval gate · SKILL.md and procedural memory\n"
                "the episodic log · the consolidator · the harness · the gateways\n"
                "One box of seven. That is the whole market, in one diagram.")

    e += S.card(1680, 660, 940, "and what the interface HIDES",
                "The FactStore contract says a write must always store. To honour it\n"
                "the mem0 adapter passes infer=False — which switches OFF the single\n"
                "feature mem0 is known for. A common interface is how you compare\n"
                "them fairly AND how you stop seeing what they are.")

    e += S.card(1680, 855, 940, "the cost of NOT knowing this",
                "Both hosted stores are eventually consistent, and both understate it.\n"
                "mem0 has no readiness signal: 14s from add() to queryable. Zep's\n"
                "processed flag goes green while the graph still has zero nodes.\n"
                "Seed, probe immediately, and you publish amnesia you caused yourself.",
                color="red")

    e.append(S.arrow(1670, 340, 1000, 810, dashed=True, color=S.PAL["red"][1]))

    # the runnable proof, mapped to the boxes above it — this is what gets filmed
    e += S.card(1680, 1075, 940, "and here it is, runnable — examples/memory-native/",
                "langmem_native.py   THE MANAGER · Job 1    3 sentences in, 2 memories out\n"
                "mem0_native.py      THE MANAGER · Jobs 1+2  add/update/del/noop, superseded\n"
                "zep_native.py       THE STORE · graph       INVALID from <timestamp>\n"
                "supabase_native.py  THE STORE · vector      and NO manager at all\n"
                "tiny_memory_agent.py  40 lines, no tools — the store with nothing around it")

    e += S.card(1680, 1280, 940, "and the rig that runs all of them",
                "waku's Arena races every backend through the SAME questions, with a\n"
                "contestant told NOTHING as the control. Any probe the control passes\n"
                "was never testing memory. That is the only reason any of this is fair.")

    # ---- SCOPE -------------------------------------------------------------
    e.append(S.text(60, 1400,
                    "SCOPE — waku: one user, one machine.   mem0: shared across agents in an org.",
                    size=S.FS_SMALL))
    e.append(S.red_note(60, 1430,
                        "Zep: a project-wide ontology set in a signup wizard shapes every user's "
                        "graph — and deleting every user never clears it."))

    e.append(S.source_label(60, 1480,
                            "measured 2026-08-13 against mem0ai 2.0.17 · zep-cloud 3.27.0 · "
                            "langmem 0.0.30 · langgraph 1.2.10"))
    e.append(S.watermark(2400, 1480))
    return e


if __name__ == "__main__":
    from waku.ops.whiteboard.build_waku_architecture import write_svg_proxy

    els = build()
    S.validate(els)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(S.document(els), indent=2), encoding="utf-8")
    write_svg_proxy(els, SVG, w=2700, h=1540)
    shapes = [x for x in els if x["type"] in ("rectangle", "ellipse", "diamond")]
    arrows = [x for x in els if x["type"] == "arrow"]
    black = [x for x in shapes if x["strokeColor"] == S.INK]
    print(f"wrote {OUT}  ·  svg {SVG}")
    print(f"  shapes {len(shapes)} · arrows {len(arrows)} "
          f"· ratio {len(arrows)/max(len(shapes),1):.2f}  (masters 0.88)")
    print(f"  black {len(black)}/{len(shapes)} = {100*len(black)//max(len(shapes),1)}%"
          f"  (masters 96%)")
