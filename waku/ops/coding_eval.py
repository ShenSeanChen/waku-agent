"""Cross-model coding eval — pi as the fixed harness, the brain swapped underneath.

The agentic battery (evals/dataset.jsonl) scores whether a model drives Waku's
OWN tools. A coding case is a different animal: it hands a real programming job to
**pi** (the same sub-agent `delegate_task` uses), but pointed at the CONTESTANT's
model, then scores by RUNNING the produced code — the `verify` command's exit code
is the verdict, SWE-bench style, not a judge's opinion.

pi natively speaks every provider we pin, so one harness auditions every brain:

    pi --provider <p> --model <m> --api-key <k> -p "<task>"

Waku stays the orchestrator; pi stays the contractor — we just get to compare
contractors. Coding cases live in `evals/coding.jsonl` (separate from the agentic
dataset so they never run through the tool-calling tier by mistake).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from waku.loop.models import PROVIDERS

_CODING = Path(__file__).resolve().parents[2] / "evals" / "coding.jsonl"

# Waku provider id -> pi's built-in provider id (see `pi --list-models`).
PI_PROVIDER = {
    "anthropic": "anthropic", "openai": "openai", "gemini": "google",
    "kimi": "moonshotai", "xai": "xai", "glm": "zai",
    "deepseek": "deepseek", "minimax": "minimax", "openrouter": "openrouter",
}


def load_coding_cases() -> list[dict]:
    """Every coding case in file order; empty list if the file is missing."""
    if not _CODING.exists():
        return []
    lines = _CODING.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def pi_available() -> bool:
    return shutil.which("pi") is not None


def coding_case_for_message(message: str, cases: list[dict] | None = None) -> dict | None:
    """The coding case whose input matches this prompt (trimmed exact match), so
    the arena knows to seed its files + score by its `verify`. None for a
    free-form coding prompt ("build snake and run it") — pi still runs and
    streams, there's just no test to score against."""
    msg = (message or "").strip()
    for c in (cases if cases is not None else load_coding_cases()):
        if (c.get("input") or "").strip() == msg:
            return c
    return None


def run_coding_stream(provider: str, model: str, task: str, files: dict | None,
                      verify: str | None, on_line, timeout: int = 300) -> tuple:
    """Like run_coding_case, but STREAMS pi's stdout line-by-line to `on_line` as
    it works — so the arena can show the terminal doing the job live. Returns
    (passed, why, seconds); `passed` is None when there's no verify (a free-form
    prompt just runs, nothing to score)."""
    pi_bin = shutil.which("pi")
    if not pi_bin:
        on_line("pi is not installed")
        return (False, "pi not installed", 0.0)
    pi_prov = PI_PROVIDER.get(provider)
    if not pi_prov:
        return (False, f"pi has no provider mapping for '{provider}'", 0.0)
    key = _key_for(provider)
    if not key:
        prov = PROVIDERS.get(provider)
        return (False, f"no api key ({prov.key_env if prov else provider})", 0.0)

    workdir = Path(tempfile.mkdtemp(prefix=f"code-{provider}-"))
    for name, content in (files or {}).items():
        (workdir / name).write_text(content, encoding="utf-8")
    on_line(f"$ pi --provider {pi_prov} --model {model} -p …")

    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            [pi_bin, "--provider", pi_prov, "--model", model, "--api-key", key,
             "-p", task, "-a", "--no-session"],
            cwd=workdir, stdin=subprocess.DEVNULL,   # no TTY under the server: pi
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # must not block on stdin
            text=True, bufsize=1, env=os.environ.copy())
    except OSError as exc:
        return (False, f"couldn't launch pi: {exc}", round(time.perf_counter() - t0, 1))

    killer = threading.Timer(timeout, proc.kill)   # watchdog: kill a hung pi
    killer.start()
    try:
        for line in proc.stdout:                    # blocks per line until pi exits
            on_line(line.rstrip("\n"))
        proc.wait()
    finally:
        killer.cancel()
    secs = round(time.perf_counter() - t0, 1)

    if not verify:
        on_line("[done — no test to score]")
        return (None, "ran (no test)", secs)
    try:
        v = subprocess.run(verify, shell=True, cwd=workdir, capture_output=True,
                           text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        on_line("[verify timed out]")
        return (False, "verify timed out", secs)
    if v.returncode == 0:
        on_line("[verify] tests pass")
        return (True, "tests pass", secs)
    tail = (v.stdout or v.stderr).strip().splitlines()
    why = tail[-1][:120] if tail else "tests failed"
    on_line(f"[verify] FAILED — {why}")
    return (False, why, secs)


def _key_for(provider: str) -> str:
    prov = PROVIDERS.get(provider)
    return os.getenv(prov.key_env, "") if prov else ""


def run_coding_case(provider: str, model: str, case: dict,
                    timeout: int = 300) -> tuple[bool, str, float]:
    """Run one coding case on (provider, model) through pi, then score by the
    case's `verify` command. Returns (passed, why, seconds).

    The judgment is the exit code of `verify`, run in the sandbox pi worked in —
    so "passed" means the code actually does what was asked, however the model
    got there. Nothing here trusts pi's prose."""
    pi_bin = shutil.which("pi")
    if not pi_bin:
        return (False, "pi not installed", 0.0)
    pi_prov = PI_PROVIDER.get(provider)
    if not pi_prov:
        return (False, f"pi has no provider mapping for '{provider}'", 0.0)
    key = _key_for(provider)
    if not key:
        prov = PROVIDERS.get(provider)
        return (False, f"no api key ({prov.key_env if prov else provider})", 0.0)

    workdir = Path(tempfile.mkdtemp(prefix=f"code-{provider}-"))
    for name, content in (case.get("files") or {}).items():
        (workdir / name).write_text(content, encoding="utf-8")

    t0 = time.perf_counter()
    try:
        # -a trusts project-local files; --no-session keeps the run ephemeral.
        subprocess.run(
            [pi_bin, "--provider", pi_prov, "--model", model, "--api-key", key,
             "-p", case["input"], "-a", "--no-session"],
            cwd=workdir, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return (False, f"pi timed out after {timeout}s", round(time.perf_counter() - t0, 1))
    except OSError as exc:
        return (False, f"couldn't launch pi: {exc}", round(time.perf_counter() - t0, 1))

    # We DON'T gate on pi's own exit code — a nonzero exit can still have written
    # working code. The verify command is the only judge that matters.
    verify = case.get("verify")
    if not verify:
        return (True, "no verify (ran clean)", round(time.perf_counter() - t0, 1))
    try:
        v = subprocess.run(verify, shell=True, cwd=workdir, capture_output=True,
                           text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return (False, "verify timed out", round(time.perf_counter() - t0, 1))
    secs = round(time.perf_counter() - t0, 1)
    if v.returncode == 0:
        return (True, "tests pass", secs)
    tail = (v.stdout or v.stderr).strip().splitlines()
    return (False, (tail[-1][:120] if tail else "tests failed"), secs)
