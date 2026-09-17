"""The knowledge map, read and written — the two wires that make a harness a brain.

A harness's `project.toml [[map]]` is addressable knowledge (docs/project-harness.md
§4). Three things happen to it here, and nowhere else:

  knowledge_context(harness) — RENDER the map into a role's prompt: the index
      (every entry with its authority and status), then the bodies of the
      authoritative entries (source-of-truth, decided) up to a character budget.
      Authority is explicit in the prompt exactly as it is in the map, so a
      proposal can never read as doctrine.

  record_decision(harness, ...) — WRITE a deliberation's verdict back: a file
      under decisions/ and a [[map]] entry with authority = "proposal",
      status = "open". The owner promotes it, or not. This is the only way the
      map grows from machine work, and the machine never grants authority to
      its own output.

  downstream(entries, ids) — walk the dependency edges (depends_on /
      depended_on_by) transitively, so reconsider names everything a falsified
      assumption touches, not only its direct dependents.

Stdlib only. tomllib reads; the record step APPENDS a block as text (tomllib
does not write, and a harness is a hand-edited file — appending keeps the
owner's layout) and re-parses to prove the file is still valid.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import date
from pathlib import Path

CONFIG = "project.toml"

# Least → most subordinate. Bodies are inlined for the first two only.
AUTHORITY_RANK = {"source-of-truth": 0, "decided": 1, "working-model": 2,
                  "proposal": 3, "history": 4}
INLINE_AUTHORITY = ("source-of-truth", "decided")

AUTHORITY_RULE = ("Authority is explicit: source-of-truth and decided entries bind; "
                  "working-model is subordinate to them; proposal is not authorized; "
                  "history has no standing. A proposal never overrides a source-of-truth.")


def load_map(harness: str | Path) -> tuple[dict, list[dict]]:
    """(config, map entries) from a harness's project.toml; ({}, []) when absent."""
    path = Path(harness) / CONFIG
    if not path.is_file():
        return {}, []
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    return cfg, list(cfg.get("map", []))


def _file_of(entry: dict, harness: Path) -> Path | None:
    """The file an entry points at (a `file#section` anchor → the file), or None
    for a recorded claim (`none`), a directory, or a missing path."""
    p = (entry.get("path") or "").strip()
    if not p or p == "none":
        return None
    f = harness / p.split("#", 1)[0].strip()
    return f if f.is_file() else None


def _sorted(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (AUTHORITY_RANK.get(e.get("authority"), 9),
                                          str(e.get("id", ""))))


def knowledge_context(harness: str | Path, budget_chars: int | None = None) -> str:
    """The map rendered for a role's prompt, or '' with no harness / no map.

    Index first (every entry, sorted by authority), then the authoritative
    bodies within `budget_chars` (default WAKU_HARNESS_CONTEXT_CHARS, 60000).
    A body that does not fit is truncated with a pointer to the file, so a
    coding-agent role running in the harness directory can read the rest."""
    if not harness:
        return ""
    harness = Path(harness)
    cfg, entries = load_map(harness)
    if not entries:
        return ""
    budget = (budget_chars if budget_chars is not None
              else int(os.getenv("WAKU_HARNESS_CONTEXT_CHARS", "60000")))

    title = cfg.get("title") or cfg.get("name") or "project"
    out = [f"Project knowledge map — {title}."]
    if cfg.get("thesis"):
        out.append(f"Thesis: {cfg['thesis']}")
    out += [AUTHORITY_RULE, ""]
    for e in _sorted(entries):
        line = (f"- {e.get('id', '?')} [{e.get('authority', '?')} / "
                f"{e.get('status', '?')}] {e.get('path', 'none')}")
        if e.get("note"):
            line += f" — {e['note']}"
        out.append(line)

    # Bodies: one per file (a `file#section` entry shares its file), in
    # authority order. The budget is SHARED — each file gets an equal slice and
    # the slack goes to whoever was cut, in authority order — so one long
    # source-of-truth cannot crowd a short decided record out of the prompt.
    seen: set[Path] = set()
    bodies: list[tuple[str, str, str]] = []   # (relative path, authority, text)
    for e in _sorted(entries):
        if e.get("authority") not in INLINE_AUTHORITY:
            continue
        f = _file_of(e, harness)
        if f is None or f in seen:
            continue
        seen.add(f)
        bodies.append((str(f.relative_to(harness)), str(e.get("authority")),
                       f.read_text(encoding="utf-8", errors="replace")))
    if bodies:
        share = budget // len(bodies)
        alloc = [min(len(t), share) for _, _, t in bodies]
        slack = budget - sum(alloc)
        for i, (_, _, t) in enumerate(bodies):
            extra = min(slack, len(t) - alloc[i])
            alloc[i] += extra
            slack -= extra
        for (rel, authority, text), room in zip(bodies, alloc):
            f = harness / rel
            if room <= 0:
                out.append(f"\n[{rel} omitted: context budget exhausted — read it at {f}]")
                continue
            if len(text) > room:
                text = text[:room] + f"\n[truncated — the full text is at {f}]"
            out += ["", f"=== {rel} ({authority}) ===", text]
    return "\n".join(out)


