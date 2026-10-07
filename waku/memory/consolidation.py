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

A fact about the assistant's own operating state is never kept: "User's treg
balance is $0.759 USD (759,000 micro)" was kept on 2026-10-05, and so was a
balance "insufficient to complete YouTube research". Balances, credits,
billing, 402s and 429s, rate limits, tool errors and token counts are
transient, and `is_ops_state` drops them whatever the summariser proposed. A fact
about what memory holds or lacks is dropped the same way (`is_memory_state`).

A turn that answered from memory it read (a recall turn) passes that memory
as `recalled`. The answer repeats it, so the summariser would extract it
again and Waku Memory would get a second copy of every finding (2026-10-05:
one "what did we find on Mem0's competitors?" kept five). A proposed fact
whose every name, number and date already appears in what the turn read is
dropped (`restates`). Something new the user says has a name or a number
the memory does not, so it is kept.
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

Never extract the assistant's own operating state: account balances, credits,
billing or spend, rate limits, tool errors or status codes, token counts, or
how the assistant or its tools work. It changes by the hour and says nothing
about the user.

Extract only what the person said. Never extract from the assistant's replies,
and never extract what the assistant's memory holds or lacks, such as "no
address is stored" or "it could not find X". An answer the assistant gave is
not a fact about the person.

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

# Added to the prompt when the turn answered from memory it read. The
# deterministic `restates` check below is what guarantees the drop; this only
# saves the summariser proposing what would be dropped.
RECALL_RULE = """
These exchanges answered from memory that is already kept, quoted below. Do NOT
extract a fact it already holds, even reworded. Keep only what is new: what
the user said, decided or asked to note.
Memory read this turn:
{recalled}"""

