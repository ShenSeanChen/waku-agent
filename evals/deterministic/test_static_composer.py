"""DETERMINISTIC EVAL — the chat composer wraps instead of scrolling sideways.

The dock's composer used to be `<input id="dmsg">`. An `<input>` is a single
line BY DEFINITION, so a message longer than the box scrolled out of sight to
the left and you could not read back what you had typed. The fix is a
`<textarea>` plus autogrow().

There is no JS test runner here (no build step, on purpose — see
static/README.md), so these are text-level checks in the same spirit as
test_static_assets.py. They are cheap and they pin the four ways this silently
reverts: the element goes back to an input, the CSS rule stops matching it, the
Enter key eats the newline, or code sets .value without resizing the box.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
INDEX = (STATIC / "index.html").read_text()
CSS = (STATIC / "style.css").read_text()
RENDER_JS = (STATIC / "js" / "render.js").read_text()
MAIN_JS = (STATIC / "js" / "main.js").read_text()


def test_the_composer_is_a_textarea():
    """THE case that would have caught the bug. An <input> cannot wrap."""
    assert not re.search(r'<input[^>]*\bid="dmsg"', INDEX), (
        "#dmsg is an <input> again — an input is one line by definition, so a "
        "long message scrolls out of view sideways. It must be a <textarea>."
    )
    assert re.search(r'<textarea[^>]*\bid="dmsg"', INDEX), "#dmsg must be a <textarea>"


def test_the_textarea_tag_holds_no_whitespace():
    """A textarea's content IS its value: a newline between the tags makes the
    box open pre-filled and two rows tall."""
    match = re.search(r'(<textarea[^>]*\bid="dmsg".*?</textarea>)', INDEX, re.DOTALL)
    assert match, "could not find the #dmsg textarea"
    assert re.search(r"></textarea>$", match.group(1)), (
        f"#dmsg must close tight against its opening tag: {match.group(1)!r}"
    )


def test_the_composer_rule_matches_the_textarea():
    """The styling rule selects `.chatbar input`. If it is not widened to the
    textarea too, the box loses its border, padding and colour entirely."""
    rule = re.search(r"^\s*\.chatbar input[^{]*\{", CSS, re.MULTILINE)
    assert rule and "textarea" in rule.group(0), (
        "the .chatbar composer rule must select the textarea as well as the input"
    )


def test_the_textarea_is_not_left_in_the_browser_monospace():
    """A textarea does NOT inherit the page font — without an explicit --face-*
    the composer renders in the browser default, which is monospace."""
    # anchored: ".chatbar textarea{" is also a substring of the shared
    # ".chatbar input,.chatbar textarea{" rule, which carries no font-family.
    block = re.search(r"^\s*\.chatbar textarea\{([^}]*)\}", CSS, re.MULTILINE)
    assert block, "expected a .chatbar textarea rule"
    assert "font-family:var(--face-" in block.group(1), (
        "the composer textarea needs an explicit --face-* font-family"
    )
    assert "resize:none" in block.group(1), "the drag handle fights autogrow"


def test_the_chatbar_does_not_stretch_its_buttons():
    """.chatbar is a flex row. Left at the default `stretch`, the mic and Send
    buttons grow as tall as the composer does."""
    rule = re.search(r"^\s*\.chatbar\{([^}]*)\}", CSS, re.MULTILINE)
    assert rule and "align-items:flex-end" in rule.group(1), (
        "the .chatbar row must pin its buttons to the bottom as the composer grows"
    )


def test_enter_sends_but_shift_enter_makes_a_newline():
    """Now that a newline is possible, plain Enter must still send and
    Shift+Enter must not."""
    handler = re.search(r"i\.onkeydown\s*=\s*e\s*=>\s*\{(.*?)\n  \};", RENDER_JS, re.DOTALL)
    assert handler, "expected the #dmsg onkeydown handler in render.js"
    body = handler.group(1)
    assert "shiftKey" in body, "Shift+Enter must insert a newline, not send"
    assert "preventDefault" in body, (
        "the sending keystroke must not also insert its newline"
    )


def test_every_scripted_value_change_resizes_the_box():
    """A textarea never resizes itself. Any code path that writes .value —
    clearing on send, and dropping a voice transcription in — has to call
    autogrow, or a long line lands in a one-row box and the bug is back.
    """
    assert "function autogrow(" in RENDER_JS, "autogrow() must stay a global in render.js"
    for name, src in (("render.js", RENDER_JS), ("main.js", MAIN_JS)):
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"\binput\.value\s*=", line):
                continue
            window = " ".join(lines[i : i + 2])
            assert "autogrow" in window, (
                f"{name}:{i + 1} sets input.value without calling autogrow — "
                f"the box will not resize: {line.strip()!r}"
            )


def test_the_two_buttons_match_each_other_in_height():
    """#mic has no vertical padding; .btn gives Send 10px. The old flex default
    (stretch) hid that by forcing both to the row height — once the composer
    grows, stretch is wrong and their natural heights differ by half. They are
    grouped so they stretch to each other instead.
    """
    assert re.search(r'<div class="chatbar-actions">', INDEX), (
        "mic and Send must stay grouped, or their heights drift apart"
    )
    for button in ('id="mic"', 'id="dsend"'):
        assert re.search(
            r'<div class="chatbar-actions">.*?' + re.escape(button), INDEX, re.DOTALL
        ), f"{button} must live inside .chatbar-actions"
    rule = re.search(r"^\s*\.chatbar-actions\{([^}]*)\}", CSS, re.MULTILINE)
    assert rule and "align-items:stretch" in rule.group(1), (
        ".chatbar-actions must stretch its buttons to each other"
    )


def test_an_empty_composer_is_exactly_as_tall_as_its_buttons():
    """On load the composer is one row (13px x 1.55 + 16 padding + 2 border =
    38.15) but a .btn is 34 (12 + 20 padding + 2 border), so they sat 4px apart.
    Both now floor at the SAME derived expression — this pins that they stay the
    same expression, which is the only thing that keeps them equal.
    """
    def min_height(selector: str) -> str:
        rule = re.search(r"^\s*" + re.escape(selector) + r"\{([^}]*)\}", CSS, re.MULTILINE)
        assert rule, f"expected a {selector} rule"
        found = re.search(r"min-height:([^;}]+)", rule.group(1))
        assert found, f"{selector} needs a min-height, or it drifts from its neighbour"
        return " ".join(found.group(1).split())

    composer, buttons = min_height(".chatbar textarea"), min_height(".chatbar-actions")
    assert composer == buttons, (
        "the composer and its buttons must floor at the same height:\n"
        f"  .chatbar textarea -> {composer}\n  .chatbar-actions  -> {buttons}"
    )
    assert "var(--text-sm)" in composer and "var(--leading-normal)" in composer, (
        "derive the row height from the type tokens, not a magic pixel value"
    )
