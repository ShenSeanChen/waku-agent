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

4 files under scripts/, 71 under evals/, 2 under lab/ and 1 under examples/
import waku, and evals/conftest.py already puts the repo root on sys.path, so
every one of them is reachable from hosted/. That is the shape group A's route
guard lost with fourteen times across five rounds before it was inverted.

So the hosted side is inverted. Three layers:

  1. Every import root in hosted/ must be in ALLOWED_IMPORT_ROOTS_IN_HOSTED --
     the standard library, minus the handful of stdlib modules whose job is to
     load arbitrary code, plus "hosted" itself and the two distributions the
     hosted extra declares. Anything else -- waku, scripts, evals, lab,
     examples, a package invented next year -- fails by default.
  2. No dynamic import machinery by name: __import__, exec and eval, which are
     builtins and therefore not import roots at all.
  3. A runtime check: import every module under hosted/ in a fresh interpreter
     and assert "waku" never appears in THAT interpreter's sys.modules. This
     catches import-time dynamic loading that no AST walk can see -- a
     module-scope pkgutil.resolve_name or runpy.run_module call, for one.

  None of the three is a closed set for "however it was spelled, did waku
  actually get loaded" -- layer 3 only ever inspects one process's
  sys.modules, at import time. Two escapes are known and stay open on
  purpose (task-B1-review.md, findings B1-F3 and B1-F4):

    getattr(builtins, "__import__")("waku.config")   # called from INSIDE a
                                                       # function body: every
                                                       # module has already
                                                       # finished importing
                                                       # cleanly by the time
                                                       # that function runs
    subprocess.run([sys.executable, "-c", "import waku.config"])
                                                       # genuinely loads
                                                       # waku.config, in a
                                                       # grandchild process
                                                       # whose sys.modules
                                                       # layer 3 never looks
                                                       # inside

  Both require deliberately routing around a named guard rather than writing
  an ordinary import, so both are out of scope: this is a tripwire against
  accidental drift, not a sandbox against an adversary who already has write
  access to hosted/. Closing them would cost a fourth layer for a threat this
  guard does not claim to cover.

The waku/ -> hosted/ direction stays a denylist. waku/ imports no repo-root
package today, hard rule 5 already forbids two of them, and waku/ genuinely
needs importlib (seven call sites across five files: connect.py,
ops/dashboard.py, ops/commands.py, integrations.py and tools/waku_memory.py),
so the inversion would have to carve exceptions rather than close a hole.

IT DOES NOT LOOK AT STRING LITERALS, on purpose (tasks.md B1): hosted/ names
"waku" as an image tag, a container name, a Unix user and a directory, and a
guard that failed on those would be turned off within a week.

