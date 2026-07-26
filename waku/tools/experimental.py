"""Roadmap tools — the whiteboard boxes beyond the flagship task.

One of them is now ALIVE: `delegate_task` (the Sub-Agents box) hands a coding
job to pi (https://github.com/earendil-works/pi) — a minimal open-source coding
agent by Mario Zechner — through its headless print mode (`pi -p "task"`).
The division of labor is the teaching point: Waku is the orchestrator (memory,
working-memory assembly, evals, the human's context) and pi is the specialist
contractor (read/bash/edit/write, pure coding craft). Waku hires; pi codes;
Waku's release gate can then inspect the work.

v2 is now wired: when pi supports `--mode json` we run it that way and get its
native event stream on stdout — one JSON object per line. Two things fall out:

  * OBSERVABILITY — curated events (tool calls, text deltas, turn ends) are
    relayed through the loop's observer as kind="subagent", so the dashboard
    can show the sub-agent working live instead of a black box that returns a
    summary. (pi's own critique of built-in sub-agents, answered.)
  * HONEST COST — pi's per-message token usage is appended to the SAME
    usage.jsonl ledger as the loop's own calls (kind="subagent"). Before this,
    a delegated coding run burned tokens the arena never counted, silently
    understating every coding score's cost.

Older pi builds without --mode json fall back to the plain `-p` text path.

The other three boxes are still SKELETONS on purpose: each shows the *shape* of
a capability and returns an honest "coming soon" (terminal/browser tools need a
real sandbox + safety surface first). Everything here is OFF by default; set
`WAKU_EXPERIMENTAL=1` to register these tools.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from waku.config import Settings
from waku.tools.registry import Tool

PI_INSTALL_HINT = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"

# Does this pi understand --mode json? Checked once per process (via --help so
# no model call is made); None = not probed yet.
_PI_JSON_MODE: bool | None = None


def _pi_supports_json(pi_bin: str) -> bool:
    global _PI_JSON_MODE
    if _PI_JSON_MODE is None:
        try:
            probe = subprocess.run([pi_bin, "--help"], capture_output=True, text=True,
                                   timeout=10, check=False)
            _PI_JSON_MODE = "--mode" in (probe.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            _PI_JSON_MODE = False
    return _PI_JSON_MODE


def _project_pi_flags() -> list[str]:
    """Hand the delegated pi this repo's own extensions and skills.

    This is the "pi x waku" payoff: waku upgrades its coding contractor without
    touching pi's source. pi auto-discovers project resources from cwd upward,
    but delegated runs happen in waku_workspace/ (outside the repo), so we pass
    explicit --extension / --skill flags with absolute paths. Any *.ts under
    .pi/extensions/ and every skill under .agents/skills/ rides along — drop a
    file in, and the next delegated run is stronger. No-op if the dirs are empty.
    """
    root = Path(__file__).resolve().parents[2]
    flags: list[str] = []
    for ext in sorted((root / ".pi" / "extensions").glob("*.ts")):
        flags += ["--extension", str(ext)]
    skills = root / ".agents" / "skills"
    if skills.is_dir():
        flags += ["--skill", str(skills)]
    return flags


def _record_subagent_usage(settings: Settings, tin: int, tout: int) -> None:
    """Append the sub-agent's spend to the SAME permanent ledger the loop uses
    (see Tracer._record_usage — tokens are the ground truth, dollars are
    derived). kind="subagent" so the ledger stays auditable line by line."""
    if not (tin or tout):
        return
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
              "provider": settings.provider, "model": settings.model or "",
              "kind": "subagent", "in": tin, "out": tout}
    path = settings.home / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _run_pi_json(cmd: list, workdir: Path, timeout: int, notify):
    """Run pi in --mode json, relaying curated events through `notify` as they
    stream. Returns (returncode, reply_text, stderr, raw_lines, tin, tout,
    cost) — returncode None means we killed it at the deadline.

    A reader thread feeds a queue so the deadline holds even if pi goes silent
    mid-line (a blocking readline can't be interrupted; a queue.get(timeout)
    can)."""
    proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue = queue.Queue()
    stderr_parts: list[str] = []

    def _pump_stdout():
        for ln in proc.stdout:
            lines.put(ln)
        lines.put(None)  # sentinel: stdout closed, pi is done

    def _pump_stderr():
        stderr_parts.append(proc.stderr.read() or "")

    threading.Thread(target=_pump_stdout, daemon=True).start()
    threading.Thread(target=_pump_stderr, daemon=True).start()

    deadline = time.monotonic() + timeout
    raw, reply, tin, tout, cost = [], "", 0, 0, 0.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            return None, reply, "".join(stderr_parts), raw, tin, tout, cost
        try:
            line = lines.get(timeout=min(0.5, remaining))
        except queue.Empty:
            continue
        if line is None:  # stdout closed — pi is done
            break
        raw.append(line)
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type", "")
        if kind == "message_update":
            delta = (ev.get("assistantMessageEvent") or {})
            if delta.get("type") == "text_delta" and delta.get("delta"):
                notify("subagent", {"agent": "pi", "type": "text", "delta": delta["delta"]})
        elif kind == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage") or {}
            tin += int(usage.get("input", 0) or 0)
            tout += int(usage.get("output", 0) or 0)
            cost += float(((usage.get("cost") or {}).get("total", 0)) or 0)
            texts, tools_called = [], []
            for c in msg.get("content") or []:
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "toolCall":
                    tools_called.append(c.get("name", "?"))
            if texts:
                reply = "\n".join(t for t in texts if t)
            for name in tools_called:
                notify("subagent", {"agent": "pi", "type": "tool", "tool": name})
        elif kind == "turn_end":
            notify("subagent", {"agent": "pi", "type": "turn_end",
                                "tokens_in": tin, "tokens_out": tout})
    return proc.wait(), reply, "".join(stderr_parts), raw, tin, tout, cost

# Still-skeleton boxes: name → what it will do, and its box on the whiteboard.
PLANNED = [
    {"name": "run_command", "box": "Terminal tool",
     "description": "Run a shell command in a sandbox and read the output — Hermes's 'Terminal' "
                    "tool. Needs a real sandbox + safety surface first."},
    {"name": "browse_web", "box": "Browser tool",
     "description": "Open a page and read/click it — Hermes's 'Browser' tool. (search_web already "
                    "covers read-only web lookups.)"},
    {"name": "schedule_task", "box": "Cron Job",
     "description": "Let the agent schedule its own recurring runs. Today `make brief` + a system "
                    "cron line already does scheduled runs; this would move it in-app."},
]


def make_delegate_tool(settings: Settings) -> Tool:
    """The Sub-Agents box, wired for real: delegate a coding task to pi.

    Same honesty contract as every Waku tool — the return string says exactly
    what happened (done / failed / timed out / pi not installed), short enough
    for the voice gateway to speak. The full pi transcript goes to the outbox.
    """

    def delegate_task(task: str = "", cwd: str = "", timeout_seconds: int = 0,
                      _notify=None) -> str:
        notify = _notify or (lambda kind, ev: None)
        if not task.strip():
            return ("delegate_task needs a 'task' — a plain-English description of the "
                    "coding job, e.g. 'fix the failing test in this repo'.")
        pi_bin = shutil.which("pi")
        if not pi_bin:
            return f"pi isn't installed, so I can't delegate. Install it with: {PI_INSTALL_HINT}"

        from waku.tools import workspace
        if cwd:
            workdir = Path(cwd).expanduser()
            if not workdir.is_dir():
                return f"delegate_task: the working directory '{cwd}' doesn't exist."
            in_workspace = False   # working in the user's own project; don't relocate/auto-run
        else:
            # Repo-less task: land it in a dated, documented workspace folder so
            # the scripts survive and are traceable (not a temp dir), then auto-run.
            workdir = workspace.new_run_folder(settings.model or settings.provider, task)
            in_workspace = True

        timeout = int(timeout_seconds) or int(os.getenv("WAKU_DELEGATE_TIMEOUT", "300"))
        # Run pi on the SAME brain the loop is using, so the sub-agent's coding is
        # this model's coding (that's the point of a per-model comparison). pi
        # natively speaks every provider we pin; fall back to pi's own default if
        # this provider isn't mappable. -a/--no-session = headless; stdin=DEVNULL
        # so pi never blocks on a TTY it doesn't have under the server.
        from waku.ops.coding_eval import PI_PROVIDER, _key_for
        cmd = [pi_bin]
        pi_prov = PI_PROVIDER.get(settings.provider)
        if pi_prov and settings.model:
            cmd += ["--provider", pi_prov, "--model", settings.model]
            key = _key_for(settings.provider)
            if key:
                cmd += ["--api-key", key]
        json_mode = _pi_supports_json(pi_bin)
        if json_mode:
            cmd += ["--mode", "json"]
        cmd += _project_pi_flags()   # the repo's own extensions + skills ride along
        cmd += ["-p", task, "-a", "--no-session"]

        raw_events: list[str] = []
        cost = 0.0
        if json_mode:
            try:
                code, reply, stderr, raw_events, tin, tout, cost = _run_pi_json(
                    cmd, workdir, timeout, notify)
            except OSError as exc:
                return f"Couldn't launch pi: {exc}"
            _record_subagent_usage(settings, tin, tout)   # the arena's cost now sees pi
            if code is None:
                return (f"pi was still working after {timeout}s so I stopped it — try a smaller "
                        f"task, or raise WAKU_DELEGATE_TIMEOUT.")
            stdout_text = reply
        else:
            try:
                result = subprocess.run(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                return (f"pi was still working after {timeout}s so I stopped it — try a smaller "
                        f"task, or raise WAKU_DELEGATE_TIMEOUT.")
            except OSError as exc:
                return f"Couldn't launch pi: {exc}"
            code, stdout_text, stderr = result.returncode, result.stdout, result.stderr

        # Full pi transcript alongside the work (workspace) or in the outbox;
        # in json mode the raw event stream is preserved too (pi-events.jsonl).
        transcript = (workdir / "pi-transcript.log") if in_workspace else (
            settings.home / "outbox" / f"delegate-{datetime.now():%Y%m%d-%H%M%S}.log")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(f"$ {' '.join(cmd[:-4])} -p {task!r}   (cwd: {workdir})\n\n"
                              f"--- reply ---\n{stdout_text}\n--- stderr ---\n{stderr}",
                              encoding="utf-8")
        if raw_events:
            transcript.with_name(transcript.stem + "-events.jsonl").write_text(
                "".join(raw_events), encoding="utf-8")

        if code != 0:
            err = (stderr or stdout_text).strip()[-200:] or "no output"
            return f"pi hit an error: {err} (full log: {transcript})"
        summary = (stdout_text or "").strip()[-500:] or "(pi finished but printed nothing)"
        if cost:
            summary += f"\n(sub-agent spend: ~${cost:.4f}, logged to usage.jsonl)"

        if not in_workspace:
            return f"pi finished the delegated task in {workdir}.\n{summary}\n(full log: {transcript})"

        # Scratch task: document the run (dated MANIFEST) and auto-run the script,
        # feeding the run result back into the loop so the model can react to it.
        files = workspace.created_files(workdir)
        run = workspace.autorun(workdir)
        workspace.write_manifest(workdir, settings.provider, settings.model or "(default)", task, files, run)
        made = ", ".join(p.name for p in files[:6]) or "no files"
        lines = [f"pi finished. Files saved to {workdir} ({made}).", summary]
        if run is not None:
            entry, code, out, secs = run
            verdict = "still running (interactive)" if code is None else ("ran clean" if code == 0 else f"exited {code}")
            lines.append(f"\nAuto-ran {entry}: {verdict} in {secs}s.\n{out[-400:]}")
        return "\n".join(lines)

    return Tool(
        name="delegate_task",
        description=("Delegate a CODING task (fixing tests, multi-file edits, writing "
                     "programs) to pi, a specialist coding agent running locally on this "
                     "machine. Give it a self-contained task and, when the work targets an "
                     "existing project, that project's absolute path as cwd. Use this for "
                     "real programming work instead of describing code in chat."),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "Plain-English description of the coding job, self-contained"},
                "cwd": {"type": "string",
                        "description": "Absolute path of the repo/directory to work in; omit for a scratch sandbox"},
                "timeout_seconds": {"type": "integer",
                                    "description": "Max seconds to let pi work (default 300)"},
            },
            "required": ["task"],
        },
        fn=delegate_task,
        wants_notify=True,   # streams pi's live events through the loop's observer
    )


def _stub(name: str, description: str, box: str) -> Tool:
    def fn(**kwargs) -> str:
        return (f"'{name}' maps to the '{box}' box on the architecture chart and isn't wired "
                f"in yet — it's on the roadmap (coming soon). Tell the user honestly.")

    return Tool(name=name, description=f"[coming soon] {description}",
                input_schema={"type": "object", "properties": {}}, fn=fn)


def make_tools(settings: Settings) -> list[Tool]:
    """Experimental tools, registered only when WAKU_EXPERIMENTAL=1: the live
    pi delegation plus the remaining skeletons."""
    return [make_delegate_tool(settings)] + [
        _stub(p["name"], p["description"], p["box"]) for p in PLANNED
    ]
