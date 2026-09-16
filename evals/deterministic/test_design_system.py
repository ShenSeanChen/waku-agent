"""DETERMINISTIC EVAL — the dashboard follows the Waku Memory design system.

design/tokens.css and design/controls.css are copies of Waku Memory's files.
These checks keep the copies unedited, and keep style.css and the inline
styles in js/ from writing values of their own. Read docs/context/design-system.md
before changing any of them."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "waku" / "ops" / "static"
DESIGN = STATIC / "design"
COPIED = ("tokens.css", "controls.css")


def _style() -> str:
    return (STATIC / "style.css").read_text()


def _index() -> str:
    return (STATIC / "index.html").read_text()


def _js() -> dict[str, str]:
    return {f.name: f.read_text() for f in sorted((STATIC / "js").glob("*.js"))}


def _blocks(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every innermost rule, comments removed.

    Works through @media: the regex only matches a brace pair with no brace
    inside it, so the rules inside a media block are found one by one."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css)]


def _decls(body: str) -> list[tuple[str, str]]:
    out = []
    for part in body.split(";"):
        if ":" in part:
            prop, val = part.split(":", 1)
            out.append((prop.strip().lower(), val.strip()))
    return out


def _declarations() -> list[tuple[str, str, str, str]]:
    """(file, selector, property, value) for style.css and every inline style="…" in js/."""
    rows = [("style.css", sel, p, v) for sel, body in _blocks(_style()) for p, v in _decls(body)]
    for name, src in _js().items():
        for m in re.finditer(r'style="([^"]*)"|style=\'([^\']*)\'', src):
            rows += [(name, "style=", p, v) for p, v in _decls(m.group(1) or m.group(2) or "")]
    return rows


def _is_svg_text(selector: str) -> bool:
    """The architecture and scatter charts are SVG. Their text sizes are in the
    viewBox's own units, drawn to fit boxes whose geometry is frozen, so the
    type scale does not apply to them."""
    return all(re.match(r"\.(arch|scatter)\b(?!-)", s.strip()) for s in selector.split(","))


def test_copied_files_match_source():
    """A hand edit to a copied file fails here. Change it in Waku Memory, then
    run scripts/sync_design.py."""
    source = (DESIGN / "SOURCE.md").read_text()
    recorded = {name: digest for digest, name in re.findall(r"^([0-9a-f]{64})  (\S+)$", source, re.MULTILINE)}
    assert set(recorded) == set(COPIED)
    for name in COPIED:
        assert hashlib.sha256((DESIGN / name).read_bytes()).hexdigest() == recorded[name], (
            f"design/{name} differs from the copy recorded in SOURCE.md")


def test_fonts_are_local():
    css = (DESIGN / "fonts.css").read_text()
    assert "http" not in css, "fonts.css must not fetch anything"
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert len(urls) == 3
    for url in urls:
        assert (DESIGN / url.strip("'\"")).resolve().is_file(), f"fonts.css names a missing file: {url}"
    for face in ("InstrumentSans", "JetBrainsMono", "PlayfairDisplaySC"):
        assert (STATIC / "fonts" / f"OFL-{face}.txt").is_file(), f"no license for {face}"


def test_woff2_is_served_as_a_font():
    from waku.ops.dashboard import STATIC_TYPES

    assert STATIC_TYPES[".woff2"] == "font/woff2"


def test_sync_design_copies_and_records(tmp_path, monkeypatch):
    src = tmp_path / "memory"
    (src / "public" / "design").mkdir(parents=True)
    for name in COPIED:
        (src / "public" / "design" / name).write_text(f"/* {name} */\n")
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)

    spec = importlib.util.spec_from_file_location("sync_design", ROOT / "scripts" / "sync_design.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dest = tmp_path / "design"
    dest.mkdir()
    monkeypatch.setattr(mod, "DEST", dest)

    assert mod.main(["sync_design.py", str(src)]) == 0
    source = (dest / "SOURCE.md").read_text()
    for name in COPIED:
        assert (dest / name).read_text() == f"/* {name} */\n"
        assert f"{hashlib.sha256((dest / name).read_bytes()).hexdigest()}  {name}" in source


def test_design_files_load_before_style():
    html = _index()
    order = ["design/fonts.css", "design/tokens.css", "design/type.css", "design/controls.css", "style.css"]
    pos = [html.find(f'href="/static/{name}"') for name in order]
    assert -1 not in pos, dict(zip(order, pos))
    assert pos == sorted(pos), "the design files must load before style.css"


COLOUR = re.compile(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(")


def test_no_colour_literals():
    """Every colour is a token. (An HTML entity such as &#9662; is not a colour.)"""
    found = [(f, s, p, v) for f, s, p, v in _declarations() if COLOUR.search(v)]
    assert not found, f"raw colours — use a token: {found[:10]}"
    for name, src in _js().items():
        literal = re.findall(r"""["'`]\s*#[0-9a-fA-F]{3,8}\s*["'`]""", src)
        assert not literal, f"{name} has a colour literal: {literal}"


def test_accent_is_never_text():
    """Amber measures 1.56:1 on bone — a surface, not a text colour. Text uses
    --accent-fg; text on an amber fill uses --accent-ink."""
    found = [(f, s) for f, s, p, v in _declarations() if p == "color" and v == "var(--accent)"]
    assert not found, f"use --accent-fg for amber text: {found}"


def test_no_shadows():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p == "box-shadow" and v not in ("none", "var(--shadow-sm)", "var(--shadow-md)", "var(--shadow-lg)")]
    assert not found, f"nothing casts a shadow — separate with a rule: {found}"


