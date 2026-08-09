"""The smallest agent that can answer from memory. Forty lines, no tools.

    .venv/bin/python examples/tiny_memory_agent.py "When is the design review?"

The explicit interpreter is not pedantry: on macOS `python` is usually aliased
to /usr/bin/python3, which does not have waku installed, and the failure is a
bare ModuleNotFoundError that looks like a broken example rather than a shell
pointing somewhere else. `source .venv/bin/activate` first if you prefer.

Your keys are picked up automatically — waku/config.py calls find_dotenv from
the working directory, so no exports are needed.

WHY THIS FILE EXISTS

The full Waku loop sends every registered tool schema on every turn. Measured on
a real probe, that is ~65% of the prompt — the model is handed a calendar, a web
search and a shell tool to answer "what does Jensen always wear?". For an agent
whose only job is to recall, all of it is waste you pay for on every question.

So this is the same three steps the real loop takes, with nothing else in the
frame:

    1. gate      — a small model decides: does this question need memory at all?
    2. retrieve  — if yes, search the store for the facts that matter
    3. answer    — one call, those facts, no tools

It reads the SAME store as your live agent (`WAKU_SEMANTIC_STORE` picks the
backend: sqlite, mem0, zep, supabase, langmem). It only ever reads — nothing
here writes, consolidates, or deletes.

Run it next to `make run` and compare the token counts. That difference is the
cost of a tool the question never needed.
"""

from __future__ import annotations

import sys

from waku.config import load_settings
from waku.db import connect
from waku.loop.models import get_client
from waku.memory import Memory
from waku.memory.retrieval_gate import should_retrieve

PROMPT = """You are a personal assistant answering from your memory of this user.

What you remember:
{facts}

Answer their question using ONLY what is above. If the answer is not there, say
so plainly — do not guess. Be brief."""


def ask(question: str) -> str:
    settings = load_settings()
    client = get_client(settings)
    memory = Memory(connect(settings.home), settings, client)

    # 1. THE GATE. "what's 17 times 4" needs no memory; asking anyway costs a
    #    search and risks an irrelevant fact biasing the answer.
    retrieve, query, reason = should_retrieve(client, settings.small_model, question)
    print(f"gate      : {'retrieve' if retrieve else 'skip'} — {reason}")

    # 2. RETRIEVE. Note the gate rewrites the query: the store is searched for
    #    the gate's keywords, not the user's phrasing. That is why a question
    #    asked in Chinese can still find facts stored in English.
    facts = memory.facts.search(query, settings.retrieval_top_k) if retrieve else []
    if retrieve:
        print(f"searched  : {query!r} -> {len(facts)} fact(s)")
        for f in facts:
            print(f"  - {f}")

    # 3. ANSWER. One call. No tools, so no tool schemas in the prompt.
    reply = client.messages.create(
        model=settings.model,
        max_tokens=400,
        system=PROMPT.format(facts="\n".join(f"- {f}" for f in facts) or "(nothing)"),
        messages=[{"role": "user", "content": question}],
    )
    usage = getattr(reply, "usage", None)
    if usage:
        # The receipt, printed every run. A number you cannot see is a number
        # you will not believe when it matters.
        tin = getattr(usage, "input_tokens", 0)
        tout = getattr(usage, "output_tokens", 0)
        print(f"cost      : {tin} in + {tout} out = {tin + tout} tokens, 1 call")

    return "".join(b.text for b in reply.content if b.type == "text")


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What do you remember about me?"
    print(f"question  : {q}")
    print(f"\n{ask(q)}")
