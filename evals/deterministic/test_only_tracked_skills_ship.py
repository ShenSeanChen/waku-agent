"""DETERMINISTIC EVAL — the wheel must not ship a skill that isn't in git.

Caught while building 0.1.3. The wheel contained four skills:

    meeting-prep · schedule-meeting · weekly-brief · sean-ai-stories-youtube-title

The last one is the maintainer's personal YouTube-title skill. It was never
committed — it just happened to be sitting in `skills/` — and it would have gone
to PyPI, where every installer would have received it.

The cause is that `[tool.hatch.build.targets.wheel.force-include]` copies the
directory off DISK. Hatchling honours .gitignore for ordinary includes and
bypasses it for force-include, which is what makes the bundled skills ship at
all (they live outside the package). So the build has no idea what is tracked.

That is worth guarding for reasons beyond one stray folder: a half-finished
skill, a scratch copy, or anything containing personal notes is one `python -m
build` away from being published, and PyPI uploads are permanent.

A LIMITATION WORTH STATING: CI checks out a clean tree, so there are no
untracked files there and this test always passes on CI. It earns its keep on
the MAINTAINER'S machine, which is the only place the wheel is built and the
only place the problem can exist. Run the suite before you build — that is the
whole protection.

Personal skills belong in `~/.waku/skills/`, which waku already reads and which
is nowhere near the package.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills"


def _tracked_skill_dirs() -> set[str]:
    out = subprocess.run(["git", "ls-files", "skills/"], cwd=REPO,
                         capture_output=True, text=True, check=False).stdout
    return {line.split("/")[1] for line in out.split() if line.count("/") >= 1}


def _on_disk_skill_dirs() -> set[str]:
    return {p.name for p in SKILLS.iterdir() if p.is_dir() and not p.name.startswith(".")}


def test_every_skill_on_disk_is_tracked():
    """THE regression. force-include copies whatever is in skills/ at build
    time, so an untracked folder here is a folder on PyPI."""
    untracked = _on_disk_skill_dirs() - _tracked_skill_dirs()
    assert untracked == set(), (
        f"skills/ contains untracked director{'y' if len(untracked) == 1 else 'ies'}: "
        f"{sorted(untracked)}.\n\n"
        "pyproject force-includes this whole folder into the wheel, so building "
        "a release right now would publish it to PyPI — permanently.\n\n"
        "If it is a personal skill, move it to ~/.waku/skills/ (waku reads that "
        "too, and it is nowhere near the package). If it belongs to the project, "
        "commit it."
    )


def test_no_untracked_files_inside_a_tracked_skill():
    """Same trap one level down: a committed skill folder holding an
    uncommitted draft or a notes file still ships every byte."""
    out = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "skills/"],
                         cwd=REPO, capture_output=True, text=True, check=False).stdout.split()
    assert out == [], (
        f"untracked files under skills/ would be published: {out}\n"
        "Commit them or move them out — force-include does not consult git."
    )


def test_the_build_still_force_includes_skills():
    """The guard above only matters while the wheel actually copies this folder.
    If force-include goes away, the bundled skills stop shipping entirely — the
    bug this repo already had once, where a pip install arrived with zero
    procedural memory. Pinned so the two facts stay connected."""
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("force-include", {}).get("skills") == "waku/skills"
