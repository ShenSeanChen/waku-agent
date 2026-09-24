"""DETERMINISTIC EVAL — waku/ and hosted/ never import each other.

Acceptance 12 of spec 001. The two directories meet over HTTP and nowhere
else. The direction that costs something is hosted/ -> waku/: importing
waku.config runs find_dotenv(usecwd=True), which walks up from the working
directory and loads the first .env it finds, and on the VM that file is the
platform's own secrets (design section 7). The other direction costs the
wheel: waku/ ships to PyPI and hosted/ does not, so an import would be a
module that is simply absent for everyone who installed waku.

SHAPE, AND WHY IT IS AN ALLOWLIST. The harm the spec names is waku.config
being imported AT ALL, not the token "waku" appearing in an import statement.
A denylist of the name "waku" has at least three silent bypasses, each
confirmed on this branch:

  from scripts.generate_env_example import render   # scripts/ imports waku
  pkgutil.resolve_name("waku.config:Settings")
  runpy.run_module("waku.config")

4 files under scripts/, 72 under evals/, 3 under lab/ and 1 under examples/
import waku, and evals/conftest.py already puts the repo root on sys.path, so
every one of them is reachable from hosted/. That is the shape group A's route
guard lost with fourteen times across five rounds before it was inverted.

So the hosted side is inverted. Three layers, each closed:

  1. Every import root in hosted/ must be in ALLOWED_IMPORT_ROOTS_IN_HOSTED --
     the standard library, minus the handful of stdlib modules whose job is to
     load arbitrary code, plus "hosted" itself and the two distributions the
     hosted extra declares. Anything else -- waku, scripts, evals, lab,
     examples, a package invented next year -- fails by default.
  2. No dynamic import machinery by name: __import__, exec and eval, which are
     builtins and therefore not import roots at all.
  3. A runtime check: import every module under hosted/ in a fresh interpreter
     and assert "waku" never appears in sys.modules. This is the only closed
     set for "however it was spelled, did waku actually get loaded" -- it
     catches import-time dynamic loading that no AST walk can see.

The waku/ -> hosted/ direction stays a denylist. waku/ imports no repo-root
package today, hard rule 5 already forbids two of them, and waku/ genuinely
needs importlib (five call sites in connect.py, ops/dashboard.py and
ops/commands.py), so the inversion would have to carve exceptions rather than
close a hole.

IT DOES NOT LOOK AT STRING LITERALS, on purpose (tasks.md B1): hosted/ names
"waku" as an image tag, a container name, a Unix user and a directory, and a
guard that failed on those would be turned off within a week.

IT READS *.py ONLY. hosted/deploy/*.sh and hosted/image/*.Dockerfile arrive in
groups C and F and can run `python -c "import waku..."` with nothing here to
stop them. That is C and F's problem to carry, and it is written down here so
neither group discovers it as a surprise.

The test skips itself when hosted/ is absent, because the sdist ships evals/
and not hosted/.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WAKU = ROOT / "waku"
HOSTED = ROOT / "hosted"

pytestmark = pytest.mark.skipif(
    not HOSTED.is_dir(),
    reason="hosted/ is not in this checkout (it ships in neither the wheel nor the sdist)")

# The one name waku/ may never import. waku/ imports no repo-root package at
# all today, so this list is short by nature rather than by omission.
FORBIDDEN_IN_WAKU = {"hosted"}

# Stdlib modules that exist to load code the source does not name. They are
# the reason an import-root allowlist alone is not enough: every one of them
# is in sys.stdlib_module_names and would otherwise be allowed.
IMPORT_MACHINERY = {
    "importlib", "imp", "pkgutil", "runpy", "zipimport", "modulefinder",
    "pkg_resources", "py_compile", "compileall",
}

# Everything hosted/ may import. Default-deny: a root that is not in here
# fails, whatever it is and whenever it was added.
ALLOWED_IMPORT_ROOTS_IN_HOSTED = (
    (sys.stdlib_module_names - IMPORT_MACHINERY)
    | {"hosted"}
    # The two distributions the `hosted` extra declares, by import name.
    # Nothing in group B imports either; groups D and E do.
    | {"aiohttp", "jwt"}
)

# Builtins that load code. Not import roots, so the allowlist cannot see them.
FORBIDDEN_BUILTINS_IN_HOSTED = {"__import__", "exec", "eval"}


def _roots(node: ast.AST) -> list[str]:
    """The root package name of every module an import node names.

    A relative import (level > 0) is inside hosted/ by construction, so it
    resolves to "hosted" rather than to nothing.
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level > 0:
            return ["hosted"]
        return [node.module.split(".", 1)[0]] if node.module else []
    return []


