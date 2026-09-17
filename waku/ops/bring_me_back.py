"""`python -m waku bring-me-back [project]` — reconstruct a project's state from its map.

Reads a project harness's `project.toml` (the knowledge map) and answers the five
questions a returning founder asks: what was I trying to accomplish, where did I
leave it, what changed while I was away, which decisions are unresolved, and what
are the three most consequential next things.

Deterministic and stdlib-only: it stats the files the map points at, compares
their mtime against each entry's `verified` date, and ranks by status. No model,
no network, no key — so it can run the moment you sit down, before any brain is
configured. See docs/project-harness.md.
"""

from __future__ import annotations

import sys
import tomllib
from datetime import date, datetime, time
from pathlib import Path

CONFIG = "project.toml"


def _load(project: Path) -> dict:
    path = project / CONFIG
    if not path.is_file():
        raise SystemExit(
            f"no {CONFIG} in {project} — run `waku new-project <name>`, or point "
            "at a project harness.")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _changed_since_verified(entry: dict, project: Path) -> bool:
    """True when the file an entry points at changed after its `verified` date
    (or the referenced file is missing — drift either way)."""
    p = (entry.get("path") or "").strip()
    if not p or p == "none":
        return False
    f = project / p
    if not f.exists():
        return True
    if not f.is_file():
        return False   # a directory (e.g. reviews/): existence is the only check
    verified = entry.get("verified")
    if not verified:
        return False
    try:
        d = date.fromisoformat(str(verified))
    except ValueError:
        return False
    return f.stat().st_mtime > datetime.combine(d, time.min).timestamp()


def bring_me_back(project: Path) -> str:
    cfg = _load(project)
    entries = cfg.get("map", [])
    title = cfg.get("title") or cfg.get("name") or "project"
    thesis = (cfg.get("thesis") or "").strip()

    open_ = [e for e in entries if e.get("status") == "open"]
    drifted = [e for e in entries if _changed_since_verified(e, project)]
    waiting = [e for e in entries
               if e.get("authority") == "proposal" and e.get("status") in ("open", "deferred")]

    out = [f"# {title} — bring me back", ""]
    if thesis:
        out += [f"**Thesis:** {thesis}", ""]

    out.append("## Where you left it")
    by_status: dict[str, list[dict]] = {}
    for e in entries:
        by_status.setdefault(str(e.get("status", "?")), []).append(e)
    for status in ("established", "open", "deferred", "hypothesis", "stale"):
        if status in by_status:
            names = ", ".join(e.get("id", "?") for e in by_status[status])
            out.append(f"- {status}: {names}")
    out.append("")

    out.append("## What changed while you were away")
    if drifted:
        for e in drifted:
            out.append(f"- `{e.get('path')}` changed since verified ({e.get('verified')})")
    else:
        out.append("- nothing drifted since last verified")
    out.append("")

    out.append("## Unresolved decisions")
    seen: set[str] = set()
    resolved_any = False
    for e in open_ + waiting:
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        resolved_any = True
        note = f": {e.get('note')}" if e.get("note") else ""
        out.append(f"- `{e.get('id')}` ({e.get('status')}){note}")
    if not resolved_any:
        out.append("- none")
    out.append("")

    out.append("## Three most consequential next things")
    ranked = [("resolve", e) for e in open_] + \
             [("re-verify", e) for e in drifted] + \
             [("authorize", e) for e in waiting]
    seen_ids: set[str] = set()
    top: list[tuple[str, dict]] = []
    for action, e in ranked:
        if e.get("id") in seen_ids:
            continue
        seen_ids.add(e.get("id"))
        top.append((action, e))
        if len(top) == 3:
            break
    if top:
        for i, (action, e) in enumerate(top, 1):
            note = f" — {e.get('note')}" if e.get("note") else ""
            out.append(f"{i}. {action} `{e.get('id')}`{note}")
    else:
        out.append("- nothing outstanding — the project is quiet")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    project = Path(args[0]).expanduser().resolve() if args else Path.cwd()
    print(bring_me_back(project))


if __name__ == "__main__":
    main()
