"""The package version must have exactly one source of truth.

For four releases it had two: pyproject.toml said 0.1.4 while
waku/__init__.py still said 0.1.0, because nothing forced them to agree and
nothing read __version__ loudly enough to notice. A wrong __version__ is the
kind of bug that only shows up in someone else's bug report.

pyproject now declares the version dynamic and points hatchling at
waku/__init__.py, so there is one number. These tests fail if anyone puts the
second one back.
"""

import re
import tomllib
from pathlib import Path

import waku

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_pyproject_does_not_hardcode_a_second_version():
    """A static [project] version is how the two numbers drifted apart."""
    assert "version" not in PYPROJECT["project"], (
        "pyproject.toml declares its own version again — it must stay dynamic "
        "so waku/__init__.py is the only place the number lives"
    )
    assert "version" in PYPROJECT["project"].get("dynamic", [])


def test_hatchling_reads_the_version_from_the_package():
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "waku/__init__.py"


def test_version_is_a_release_number():
    """Catches a placeholder or a leftover dev marker going out to PyPI."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", waku.__version__), waku.__version__
