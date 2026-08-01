"""HERO MOMENT #1 — the gate that decides WHETHER to retrieve memory at all.

The top audience question across platforms: "why hit the memory store every
turn?" Default-on retrieval is (a) slow — an extra search before every reply —
and (b) worse: irrelevant memories bias the answer ("over-interpretation").

So before touching any store, a cheap fast model answers one question:
    does THIS message need the user's memory?
"what's 2+2" → no. "when am I meeting Alex?" → yes, and here's the search query.

Cost: one small-model call (~a few hundred tokens). Payoff: retrieval only
when it helps. This is the same judge pattern as LLM-as-judge in evals —
a small model making one narrow decision.
"""

from __future__ import annotations

import json

import anthropic

GATE_PROMPT = """\
You are a retrieval gate for a personal assistant's long-term memory.
Given the user's message, decide if answering well requires the user's stored
memories (facts about people, projects, preferences, or past events).

Reply with ONLY this JSON, nothing else:
{{"retrieve": true/false, "query": "<search keywords if true, else empty>", "reason": "<5 words>"}}

General knowledge, math, small talk, or self-contained requests → false.
Anything referencing the user's life, people, plans, or history → true.

User message: {message}"""


def should_retrieve(
    client: anthropic.Anthropic, small_model: str, message: str
) -> tuple[bool, str, str]:
    """Returns (retrieve?, search_query, reason). Fails open: if the gate
    itself errors, we retrieve — a stale memory beats a lost one."""
    try:
        response = client.messages.create(
            model=small_model,
            # generous budget: reasoning models (Kimi K3, ...) spend a thinking
            # block BEFORE the JSON — 100 tokens was truncating the answer away
            max_tokens=600,
            messages=[{"role": "user", "content": GATE_PROMPT.format(message=message)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        if "{" not in text:   # a reasoning-only / truncated reply, not an error
            return True, message, "gate returned no JSON — failing open"
        decision = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return bool(decision.get("retrieve")), decision.get("query", message), decision.get("reason", "")
    except Exception as exc:
        return True, message, f"gate failed open ({type(exc).__name__})"
