"""DETERMINISTIC EVAL — the dashboard's static assets hang together.

There is no JS test runner (no build step, on purpose), so these cheap checks
guard the two ways the split frontend silently breaks:
  1. index.html references a <script>/<link> that doesn't exist on disk.
  2. an inline onclick=/oninput=/… handler calls a function that no js/ file
     defines (e.g. a handler was renamed or moved and a call site missed).
Both render as a dead button with no error — exactly what a Python-side check
can catch without a browser."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
INDEX = (STATIC / "index.html").read_text()
JS_FILES = sorted((STATIC / "js").glob("*.js"))
JS_SRC = "\n".join(f.read_text() for f in JS_FILES)

# JS keywords / builtins / DOM globals an inline handler may call without a js/
# definition. Kept small on purpose — anything else must be a real app function.
ALLOWED = {
    "if", "for", "while", "switch", "return", "typeof", "new", "await", "function",
    "Math", "JSON", "Date", "Number", "String", "Boolean", "Object", "Array",
    "parseInt", "parseFloat", "isNaN", "console", "setTimeout", "setInterval",
    "encodeURIComponent", "decodeURIComponent", "alert", "confirm", "prompt",
    "document", "window", "event", "fetch",
}


def test_referenced_assets_exist():
    """Every /static/... in a src=/href= points to a real file."""
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', INDEX)
    assert refs, "expected script/link references in index.html"
    for ref in refs:
        target = STATIC / ref[len("/static/"):]
        assert target.is_file(), f"index.html references missing asset: {ref}"


def _defined_names() -> set[str]:
    names = set()
    names |= set(re.findall(r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)', JS_SRC, re.MULTILINE))
    names |= set(re.findall(r'^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', JS_SRC, re.MULTILINE))
    return names


def _handler_calls(text: str) -> set[str]:
    """Function names called inside inline on*=... handlers (not method calls)."""
    called = set()
    for body in re.findall(r'\bon\w+="([^"]*)"', text) + re.findall(r"\bon\w+='([^']*)'", text):
        # identifier immediately before '(' that isn't a property access (no leading .)
        called |= set(re.findall(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(', body))
    return called


def test_inline_handlers_are_defined():
    """Every function an inline handler calls is defined in a js/ file (or is an
    allowed builtin). Catches renamed/moved handlers before they ship as dead
    buttons. Scans index.html AND the HTML the js files generate."""
    defined = _defined_names()
    called = _handler_calls(INDEX) | _handler_calls(JS_SRC)
    missing = {n for n in called if n not in defined and n not in ALLOWED}
    assert not missing, f"inline handlers call undefined functions: {sorted(missing)}"


def test_app_js_is_gone():
    """The monolith was split; index.html must not load the old single file."""
    assert not (STATIC / "app.js").exists(), "stale app.js still present"
    assert "/static/app.js" not in INDEX, "index.html still references app.js"
