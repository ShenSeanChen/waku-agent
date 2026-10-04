"""Consolidation — distilling chats into durable memory, but only sometimes.

The whiteboard's diamond: "only consolidate after N new chats". Running a
summarizer after every message is wasteful and noisy; batching N exchanges
gives the summarizer enough context to extract facts worth keeping.

A cheap model reads the unconsolidated chat log and produces:
  - facts   → semantic memory ("Alex prefers morning meetings")
  - episode → episodic memory ("2026-07-10: planned the Acme demo with Alex")

Spec 006: with Waku Memory connected, every fact it keeps is also sent there
through `remember`, a callable app.py builds from the MCP connection. This
module never sees the transport, so a fake stands in for it in the evals.

Spec 009 B: when the exchanges saved a research report, the report holds the
findings. Consolidation then keeps at most two facts, about the user or their
decisions, and drops any fact whose subject the report is about.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date

import anthropic

from waku.memory import slot_gate, tool_note
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore

SUMMARIZER_PROMPT = """\
You distill a personal assistant's recent conversation into long-term memory.

From the exchanges below, extract:
1. durable facts about the user, their people, projects, or preferences —
   only things worth remembering in a month; skip chit-chat and one-offs.
2. research findings the user looked up or decided on: companies, products,
   markets, prices, launches. Each is a fact whose subject is the company,
   product or market it is about.
3. one single-sentence episode summarizing what happened in this conversation.

Write each fact's content as one sentence that names its subject, so it reads
on its own. Set "company_research" to true when these exchanges are research
about a company or market, and false when they are about the user's own life.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>", "company_research": false}}

