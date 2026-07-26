"""Release gate — the diamond before "Release" on the whiteboard.

Changed the prompt? Swapped the model? Tuned retrieval top-k? Run the gate:

    python -m waku.ops.release_gate     (or: make gate)

Deterministic evals must pass 100% — they are unit tests; one failure blocks.
Judge evals run when a key is present and report scores. Exit code 0 = ship.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # the key check below must see .env, same as the app does

REPO = Path(__file__).resolve().parents[2]


def run(suite: str) -> tuple[int, dict]:
    """Run a pytest suite; return (exit_code, {passed, failed}). Counts come
    from the -q summary line — zero extra deps; 0/0 on a miss is honest."""
    print(f"\n=== {suite} ===")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(REPO / "evals" / suite)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    print(proc.stdout, end="")
    print(proc.stderr, end="", file=sys.stderr)
    counts = {k: (int(m.group(1)) if (m := re.search(rf"(\d+) {k}", proc.stdout)) else 0)
              for k in ("passed", "failed")}
    return proc.returncode, counts


def report(deterministic: str, judge: str, suites: dict | None = None) -> None:
    """Persist the latest verdict AND append it to the run history."""
    import json
    from datetime import datetime

    from waku.config import load_settings

    settings = load_settings()
    settings.ensure_home()
    record = {
        "deterministic": deterministic,
        "judge": judge,
        "suites": suites or {},
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (settings.home / "eval_report.json").write_text(json.dumps(record))
    with (settings.home / "eval_runs.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    suites = {}
    code, suites["deterministic"] = run("deterministic")
    if code:
        report("fail", "not run", suites)
        print("\nGATE CLOSED — deterministic evals failed. Fix before releasing.")
        sys.exit(1)

    # judge needs the ACTIVE provider's key (anthropic, openrouter, ...), same
    # rule as evals/helpers.HAS_KEY
    from waku.config import load_settings
    from waku.loop.models import PROVIDERS

    settings = load_settings()
    provider = PROVIDERS.get(settings.provider)
    if settings.api_key or (provider and os.getenv(provider.key_env)):
        code, suites["judge"] = run("judge")
        if code:
            report("pass", "fail", suites)
            print("\nGATE CLOSED — judge scores below threshold.")
            sys.exit(1)
        report("pass", "pass", suites)
    else:
        report("pass", "skipped", suites)
        print(f"\n(judge suite skipped: no API key for provider '{settings.provider}')")

    print("\nGATE OPEN — safe to release.")


if __name__ == "__main__":
    main()
