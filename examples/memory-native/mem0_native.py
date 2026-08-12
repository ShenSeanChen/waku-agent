"""mem0 on its own terms: the store that decides what is worth remembering.

    pip install mem0ai          # or, from the repo root: uv pip install -e '.[arena]'
    export MEM0_API_KEY=...
    python examples/memory-native/mem0_native.py

SDK: mem0ai 2.0.17. Written 2026-08-12, NOT YET RUN LIVE -- it writes to a
hosted account, so it waits for a deliberate go-ahead. Update this line the
first time it runs clean.

WHY THIS FILE EXISTS

Waku's arena drives mem0 through a common FactStore interface, and to satisfy
that interface it has to call `add(..., infer=False)` -- because the contract
says a write must always store exactly what it was given. That switch turns OFF
the single thing mem0 is actually for.

So this file is the opposite of the arena. Nothing but mem0, extraction left
on, no abstraction in the way. Step 3 is the one to watch: you will say one
thing and find a different thing stored. That is the product, not a bug.

Nothing here imports waku.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mem0 import MemoryClient

load_dotenv()  # your .env at the repo root, same keys waku uses

# One partition for the quickstart, so this never lands in your real memories.
USER = os.environ.get("MEM0_QUICKSTART_USER", "quickstart-mem0")

# The same three sentences in all four quickstarts, so the files are
# comparable. The third one CONTRADICTS the second -- that is the whole test.
FACTS = [
    "I met Yuki at the Lisbon AI meetup in March. She runs a robotics startup.",
    "Our product launch is scheduled for May.",
    "Actually, the launch moved to June.",
]

QUESTIONS = [
    ("exact     ", "When is the product launch?"),
    ("paraphrase", "What date did we push the ship date to?"),
    ("chinese   ", "发布会是什么时候?"),
]


def main() -> None:
    client = MemoryClient()  # reads MEM0_API_KEY
    print(f"user      : {USER}\n")

    # 1. WRITE. Note there is no `infer=False` here. mem0 reads the sentence,
    #    decides what the durable fact inside it is, and stores THAT.
    print("-- telling it three things ------------------------------------")
    for fact in FACTS:
        client.add(messages=[{"role": "user", "content": fact}], user_id=USER)
        print(f"  said : {fact}")

    # 2. READ BACK RAW. The money shot: compare this list against the list
    #    above. The wording will not match, and the count usually will not
    #    either -- mem0 merges, rewrites, and drops what it judges disposable.
    print("\n-- what it actually kept --------------------------------------")
    for row in _rows(client.get_all(user_id=USER)):
        print(f"  kept : {row.get('memory')}")

    # 3. SEARCH, three ways. The paraphrase and the Chinese question are the
    #    ones a keyword index (like waku's FTS5) struggles with.
    print("\n-- asking ------------------------------------------------------")
    for label, question in QUESTIONS:
        hits = _rows(client.search(question, user_id=USER))
        top = hits[0].get("memory") if hits else "(nothing found)"
        print(f"  {label} : {question}\n              -> {top}")

    # 4. THE CONTRADICTION. Two facts went in about the launch, one replacing
    #    the other. Did the old one survive? A row store usually keeps both and
    #    lets ranking decide; watch whether "May" is still in there anywhere.
    stale = [r for r in _rows(client.get_all(user_id=USER))
             if "may" in str(r.get("memory", "")).lower()]
    print("\n-- the superseded fact ----------------------------------------")
    print(f"  rows still mentioning May: {len(stale)}")
    for row in stale:
        print(f"    {row.get('memory')}")

    # 5. WHERE TO LOOK.
    print("\n-- see it yourself --------------------------------------------")
    print(f"  https://app.mem0.ai -> Memories -> filter user = {USER}")
    print("  Compare 'said' against 'kept' there. The diff is the whole product.")


def _rows(payload) -> list[dict]:
    """mem0 has returned both a bare list and {'results': [...]} across versions."""
    if isinstance(payload, dict):
        payload = payload.get("results", [])
    return [r for r in (payload or []) if isinstance(r, dict)]


if __name__ == "__main__":
    main()
