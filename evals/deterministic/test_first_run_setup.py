"""The first-run gate: no usable provider means no dashboard.

WHY THIS EXISTS. Before 2026-09-27 a fresh install opened on Overview with a
chat box that looked ready, and the first message came back
`APIConnectionError`. On the hosted deployment it was worse: the Models page
said the free tier was "enabled" and "current" while the proxy behind it runs
zero replicas, so the product asserted a working provider and then failed.

The gate is in main.js's `render`. These checks are static -- this repo has no
JavaScript runtime in CI -- so each one pins a property that a reader of the
source can verify, and the behaviour itself was exercised in a browser against
an empty WAKU_HOME before the change shipped.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "waku" / "ops" / "static" / "js"
MAIN = JS / "main.js"
SETUP = JS / "setup.js"
MODELS = JS / "models.js"
INDEX = ROOT / "waku" / "ops" / "static" / "index.html"


def test_render_consults_the_gate_before_choosing_a_view():
    """`needsSetup` has to be asked BEFORE `render` picks a view, or the gate
    is a redirect after the fact -- and on Overview the animated diagram is
    built by the branch above it, which would run first."""
    source = MAIN.read_text(encoding="utf-8")
    body = source[source.index("function render()"):]
    gate = body.index("needsSetup(")
    chooses = body.index('const view = VIEWS[v] ? v : "overview"')
    assert gate < chooses, (
        "render() picks a view before asking needsSetup(); the gate has to "
        "come first or a gated page still builds.")


def test_the_gate_lets_nothing_through():
    """No view is exempt.

    An earlier version let `#models` through, because that is where a key is
    entered -- and produced a page with no way back, reachable only from a
    screen that then hid it. The provider modal is a global overlay, so
    setup.js opens the same one over the gate and there is nothing to escape
    to. A `v !== "..."` beside the gate would bring the locked room back.
    """
    source = MAIN.read_text(encoding="utf-8")
    window = source[source.index("if (needsSetup("):][:200]
    assert not re.search(r'v\s*!==\s*"', window), (
        "the gate has an exemption again; see setup.js on why there is none")


def test_the_gate_and_the_models_grid_ask_one_question():
    """`needsSetup` calls `providerCardStatus`, the function the Models grid
    uses to colour a card.

    A second derivation of "is this provider usable" is this file's whole
    subject: the gate and the grid would drift, and the visible result is a
    setup screen you cannot get past while Models insists everything is fine.
    """
    setup = SETUP.read_text(encoding="utf-8")
    assert "providerCardStatus(" in setup, (
        "needsSetup derives usability itself instead of calling "
        "providerCardStatus")
    assert "function providerCardStatus" in MODELS.read_text(encoding="utf-8")
    assert "function providerCardStatus" not in setup, "two definitions"
    # And it asks for the state the grid calls enabled, not merely key-set.
    assert '=== "enabled"' in setup


def test_setup_never_writes_a_credential():
    """Every key goes through models.js's modal, so there is one path that
    writes a credential, one validation and one place to audit. A fetch or a
    POST in this file would be a second one."""
    setup = SETUP.read_text(encoding="utf-8")
    for forbidden in ("fetch(", "postJSON(", "XMLHttpRequest", "localStorage",
                      "sessionStorage"):
        assert forbidden not in setup, f"setup.js does its own {forbidden}"
    assert "openProviderModal(" in setup


def test_the_provider_buttons_escape_what_they_interpolate():
    """Provider names and keys reach an onclick attribute and a text node.
    They come from the server, but `esc`/`escAttr` is what every other view
    here does, and an unescaped one is a template nobody notices until a
    provider is named with a quote in it."""
    setup = SETUP.read_text(encoding="utf-8")
    choice = setup[setup.index("function setupChoice"):]
    choice = choice[:choice.index("\n}")]
    assert "escAttr(p.key)" in choice
    assert "esc(p.name)" in choice
    assert "${p.key}" not in choice and "${p.name}" not in choice


def test_setup_js_loads_after_what_it_calls():
    """js/ shares one global scope and the files are plain scripts in
    document order: setup.js calls providerCardStatus (models.js), uiCard
    (ui.js) and esc (util.js), and hangs VIEWS.setup on the object views.js
    creates. Loaded too early, `VIEWS` is not defined and the page is blank
    with one console error."""
    html = INDEX.read_text(encoding="utf-8")
    order = re.findall(r'<script src="/static/js/([a-z]+)\.js">', html)
    assert "setup" in order, "index.html never loads setup.js"
    for needed in ("util", "ui", "models", "views"):
        assert order.index(needed) < order.index("setup"), needed
    assert order.index("setup") < order.index("main"), (
        "main.js calls needsSetup and VIEWS.setup at load time")


def test_the_gate_hides_the_control_that_used_to_fail():
    """The chat dock is the exact thing that looked ready and returned
    APIConnectionError, and it has three parts: the panel, the drag handle
    and the reopen button. Hiding only the panel leaves the button that
    brings it back."""
    css = (ROOT / "waku" / "ops" / "static" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("body.first-run"):]
    block = block[:block.index("{ display: none }")]
    for part in ("#dock", "#dock-resizer", "#dock-reopen", "#nav"):
        assert part in block, f"first-run leaves {part} on screen"
