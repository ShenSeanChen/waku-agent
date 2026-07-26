"""Sean's Excalidraw whiteboard style, locked in code.

Every board Waku generates should be indistinguishable from Sean's hand-drawn
masters (~/Developer/Excalidraw) so he can film with it directly. The values
here were reverse-engineered from those masters, not invented:

  - Font:        Excalifont (fontFamily 5), NOT legacy Virgil (fontFamily 1)
  - Roughness:   1 (controlled hand stroke), NOT 2 (scratchy)
  - Stroke wt:   1 detail / 2 container / 4 emphasis  (a real hierarchy)
  - Type scale:  16 body -> 30 header -> 60 title      (dramatic, thumbnail-safe)
  - Palette:     signature green #b2f2bb, bold red/blue, grey structure
  - Grouping:    frames with names
  - Identity:    socials block + watermark on anything shareable

Helpers emit raw Excalidraw element dicts; `document()` wraps them; `validate()`
checks bound-text ids before you write. Deterministic ids (no wall-clock / RNG)
so re-running produces byte-identical output and diffs stay clean.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import pathlib

# ---- the style constants (from the masters) --------------------------------

FONT = 5            # Excalifont (current hand-drawn). Masters use 5, not 1.
LINE_HEIGHT = 1.25
ROUGHNESS = 1       # controlled hand stroke. Masters are 1, not 2.

# stroke-width hierarchy
SW_DETAIL, SW_BOX, SW_EMPHASIS = 1, 2, 4

# type scale — Excalidraw presets: S=16, M=20, L=28, XL=36.
# Majority of text is S/M; L is reserved for big titles only.
FS_TITLE = 28    # L  — big titles only
FS_HEADER = 20   # M  — section headers
FS_BOX = 16      # S  — node labels
FS_BODY = 16     # S  — body / notes
FS_SMALL = 16    # S  — fine print too: S is the floor, only S/M/L are allowed

INK = "#1e1e1e"
BLACK = "#000000"

# palette: name -> (fill, stroke). Fills are the masters' saturated pastels.
PAL = {
    "green":  ("#b2f2bb", "#2f9e44"),   # signature: the loop / the hero / final reply
    "red":    ("#ffc9c9", "#e03131"),   # harness boundary / honest red-ink / cost wall
    "blue":   ("#a5d8ff", "#1971c2"),   # LLM-ops / observability
    "orange": ("#ffd8a8", "#e8590c"),   # the loop step
    "pink":   ("#fcc2d7", "#c2255c"),   # LLM / agent nodes
    "grey":   ("#e9ecef", "#868e96"),   # neutral state / config
    "yellow": ("#ffec99", "#f08c00"),   # highlight / callout
    "plain":  ("transparent", INK),     # bare container
}

_ids = itertools.count(1)


def _id(prefix: str) -> str:
    return f"{prefix}-{next(_ids):04d}"


def _seed(n: int) -> int:
    # stable pseudo-seed derived from element ordinal (no RNG -> reproducible)
    return (n * 2654435761) % (2**31)


def _base(kind: str, x, y, w, h, stroke, bg, sw, frame=None, group=None):
    n = next(_ids)
    return {
        "id": f"{kind[:3]}-{n:04d}",
        "type": kind,
        "x": float(x), "y": float(y),
        "width": float(w), "height": float(h),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": bg,
        "fillStyle": "solid",
        "strokeWidth": sw,
        "strokeStyle": "solid",
        "roughness": ROUGHNESS,
        "opacity": 100,
        "groupIds": [group] if group else [],
        "frameId": frame,
        "roundness": None,
        "seed": _seed(n),
        "version": 1, "versionNonce": _seed(n + 7),
        "isDeleted": False,
        "boundElements": [],
        "updated": 1, "link": None, "locked": False,
    }


# ---- primitives ------------------------------------------------------------

def text(x, y, s, *, size=FS_BODY, color=INK, align="left", frame=None,
         group=None, width=None):
    n = next(_ids)
    lines = s.split("\n")
    w = width if width else max((len(ln) for ln in lines), default=1) * size * 0.55
    h = len(lines) * size * LINE_HEIGHT
    return {
        "id": f"txt-{n:04d}", "type": "text",
        "x": float(x), "y": float(y), "width": float(w), "height": float(h),
        "angle": 0, "strokeColor": color, "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": SW_DETAIL, "strokeStyle": "solid",
        "roughness": ROUGHNESS, "opacity": 100,
        "groupIds": [group] if group else [], "frameId": frame,
        "roundness": None, "seed": _seed(n), "version": 1,
        "versionNonce": _seed(n + 3), "isDeleted": False, "boundElements": [],
        "updated": 1, "link": None, "locked": False,
        "text": s, "fontSize": size, "fontFamily": FONT,
        "textAlign": align, "verticalAlign": "top",
        "containerId": None, "originalText": s, "lineHeight": LINE_HEIGHT,
    }


def labeled_box(x, y, w, h, label, *, color="plain", size=FS_BOX, rounded=True,
                sw=SW_BOX, fill="node", frame=None, group=None):
    """A rounded container with centered bound text — the workhorse shape.

    fill mode (Sean's idiom is mostly WHITE shapes, color in the stroke):
      "node"  -> white fill, colored stroke   (default; the common case)
      "chip"  -> solid pastel fill            (only for user-prompt / reply chips)
      "open"  -> transparent fill             (subsystem interiors)
    """
    pal_fill, stroke = PAL[color]
    bg = {"node": "#ffffff", "chip": pal_fill, "open": "transparent"}[fill]
    if color == "plain":
        stroke = INK
    box = _base("rectangle", x, y, w, h, stroke, bg, sw, frame, group)
    box["roundness"] = {"type": 3} if rounded else None
    t = text(0, 0, label, size=size, color=INK, align="center",
             frame=frame, group=group)
    t["containerId"] = box["id"]
    t["verticalAlign"] = "middle"
    t["width"], t["height"] = w - 20, h - 20
    t["x"], t["y"] = x + 10, y + h / 2 - t["height"] / 2
    box["boundElements"] = [{"type": "text", "id": t["id"]}]
    return [box, t]


def pill_header(x, y, w, title, *, color="grey", frame=None, group=None):
    """Section header: a wide low WHITE pill with a colored stroke + title."""
    return labeled_box(x, y, w, 46, title, color=color, size=FS_HEADER,
                       rounded=True, sw=SW_BOX, fill="node", frame=frame, group=group)


def boundary(x, y, w, h, title, *, color="red", frame=None, group=None):
    """Big colored-stroke subsystem boundary with a hand-script title top-left.

    This is the master idiom: red Harness / orange Loop / blue LLM Ops — a
    transparent thick-stroke box you drop other nodes inside.
    """
    _, stroke = PAL[color]
    box = _base("rectangle", x, y, w, h, stroke, "transparent", SW_BOX, frame, group)
    box["roundness"] = {"type": 3}
    t = text(x + 26, y + 18, title, size=FS_HEADER, color=stroke,
             frame=frame, group=group)
    return [box, t]


def chip(x, y, w, h, label, *, color="green", frame=None, group=None):
    """Small solid-fill chip — user prompt / final reply (the ONE place fill is right)."""
    return labeled_box(x, y, w, h, label, color=color, size=FS_BODY,
                       rounded=True, sw=SW_DETAIL, fill="chip",
                       frame=frame, group=group)


def annotate(x, y, s, *, frame=None, group=None):
    """Blue floating side-note next to an arrow — the master's commentary voice."""
    return text(x, y, s, size=FS_BODY, color=PAL["blue"][1], align="left",
                frame=frame, group=group)


def ellipse(x, y, w, h, label="", *, color="pink", fill="chip", frame=None,
            group=None):
    """Agent / LLM node. Default pink chip fill (masters tint agent ovals)."""
    pal_fill, _stroke = PAL[color]
    bg = pal_fill if fill == "chip" else "#ffffff"
    e = _base("ellipse", x, y, w, h, BLACK, bg, SW_DETAIL, frame, group)
    out = [e]
    if label:
        t = text(0, 0, label, size=FS_BODY, color=INK, align="center",
                 frame=frame, group=group)
        t["containerId"] = e["id"]; t["verticalAlign"] = "middle"
        t["width"], t["height"] = w - 16, min(h - 12, FS_BODY * LINE_HEIGHT * 2)
        t["x"], t["y"] = x + 8, y + h / 2 - t["height"] / 2
        e["boundElements"] = [{"type": "text", "id": t["id"]}]
        out.append(t)
    return out


def diamond(x, y, w, h, label="", *, color="green", frame=None, group=None):
    """Decision / gate."""
    fill, stroke = PAL[color]
    d = _base("diamond", x, y, w, h, stroke, fill, SW_BOX, frame, group)
    out = [d]
    if label:
        t = text(0, 0, label, size=FS_BODY, color=INK, align="center",
                 frame=frame, group=group)
        t["containerId"] = d["id"]; t["verticalAlign"] = "middle"
        t["width"], t["height"] = w - 24, FS_BODY * LINE_HEIGHT * 2
        t["x"], t["y"] = x + 12, y + h / 2 - t["height"] / 2
        d["boundElements"] = [{"type": "text", "id": t["id"]}]
        out.append(t)
    return out


def arrow(x1, y1, x2, y2, *, color=INK, sw=SW_BOX, dashed=False, frame=None,
          group=None):
    a = _base("arrow", x1, y1, x2 - x1, y2 - y1, color, "transparent", sw,
              frame, group)
    a["strokeStyle"] = "dashed" if dashed else "solid"
    a["points"] = [[0.0, 0.0], [float(x2 - x1), float(y2 - y1)]]
    a["lastCommittedPoint"] = None
    a["startBinding"] = a["endBinding"] = None
    a["startArrowhead"] = None
    a["endArrowhead"] = "arrow"
    return a


def labeled_arrow(x1, y1, x2, y2, label, *, color=INK, dashed=False, frame=None,
                  group=None):
    a = arrow(x1, y1, x2, y2, color=color, dashed=dashed, frame=frame, group=group)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    t = text(mx - len(label) * FS_SMALL * 0.28, my - 22, label, size=FS_SMALL,
             color=color, align="center", frame=frame, group=group)
    return [a, t]


def hand_line(x, y, pts, *, color=INK, sw=SW_DETAIL, frame=None, group=None):
    """Hand-drawn accent (underline / scribble). pts relative to (x,y)."""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    ln = _base("line", x, y, max(xs) - min(xs) or 1, max(ys) - min(ys) or 1,
               color, "transparent", sw, frame, group)
    ln["points"] = [[float(a), float(b)] for a, b in pts]
    ln["lastCommittedPoint"] = None
    ln["startBinding"] = ln["endBinding"] = None
    ln["startArrowhead"] = ln["endArrowhead"] = None
    return ln


def underline(x, y, w, *, color=INK, frame=None, group=None):
    """A wavy hand underline for titles."""
    pts = [[0, 0], [w * 0.25, 4], [w * 0.5, -2], [w * 0.75, 4], [w, 0]]
    return hand_line(x, y, pts, color=color, sw=SW_BOX, frame=frame, group=group)


def frame(x, y, w, h, name):
    n = next(_ids)
    return {
        "id": f"frm-{n:04d}", "type": "frame",
        "x": float(x), "y": float(y), "width": float(w), "height": float(h),
        "angle": 0, "strokeColor": "#bbb", "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": SW_BOX, "strokeStyle": "solid",
        "roughness": 0, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _seed(n), "version": 1,
        "versionNonce": _seed(n + 1), "isDeleted": False, "boundElements": [],
        "updated": 1, "link": None, "locked": False, "name": name,
    }


def source_label(x, y, s, *, frame=None, group=None):
    """Small grey citation chip, per Sean's source-label rule."""
    return text(x, y, s, size=FS_SMALL, color=PAL["grey"][1], align="left",
                frame=frame, group=group)


def red_note(x, y, s, *, frame=None, group=None):
    """Honest red-ink opinion note."""
    return text(x, y, s, size=FS_BODY, color=PAL["red"][1], align="left",
                frame=frame, group=group)


def socials_block(x, y):
    """Sean's socials block for shareable charts (top-right)."""
    return [
        text(x, y, "GitHub  @ShenSeanChen", size=FS_BODY, color=INK),
        text(x, y + 24, "YouTube @SeanAIStories", size=FS_BODY, color=INK),
        text(x, y + 48, "X       @ShenSeanChen", size=FS_BODY, color=INK),
    ]


def watermark(x, y):
    return text(x, y, "@ShenSeanChen", size=FS_BODY, color=PAL["grey"][1])


# ---- standard components (match the hand-drawn masters) --------------------

def card(x, y, w, title, body, *, color="plain", title_color=None,
         body_color=INK, min_h=0, frame=None, group=None):
    """The workhorse teaching box: rounded, title + short bullets, auto-height.

    Default is BLACK stroke / white fill (color="plain") — matching the masters,
    where 146/159 boxes are black and colour is reserved for container boundaries
    and highlights. Pass color= only to deliberately highlight a box.
    """
    lines = body.count("\n") + 1 if body else 0
    h = max(min_h, 42 + lines * 22 + 20)
    box = labeled_box(x, y, w, h, "", color=color, frame=frame, group=group)
    tcol = title_color or (INK if color == "plain" else PAL[color][1])
    out = list(box)
    out.append(text(x + 18, y + 15, title, size=FS_HEADER, color=tcol,
                    frame=frame, group=group))
    if body:
        out.append(text(x + 18, y + 52, body, size=FS_BODY, color=body_color,
                        frame=frame, group=group))
    return out


# ---- images + social logos -------------------------------------------------

ASSETS = pathlib.Path(__file__).resolve().parent / "assets"
_FILES: dict = {}


def image(path, x, y, w, h, *, frame=None, group=None):
    """Embed a PNG/JPEG as an Excalidraw image element (registers its file)."""
    p = pathlib.Path(path)
    if not p.is_absolute():
        p = ASSETS / path
    data = p.read_bytes()
    mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    fid = hashlib.sha1(str(p).encode()).hexdigest()
    if fid not in _FILES:
        b64 = base64.b64encode(data).decode()
        _FILES[fid] = {"mimeType": mime, "id": fid,
                       "dataURL": f"data:{mime};base64,{b64}", "created": 1}
    n = next(_ids)
    return {
        "id": f"img-{n:04d}", "type": "image",
        "x": float(x), "y": float(y), "width": float(w), "height": float(h),
        "angle": 0, "strokeColor": "transparent", "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid",
        "roughness": 0, "opacity": 100, "groupIds": [group] if group else [],
        "frameId": frame, "roundness": None, "seed": _seed(n),
        "version": 1, "versionNonce": _seed(n + 5), "isDeleted": False,
        "boundElements": [], "updated": 1, "link": None, "locked": False,
        "status": "saved", "fileId": fid, "scale": [1, 1],
    }


def socials_logos(x, y, *, size=44, row=58, text_dx=64):
    """Sean's socials block WITH the real logos (github / youtube / x)."""
    rows = [("github.png", 1.0, "@ShenSeanChen"),
            ("youtube.png", 85 / 60, "@SeanAIStories"),
            ("x.jpg", 1.0, "@ShenSeanChen")]
    out = []
    for i, (logo, aspect, handle) in enumerate(rows):
        yy = y + i * row
        out.append(image(logo, x, yy, size * aspect, size))
        out.append(text(x + text_dx, yy + size / 2 - FS_BODY * LINE_HEIGHT / 2,
                        handle, size=FS_BODY, color=INK))
    return out


# ---- document + validation -------------------------------------------------

def document(elements):
    return {
        "type": "excalidraw", "version": 2, "source": "waku/ops/whiteboard",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {fid: dict(f) for fid, f in _FILES.items()},
    }


def validate(elements):
    """Raise if any bound text is broken — catches the classic Excalidraw bug."""
    ids = {e["id"] for e in elements}
    for e in elements:
        if e["type"] == "text" and e.get("containerId"):
            assert e["containerId"] in ids, f"text {e['id']} -> missing container"
        for be in e.get("boundElements") or []:
            assert be["id"] in ids, f"{e['id']} -> missing bound {be['id']}"
    return True
