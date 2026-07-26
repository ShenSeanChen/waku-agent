---
name: excalidraw
description: >
  Generate an Excalidraw whiteboard in Sean's hand-drawn video style (Excalifont,
  roughness 1, green signature, socials + watermark, source labels). Use whenever
  the user wants a whiteboard, diagram, teaching board, or "chart" for a video or
  the docs/whiteboards gallery — anything Sean will film with.
---

# Excalidraw whiteboards, Sean's way

Boards are generated from code so they match the masters in `~/Developer/Excalidraw`
exactly — Sean films with them directly. Never hand-write `.excalidraw` JSON; use the
style engine, which locks the values that a from-scratch board always gets wrong.

## The five things a naive board gets wrong (all fixed by the engine)

1. `fontFamily 5` (Excalifont) — NOT `1` (legacy Virgil). Wrong font = wrong board.
2. `roughness 1` (controlled) — NOT `2` (scratchy).
3. Stroke-width hierarchy `1` detail / `2` container / `4` emphasis — not flat `2`.
4. Type scale = Excalidraw presets S=16 / M=20 / L=28. Majority is S/M;
   L (28) reserved for big titles only. Don't inflate past the presets.
5. Signature green `#b2f2bb`, frames, socials block, watermark, source labels.

## How to build one

```python
from waku.ops.whiteboard import style as S

e = []
e.append(S.text(60, 40, "Title", size=S.FS_TITLE))
e.append(S.underline(64, 120, 800, color=S.PAL["orange"][1]))
e += S.labeled_box(80, 200, 300, 140, "A box\nwith a note", color="green")
e += S.pill_header(80, 400, 600, "SECTION", color="blue")
e += S.ellipse(500, 200, 180, 120, "agent", color="pink")
e += S.diamond(700, 200, 140, 100, "gate?", color="green")
e += S.labeled_arrow(380, 270, 500, 260, "tool calls")
e += S.socials_block(1980, 44)
e.append(S.watermark(80, 1030))
e.append(S.source_label(80, 900, "per <vendor>, <date>"))   # standing rule
e.append(S.red_note(80, 960, "honest red-ink opinion"))     # standing rule

S.validate(e)                       # catches broken bound-text ids
doc = S.document(e)                 # wraps with appState + white bg
```

Write `json.dumps(doc, indent=2)` to `docs/whiteboards/<name>.excalidraw`.
See `waku/ops/whiteboard/build_k3_tutorial.py` for a full two-board example.

## Palette (name → fill/stroke), meaning follows Sean's color system

- `green` loop / hero / final reply · `red` harness boundary / cost / honest-ink
- `blue` LLM-ops / observability · `orange` a loop step · `pink` LLM/agent nodes
- `grey` neutral state/config · `yellow` callout · `plain` bare container

## Always, before delivering

1. `S.validate(elements)` — every bound text must map to a container.
2. Eyeball via SVG proxy → PNG (QuickLook crops wide canvases; use headless
   Chrome `--screenshot --window-size=W,H` for the full board). Check for
   overlaps and off-canvas elements.
3. Shareable boards get the socials block + `@ShenSeanChen` watermark.
4. Every technical/vendor claim gets a dated `source_label`.

## Layout lessons (from comparing generated vs. hand-drawn)

- Poster-tight beats sprawl for a single teaching idea — fit it in one landscape
  frame (~2500×1100) so it reads without panning. Reserve the 8000×20000 sprawl
  for full system maps.
- A diagonal "ladder" reads as progress; a red barrier + a green break-through
  reads as problem→solution. Reuse those two shapes.