def test_only_a_floating_menu_casts_a_shadow():
    found = [(f, s) for f, s, p, v in _declarations()
             if p == "box-shadow" and v != "none"
             and not (s.strip().startswith(".menu") or "::picker(select)" in s)]
    assert not found, f"only a floating menu (.menu, a select's open list) takes --shadow-md: {found}"


BUTTONS = re.compile(r"(?:^|[\s,>+~])(?:button|\.save|\.btn|\.sessbtn|\.cmp-sortbtn|#dsend|#mic|#dock-reopen|#nav-reopen)\b")


def test_buttons_never_fill():
    """Waku Memory's button has four levels and none of them is a colour fill."""
    grounds = {"transparent", "none", "var(--surface-paper)", "var(--surface-raised)", "var(--surface-sunk)", "var(--surface-bg)"}
    found = [(s, v) for f, s, p, v in _declarations()
             if f == "style.css" and BUTTONS.search(s) and p in ("background", "background-color") and v not in grounds]
    assert not found, f"buttons never fill: {found}"


def test_disabled_is_a_colour_not_an_opacity():
    found = [s for s, body in _blocks(_style()) if ":disabled" in s and "opacity" in body]
    assert not found, f"controls.css colours disabled controls; drop the opacity: {found}"


SIZES = {f"var(--text-{s})" for s in ("xs", "sm", "base", "lg", "xl", "2xl")}
WEIGHTS = {"400", "500", "normal", "var(--font-weight-normal)", "var(--font-weight-medium)"}


def test_font_sizes_use_the_scale():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p == "font-size" and v not in SIZES | {"inherit"} and not _is_svg_text(s)]
    assert not found, f"font-size must be a --text-* token: {found[:10]}"
    shorthand = [(f, s, v) for f, s, p, v in _declarations() if p == "font" and v != "inherit"]
    assert not shorthand, f"use font-family/font-size, not the font shorthand: {shorthand}"


def test_two_weights():
    found = [(f, s, v) for f, s, p, v in _declarations() if p == "font-weight" and v not in WEIGHTS]
    assert not found, f"only 400 and 500: {found[:10]}"


def test_faces_come_from_tokens():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p == "font-family" and v != "inherit" and not v.startswith("var(--face-")]
    assert not found, f"font-family must be a --face-* token: {found}"


def test_one_tracking_value():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p == "letter-spacing" and v not in ("var(--tracking-label)", "0", "normal") and not _is_svg_text(s)]
    assert not found, f"letter-spacing is --tracking-label or 0: {found}"


