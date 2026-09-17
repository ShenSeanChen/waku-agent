"""Roadmap tools — the whiteboard boxes beyond the flagship task.

Two of them are now ALIVE:

  * `delegate_task` (the Sub-Agents box) hands a coding job to a specialist
    coding agent — pi, Claude Code, or Codex — through each one's headless
    print mode. The division of labor is the teaching point: Waku is the
    orchestrator (memory, working-memory assembly, evals, the human's context)
    and the sub-agent is the specialist contractor (read/bash/edit/write, pure
    coding craft). Waku hires; the agent codes; Waku's release gate can then
    inspect the work.

    Drivers:

      pi      pi -p <task> -a --no-session [--mode json]        (probed)
      claude  claude -p <task> --model <m> --output-format stream-json [--effort e]
      codex   codex exec --json -m <m> -s workspace-write <task>

    Each json stream is parsed into the SAME curated relays (text / tool /
    turn_end / usage) so the dashboard shows the sub-agent working and the
    permanent usage.jsonl ledger sees its spend, whichever agent is driving.
    The raw event stream is always preserved next to the transcript.

  * The other three boxes are still SKELETONS on purpose: each shows the *shape*
    of a capability and returns an honest "coming soon" (terminal/browser tools
    need a real sandbox + safety surface first).

Everything here is OFF by default; set `WAKU_EXPERIMENTAL=1` to register these
tools. The `agent`/`model`/`effort` parameters on delegate_task carry the
per-call brain: model ids are the caller's own, never hardcoded here.
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
from waku.tools._env import delegate_env as _delegate_env
from waku.tools.registry import Tool

PI_INSTALL_HINT = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
CLAUDE_INSTALL_HINT = "npm install -g @anthropic-ai/claude-code"
CODEX_INSTALL_HINT = "npm install -g @openai/codex"

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


def _record_subagent_usage(settings: Settings, tin: int, tout: int,
                           provider: str = "", model: str = "") -> None:
    """Append the sub-agent's spend to the SAME permanent ledger the loop uses
    (see Tracer._record_usage — tokens are the ground truth, dollars are
    derived). kind="subagent" so the ledger stays auditable line by line.
    `provider`/`model` override the loop's own when the sub-agent runs on a
    different brain (claude/codex) than the loop."""
    if not (tin or tout):
        return
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
              "provider": provider or settings.provider,
              "model": model or settings.model or "",
              "kind": "subagent", "in": tin, "out": tout}
    path = settings.home / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _run_json_stream(cmd: list, workdir: Path, timeout: int, notify, agent: str,
                     parse) -> tuple:
    """Run a coding agent in JSON-stream mode, relaying curated events through
    `notify` as they stream. Returns (returncode, reply_text, stderr, raw_lines,
    tin, tout, cost) — returncode None means we killed it at the deadline.

    `parse(event) -> dict | None` maps ONE json line to update keys:
      delta (text to relay + fallback reply) · tools (list of tool names)
      text (final reply, overwrites) · usage {"in","out"} · cost (float)
      turn_end (True → relay the turn_end marker with tokens-so-far)

    A reader thread feeds a queue so the deadline holds even if the agent goes
    silent mid-line (a blocking readline can't be interrupted; a queue.get with
    a timeout can)."""
    proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_delegate_env())
    lines: queue.Queue = queue.Queue()
    stderr_parts: list[str] = []

    def _pump_stdout():
        for ln in proc.stdout:
            lines.put(ln)
        lines.put(None)  # sentinel: stdout closed, agent is done

    def _pump_stderr():
        stderr_parts.append(proc.stderr.read() or "")

    threading.Thread(target=_pump_stdout, daemon=True).start()
    threading.Thread(target=_pump_stderr, daemon=True).start()

    deadline = time.monotonic() + timeout
    raw, reply, tin, tout, cost = [], "", 0, 0, 0.0
    deltas: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            return None, reply, "".join(stderr_parts), raw, tin, tout, cost
        try:
            line = lines.get(timeout=min(0.5, remaining))
        except queue.Empty:
            continue
        if line is None:  # stdout closed — agent is done
            break
        raw.append(line)
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        upd = parse(ev) or {}
        if upd.get("delta"):
            deltas.append(upd["delta"])
            notify("subagent", {"agent": agent, "type": "text", "delta": upd["delta"]})
        for name in upd.get("tools") or []:
            notify("subagent", {"agent": agent, "type": "tool", "tool": name})
        if "text" in upd:
            reply = upd["text"]
        u = upd.get("usage") or {}
        tin += int(u.get("in", 0) or 0)
        tout += int(u.get("out", 0) or 0)
        cost += float(upd.get("cost", 0) or 0)
        if upd.get("turn_end"):
            notify("subagent", {"agent": agent, "type": "turn_end",
                                "tokens_in": tin, "tokens_out": tout})
    if not reply and deltas:  # no explicit final text → reconstruct from deltas
        reply = "".join(deltas)
    return proc.wait(), reply, "".join(stderr_parts), raw, tin, tout, cost


def _parse_pi_event(ev: dict) -> dict | None:
    """pi's --mode json protocol → the curated update shape."""
    kind = ev.get("type", "")
    if kind == "message_update":
        delta = (ev.get("assistantMessageEvent") or {})
        if delta.get("type") == "text_delta" and delta.get("delta"):
            return {"delta": delta["delta"]}
    elif kind == "message_end":
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            return None
        usage = msg.get("usage") or {}
        upd = {"usage": {"in": usage.get("input", 0) or 0, "out": usage.get("output", 0) or 0},
               "cost": float((usage.get("cost") or {}).get("total", 0) or 0),
               "tools": [c.get("name", "?") for c in msg.get("content") or []
                         if c.get("type") == "toolCall"]}
        texts = [c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text"]
        if texts:
            upd["text"] = "\n".join(t for t in texts if t)
        return upd
    elif kind == "turn_end":
        return {"turn_end": True}
    return None


def _parse_claude_event(ev: dict) -> dict | None:
    """Claude Code --output-format stream-json → the curated update shape.

    assistant events carry the working text/tool deltas; the result event is
    the terminal message and carries the final text + usage + cost."""
    kind = ev.get("type", "")
    if kind == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        upd: dict = {}
        for c in content:
            if c.get("type") == "text" and c.get("text"):
                upd["delta"] = c["text"]
            elif c.get("type") == "tool_use":
                upd.setdefault("tools", []).append(c.get("name", "?"))
        return upd or None
    if kind == "result":
        usage = ev.get("usage") or {}
        return {"text": ev.get("result", "") or "",
                "usage": {"in": usage.get("input_tokens", 0) or 0,
                          "out": usage.get("output_tokens", 0) or 0},
                "cost": float(ev.get("total_cost_usd", 0) or 0),
                "turn_end": True}
    return None


def _parse_codex_event(ev: dict) -> dict | None:
    """Codex `--json` (JSONL) → the curated update shape.

    The error + lifecycle events (thread.started, turn.started, error,
    turn.failed) are verified against the live CLI; the success-path message and
    tool shapes (item.*) remain best-effort. Raw lines are preserved regardless,
    so an unrecognized event still leaves a complete record."""
    kind = ev.get("type", "")
    if kind == "error":
        return {"text": ev.get("message", "codex error")}
    if kind == "turn.failed":
        err = (ev.get("error") or {}).get("message") or "codex turn failed"
        return {"text": err, "turn_end": True}
    item = ev.get("item") or {}
    if (kind in ("item.completed", "item.started")
            and item.get("type") in ("message", "agent_message")
            and item.get("role") == "assistant"):
        upd: dict = {}
        texts = []
        for c in item.get("content") or []:
            t = c.get("type")
            if t in ("output_text", "text") and c.get("text"):
                texts.append(c["text"])
            elif t in ("function_call", "tool_call", "local_shell_call"):
                upd.setdefault("tools", []).append(c.get("name") or c.get("call_id") or "?")
        if texts:
            upd["text"] = "\n".join(texts)
        return upd or None
    usage = ev.get("usage") or {}
    tin = usage.get("input_tokens", 0) or usage.get("input", 0) or 0
    tout = usage.get("output_tokens", 0) or usage.get("output", 0) or 0
    if tin or tout:
        return {"usage": {"in": tin, "out": tout}}
    if kind in ("turn.completed", "thread.completed", "turn_context", "run.completed"):
        return {"turn_end": True}
    return None


def _pi_cmd(exe: str, settings: Settings, task: str, model: str, effort: str,
            sandbox: str = "") -> list[str]:
    """pi runs on the SAME brain the loop is using by default, so the sub-agent's
    coding is this model's coding (that's the point of a per-model comparison).
    An explicit `model` wins. pi natively speaks every provider we pin. (pi has
    no sandbox flag — `sandbox` is accepted for a uniform signature, unused.)"""
    from waku.ops.coding_eval import PI_PROVIDER, _key_for
    cmd = [exe]
    pi_prov = PI_PROVIDER.get(settings.provider)
    if pi_prov and (model or settings.model):
        cmd += ["--provider", pi_prov, "--model", model or settings.model]
        key = _key_for(settings.provider)
        if key:
            cmd += ["--api-key", key]
    if _pi_supports_json(exe):
        cmd += ["--mode", "json"]
    cmd += _project_pi_flags()   # the repo's own extensions + skills ride along
    cmd += ["-p", task, "-a", "--no-session"]  # headless; stdin=DEVNULL downstream
    return cmd


def _claude_cmd(exe: str, settings: Settings, task: str, model: str, effort: str,
                sandbox: str = "") -> list[str]:
    # --verbose is required: with --print, --output-format=stream-json refuses to
    # run without it (verified against the live CLI). claude's permission model
    # is --permission-mode/--allowedTools, not a single sandbox flag, so
    # `sandbox` is accepted for a uniform signature but not mapped here yet.
    cmd = [exe, "-p", task, "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]   # low | medium | high | xhigh | max
    return cmd


def _codex_cmd(exe: str, settings: Settings, task: str, model: str, effort: str,
               sandbox: str = "") -> list[str]:
    # -s <sandbox> bounds what the agent may touch (read-only / workspace-write /
    # danger-full-access); defaults to workspace-write. --skip-git-repo-check so
    # a scratch workspace still runs.
    cmd = [exe, "exec", "--json", "-s", sandbox or "workspace-write", "--skip-git-repo-check"]
    if model:
        cmd += ["-m", model]
    cmd += [task]
    return cmd


# agent name → how to find, launch, and parse it. `stream` is a callable over
# the resolved binary: pi probes for --mode json, claude/codex always stream.
_DRIVERS = {
    "pi": {"bin": "pi", "hint": PI_INSTALL_HINT, "cmd": _pi_cmd,
           "parse": _parse_pi_event, "stream": _pi_supports_json},
    "claude": {"bin": "claude", "hint": CLAUDE_INSTALL_HINT, "cmd": _claude_cmd,
               "parse": _parse_claude_event, "stream": lambda _exe: True, "provider": "anthropic"},
    "codex": {"bin": "codex", "hint": CODEX_INSTALL_HINT, "cmd": _codex_cmd,
              "parse": _parse_codex_event, "stream": lambda _exe: True, "provider": "openai"},
}

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
    """The Sub-Agents box, wired for real: delegate a coding task to pi, Claude
    Code, or Codex.

    Same honesty contract as every Waku tool — the return string says exactly
    what happened (done / failed / timed out / agent not installed), short enough
    for the voice gateway to speak. The full transcript goes to the outbox (or
    the workspace, for a scratch task)."""

    def delegate_task(task: str = "", agent: str = "pi", model: str = "",
                      effort: str = "", sandbox: str = "", cwd: str = "",
                      timeout_seconds: int = 0, _notify=None) -> str:
        notify = _notify or (lambda kind, ev: None)
        if not task.strip():
            return ("delegate_task needs a 'task' — a plain-English description of the "
                    "coding job, e.g. 'fix the failing test in this repo'.")
        agent = (agent or "pi").strip().lower()
        drv = _DRIVERS.get(agent)
        if drv is None:
            return f"Unknown agent '{agent}' — pick one of: {', '.join(_DRIVERS)}."
        exe = shutil.which(drv["bin"])
        if not exe:
            return f"{agent} isn't installed, so I can't delegate. Install it with: {drv['hint']}"

        from waku.tools import workspace
        if cwd:
            workdir = Path(cwd).expanduser()
            if not workdir.is_dir():
                return f"delegate_task: the working directory '{cwd}' doesn't exist."
            in_workspace = False   # working in the user's own project; don't relocate/auto-run
        else:
            # Repo-less task: land it in a dated, documented workspace folder so
            # the scripts survive and are traceable (not a temp dir), then auto-run.
            workdir = workspace.new_run_folder(model or settings.model or agent, task)
            in_workspace = True

        timeout = int(timeout_seconds) or int(os.getenv("WAKU_DELEGATE_TIMEOUT", "300"))
        cmd = drv["cmd"](exe, settings, task, model, effort, sandbox)
        stream = bool(drv["stream"](exe))

        raw_events: list[str] = []
        cost = 0.0
        if stream:
            try:
                code, reply, stderr, raw_events, tin, tout, cost = _run_json_stream(
                    cmd, workdir, timeout, notify, agent, drv["parse"])
            except OSError as exc:
                return f"Couldn't launch {agent}: {exc}"
            # Honest attribution: pi runs on the loop's brain (provider/model
            # default to settings); claude/codex run their own, and with no
            # explicit model we record "(default)" rather than mis-credit the
            # loop's model as the sub-agent's.
            ledger_provider = drv.get("provider", "")
            ledger_model = model
            if ledger_provider and not ledger_model:
                ledger_model = "(default)"
            _record_subagent_usage(settings, tin, tout,
                                   provider=ledger_provider, model=ledger_model)
            if code is None:
                return (f"{agent} was still working after {timeout}s so I stopped it — try a smaller "
                        f"task, or raise WAKU_DELEGATE_TIMEOUT.")
            stdout_text = reply
        else:
            try:
                result = subprocess.run(cmd, cwd=workdir, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=timeout,
                                        check=False, env=_delegate_env())
            except subprocess.TimeoutExpired:
                return (f"{agent} was still working after {timeout}s so I stopped it — try a smaller "
                        f"task, or raise WAKU_DELEGATE_TIMEOUT.")
            except OSError as exc:
                return f"Couldn't launch {agent}: {exc}"
            code, stdout_text, stderr = result.returncode, result.stdout, result.stderr

        # Full transcript alongside the work (workspace) or in the outbox; in
        # json mode the raw event stream is preserved too (<agent>-events.jsonl).
        tname = f"{agent}-transcript.log"
        transcript = (workdir / tname) if in_workspace else (
            settings.home / "outbox" / f"delegate-{datetime.now():%Y%m%d-%H%M%S}.log")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(f"$ {' '.join(cmd)}   (cwd: {workdir})\n\n"
                              f"--- reply ---\n{stdout_text}\n--- stderr ---\n{stderr}",
                              encoding="utf-8")
        if raw_events:
            transcript.with_name(transcript.stem + "-events.jsonl").write_text(
                "".join(raw_events), encoding="utf-8")

        if code != 0:
            err = (stderr or stdout_text).strip()[-200:] or "no output"
            return f"{agent} hit an error: {err} (full log: {transcript})"
        summary = (stdout_text or "").strip()[-500:] or f"({agent} finished but printed nothing)"
        if cost:
            summary += f"\n(sub-agent spend: ~${cost:.4f}, logged to usage.jsonl)"

        if not in_workspace:
            return f"{agent} finished the delegated task in {workdir}.\n{summary}\n(full log: {transcript})"

        # Scratch task: document the run (dated MANIFEST) and auto-run the script,
        # feeding the run result back into the loop so the model can react to it.
        files = workspace.created_files(workdir)
        run = workspace.autorun(workdir)
        workspace.write_manifest(workdir, settings.provider, model or settings.model or agent, task, files, run)
        made = ", ".join(p.name for p in files[:6]) or "no files"
        lines = [f"{agent} finished. Files saved to {workdir} ({made}).", summary]
        if run is not None:
            entry, code, out, secs = run
            verdict = "still running (interactive)" if code is None else ("ran clean" if code == 0 else f"exited {code}")
            lines.append(f"\nAuto-ran {entry}: {verdict} in {secs}s.\n{out[-400:]}")
        return "\n".join(lines)

    return Tool(
        name="delegate_task",
        description=("Delegate a CODING task (fixing tests, multi-file edits, writing "
                     "programs) to a specialist coding agent running locally on this "
                     "machine: pi, Claude Code, or Codex. Give it a self-contained task "
                     "and, when the work targets an existing project, that project's "
                     "absolute path as cwd. Use this for real programming work instead "
                     "of describing code in chat."),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "Plain-English description of the coding job, self-contained"},
                "agent": {"type": "string", "enum": ["pi", "claude", "codex"],
                          "description": "Which coding agent to hire (default pi)"},
                "model": {"type": "string",
                          "description": "Model id for the sub-agent (claude/codex); pi uses the loop's model"},
                "effort": {"type": "string",
                           "description": "Reasoning effort (claude: low|medium|high|xhigh|max)"},
                "cwd": {"type": "string",
                        "description": "Absolute path of the repo/directory to work in; omit for a scratch sandbox"},
                "timeout_seconds": {"type": "integer",
                                    "description": "Max seconds to let the sub-agent work (default 300)"},
            },
            "required": ["task"],
        },
        fn=delegate_task,
        wants_notify=True,   # streams the sub-agent's live events through the loop's observer
    )


def _stub(name: str, description: str, box: str) -> Tool:
    def fn(**kwargs) -> str:
        return (f"'{name}' maps to the '{box}' box on the architecture chart and isn't wired "
                f"in yet — it's on the roadmap (coming soon). Tell the user honestly.")

    return Tool(name=name, description=f"[coming soon] {description}",
                input_schema={"type": "object", "properties": {}}, fn=fn)


def make_tools(settings: Settings) -> list[Tool]:
    """Experimental tools, registered only when WAKU_EXPERIMENTAL=1: the live
    delegation (pi/claude/codex) plus the remaining skeletons."""
    return [make_delegate_tool(settings)] + [
        _stub(p["name"], p["description"], p["box"]) for p in PLANNED
    ]
