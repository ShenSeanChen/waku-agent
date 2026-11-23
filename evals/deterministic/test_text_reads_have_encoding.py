"""DETERMINISTIC EVAL — every text read names its encoding (issue #321).

The repo stores its files as UTF-8, but CI runs on Linux where Python's
default text encoding happens to be UTF-8 too, so a `read_text()` that omits
`encoding=` is invisible there. On Windows the platform default is cp1252:
the dashboard's JavaScript and CSS contain em dashes and middots, and 56
deterministic evals plus 3 collection errors came from nothing more exotic
than a decoder mismatch.

The mechanical fix lives in test code, so no other test would catch a new
call site that omits the encoding. This eval closes the set: it walks every
Python file under `evals/` and `waku/` and fails on any text read without an
explicit encoding — `read_text(...)` without `encoding=`, and a builtin
`open(...)` whose mode is a text read. Binary reads ("rb"), write/append-only
opens, and non-builtin `.open` methods (tarfile, SecretBox) are out of scope:
they either carry no text decoding or are not reads.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = (ROOT / "evals", ROOT / "waku")


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _mode_of(call: ast.Call) -> str | None:
    """The mode of a builtin open() call, positional or keyword."""
    value = _kwarg(call, "mode")
    if value is None and len(call.args) >= 2:
        value = call.args[1]
    return ast.literal_eval(value) if isinstance(value, ast.Constant) else None


def _offender(call: ast.Call) -> str | None:
    """The kind of uncoded text read at this call, or None if it is fine."""
    if any(kw.arg == "encoding" for kw in call.keywords):
        return None
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "read_text" and not call.args:
        return "read_text"
    if isinstance(func, ast.Name) and func.id == "open":
        mode = _mode_of(call)
        # No mode means "r". A read with "b" decodes nothing itself.
        if mode is None or ("r" in mode and "b" not in mode):
            return "open"
    return None


def test_every_text_read_names_its_encoding():
    offenders: list[str] = []
    for tree_dir in TREES:
        for path in sorted(tree_dir.rglob("*.py")):
            try:
                module = ast.parse(path.read_bytes())
            except SyntaxError:
                continue
            for node in ast.walk(module):
                if isinstance(node, ast.Call):
                    kind = _offender(node)
                    if kind:
                        rel = path.relative_to(ROOT)
                        offenders.append(f"{rel}:{node.lineno} {kind} without encoding")
    assert not offenders, (
        "Text reads without an explicit encoding= break on Windows (the "
        "platform default is cp1252, not UTF-8). Pass encoding= explicitly:\n"
        + "\n".join(offenders)
    )