Exchanges:
{log}"""

# Spec 009 B: added to the prompt when these exchanges saved a research
# report. One research turn used to keep 10 to 19 facts that repeated the
# report saved beside them; the report is where findings live.
REPORT_RULE = """
These exchanges saved a research report, so its findings are already kept,
in the report. Do NOT extract facts about the companies, products or markets
it covers. Keep only facts about the user, their people, or a decision they
made, at most {cap}, and set "company_research" to false.
The report:
{report}"""

# The most loose facts a report turn keeps (spec 009 B), whatever the model
# proposed.
REPORT_TURN_MAX_FACTS = 2


# Spec 006: the Waku Memory project company research is remembered in. The
# one place the name lives; everything else is remembered in scope "global".
COMPANY_PROJECT = "Company brain"

log = logging.getLogger(__name__)

# remember(body, scope) stores one fact in Waku Memory and returns its id
# there (None if the server named none). It raises when the send failed.
Remember = Callable[[str, str], "str | None"]


def consolidate_if_due(
    conn,
    client: anthropic.Anthropic,
    small_model: str,
    every_n: int,
    facts: SqliteFactStore,
    episodes: SqliteEpisodeStore,
    remember: Remember | None = None,
    report: str = "",
) -> int:
    """Returns how many new facts were written (0 = not due or nothing worth keeping)."""
    return len(kept_if_due(conn, client, small_model, every_n, facts, episodes, remember,
                           report))


def _send(remember: Remember, content: str, scope: str) -> tuple[bool, str | None]:
    """One fact to Waku Memory. A failure is logged and reported, never raised:
    the turn has already been answered, and the fact is safe in state.db."""
    try:
        return True, remember(content, scope)
    except Exception as exc:
        log.warning("Waku Memory did not take a kept fact (%s); "
                    "it is sent again at the next consolidation", exc)
        return False, None


def kept_if_due(
    conn,
    client: anthropic.Anthropic,
    small_model: str,
    every_n: int,
    facts: SqliteFactStore,
    episodes: SqliteEpisodeStore,
    remember: Remember | None = None,
    report: str = "",
) -> list[dict]:
    """The facts this consolidation kept, each as {subject, content, project,
    memory_id}. [] when it was not due or nothing was worth keeping.

    `report` is the research report this turn saved. A batch whose rows saved
    one earlier (a laptop consolidates every six exchanges) is a report batch
    too, with that report's title and summary, which the chat log keeps.

    With `remember`, facts an earlier send failed on go first, then each kept
    fact once. Only the SQLite store records which are still unsent; with
    another store a failed send is logged and not retried. After one failure
    the rest wait too, so a server that is down costs one timeout, not one
    per fact.
    """
    rows = conn.execute(
        "SELECT id, role, content, meta FROM chat_log WHERE consolidated = 0 ORDER BY id"
    ).fetchall()
    if len(rows) < every_n * 2:  # each exchange = 2 rows (user + assistant)
        return []

    tracked = remember is not None and isinstance(facts, SqliteFactStore)
    reachable = remember is not None
    if tracked:
        for fact in facts.unsynced():
            reachable, _ = _send(remember, fact["content"], fact["scope"])
            if not reachable:
                break
            facts.mark_synced(fact["id"])

    # tool_note.clip: a row from before tool notes were compact can carry a
    # whole tool output, which the summarizer has no use for
    log = "\n".join(f"{r['role']}: {tool_note.clip(r['content'])}" for r in rows)
    report = "\n\n".join(p for p in (report, *_saved_reports(rows)) if p)
    prompt = SUMMARIZER_PROMPT.format(log=log)
    if report:
        prompt += REPORT_RULE.format(cap=REPORT_TURN_MAX_FACTS, report=report)
    try:
        response = client.messages.create(
            model=small_model,
            # generous budget: reasoning models (Kimi K2.6/K3, ...) spend a
            # thinking block BEFORE the JSON, and this prompt carries the whole
            # unconsolidated log (not one short message like the retrieval
            # gate) — 600 was measured truncating kimi-k2.6 to a thinking-only
            # reply (stop_reason=max_tokens, zero text blocks) on a 40-row backlog.
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        if "{" not in text:  # a reasoning-only / truncated reply, not a parse error
            return []
        distilled = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except Exception:
        return []  # never lose the log — it stays unconsolidated for next time

    proposed = [f for f in distilled.get("facts", [])
                if isinstance(f, dict) and f.get("subject") and f.get("content")]
    # Spec 005: Jev drops what no later answer would need. Off by default, and
    # it fails open, so without WAKU_SLOT_GATE=jev every proposed fact is kept.
    if report:
        proposed = [f for f in proposed if not _about_report(f, report)]
    kept = slot_gate.keep(proposed)
    # A model may answer the flag as "true"; anything else, or no flag, is personal.
    research = str(distilled.get("company_research")).lower() == "true"
    if report:
        # What is left is about the person, so it is theirs, not the company's.
        kept, research = kept[:REPORT_TURN_MAX_FACTS], False
    project = COMPANY_PROJECT if research else None
    scope = f"project:{project}" if project else "global"
    out = []
    for fact in kept:
        record = {"subject": fact["subject"], "content": fact["content"],
                  "project": project, "memory_id": None}
        if tracked:
            fact_id = facts.add_unsynced(fact["subject"], fact["content"], scope)
        else:
            fact_id = None
            facts.add(fact["subject"], fact["content"], source="consolidation")
        if reachable:
            reachable, record["memory_id"] = _send(remember, fact["content"], scope)
            if reachable and fact_id is not None:
                facts.mark_synced(fact_id)
        out.append(record)
    if distilled.get("episode"):
        episodes.add(distilled["episode"], happened_at=date.today().isoformat())

    conn.execute(
        f"UPDATE chat_log SET consolidated = 1 WHERE id IN ({','.join('?' * len(rows))})",
        [r["id"] for r in rows],
    )
    conn.commit()
    return out


def _saved_reports(rows) -> list[str]:
    """The title and summary of each report these rows saved (spec 007 C keeps
    them in the assistant row's meta), one text per report."""
    found = []
    for row in rows:
        try:
            saved = (json.loads(row["meta"] or "null") or {}).get("report")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(saved, dict) and saved.get("title"):
            found.append("\n".join([f"# {saved['title']}",
                                     *(f"- {b}" for b in saved.get("summary") or [])]))
    return found


def _about_report(fact: dict, report: str) -> bool:
    """A fact whose subject the report names is a finding, and the report
    already holds it: "Mem0" in a report on mem0's competitors."""
    subject = " ".join(str(fact.get("subject", "")).split())
    if len(subject) < 2:
        return False
    whole_word = r"(?<!\w)" + re.escape(subject) + r"(?!\w)"
    return re.search(whole_word, " ".join(report.split()), re.IGNORECASE) is not None
