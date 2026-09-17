"""`python -m waku new-project <name>` — stamp a project harness skeleton.

One kernel, many harnesses (see docs/project-harness.md). This command stamps
the greenfield skeleton for ONE project: the directory layout plus a
`project.yaml` seeded with the schema's identity + authority fields and an
empty knowledge map. It does not copy the project's knowledge in — the map is
filled as the project accrues (or, for an existing project, seeded by hand to
point INTO the project's own files, so nothing gets moved).

Nothing here is project-specific: the name/thesis/owner are the caller's.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SKELETON = ["doctrine", "knowledge", "architecture", "decisions", "plans",
            "experiments", "operations", "risk", "agents", "skills", "evals",
            "workflows", "memory"]

PROJECT_YAML = """\
name: {name}
title: {title}
owner: {owner}
thesis: ""                # one line: what this project is and why it exists
kernel: compatible-any    # a kernel version this harness requires
created: {date}
updated: {date}

authority:               # the ladder (docs/project-harness.md §5)
  default: recommend
  irreversible: []       # tools that never run without the owner, e.g. [settle, prod_deploy]

map: []                  # the knowledge map (docs/project-harness.md §4)
watches: []              # assumptions to monitor (docs/project-harness.md §6)
"""

README = """# {title} — project harness

This directory is the **project harness** for {title}: the institutional brain
and technical management layer that instantiates the shared CTO kernel against
this project. It is a MAP of knowledge, not a copy of it — each entry in
`project.yaml` points to where a piece of consequential knowledge lives and
records its authority, status, and dependencies.

- `project.yaml` — identity, the authority ladder, the knowledge map, the watches.
- The folders hold this project's artifacts. For an existing project, the map in
  `project.yaml` points at the project's real files instead of moving them here.

The full schema lives in `docs/project-harness.md` in the kernel.
"""


def stamp(name: str, dest: str | None = None) -> Path:
    """Create the harness skeleton and return its path. Refuses to overwrite an
    existing `project.yaml` — a harness is a decision record, not a temp file."""
    name = (name or "").strip()
    if not name:
        raise SystemExit("new-project needs a name, e.g. `waku new-project qamata`")
    root = Path(dest).expanduser().resolve() if dest else (Path.cwd() / name)
    yaml_path = root / "project.yaml"
    if yaml_path.exists():
        raise SystemExit(f"{yaml_path} already exists — not overwriting a harness.")
    root.mkdir(parents=True, exist_ok=True)
    for d in SKELETON:
        (root / d).mkdir(exist_ok=True)
        (root / d / ".gitkeep").touch(exist_ok=True)
    yaml_path.write_text(PROJECT_YAML.format(
        name=name, title=name.replace("-", " ").title(),
        owner="", date=date.today().isoformat()), encoding="utf-8")
    (root / "README.md").write_text(README.format(title=name), encoding="utf-8")
    return root


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m waku new-project <name>")
        sys.exit(1)
    root = stamp(args[0])
    print(f"Created project harness at {root}")
    print("  next: fill the thesis + map in project.yaml (see docs/project-harness.md)")


if __name__ == "__main__":
    main()
