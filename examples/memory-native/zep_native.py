"""Zep on its own terms: not rows, a temporal knowledge graph.

    pip install zep-cloud        # or, from the repo root: uv pip install -e '.[arena]'
    export ZEP_API_KEY=...
    python examples/memory-native/zep_native.py

SDK: zep-cloud 3.27.0. Verified live 2026-08-12.

WHY THIS FILE EXISTS

Every other store in this folder keeps a list and ranks it. Zep decomposes what
you say into entities and edges, and stamps each edge with a VALIDITY INTERVAL.
When you correct yourself, the old edge is not out-ranked -- it is marked
invalid from a point in time, and it stays queryable as history.

Step 4 is why this file is longer than the others. It is the only place in this
folder where "the launch moved to June" does something structurally different
from appending a row.

Two things nobody tells you, both of which will make you think Zep forgot:

  1. INGESTION IS ASYNCHRONOUS. graph.add returns in ~0.2s with
     processed=False. Query immediately and you get nothing. This script waits.
  2. SEARCH RETURNS ZEP'S RENDERING of an edge or node, not the sentence you
     sent. The answers read differently from every other backend here even
     when they are equally correct.

Nothing here imports waku.
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from zep_cloud import Zep

load_dotenv()  # your .env at the repo root, same keys waku uses

USER = os.environ.get("ZEP_QUICKSTART_USER", "quickstart-zep")

# The same three sentences in all four quickstarts. The third CONTRADICTS the
# second -- that is the whole test.
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

MAX_WAIT_SECONDS = int(os.environ.get("ZEP_MAX_WAIT_SECONDS", "120"))


def main() -> None:
    client = Zep(api_key=os.environ["ZEP_API_KEY"])
    print(f"user      : {USER}\n")

    try:
        client.user.add(user_id=USER)
    except Exception:
        pass  # already exists

    # 1. WRITE, then WAIT. The wait is not politeness, it is correctness --
    #    this is the step whose absence makes benchmarks score Zep as having
    #    forgotten something it was still filing.
    print("-- telling it three things ------------------------------------")
    for fact in FACTS:
        episode = client.graph.add(user_id=USER, type="text", data=fact)
        print(f"  said : {fact}")
        print(f"         (accepted, processed={getattr(episode, 'processed', None)})")
        _wait_until_processed(client)

    # 2. READ BACK RAW -- as nodes, because that is what Zep made of you.
    #    Compare these against the three sentences. They are not a subset of
    #    your words; they are entities Zep decided exist.
    print("\n-- what it actually built -------------------------------------")
    for node in client.graph.node.get_by_user_id(user_id=USER, limit=50) or []:
        summary = getattr(node, "summary", "") or ""
        print(f"  node : {getattr(node, 'name', '?')} -- {summary[:90]}")

    # 3. SEARCH, three ways.
    print("\n-- asking ------------------------------------------------------")
    for label, question in QUESTIONS:
        found = client.graph.search(query=question, user_id=USER, limit=3)
        print(f"  {label} : {question}\n              -> {_first(found)}")

    # 4. THE CONTRADICTION -- the reason to care about Zep at all.
    #    Look for an edge carrying an `invalid_at` (or `expired_at`) timestamp.
    #    In a row store, "launch is in May" just sits there forever, equally
    #    retrievable. Here it should be marked superseded AT a point in time.
    print("\n-- the superseded fact ----------------------------------------")
    edges = getattr(client.graph.search(query="product launch date", user_id=USER,
                                        limit=10, scope="edges"), "edges", None) or []
    if not edges:
        print("  (no edges returned -- try re-running; ingestion may still be settling)")
    for edge in edges:
        invalid = getattr(edge, "invalid_at", None) or getattr(edge, "expired_at", None)
        state = f"INVALID from {invalid}" if invalid else "still valid"
        print(f"  {getattr(edge, 'fact', '?')}\n      -> {state}")

    # 5. WHERE TO LOOK.
    print("\n-- see it yourself --------------------------------------------")
    print(f"  https://app.getzep.com -> your project -> Users -> {USER}")
    print("  'Graph' for the picture, 'Episodes' for the raw text you sent.")
    print("  The graph view is the thing worth putting on screen.")


def _wait_until_processed(client) -> None:
    """Poll until Zep says it has filed everything we have sent so far.

    Without this the next read is a coin flip, and a fast machine loses it
    more often than a slow one.

    The first version of this waited on the uuid that graph.add() returned,
    which never matched anything episode.get_by_user_id() came back with, so
    it burned the full timeout on every call and then read the graph early.
    The result looked exactly like Zep having forgotten the third sentence --
    the precise failure this file's docstring warns about, reproduced by the
    file itself. Waiting on "are all recent episodes processed" needs no id
    and cannot silently miss.
    """
    started = time.time()
    while time.time() - started < MAX_WAIT_SECONDS:
        try:
            found = client.graph.episode.get_by_user_id(user_id=USER, lastn=20)
            episodes = getattr(found, "episodes", found) or []
            if episodes and all(getattr(e, "processed", False) for e in episodes):
                print(f"         (processed after {int(time.time() - started)}s)")
                return
        except Exception:
            pass
        time.sleep(2)
    print(f"         (gave up waiting after {MAX_WAIT_SECONDS}s -- results may be incomplete)")


def _first(found) -> str:
    for attr in ("edges", "nodes", "episodes"):
        rows = getattr(found, attr, None) or []
        if rows:
            row = rows[0]
            return getattr(row, "fact", None) or getattr(row, "summary", None) or str(row)[:90]
    return "(nothing found)"


if __name__ == "__main__":
    main()
