"""Consolidation — distilling chats into durable memory, but only sometimes.

The whiteboard's diamond: "only consolidate after N new chats". Running a
summarizer after every message is wasteful and noisy; batching N exchanges
gives the summarizer enough context to extract facts worth keeping.

A cheap model reads the unconsolidated chat log and produces:
  - facts   → semantic memory ("Alex prefers morning meetings")
  - episode → episodic memory ("2026-07-10: planned the Acme demo with Alex")
"""

from __future__ import annotations

import json
from datetime import date

import anthropic

from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore

SUMMARIZER_PROMPT = """\
You distill a personal assistant's recent conversation into long-term memory.

From the exchanges below, extract:
1. durable facts about the user, their people, projects, or preferences —
   only things worth remembering in a month; skip chit-chat and one-offs.
2. one single-sentence episode summarizing what happened in this conversation.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>"}}

Exchanges:
{log}"""


def consolidate_if_due(
    conn,
    client: anthropic.Anthropic,
    small_model: str,
    every_n: int,
    facts: SqliteFactStore,
    episodes: SqliteEpisodeStore,
) -> int:
    """Returns how many new facts were written (0 = not due or nothing worth keeping)."""
    rows = conn.execute(
        "SELECT id, role, content FROM chat_log WHERE consolidated = 0 ORDER BY id"
    ).fetchall()
    if len(rows) < every_n * 2:  # each exchange = 2 rows (user + assistant)
        return 0

    log = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    try:
        response = client.messages.create(
            model=small_model,
            # generous budget: reasoning models (Kimi K2.6/K3, ...) spend a
            # thinking block BEFORE the JSON, and this prompt carries the whole
            # unconsolidated log (not one short message like the retrieval
            # gate) — 600 was measured truncating kimi-k2.6 to a thinking-only
            # reply (stop_reason=max_tokens, zero text blocks) on a 40-row backlog.
            max_tokens=4096,
            messages=[{"role": "user", "content": SUMMARIZER_PROMPT.format(log=log)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        if "{" not in text:  # a reasoning-only / truncated reply, not a parse error
            return 0
        distilled = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except Exception:
        return 0  # never lose the log — it stays unconsolidated for next time

    for fact in distilled.get("facts", []):
        if fact.get("subject") and fact.get("content"):
            facts.add(fact["subject"], fact["content"], source="consolidation")
    if distilled.get("episode"):
        episodes.add(distilled["episode"], happened_at=date.today().isoformat())

    conn.execute(
        f"UPDATE chat_log SET consolidated = 1 WHERE id IN ({','.join('?' * len(rows))})",
        [r["id"] for r in rows],
    )
    conn.commit()
    return len(distilled.get("facts", []))
