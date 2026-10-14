"""DETERMINISTIC EVAL — a dialog footer never pushes its message off-screen.

Issue #223: the connection dialog's footer holds a status message and two
buttons in one flex row. The row could not wrap and was packed to the right,
so a long error ("missing WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, ...")
overflowed past the dialog's LEFT edge, where it was clipped and could not be
scrolled to. The buttons stretched to the height of the squeezed message.

There is no browser here, so these checks resolve the cascade the way a
browser would at desktop width: every top-level rule for a selector, in order,
last declaration wins, @media blocks left out. The narrow-screen query used to
be the only place the message got its own line, and it could not work there
either, because the row never wrapped.
"""

from __future__ import annotations

import re
from pathlib import Path

STYLE = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "style.css"


def _desktop_rules(selector: str) -> dict[str, str]:
    """The declarations a browser applies to `selector` outside any @media."""
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(), flags=re.DOTALL)
    css = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    out: dict[str, str] = {}
    for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
        if selector not in (s.strip() for s in sel.split(",")):
            continue
        for part in body.split(";"):
            if ":" in part:
                prop, val = part.split(":", 1)
                out[prop.strip().lower()] = val.strip()
    return out


def _flex_basis(rules: dict[str, str]) -> str | None:
    """flex-basis after the `flex` shorthand and any longhand that follows it."""
    basis = None
    if "flex" in rules:
        parts = rules["flex"].split()
        basis = parts[2] if len(parts) == 3 else None
    return rules.get("flex-basis", basis)


def test_dialog_footer_wraps_instead_of_overflowing_left():
    foot = _desktop_rules(".dialog-foot")
    assert foot.get("justify-content") == "flex-end", "the footer packs its buttons to the right"
    # A row packed to the right that cannot wrap spills to the LEFT, off-screen.
    assert foot.get("flex-wrap") == "wrap"


def test_dialog_footer_buttons_keep_their_height():
    # The default (stretch) makes every item as tall as the tallest one.
    assert _desktop_rules(".dialog-foot").get("align-items") == "center"


def test_connection_message_takes_its_own_line_and_breaks_long_names():
    msg = _desktop_rules(".connmodal-message")
    assert _flex_basis(msg) == "100%", "a message shares no line with the buttons"
    # Env var names have no spaces; without this one name can still overflow.
    assert msg.get("overflow-wrap") == "anywhere"


def test_empty_connection_message_adds_no_blank_line():
    # Before anything is saved the message is empty: it must sit beside the
    # buttons at zero width, not claim a line of its own. It stays displayed
    # (not display:none) so its aria-live region announces the first message.
    empty = _desktop_rules(".connmodal-message:empty")
    assert _flex_basis(empty) == "0"
    assert empty.get("display") != "none"