def _slug(text: str, words: int = 6) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())[:words]
    return "-".join(parts) or "decision"


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def record_decision(harness: str | Path, task: str, decision: str, mode: str = "design",
                    unavailable: dict | None = None) -> Path:
    """Write a deliberation's outcome under `<harness>/decisions/` and append a
    `[[map]]` proposal entry pointing at it. Returns the decision file's path.

    The entry is `authority = "proposal"`, `status = "open"`, `owner = "harness"`:
    bring-me-back lists it under "unresolved decisions" and "authorize", and the
    owner edits the authority by hand. If the appended TOML fails to re-parse
    the original file is restored and the error raised."""
    harness = Path(harness)
    cfg, _ = load_map(harness)
    if not cfg:
        raise FileNotFoundError(f"no {CONFIG} in {harness} — nothing to record into")
    name = cfg.get("name") or "project"
    today = date.today().isoformat()

    ddir = harness / "decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    stem = f"{today}-{_slug(task)}"
    path, n = ddir / f"{stem}.md", 2
    while path.exists():
        path, n = ddir / f"{stem}-{n}.md", n + 1
    entry_id = f"{name}.decision.{path.stem}"
    call = "Verdict" if mode == "review" else "Decision"

    lines = [f"# {task.strip()[:80]} — deliberation ({mode})", "",
             "| Field | Value |", "| --- | --- |",
             "| Status | **proposal** — awaiting owner promotion |",
             f"| Date | {today} |", f"| Mode | {mode} |",
             "| Recorded by | `waku deliberate` — a machine draft, not a decision |", ""]
    if unavailable:
        lines += ["> **Degraded run:** " +
                  "; ".join(f"{k}: {v}" for k, v in unavailable.items()), ""]
    lines += ["## Task", "", task.strip(), "", f"## {call}", "", decision.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")

    note = f"{call} from waku deliberate, awaiting owner promotion: {task.strip()[:80]}"
    block = "\n".join([
        "", "[[map]]",
        f"id = {_toml_str(entry_id)}",
        'kind = "decision"',
        f"path = {_toml_str('decisions/' + path.name)}",
        'authority = "proposal"',
        'status = "open"',
        'owner = "harness"',
        f'verified = "{today}"',
        f"note = {_toml_str(note)}", ""])
    toml_path = harness / CONFIG
    original = toml_path.read_text(encoding="utf-8")
    sep = "" if original.endswith("\n") else "\n"
    toml_path.write_text(original + sep + block, encoding="utf-8")
    try:
        tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        toml_path.write_text(original, encoding="utf-8")
        raise
    return path


def downstream(entries: list[dict], ids: list[str] | tuple[str, ...]) -> list[str]:
    """Every map id that (transitively) depends on any of `ids`, in breadth-first
    order, seeds excluded. Both edge spellings count: an entry's own
    `depended_on_by`, and any entry whose `depends_on` names it."""
    dependents: dict[str, list[str]] = {}
    for e in entries:
        for d in e.get("depended_on_by", []) or []:
            dependents.setdefault(e.get("id"), []).append(d)
        for up in e.get("depends_on", []) or []:
            dependents.setdefault(up, []).append(e.get("id"))
    seen: set[str] = set(ids)
    order: list[str] = []
    queue = list(ids)
    while queue:
        cur = queue.pop(0)
        for nxt in dependents.get(cur, []):
            if nxt and nxt not in seen:
                seen.add(nxt)
                order.append(nxt)
                queue.append(nxt)
    return order