SHAPES = {"0", "var(--radius)", "var(--shape-chip)", "var(--shape-circle)", "var(--shape-pill)",
          "var(--shape-bubble)"}


def test_corners_come_from_tokens():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p == "border-radius" and not set(v.split()) <= SHAPES]
    assert not found, f"border-radius must be --radius or a --shape-* token: {found[:10]}"


def test_bubble_shape_is_only_the_chat_bubble():
    found = [(f, s) for f, s, p, v in _declarations() if "--shape-bubble" in v and ".bubble" not in s]
    assert not found, f"--shape-bubble is for the chat bubble only: {found}"


def test_one_duration():
    found = [(f, s, v) for f, s, p, v in _declarations()
             if p in ("transition", "transition-duration") and re.search(r"(?<![\w-])\d*\.?\d+m?s\b", v)]
    assert not found, f"transitions use var(--motion-fast): {found}"


def test_native_controls_get_a_data_slot():
    """controls.css styles by data-slot. The views build controls as HTML
    strings, so util.js stamps the attribute instead of every string."""
    js = _js()
    assert re.search(r"^function stampSlots\(", js["util.js"], re.MULTILINE)
    assert re.search(r"^function watchSlots\(", js["util.js"], re.MULTILINE)
    assert "MutationObserver" in js["util.js"]
    assert re.search(r"^watchSlots\(\);", js["main.js"], re.MULTILINE), "main.js must call watchSlots() at bootstrap"


def test_title_has_no_ligatures():
    """Playfair Display SC's fi and fl ligatures are lowercase glyphs, so a
    title like "Graph workflows" rendered as "WORKfLOWS" between small caps."""
    title = "".join(body for sel, body in _blocks(_style()) if sel == "h1")
    assert "font-variant-ligatures:none" in title.replace(" ", "")


def test_theme_is_applied_before_paint():
    """The stored theme is applied in <head>, before the design files load,
    so a reader who chose dark never sees a flash of the light ground."""
    html = _index()
    head = html[: html.index("</head>")]
    script = head.find("waku-theme")
    assert script != -1, "<head> must apply the stored waku-theme"
    assert script < head.find('href="/static/design/fonts.css"'), "apply the theme before the design files load"


def test_theme_toggle_is_wired():
    assert 'id="theme-toggle"' in _index()
    assert re.search(r"^function cycleTheme\(", _js().get("theme.js", ""), re.MULTILINE)


def test_rail_links_carry_their_letter():
    """Collapsed, the rail shows each item's first letter (from data-short);
    the full name stays in aria-label for a screen reader and the tooltip."""
    links = re.findall(r'(<a href="#[^"]*"[^>]*data-v="[^"]*"[^>]*>)<span class="lbl">([^<]+)</span>', _index())
    assert len(links) == 13, f"expected 13 rail links with a .lbl label, found {len(links)}"
    for tag, label in links:
        assert f'data-short="{label[0]}"' in tag, f"{label}: data-short must be its first letter"
        assert f'aria-label="{label}"' in tag, f"{label}: aria-label must be its full name"


def test_rail_resize_and_model_line_are_gone():
    """The rail collapses instead of resizing, and the model line moved to the
    chat dock's model chip."""
    src = _index() + "".join(_js().values())
    for gone in ("navW", "navHidden", "nav-resizer", "nav-reopen", 'id="model"', 'getElementById("model")'):
        assert gone not in src, f"{gone} belongs to the old sidebar"


UI_FUNCTIONS = ("uiCard", "uiBadge", "uiTable", "uiTabs", "uiNotice", "uiStatBand", "uiRow", "openDialog", "closeDialog",
                "uiButton", "uiLink", "openMenu", "closeMenu", "uiMenuLabel", "uiMenuItem", "uiMenuSep")

# Buttons and actions the views used to style by hand. index.html is the one
# exception: it is static, so its few buttons use the btn classes directly.
HAND_BUTTON = re.compile(r'<button[^>]*class="(save|sessbtn|msg-copy|mdcode-copy|connmodal-close|model-picker-toggle)[ "]')


