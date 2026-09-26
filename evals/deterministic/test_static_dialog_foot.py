"""DETERMINISTIC EVAL — a dialog footer wraps instead of clipping its message.

The connection dialog puts its status message and the Save / Test connection
buttons in one `.dialog-foot` flex row. The dialog is --measure-narrow wide, so
an error like "missing WHATSAPP_PHONE_NUMBER_ID, ..." did not fit beside the
buttons. The row could not wrap, and `justify-content:flex-end` pushed the
overflow off the LEFT edge, so only the tail of each line showed. The default
`stretch` also made both buttons as tall as the three-line message.

There is no JS test runner here (see static/README.md), so these are text-level
checks on style.css, in the same spirit as test_static_composer.py.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "style.css").read_text()


def _rule(selector: str) -> str:
    match = re.search(r"^\s*" + re.escape(selector) + r"\{([^}]*)\}", CSS, re.MULTILINE)
    assert match, f"expected a {selector} rule in style.css"
    return match.group(1)


def test_the_dialog_footer_wraps():
    """THE case that would have caught the bug: a flex-end row that cannot
    wrap loses whatever overflows it off the left edge."""
    assert "flex-wrap:wrap" in _rule(".dialog-foot"), (
        ".dialog-foot must wrap, or a long message overflows off its left edge"
    )


def test_the_dialog_footer_does_not_stretch_its_buttons():
    assert "align-items:center" in _rule(".dialog-foot"), (
        ".dialog-foot must not stretch its buttons to a multi-line message"
    )


def test_the_connection_message_takes_its_own_line_and_breaks_long_names():
    rule = _rule(".connmodal-message")
    assert "flex-basis:100%" in rule, "the message needs its own line above the buttons"
    assert "min-width:160px" not in rule, "a fixed floor forces the row wider than the dialog"
    assert "overflow-wrap:anywhere" in rule, "long env names must break, not overflow"
