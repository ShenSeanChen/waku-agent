"""DETERMINISTIC EVAL -- the two build contexts are allowlists, and what they
admit.

THIS IS A DRIFT CHECK, NOT THE GUARD. It reads two .dockerignore files as
text. The guard that a secret does not enter the image is
evals/hosted_docker/test_image.py::
test_a_secret_planted_inside_an_admitted_tree_stays_out_of_the_image, which
plants a secret-shaped file inside an admitted tree, builds, and looks -- the
only test in this task that fails if the ignore file is deleted, since the
tenant Dockerfile COPYs named paths and its /app listing does not change.
test_the_tenant_image_holds_what_it_copies_and_nothing_more, in the same file,
is the guard on the COPY list. A test that reads the exclude
config instead of the artefact is the defect this spec has found five times,
most recently as "a packaging test asserting an exclude string instead of the
built artefact"; naming which of the two this is, in the file itself, is how
the next reader knows not to trust it alone.

What it is FOR: the Docker tier needs a daemon and does not run in `make gate`
or on a contributor's PR. This one runs everywhere, offline, in under a
millisecond, and catches the one mistake that is easy to make and expensive to
find later -- adding a deny line (`.env`) to a file whose whole design is
allow lines, which reads as tightening and is in fact loosening, because the
next secret is not called .env.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMAGE = ROOT / "hosted" / "image"

# Spec, "Images": "The tenant image admits only waku/, skills/, pyproject.toml,
# uv.lock, README.md and the license files" -- the two license files being the
# ones pyproject.toml's license-files names at the repo root. "The services
# image admits hosted/, pyproject.toml, uv.lock and the license files".
ADMITTED = {
    "tenant.Dockerfile.dockerignore": {
        "waku/", "skills/", "pyproject.toml", "uv.lock", "README.md",
        "LICENSE", "LICENSE-BRAND",
    },
    "services.Dockerfile.dockerignore": {
        "hosted/", "pyproject.toml", "uv.lock", "LICENSE", "LICENSE-BRAND",
    },
}

# The re-exclusions that reach INSIDE the admitted trees, in the order they
# must appear. Order is not decoration: Docker is last-match-wins, so a
# re-exclusion above the `!` line that admits its tree is re-admitted by that
# line and does nothing at all. A review defeated the first version of this
# file twice with all four tests green -- once by moving these four lines
# above the admissions, once by deleting them -- so the sequence is pinned
# here, not just the membership.
#
# The last four are secrets, and they are the reason this list is not only
# about build noise. The allowlist admits whole trees; without these, a
# waku/.env or a skills/deploy.pem written by somebody debugging enters the
# build context and the tenant image, invisible to an allowlist that only
# names top-level entries. evals/hosted_docker/test_image.py::
# test_a_secret_planted_inside_an_admitted_tree_stays_out_of_the_image is the
# guard that drives it; this is the drift check on its shape.
RE_EXCLUDED_INSIDE = (
    "**/__pycache__/",
    "**/*.pyc",
    "**/.pytest_cache/",
    "**/.ruff_cache/",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
)


def _lines(name: str) -> list[str]:
    text = (IMAGE / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_both_ignore_files_exclude_everything_first():
    """An allowlist starts by refusing everything. If `*` is not the first
    rule, every later `!` line is decoration: the default is already admit."""
    for name in ADMITTED:
        lines = _lines(name)
        assert lines[0] == "*", (
            f"{name} does not begin with `*`, so it is not an allowlist and its "
            f"`!` lines admit nothing that was not already admitted. First "
            f"line: {lines[0]!r}")
        assert ".*" in lines[:2], (
            f"{name} must also exclude `.*`. Docker's matcher is not a shell "
            "glob and `*` is documented to match dot-prefixed names, but the "
            "file that keeps .env out of an image should not rest on that.")


def test_the_admitted_set_is_the_specs_set():
    for name, expected in ADMITTED.items():
        admitted = {line[1:] for line in _lines(name) if line.startswith("!")}
        assert admitted == expected, (
            f"{name} admits {sorted(admitted)}, the spec says {sorted(expected)}.\n"
            "Widening this is a spec change, not a build fix.")


def test_neither_image_can_see_the_other_side():
    """The half of the waku/ <-> hosted/ boundary that
    evals/deterministic/test_hosted_boundary.py names as out of its reach --
    its docstring says a Dockerfile 'can run `python -c "import waku..."` with
    nothing here to stop them'.

    It is closed here by the CONTEXT, not by forbidding a line: with waku/
    outside the services image's context, such a RUN fails the build loudly.
    """
    tenant = {line[1:] for line in _lines("tenant.Dockerfile.dockerignore")
              if line.startswith("!")}
    services = {line[1:] for line in _lines("services.Dockerfile.dockerignore")
                if line.startswith("!")}
    assert "hosted/" not in tenant, "the tenant image's context admits hosted/"
    assert "waku/" not in services, "the services image's context admits waku/"


def test_the_re_exclusions_follow_the_admissions_and_are_all_there():
    """Docker is LAST-MATCH-WINS, so a `**/` re-exclusion only re-excludes
    anything if it comes after the `!` line that admitted its tree.

    Both halves are load-bearing, and a review proved it by defeating the
    earlier version of this file twice while every test stayed green:

      move the four `**/` lines above the seven `!` lines -> __pycache__,
      *.pyc, .pytest_cache and .ruff_cache are re-admitted into the image, and
      `ls -a /app` does not change, so the Docker tests stay green too

      delete the four `**/` lines -> the same effect, one fewer edit

    So this pins the sequence AND the exact list. Adding a re-exclusion is a
    one-line change here; moving one is not a change anybody makes by accident.
    """
    for name in ADMITTED:
        lines = _lines(name)[2:]
        admissions = [i for i, line in enumerate(lines) if line.startswith("!")]
        re_exclusions = [i for i, line in enumerate(lines) if line.startswith("**/")]
        assert admissions, f"{name} admits nothing"
        assert re_exclusions, (
            f"{name} re-excludes nothing inside the trees it admits. The four "
            "build-noise lines and the four secret lines are not decoration: "
            "without them __pycache__ and a planted waku/.env ride into the "
            "image inside an admitted tree.")
        assert min(re_exclusions) > max(admissions), (
            f"{name} has a `**/` re-exclusion at line {min(re_exclusions) + 3} "
            f"before its last `!` admission at line {max(admissions) + 3}. "
            "Docker is last-match-wins, so that admission re-admits everything "
            "the re-exclusion just excluded, and the re-exclusion does nothing.")
        found = tuple(lines[i] for i in re_exclusions)
        assert found == RE_EXCLUDED_INSIDE, (
            f"{name} re-excludes {list(found)}, this file expects "
            f"{list(RE_EXCLUDED_INSIDE)}.\n"
            "Dropping one of these re-admits it into an admitted tree; the "
            "four secret patterns are the ones that matter.")


def test_no_deny_line_hides_among_the_allow_lines():
    """After `*` and `.*`, every line is either an admission (`!...`) or a
    re-exclusion INSIDE an admitted tree (`**/...`). A bare `xyz` line -- the
    natural shape of `.env` added by somebody being careful -- is neither, and
    it is how an allowlist turns back into a denylist one line at a time."""
    for name in ADMITTED:
        stray = [line for line in _lines(name)[2:]
                 if not line.startswith("!") and not line.startswith("**/")]
        assert not stray, (
            f"{name} has deny lines among the allow lines: {stray}\n"
            "This file excludes everything already. Naming one more thing to "
            "exclude does nothing, and it teaches the next reader that the "
            "file is a denylist, which is when a secret gets in.")