def test_cards_buttons_and_actions_call_the_primitives():
    for name, src in _js().items():
        if name == "ui.js":
            continue
        assert not re.search(r'class="card[ "]', src), f"{name} writes a card by hand — call uiCard()"
        assert not re.search(r'class="reveal[ "]', src), f"{name} has an <a class=\"reveal\"> action — call uiButton() or uiLink()"
        hand = HAND_BUTTON.search(src)
        assert not hand, f"{name} styles a button by hand ({hand.group(1) if hand else ''}) — call uiButton()"


def test_line_heights_use_tokens():
    """--leading-normal is 1.55 in the dashboard and in Memory (design owner,
    2026-09-13); every other line height is one of the three tokens, or 1 for
    a single-line control."""
    allowed = {"1", "var(--leading-tight)", "var(--leading-snug)", "var(--leading-normal)"}
    found = [(f, s, v) for f, s, p, v in _declarations() if p == "line-height" and v not in allowed]
    assert not found, f"line-height must be a --leading-* token: {found[:10]}"
    assert re.search(r"--leading-normal:\s*1\.55;", (DESIGN / "type.css").read_text())


def test_primitives_are_defined_and_load_first():
    """Views build screens from js/ui.js, so it must define every primitive
    and load before any view calls one."""
    src = _js().get("ui.js", "")
    for fn in UI_FUNCTIONS:
        assert re.search(rf"^function {fn}\(", src, re.MULTILINE), f"js/ui.js must define {fn}"
    html = _index()
    assert "/static/js/ui.js" in html, "index.html must load js/ui.js"
    assert html.index("/static/js/ui.js") < html.index("/static/js/views.js"), "ui.js loads before the views"


OLD_NAME = re.compile(r"var\(--(?:bg|panel|line2?|ink[23]?|accent-soft|good-soft|bad-soft|good|mono)\)")

def test_no_old_names():
    """PR 1 kept the short names (--ink2, --line, …) as aliases so it could
    stay small. PR 3 moved every use onto the token name and deleted them."""
    assert not OLD_NAME.search(_style()), "style.css still reads an old name"
    for name, src in _js().items():
        assert not OLD_NAME.search(src), f"{name} still reads an old name"
    root = "".join(body for sel, body in _blocks(_style()) if sel == ":root")
    assert not re.search(r"--(bg|panel|line2?|ink[23]?|good|mono)\s*:", root), "the alias block is gone"


# Spacing comes from Memory's steps: the five named ones between things, and
# small multiples of --spacing inside one control (a badge's 2px, a menu's 6px).
SPACING_PART = re.compile(
    r"0|auto|var\(--space-[23468]\)|var\(--spacing\)|var\(--(control-pad-[xy]|field-pad-x|control-gap)\)"
    r"|calc\(var\(--spacing\) \* (0\.5|1|1\.5|5)\)|calc\(var\(--space-[23468]\) \* -1\)")


def _parts(value: str) -> list[str]:
    """Split a CSS value on spaces that are not inside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in value:
        depth += ch == "("
        depth -= ch == ")"
        if ch == " " and depth == 0:
            if cur:
                parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + ([cur] if cur else [])


def test_spacing_comes_from_tokens():
    found = []
    for f, s, p, v in _declarations():
        if not (p.startswith(("padding", "margin")) or p in ("gap", "row-gap", "column-gap")):
            continue
        for part in _parts(v.replace("!important", "").strip()):
            if "${" not in part and not SPACING_PART.fullmatch(part):
                found.append((f, s, p, v))
                break
    assert not found, f"padding/margin/gap must use --space-* or calc(var(--spacing) * N): {found[:10]}"


PRIMITIVE_CLASSES = ("notice", "stat-band", "tabs", "tbl", "list-row", "badge")


def test_views_call_the_primitives():
    """A view calls uiNotice(), uiBadge() and the rest instead of writing
    their markup, so every Badge on screen is the same Badge."""
    for name, src in _js().items():
        if name == "ui.js":
            continue
        for cls in PRIMITIVE_CLASSES:
            assert not re.search(rf'class="{cls}[ "]', src), f"{name} writes a {cls} by hand — call the ui.js function"