# How much of the recalled memory goes into the prompt. The `restates` check
# reads all of it.
RECALL_PROMPT_CHARS = 6000

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
    recalled: str = "",
) -> int:
    """Returns how many new facts were written (0 = not due or nothing worth keeping)."""
    return len(kept_if_due(conn, client, small_model, every_n, facts, episodes, remember,
                           report, recalled))


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
    recalled: str = "",
) -> list[dict]:
    """The facts this consolidation kept, each as {subject, content, project,
    memory_id, sent}. [] when it was not due or nothing was worth keeping.
    `sent` is True when Waku Memory took the fact, False when it did not (the
    fact is kept on this agent and sent again next time), and None when no
    Waku Memory is connected. Only `sent` says whether a send worked:
    `memory_id` is also None when the server took a fact and named no id.

    `report` is the research report this turn saved. A batch whose rows saved
    one earlier (a laptop consolidates every six exchanges) is a report batch
    too, with that report's title and summary, which the chat log keeps.

    `recalled` is the memory this turn read: what the retrieval gate found,
    what research read first, and what the model's own Waku Memory searches
    and gets returned. A proposed fact that only restates it is dropped.

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
    if recalled:
        prompt += RECALL_RULE.format(recalled=recalled[:RECALL_PROMPT_CHARS])
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
    proposed = [f for f in proposed if not is_ops_state(f) and not is_memory_state(f)]
    if report:
        proposed = [f for f in proposed if not _about_report(f, report)]
    if recalled:
        proposed = [f for f in proposed if not restates(f, recalled)]
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
                  "project": project, "memory_id": None, "sent": None}
        if tracked:
            fact_id = facts.add_unsynced(fact["subject"], fact["content"], scope)
        else:
            fact_id = None
            facts.add(fact["subject"], fact["content"], source="consolidation")
        if reachable:
            reachable, record["memory_id"] = _send(remember, fact["content"], scope)
            if reachable and fact_id is not None:
                facts.mark_synced(fact_id)
        if remember is not None:
            # a fact never tried, because an earlier send failed, was not sent
            record["sent"] = reachable
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


def _words(text: str) -> set[str]:
    """The words of a text, lowercased, as `restates` compares them: "$10M,"
    is "10m", "Mem0's" is "mem0", and a date like 2026-10-05 stays one word,
    so it never matches a bare 2026. A tool's answer is often JSON, so its
    escaped line breaks and apostrophes are read as the characters they are."""
    text = re.sub(r"\\[nrt]", " ", text).replace("\\u2019", "'").replace("\u2019", "'")
    found = set()
    for word in re.findall(r"[\w$'.,%-]+", text):
        word = word.strip("'.,-").lstrip("$").lower()
        word = word.removesuffix("'s")
        if word:
            found.add(word)
    return found


def restates(fact: dict, recalled: str) -> bool:
    """True when the fact only repeats memory the turn read: it has a number
    or a date, and every name, number and date in it (each word with a
    capital letter or a digit) is already in `recalled`.

    "Zep raised a $2.3M pre-seed round on 2023-12-27." restates a report that
    has Zep, $2.3M and 2023-12-27. "Zep raised again in 2026" does not: 2026
    is new. A fact with no number is never dropped here, however familiar its
    names ("Sean decided to price below Mem0" names two things memory already
    knows), so something new the user says is never lost; RECALL_RULE asks
    the summariser to leave those out instead."""
    content = str(fact.get("content", "")).replace("\u2019", "'")
    key = _words(" ".join(w for w in content.split()
                          if any(c.isupper() or c.isdigit() for c in w)))
    has_number = any(w[0].isdigit() for w in key)   # "10m", "2023-12-27"; not "mem0"
    return has_number and key <= _words(recalled)


# The assistant's own operating state, in a proposed fact. Each pattern is
# narrow on purpose: "work-life balance" and "pays by credit card" are about
# the person and are kept; "balance" counts only beside money or an account.
_MONEYISH = re.compile(r"\$|\busd\b|\bcredits?\b|\bmicro\b|\baccount\b|\bwallet\b|"
                       r"\btop(?:ped)?[- ]?up\b|\binsufficient\b|\btreg\b|\bapi\b", re.IGNORECASE)
_OPS_STATE = re.compile(
    r"\binsufficient\b|"
    r"\b(?:out of|no|remaining|left in)\s+(?:\w+\s+)?credits?\b|\bcredits?\s+(?:left|remaining|balance)\b|"
    r"\d[\d,.]*\s*micro(?:-?usd)?\b|"
    r"\b(?:http|status(?: code)?|error(?: code)?)\s*[45]\d\d\b|"
    r"\b(?:returned|got|hit|with|at)\s+(?:an?\s+)?(?:http\s+)?(?:402|429)\b|"
    r"\brate[- ]limit|\btoo many requests\b|\bquota (?:exceeded|hit|reached|used)\b|"
    r"\b\d[\d,.]*\s*k?\s*(?:input |output |prompt |completion )?tokens\b|"
    r"\b(?:timed out|returned an error|failed with|tool error)\b|"
    r"\b(?:billing|billed|charged|spend|spent)\b.*\b(?:treg|api|platform|waku|per call|account)\b|"
    r"\b(?:treg|api|platform|waku)\b.*\b(?:billing|billed|charged)\b|"
    r"\b(?:system prompt|context window|tool calls?)\b",
    re.IGNORECASE)


def is_ops_state(fact: dict) -> bool:
    """True when a proposed fact is about the assistant's own operating state
    (a balance, credits, billing, a 402 or 429, a rate limit, a tool error, a
    token count, how the harness works) rather than about the user. Such a
    fact is stale within the hour, so it is never kept."""
    text = f"{fact.get('subject', '')} {fact.get('content', '')}"
    if re.search(r"\bbalance\b", text, re.IGNORECASE) and _MONEYISH.search(text):
        return True
    return _OPS_STATE.search(text) is not None


# Spec 018: what memory holds or lacks is the assistant's own knowledge state, the
# same kind of sentence as a balance or an error. 2026-10-07: asked for the
# company's address, the agent said it had none, and the summariser kept "The user
# does not have the company's business address stored in memory ...". Each pattern
# needs the word memory beside the storing verb, so "Sean has no passport" and "Sean
# keeps passwords in a notes app" are about the person and stay.
_MEMORY_STATE = re.compile(
    r"\b(?:stored|saved|recorded|kept|held|found|listed)\s+in\s+(?:the\s+)?(?:waku\s+)?memory\b|"
    r"\b(?:find|locate|located)\b.*\bin\s+(?:the\s+)?(?:waku\s+)?memory\b|"
    r"\bno\s+(?:record|trace|mention)\s+of\b.*\bmemory\b|"
    r"\bmemory\b.*\bno\s+(?:record|trace|mention)\s+of\b|"
    r"\b(?:waku\s+)?memory\s+(?:has|holds|contains|lacks)\s+(?:no|nothing)\b",
    re.IGNORECASE)


def is_memory_state(fact: dict) -> bool:
    """True when a proposed fact only describes what memory holds or lacks, such
    as "no address is stored in memory". The assistant said it, not the person,
    and it is stale as soon as the thing is saved."""
    text = f"{fact.get('subject', '')} {fact.get('content', '')}"
    return _MEMORY_STATE.search(text) is not None
