"""Skill installer — try anyone's skill in one command.

    python -m waku skill install https://github.com/<user>/<repo>/blob/main/skills/foo/SKILL.md
    python -m waku skill install https://gist.github.com/<user>/<id>

Downloads the SKILL.md, validates the frontmatter (same check CI runs on
community PRs), and drops it in WAKU_HOME/skills/<name>/ where the loader
picks it up on next start. Skills are markdown — read what you install.
"""

from __future__ import annotations

import urllib.request

from waku.config import load_settings
from waku.memory.procedural.loader import _parse


def _raw_url(url: str) -> str:
    """Turn common GitHub/Gist page URLs into raw-content URLs."""
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    if "gist.github.com" in url and not url.endswith("/raw"):
        return url.rstrip("/") + "/raw"
    return url


def install(url: str) -> None:
    raw = _raw_url(url)
    print(f"Fetching {raw}")
    with urllib.request.urlopen(raw, timeout=15) as response:
        text = response.read().decode("utf-8", errors="replace")

    settings = load_settings()
    settings.ensure_home()
    tmp = settings.home / "skills" / "_incoming" / "SKILL.md"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")

    skill = _parse(tmp)
    if skill is None:
        tmp.unlink()
        raise SystemExit(
            "Invalid skill: SKILL.md needs YAML frontmatter with `name` and `description`. "
            "See skills/TEMPLATE.md in the repo."
        )

    dest = settings.home / "skills" / skill.name / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    tmp.parent.rmdir()
    print(f"Installed '{skill.name}' → {dest}")
    print(f"  {skill.description}")
    print("It loads next time Waku starts. Read it first — skills are instructions.")
