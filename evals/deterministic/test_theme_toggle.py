"""DETERMINISTIC EVAL — the dashboard theme switch has exactly one switch.

The dashboard used to follow the OS only, through `prefers-color-scheme`
queries. The System / Light / Dark button works by resolving the choice in
`js/theme.js` and stamping `data-theme="light|dark"` on `<html>`, so the CSS
reads ONE attribute.

That only holds while nobody adds a media query back. A stray
`@media (prefers-color-scheme:dark)` would win over the picked theme for
whatever it styles, and the symptom is a half-dark page nobody can explain from
reading either file alone — so pin the invariant here instead.

The other two checks cover the ways the button dies silently: the script has to
load in <head> (or the page flashes the wrong palette on every reload) and the
button has to be wired to a real handler."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
INDEX = (STATIC / "index.html").read_text()
CSS = (STATIC / "style.css").read_text()
THEME_JS = (STATIC / "js" / "theme.js").read_text()

# CSS comments explain the rule; only real at-rules break it.
CSS_RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)


def test_no_prefers_color_scheme_queries_remain():
    """One switch, not two. Style the dark case with `[data-theme="dark"]`."""
    strays = re.findall(r"@media[^{]*prefers-color-scheme[^{]*", CSS_RULES)
    assert not strays, (
        "style.css must key off data-theme alone — these would override a "
        f"manual Light/Dark pick: {strays}"
    )


def test_dark_palette_is_reachable():
    """The attribute the JS writes and the selector the CSS reads must match."""
    assert ':root[data-theme="dark"]{' in CSS_RULES
    assert 'setAttribute(\'data-theme\'' in THEME_JS or 'setAttribute("data-theme"' in THEME_JS


def test_native_controls_follow_the_picked_theme():
    """`color-scheme:light dark` would leave scrollbars and form controls on the
    OS setting, which is the one thing the button exists to override."""
    assert "color-scheme:light;" in CSS_RULES
    assert "color-scheme:dark;" in CSS_RULES
    assert "color-scheme:light dark" not in CSS_RULES


def test_theme_script_loads_in_head():
    """Anywhere else and the page paints the default palette first, then
    repaints — a white flash on every reload for a Dark user."""
    head = INDEX.split("</head>")[0]
    assert '<script src="/static/js/theme.js"></script>' in head


def test_button_is_wired():
    assert 'id="theme-toggle"' in INDEX and 'onclick="cycleTheme()"' in INDEX
    assert "function cycleTheme()" in THEME_JS


def test_all_three_choices_are_offered():
    """System stays first: it is the default, and the cycle has to return to it."""
    assert "const THEME_ORDER = ['system', 'light', 'dark'];" in THEME_JS
