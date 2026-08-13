"""mem0 on its own terms: the store that decides what is worth remembering.

    pip install mem0ai          # or, from the repo root: uv pip install -e '.[arena]'
    export MEM0_API_KEY=...
    python examples/memory-native/mem0_native.py

SDK: mem0ai 2.0.17. Verified live 2026-08-12.

Note the reads below pass `filters={"user_id": ...}, version="v2"`. Passing
`user_id=` top-level works for add() and raises on get_all() -- the SDK is
explicit about it, but only once you run it.

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
    "I met Alex at the Lisbon AI meetup in March. He runs a robotics startup.",
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
    rows = _rows(client.get_all(filters={"user_id": USER}, version="v2"))
    for row in rows:
        print(f"  kept : {row.get('memory')}  [{_state(client, row)}]")

    # 3. SEARCH, three ways -- and watch the scores, not just the winner.
    print("\n-- asking ------------------------------------------------------")
    for label, question in QUESTIONS:
        hits = _rows(client.search(question, filters={"user_id": USER}, version="v2"))
        print(f"  {label} : {question}")
        for i, hit in enumerate(hits[:2]):
            arrow = "->" if i == 0 else "  "
            print(f"           {arrow} {hit.get('score'):.4f}  [{_state(client, hit):10}]"
                  f"  {hit.get('memory')}")

    # 4. THE CONTRADICTION -- and the trap.
    #
    #    mem0 DOES resolve this. The May row gets lifecycle_state="superseded"
    #    and replaced_by=<the June row's id>. But get_all() returns NEITHER
    #    field, so reading the list above tells you nothing is wrong. You only
    #    see it by fetching a row on its own, which is what _state() does.
    #
    #    Worse: search() returns the superseded row anyway, and on a real run
    #    it scored HIGHER than the live one (0.3338 vs 0.3307). Which answer
    #    you get is scoring noise. Ask twice, get two answers, same language.
    print("\n-- the superseded fact ----------------------------------------")
    for row in rows:
        state = _state(client, row)
        if state == "superseded":
            print(f"  {row.get('memory')}")
            print(f"      -> superseded, replaced by {_full(client, row).get('replaced_by')}")
    print("  A plain search still returns it. If you do not read lifecycle_state,")
    print("  a store that resolved the contradiction correctly will still hand")
    print("  you the dead fact with a straight face.")

    # 5. WHERE TO LOOK.
    print("\n-- see it yourself --------------------------------------------")
    print(f"  https://app.mem0.ai -> Memories -> filter user = {USER}")
    print("  Compare 'said' against 'kept' there. The diff is the whole product.")


_FULL: dict[str, dict] = {}


def _full(client, row: dict) -> dict:
    """Fetch a row on its own, because the list view is not the whole row.

    get_all() and search() both omit `lifecycle_state` and `replaced_by`.
    get(memory_id) returns them. Cached so a demo does not make one HTTP call
    per row per print.
    """
    mid = str(row.get("id", ""))
    if mid not in _FULL:
        try:
            _FULL[mid] = client.get(mid) or {}
        except Exception:
            _FULL[mid] = {}
    return _FULL[mid]


def _state(client, row: dict) -> str:
    return str(_full(client, row).get("lifecycle_state") or "active")


def _rows(payload) -> list[dict]:
    """mem0 has returned both a bare list and {'results': [...]} across versions."""
    if isinstance(payload, dict):
        payload = payload.get("results", [])
    return [r for r in (payload or []) if isinstance(r, dict)]


if __name__ == "__main__":
    main()
