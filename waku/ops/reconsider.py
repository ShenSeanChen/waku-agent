"""`python -m waku reconsider [project] [metric=value ...]` — falsified assumptions, reasoned over.

The reconsider step of the learning loop (docs/project-harness.md §6): when an
observation contradicts a watched assumption, this does not merely log a metric
— it names the claim, the observation vs its threshold, everything downstream of
it (`depended_on_by`), and the experiment that decides temporary vs structural.

Deterministic and stdlib-only: it reads the harness's `project.toml [[watches]]`
and evaluates each against the observations you pass. It does NOT mutate the map
— marking items `stale` is a separate, deliberate step the owner makes.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

CONFIG = "project.toml"

_OPS = {"lt": lambda v, t: v < t, "le": lambda v, t: v <= t,
        "gt": lambda v, t: v > t, "ge": lambda v, t: v >= t}
_SYMBOLS = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _load(project: Path) -> dict:
    path = project / CONFIG
    if not path.is_file():
        raise SystemExit(f"no {CONFIG} in {project} — run `waku new-project <name>`.")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def evaluate_watch(watch: dict, observations: dict) -> tuple[bool, float | None]:
    """Return (fired, observed_value). `fired` means the claim no longer holds.
    A watch with no observation for its metric does not fire — insufficient
    evidence is not a falsification."""
    metric = (watch.get("metric") or "").strip()
    if not metric or metric not in observations:
        return False, None
    value = float(observations[metric])
    op = _OPS.get(watch.get("op", "lt"))
    if op is None:
        return False, value
    return (not op(value, float(watch.get("threshold", 0)))), value


def reconsider(project: Path, observations: dict) -> str:
    cfg = _load(project)
    watches = cfg.get("watches", [])
    title = cfg.get("title") or cfg.get("name") or "project"

    if not watches:
        return f"# {title} — reconsider\n\nNo watches recorded — nothing to reconsider."

    fired: list[tuple[dict, float | None]] = []
    for w in watches:
        is_fired, value = evaluate_watch(w, observations)
        if is_fired:
            fired.append((w, value))

    out = [f"# {title} — reconsider", ""]
    for w, value in fired:
        metric = w.get("metric", "?")
        sym = _SYMBOLS.get(w.get("op", "lt"), w.get("op", "lt"))
        claim = w.get("claim") or w.get("id", "?")
        downstream = w.get("depended_on_by", [])
        out += [
            f"## Watch fired: {w.get('id', '?')}",
            f'- Claim: "{claim}"',
            f"- Observed: {metric} = {value} (expected {sym} {w.get('threshold', '?')})",
            "- Our prior assumption no longer holds.",
        ]
        if downstream:
            out.append(f"- Downstream (mark stale): {', '.join(downstream)}")
        out.append("- Proposed experiment: determine whether this is temporary or structural.")
        out.append("")

    out.append(f"{len(fired)} of {len(watches)} watches fired.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    project = Path.cwd()
    if args and "=" not in args[0]:
        project = Path(args[0]).expanduser().resolve()
        args = args[1:]
    observations: dict[str, float] = {}
    for a in args:
        if "=" in a:
            k, _, v = a.partition("=")
            try:
                observations[k] = float(v)
            except ValueError:
                observations[k] = v  # non-numeric observation: keep as-is
    print(reconsider(project, observations))


if __name__ == "__main__":
    main()
