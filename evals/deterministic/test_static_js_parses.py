"""DETERMINISTIC EVAL — the dashboard's JavaScript actually parses.

Written the moment it was needed. A `title=` attribute added to a button
contained the word waku wrapped in backticks, inside a template literal:

    title="... the `waku` partition is never named ..."

The backticks closed the template literal early and the file stopped parsing.
Not the button, not the panel — the WHOLE file. Every view compare.js renders
went blank, and the only symptom was a page that looked like it had not
reloaded. It cost a full round of "is the browser caching?" before anyone
checked the console.

Python has ruff and 580 tests standing between a typo and a broken dashboard.
The JavaScript, which is most of what a user actually touches, had nothing.
`node --check` is a syntax check and no more — it will not catch a wrong
selector or a bad fetch — but this class of bug turns the entire page off,
which is the loudest possible failure for the cheapest possible test.

Skips when node is absent rather than failing: a contributor without node
should still get a green suite, and CI has it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

JS_DIR = pathlib.Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "js"
SCRIPTS = sorted(JS_DIR.glob("*.js"))


def test_there_are_scripts_to_check():
    """A guard whose glob silently matches nothing passes forever and protects
    nothing. If the frontend moves, this fails and says so."""
    assert SCRIPTS, f"no .js found under {JS_DIR} — did the frontend move?"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_every_dashboard_script_parses(script: pathlib.Path):
    result = subprocess.run(  # noqa: S603 — fixed argv, path from our own glob
        [shutil.which("node"), "--check", str(script)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"{script.name} does not parse — the dashboard will render nothing.\n"
        f"{result.stderr.strip()}"
    )
