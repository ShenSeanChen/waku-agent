"""`python -m waku gather` — the morning briefing, run as a graph.

    30 7 * * *  cd ~/waku-agent && make gather

This is the ONE place where real callables meet the pure workflow in
waku/graph/workflows/gather.py. Both the CLI and the dashboard's
/api/graph/stream come through build_bound_graph, so there is exactly one
definition of what a gather is allowed to touch — and a reviewer only has to
read one function to know.

Compare `brief.py` next door, which is the same job done as a LOOP: one prompt
in, the model decides which tools to call, four sequential round trips. Both
ship. Reading them side by side is the point.

Nothing here can act. The scans are reads, the synthesis is one model call with
no tools, and the only write is a markdown file in the outbox. See rule 1 in
the workflow's docstring.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from rich.console import Console

from waku.app import Waku
from waku.graph import run_graph
from waku.graph.workflows.gather import DIGEST_PROMPT, build_gather_graph

DEFAULT_TOPICS = "AI agent harness loop memory eval"


def _github(settings) -> dict:
    """Open PRs and issues. Calls waku/tools/github.py as a LIBRARY, not through
    the registry — so a gather works with WAKU_GH_TOOL off. That flag decides
    whether the MODEL can reach GitHub; this is our own code asking."""
    from waku.tools import github

    repo = getattr(settings, "gh_repo", "")
    ok_p, prs = github.list_prs(repo, 20)
    ok_i, issues = github.list_issues(repo, 20)
    prs = prs if ok_p and isinstance(prs, list) else []
    issues = issues if ok_i and isinstance(issues, list) else []
    lines = []
    if ok_p:
        lines.append("Open PRs:\n" + (github._format(prs) if prs else "(none)"))
    else:
        lines.append(f"Open PRs: unavailable — {prs}")
    if ok_i:
        lines.append("Open issues:\n" + (github._format(issues) if issues else "(none)"))
    else:
        lines.append(f"Open issues: unavailable — {issues}")
    return {"gh_text": "\n\n".join(lines),
            "gh_open_prs": len(prs), "gh_open_issues": len(issues)}


def _web(settings) -> str:
    from waku.tools import search

    topics = getattr(settings, "gh_repo", "") or DEFAULT_TOPICS
    return search.make_tool().fn(query=f"{topics} {DEFAULT_TOPICS} this week")


def _calendar(settings) -> dict:
    """Reuses triage's .ics reader rather than re-implementing it — one parser
    for "what is on today" means the two workflows can never disagree."""
    from waku.graph.workflows.triage import todays_events

    text = todays_events(settings.home)
    empty = text.startswith("(")          # "(no calendar)" / "(nothing today)"
    return {"cal_text": text, "cal_event_count": 0 if empty else text.count(";") + 1}


def _memory(settings) -> str:
    """Straight fact search, deliberately NOT gated_retrieve: the gate spends a
    small-model call deciding whether memory is relevant, and for a briefing we
    already know the answer is yes.

    Opens its OWN sqlite connection rather than reusing waku.memory's. A
    connection belongs to the thread that made it, and this scan runs in a pool
    thread — the first live gather returned "local DB threading error" for
    exactly that reason, with _safe absorbing it so the digest still shipped
    minus a source. Passing check_same_thread=False on the shared handle would
    also have silenced it, and would have been the wrong fix: parallel nodes
    sharing a mutable handle is the hazard, not the thread check that reports
    it. A short-lived read-only connection is honest and costs microseconds.
    """
    import sqlite3

    from waku.db import connect
    from waku.memory.semantic.store import SqliteFactStore

    conn = None
    try:
        conn = connect(settings.home)
        found = SqliteFactStore(conn).search("project repo contributors release", 8)
        return "\n".join(found) or "(nothing relevant)"
    except sqlite3.Error as exc:
        return f"(memory unavailable: {exc})"
    finally:
        if conn is not None:
            conn.close()


def _synthesize(waku, state: dict) -> str:
    """One model call, NO tools parameter. That absence is the propose-never-act
    guarantee — a model with no tool schemas cannot call a tool."""
    prompt = DIGEST_PROMPT.format(
        gh_text=state.get("gh_text", ""), web_text=state.get("web_text", ""),
        cal_text=state.get("cal_text", ""), mem_text=state.get("mem_text", ""))
    resp = waku.client.messages.create(
        model=waku.settings.model, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _draft(home: Path, state: dict) -> str:
    """Write the digest where a human will find it. Resolves the path and checks
    it is inside the outbox before writing — same posture as messages.py, which
    drafts and never sends."""
    outbox = (home / "outbox").resolve()
    outbox.mkdir(parents=True, exist_ok=True)
    dest = (outbox / f"gather-{date.today().isoformat()}.md").resolve()
    if outbox not in dest.parents:
        return "refused: draft path escaped the outbox"
    dest.write_text(state.get("digest", "") + "\n", encoding="utf-8")
    return str(dest)


def build_bound_graph(waku: Waku):
    """The pure workflow, wired to this machine."""
    s = waku.settings
    return build_gather_graph(
        github_fn=lambda: _github(s),
        web_fn=lambda: _web(s),
        calendar_fn=lambda: _calendar(s),
        memory_fn=lambda: _memory(s),
        synth_fn=lambda state: _synthesize(waku, state),
        draft_fn=lambda state: _draft(s.home, state),
    )


def run_gather(waku: Waku | None = None, observer=None) -> dict:
    """Run one gather to completion. Returns the final state; never raises.

    The observer is composed with the tracer so a gather lands in
    traces/*.jsonl like any turn — which means the dashboard's existing
    /api/events poller animates the topology chart for free.
    """
    own = waku is None
    waku = waku or Waku()
    try:
        def notify(kind: str, ev: dict) -> None:
            waku.tracer.event(kind, ev)
            if observer:
                observer(kind, ev)

        return run_graph(build_bound_graph(waku), {}, observer=notify)
    finally:
        if own:
            waku.close()


def main() -> None:
    console = Console()
    waku = Waku()
    try:
        console.print("[dim]gathering — github, web, calendar and memory, together…[/dim]")
        state = run_gather(waku)
        console.print(state.get("digest") or "(no digest — every source was empty)")
        if state.get("draft_path"):
            console.print(f"[dim]saved to {state['draft_path']}[/dim]")
        for node, err in (state.get("errors") or {}).items():
            console.print(f"[dim]{node}: {err}[/dim]")
    finally:
        waku.close()


if __name__ == "__main__":
    main()
