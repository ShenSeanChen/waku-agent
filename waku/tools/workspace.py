"""A dated, self-documenting workspace for delegated coding.

pi (via delegate_task) writes REAL files. Without a home they vanish in a temp
dir — so each scratch delegation instead lands in

    <WAKU_WORKSPACE>/<YYYY-MM-DD>/<HHMMSS>-<model>-<slug>/
        <the files pi wrote>
        MANIFEST.md      date, model, the task, files created, the auto-run result
        run.log          stdout/exit of the auto-run
        pi-transcript.log

so a coding run is traceable, not a mystery temp dir. Code artifacts are
DELIVERABLES, not agent state (memory / calendar / db), so this deliberately
lives OUTSIDE .waku and is git-ignored — it never pollutes the agent's real
state or the repo.

Auto-run: after pi finishes, the entry script is run (headless, captured, with a
timeout) and the result is written to the manifest AND returned to the loop — so
the model sees whether its own code actually ran, and can react.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKSPACE_ENV = "WAKU_WORKSPACE"          # root dir; default ./waku_workspace
AUTORUN_ENV = "WAKU_DELEGATE_AUTORUN"     # "0"/"false"/"no" to disable auto-run
RUN_TIMEOUT = int(os.getenv("WAKU_AUTORUN_TIMEOUT", "30"))
_OURS = {"MANIFEST.md", "run.log", "pi-transcript.log"}
_ENTRY_PREFS = ("main.py", "app.py", "run.py", "game.py")


def workspace_root() -> Path:
    return Path(os.getenv(WORKSPACE_ENV, "waku_workspace")).expanduser().resolve()


def _slug(text: str, n: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:n]) or "task"


def new_run_folder(model: str, task: str, now: datetime | None = None) -> Path:
    """Create and return a fresh dated run folder for one delegation."""
    now = now or datetime.now()
    name = f"{now.strftime('%H%M%S')}-{_slug(model, 2)}-{_slug(task)}"
    folder = workspace_root() / now.strftime("%Y-%m-%d") / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def created_files(folder: Path) -> list[Path]:
    """Everything pi wrote in `folder` — excluding our own manifest/logs and
    __pycache__ — newest first."""
    files = [p for p in folder.rglob("*")
             if p.is_file() and p.name not in _OURS and "__pycache__" not in p.parts]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _pick_entry(files: list[Path]) -> Path | None:
    py = [p for p in files if p.suffix == ".py"]
    if not py:
        return None
    by_name = {p.name: p for p in py}
    for pref in _ENTRY_PREFS:
        if pref in by_name:
            return by_name[pref]
    with_main = [p for p in py if "__main__" in p.read_text(encoding="utf-8", errors="ignore")]
    if with_main:
        return with_main[0]
    return py[0] if len(py) == 1 else None


def autorun(folder: Path) -> tuple | None:
    """Run the entry .py in `folder` (headless, captured, timeout). Returns
    (entry_name, exit_code, output, seconds) — exit_code None means it was still
    running (likely interactive) and got stopped. None if nothing runnable or
    auto-run is disabled."""
    if os.getenv(AUTORUN_ENV, "1") in ("0", "false", "no"):
        return None
    entry = _pick_entry(created_files(folder))
    if entry is None:
        return None
    t0 = time.perf_counter()
    try:
        r = subprocess.run([sys.executable, entry.name], cwd=folder,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT, check=False)
        out = (r.stdout + r.stderr).strip()
        result = (entry.name, r.returncode, out, round(time.perf_counter() - t0, 1))
    except subprocess.TimeoutExpired:
        result = (entry.name, None,
                  f"(still running after {RUN_TIMEOUT}s — likely interactive; stopped)",
                  RUN_TIMEOUT)
    except OSError as exc:
        result = (entry.name, -1, f"couldn't launch: {exc}", 0.0)
    (folder / "run.log").write_text(
        f"$ python3 {result[0]}\nexit: {result[1]}\n\n{result[2]}\n", encoding="utf-8")
    return result


def write_manifest(folder: Path, provider: str, model: str, task: str,
                   files: list[Path], run: tuple | None) -> None:
    """Document the run: date (from the folder), model, task, files, run result."""
    when = f"{folder.parent.name} {folder.name.split('-')[0]}"
    lines = [f"# Delegated coding run — {when}", "",
             f"- Model: `{provider}:{model}`",
             f"- Task: {task}", "", "## Files created"]
    lines += [f"- `{p.relative_to(folder)}` ({p.stat().st_size} bytes)" for p in files] or ["- (none)"]
    if run is not None:
        entry, code, out, secs = run
        status = "still running (interactive?)" if code is None else f"exit {code}"
        lines += ["", f"## Auto-run: `python3 {entry}` — {status} in {secs}s", "",
                  "```", out[:2000], "```"]
    (folder / "MANIFEST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