def _python_files(top: Path) -> list[Path]:
    return sorted(p for p in top.rglob("*.py") if "__pycache__" not in p.parts)


def test_waku_never_imports_hosted():
    offenders = {
        str(py.relative_to(ROOT)): sorted(set(bad))
        for py in _python_files(WAKU)
        if (bad := [r for node in ast.walk(ast.parse(py.read_text(encoding="utf-8")))
                    for r in _roots(node) if r in FORBIDDEN_IN_WAKU])
    }
    assert not offenders, (
        f"waku/ imports hosted/: {offenders}\n"
        "hosted/ ships in neither the wheel nor the sdist, so this module is "
        "absent for everyone who installed waku. The two meet over HTTP.")


def test_hosted_imports_nothing_outside_the_allowlist():
    """Default-deny. waku is refused, and so is every package that could reach
    it on hosted/'s behalf: scripts, evals, lab, examples, and anything a
    future contributor adds at the repo root."""
    offenders = {
        str(py.relative_to(ROOT)): sorted(set(bad))
        for py in _python_files(HOSTED)
        if (bad := [r for node in ast.walk(ast.parse(py.read_text(encoding="utf-8")))
                    for r in _roots(node) if r not in ALLOWED_IMPORT_ROOTS_IN_HOSTED])
    }
    assert not offenders, (
        f"hosted/ imports outside its allowlist: {offenders}\n"
        "hosted/ may import the standard library (minus the import machinery), "
        "hosted itself, aiohttp and jwt. Everything else is refused, because a "
        "sibling package that imports waku is a path to waku.config, which "
        "loads the platform's own .env on the VM.")


def test_hosted_has_no_dynamic_import_machinery():
    """The allowlist reads import STATEMENTS. __import__, exec and eval are
    builtins, so no import statement names them and the allowlist never sees
    them. waku/ needs importlib and keeps it; hosted/ does not get it."""
    offenders: dict[str, list[str]] = {}
    for py in _python_files(HOSTED):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        found = sorted({
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id in FORBIDDEN_BUILTINS_IN_HOSTED)
            or (isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_BUILTINS_IN_HOSTED)
        })
        if found:
            offenders[str(py.relative_to(ROOT))] = found
    assert not offenders, (
        f"hosted/ reaches for code-loading builtins: {offenders}\n"
        "Write a plain import. The allowlist above reads import statements, "
        "and this is how code walks past it.")


def test_importing_every_hosted_module_never_loads_waku():
    """The third layer, and the only one that is closed against spelling.

    Whatever the source says, this imports each module under hosted/ in a
    fresh interpreter and asks whether waku ended up in sys.modules. An
    import-time pkgutil.resolve_name, runpy.run_module or __import__ fails
    here even if it somehow read as legal above.
    """
    modules = sorted(
        ".".join(py.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
        for py in _python_files(HOSTED))
    program = (
        "import sys, importlib\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "leaked = sorted(m for m in sys.modules if m == 'waku' or m.startswith('waku.'))\n"
        "print('LEAKED', leaked)\n"
    )
    result = subprocess.run([sys.executable, "-c", program], cwd=ROOT, check=False,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"importing hosted/ failed:\n{result.stderr[-2000:]}")
    assert result.stdout.strip() == "LEAKED []", (
        f"importing hosted/ loaded waku: {result.stdout.strip()}\n"
        "Something under hosted/ reaches waku at import time, however it is "
        "spelled. On the VM that call loads the platform's own .env.")


def test_hosted_ships_in_neither_the_wheel_nor_the_sdist():
    """The extra is on PyPI; the code is not. A contributor who packages
    hosted/ by accident hands every `pip install waku-agent` a copy of the
    platform's deployment."""
    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = build["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["waku"], "the wheel ships waku/ and nothing else"
    exclude = targets["sdist"]["exclude"]
    assert "/hosted" in exclude, (
        'add "/hosted" to the sdist excludes in pyproject.toml')
    assert "hosted" not in exclude, (
        'the sdist exclude must be "/hosted", anchored at the repo root: an '
        'unanchored "hosted" would also drop evals/deterministic/hosted/, '
        "which has to ship so a run from the sdist can skip it rather than "
        "fail on a missing directory")


def test_the_hosted_extra_holds_only_what_hosted_needs():
    """AGENTS.md hard rule 3. The default install stays stdlib plus the
    Anthropic and OpenAI clients; these two are the hosted services' own."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extra = project["optional-dependencies"]["hosted"]
    assert sorted(name.split(">=")[0] for name in extra) == ["PyJWT[crypto]", "aiohttp"]
    defaults = [name.split(">=")[0].split("[")[0] for name in project["dependencies"]]
    assert "aiohttp" not in defaults and "PyJWT" not in defaults