IT READS *.py ONLY. hosted/deploy/*.sh and hosted/image/*.Dockerfile arrive in
groups C and F and can run `python -c "import waku..."` with nothing here to
stop them. That is C and F's problem to carry, and it is written down here so
neither group discovers it as a surprise. A *.py file does not need to be a
script or a Dockerfile to do the same thing: the module-scope subprocess
escape named above is plain Python, under hosted/, and still gets through --
see the note on layer 3.

The test skips itself when hosted/ is absent, because the sdist ships evals/
and not hosted/.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WAKU = ROOT / "waku"
HOSTED = ROOT / "hosted"
UV = shutil.which("uv")

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
    """The third layer: catches spellings layers 1-2 cannot recognise.

    Whatever the source says, this imports each module under hosted/ in a
    fresh interpreter and asks whether waku ended up in THAT interpreter's
    sys.modules. An import-time pkgutil.resolve_name, runpy.run_module, or a
    module-scope getattr(builtins, "__import__")(...) fails here even if it
    somehow read as legal above.

    It is not a closed set. A module-scope subprocess.run that shells out to
    `python -c "import waku.config"` loads waku.config for real, in a
    grandchild process whose sys.modules this loop never inspects; and a
    getattr(builtins, "__import__")(...) called from inside a function body
    runs after this loop has already finished importing every module
    cleanly. Both pass all six tests in this file (task-B1-review.md,
    findings B1-F3 and B1-F4) -- closing them is out of scope for a guard
    whose job is to catch accidental drift, not deliberate evasion.
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
        f"importing a hosted/ module raised instead of completing:\n{result.stderr[-2000:]}\n"
        "A nonzero exit here is itself a boundary finding, not an unrelated bug to "
        "triage: a relative import that walks past hosted/'s own top-level package "
        '(for example `from ...waku import config`) raises ImportError here rather '
        "than ever reaching the LEAKED check below. Read the traceback above for "
        "what the offending import actually named.")
    assert result.stdout.strip() == "LEAKED []", (
        f"importing hosted/ loaded waku: {result.stdout.strip()}\n"
        "Something under hosted/ reaches waku at import time, however it is "
        "spelled. On the VM that call loads the platform's own .env.")


@pytest.mark.skipif(
    UV is None, reason="uv not on PATH -- install it to build and inspect the packaged artifacts")
def test_hosted_and_lab_never_ship_in_the_wheel_or_sdist(tmp_path):
    """The extra is on PyPI; the code is not. A contributor who packages
    hosted/ by accident hands every `pip install waku-agent` a copy of the
    platform's deployment.

    An earlier version of this test read pyproject.toml as text and asserted
    the string "/hosted" was present in the sdist's exclude list. That is a
    claim about the configuration, not about what `pip install waku-agent`
    actually receives, and in this repository the two had already come
    apart: the neighbouring entry "lab", three items earlier in the very
    same exclude list, did NOT keep lab/ out of the sdist. hatchling merges
    .gitignore's patterns with pyproject.toml's `exclude` list into one
    ordered pathspec, and the .gitignore lines that un-ignore two
    directories' committed board diagrams for lab/ (`!lab/kimi-k3/**/*.excalidraw`,
    `!lab/pi-agent/**/*.excalidraw` -- the lab/README.md convention of
    committing screenshots, not board sources) survived a bare "lab" exclude
    entry: only a pattern naming the directory's CONTENTS ("/lab/**") reliably
    re-excludes what an earlier negation un-ignored; a bare directory name does
    not. "/hosted" happened to work because nothing in .gitignore negates a
    path under hosted/ -- it was correct by neighbourhood, not by anything the
    old assertions checked (task-B1-review.md, finding B1-F1).

    So: build both real artifacts with `uv build` and look inside them,
    instead of reading intent off the config that is supposed to produce it.
    """
    out = tmp_path / "dist"
    result = subprocess.run(
        [UV, "build", "--sdist", "--wheel", "--out-dir", str(out), str(ROOT)],
        capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, f"uv build failed:\n{result.stderr[-4000:]}"

    sdist_path = next(out.glob("*.tar.gz"))
    wheel_path = next(out.glob("*.whl"))

    with tarfile.open(sdist_path) as tf:
        sdist_names = tf.getnames()
    with zipfile.ZipFile(wheel_path) as zf:
        wheel_names = zf.namelist()

    # The sdist wraps every path in a "<name>-<version>/" prefix; the first
    # real path component is what a repo-root directory would appear as.
    sdist_top_dirs = {n.split("/", 2)[1] for n in sdist_names if n.count("/") >= 1}
    # The wheel has no such prefix -- "waku/..." and "waku_agent-*.dist-info/..."
    # sit at the top directly.
    wheel_top_dirs = {n.split("/", 1)[0] for n in wheel_names}

    assert "hosted" not in sdist_top_dirs, (
        f"the sdist ships a top-level hosted/ directory: "
        f"{sorted(n for n in sdist_names if '/hosted/' in n)}")
    assert "lab" not in sdist_top_dirs, (
        f"the sdist ships a top-level lab/ directory: "
        f"{sorted(n for n in sdist_names if '/lab/' in n)}")
    assert "hosted" not in wheel_top_dirs, f"the wheel ships a hosted/ directory: {wheel_top_dirs}"
    assert "lab" not in wheel_top_dirs, f"the wheel ships a lab/ directory: {wheel_top_dirs}"

    dist_info_dirs = {d for d in wheel_top_dirs if d.endswith(".dist-info")}
    assert wheel_top_dirs == {"waku"} | dist_info_dirs, (
        f"the wheel ships more than waku/ and its dist-info: {wheel_top_dirs}")

    # The anchoring must not over-exclude either: evals/deterministic/hosted/
    # holds only the skip conftest (B2-B4's own tests never land in the
    # sdist, since hosted/ itself does not), and it has to ship so a test run
    # from an unpacked sdist skips instead of failing on a missing directory.
    assert any(n.endswith("evals/deterministic/hosted/conftest.py") for n in sdist_names), (
        "the sdist must still ship evals/deterministic/hosted/conftest.py -- "
        'the "/hosted" exclude is anchored at the repo root precisely so it '
        "does not also drop this file")


def test_the_hosted_extra_holds_only_what_hosted_needs():
    """AGENTS.md hard rule 3. The default install stays stdlib plus the
    Anthropic and OpenAI clients; these two are the hosted services' own."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extra = project["optional-dependencies"]["hosted"]
    assert sorted(name.split(">=")[0] for name in extra) == ["PyJWT[crypto]", "aiohttp"]
    defaults = [name.split(">=")[0].split("[")[0] for name in project["dependencies"]]
    assert "aiohttp" not in defaults and "PyJWT" not in defaults
