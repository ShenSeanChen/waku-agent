"""DETERMINISTIC EVAL — esc() must survive being used in an HTML attribute.

`esc()` escaped `&`, `<` and `>` and nothing else. For text nodes that is
enough, and for a long time nothing put model output inside an ATTRIBUTE, so
the gap could not bite.

The copy buttons added in #58 are the first place it happens:

    <button class="msg-copy" data-text="${esc(t.reply)}">

A reply containing one double quote closes that attribute and everything after
it becomes markup. Measured before the fix:

    data-text="hi" onmouseover="alert(1)" x=""

That is a real path, not a theoretical one. The model's output is not entirely
ours — `search_web` and `browse_web` pull text off the open web and the model
can echo it — and this dashboard holds the memory, the traces and the settings.

CI has no browser, so this asserts on the escaper itself rather than on
rendering. That is the right level anyway: the fix belongs in the helper, so
every future caller inherits it instead of having to remember.
"""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "waku/ops/static/js/util.js"


def _esc_source() -> str:
    src = JS.read_text(encoding="utf-8")
    m = re.search(r"^const esc = .*?;$", src, re.MULTILINE | re.DOTALL)
    assert m, "esc() not found in util.js"
    return m.group(0)


def test_esc_escapes_both_quote_characters():
    """THE regression. Without these two, any model reply containing a quote can
    break out of an attribute and attach its own event handler."""
    src = _esc_source()
    for ch, entity in (('"', "&quot;"), ("'", "&#39;")):
        assert entity in src, (
            f"esc() no longer escapes {ch!r} — a reply containing it can escape "
            "an HTML attribute. See waku/ops/static/js/render.js's copy buttons."
        )


def test_esc_still_escapes_the_original_three():
    src = _esc_source()
    for entity in ("&amp;", "&lt;", "&gt;"):
        assert entity in src


def test_the_character_class_covers_everything_it_maps():
    """A map entry with no matching character in the regex is a silent no-op —
    the code reads as if it escapes the character and does not."""
    src = _esc_source()
    cls = re.search(r"replace\(/\[([^\]]+)\]/g", src)
    assert cls, f"could not read esc()'s character class from: {src}"
    chars = cls.group(1).replace("\\", "")
    for ch in ("&", "<", ">", '"', "'"):
        assert ch in chars, f"{ch!r} is mapped but not matched by the regex"


def test_model_output_in_an_attribute_is_escaped_at_the_helper():
    """The copy buttons put a whole reply into data-text=. Pin that the file
    doing it relies on esc() rather than hand-rolling its own escaping, so this
    test keeps covering it."""
    render = (JS.parent / "render.js").read_text(encoding="utf-8")
    for attr in ('data-text="${esc(', 'data-text="${esc('):
        if attr in render:
            return
    raise AssertionError(
        "the copy buttons no longer pass their text through esc() — either they "
        "were removed, or something is escaping by hand and this test has "
        "stopped protecting it"
    )
