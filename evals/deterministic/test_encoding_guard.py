"""DETERMINISTIC EVAL — all file reads specify an explicit encoding.

Conventions §4 closed-set guard: prevent Windows cp1252 UnicodeDecodeError regressions in Linux CI.
Any text file read with Path.read_text() or open() must explicitly specify encoding.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _unencoded_calls(folder: Path) -> list[str]:
    violations = []
    for p in sorted(folder.rglob("*.py")):
        tree = ast.parse(p.read_bytes(), filename=str(p))
        rel = p.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check Path.read_text(...)
                if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
                    has_enc = any(kw.arg == "encoding" for kw in node.keywords) or len(node.args) >= 1
                    if not has_enc:
                        violations.append(f"{rel}:{node.lineno} -> {p.name}.read_text() without explicit encoding")
                # Check open(...) in text mode
                is_open = (isinstance(node.func, ast.Name) and node.func.id == "open") or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "open"
                )
                if is_open:
                    is_bin = False
                    if len(node.args) >= 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and "b" in node.args[0].value:
                        is_bin = True
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) and "b" in node.args[1].value:
                        is_bin = True
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) and "b" in kw.value.value:
                            is_bin = True
                    has_enc = any(kw.arg == "encoding" for kw in node.keywords) or len(node.args) >= 4
                    if not is_bin and not has_enc:
                        violations.append(f"{rel}:{node.lineno} -> open() in text mode without explicit encoding")
    return violations


def test_all_file_reads_in_evals_specify_encoding():
    violations = _unencoded_calls(ROOT / "evals")
    assert not violations, "evals/ must not omit encoding (fails on Windows cp1252):\n" + "\n".join(violations)


def test_all_file_reads_in_waku_specify_encoding():
    violations = _unencoded_calls(ROOT / "waku")
    assert not violations, "waku/ must not omit encoding (fails on Windows cp1252):\n" + "\n".join(violations)


def test_all_file_reads_in_scripts_specify_encoding():
    violations = _unencoded_calls(ROOT / "scripts")
    assert not violations, "scripts/ must not omit encoding (fails on Windows cp1252):\n" + "\n".join(violations)
