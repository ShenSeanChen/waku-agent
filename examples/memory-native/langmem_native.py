"""LangMem on its own terms: a library, not a service. There is no console.

    pip install langmem langgraph   # or: uv pip install -e '.[arena]'
    export ANTHROPIC_API_KEY=...    # the extractor is an LLM call
    python examples/memory-native/langmem_native.py

SDK: langmem 0.0.30 + langgraph 1.2.10. Verified live 2026-08-12.

WHY THIS FILE EXISTS

LangMem sits in every "AI memory tools" listicle next to two hosted products,
and that placement is misleading. It is a set of LangGraph primitives you run
inside your own process. There is no dashboard to open, no account, and by
default no persistence at all -- the store lives in RAM and dies when Python
exits. Step 5 demonstrates that rather than asserting it.

This is also why waku's arena reports LangMem as "unreadable" rather than
"0 facts": every read through the adapter constructs a fresh empty store, and
calling that zero would be a true statement about the wrong object.

Nothing here imports waku.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langgraph.store.memory import InMemoryStore

load_dotenv()  # your .env at the repo root, same keys waku uses

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

NAMESPACE = ("quickstart", "facts")
MODEL = os.environ.get("LANGMEM_MODEL", "anthropic:claude-sonnet-4-5")


def main() -> None:
    # 1. THE STORE IS YOURS TO PROVIDE. This is the fork in the road: an
    #    InMemoryStore is a dict with a search method, and a PostgresStore is
    #    the same interface backed by a database YOU run. LangMem itself
    #    stores nothing.
    store = InMemoryStore(index={"dims": 1536, "embed": "openai:text-embedding-3-small"})
    print("store     : InMemoryStore (in this process, gone when it exits)")
    print(f"model     : {MODEL}\n")

    # 2. EXTRACT. create_memory_manager is LangMem's actual product: an LLM
    #    that reads a conversation and decides what the durable facts are.
    #    This is the same job waku's consolidation.py does.
    from langmem import create_memory_manager

    manager = create_memory_manager(MODEL)
    conversation = [{"role": "user", "content": f} for f in FACTS]

    print("-- telling it three things ------------------------------------")
    for fact in FACTS:
        print(f"  said : {fact}")

    extracted = manager.invoke({"messages": conversation})

    # 3. READ BACK RAW. Compare against the three sentences above. Note the
    #    manager gets the WHOLE conversation at once, so it can resolve the
    #    contradiction before anything is stored -- a real advantage over
    #    stores that ingest one sentence at a time.
    print("\n-- what it actually kept --------------------------------------")
    for item in extracted:
        content = _text(item)
        print(f"  kept : {content}")
        store.put(NAMESPACE, _key(content), {"text": content})

    # 4. SEARCH. This is the STORE's search, not LangMem's -- another sign of
    #    where the library ends and your infrastructure begins.
    print("\n-- asking ------------------------------------------------------")
    for label, question in QUESTIONS:
        hits = store.search(NAMESPACE, query=question, limit=3)
        top = hits[0].value.get("text") if hits else "(nothing found)"
        print(f"  {label} : {question}\n              -> {top}")

    # 5. THERE IS NO CONSOLE. Prove the ephemerality instead of claiming it.
    print("\n-- see it yourself --------------------------------------------")
    print("  There is no dashboard. This is the entire storage layer:")
    print(f"    {len(store.search(NAMESPACE, limit=100))} item(s) in a Python dict at {hex(id(store))}")
    print("  Restart this script and the count is the same, not cumulative --")
    print("  nothing persisted. For persistence you bring your own Postgres:")
    print("    from langgraph.store.postgres import PostgresStore")


def _text(item) -> str:
    """Unwrap ExtractedMemory(id=..., content=Memory(content='...')).

    Two layers deep, and printing the outer one gives you `content='...'`
    instead of a sentence -- which is fine in a REPL and embarrassing on a
    slide.
    """
    node = getattr(item, "content", item)
    return str(getattr(node, "content", node))


def _key(content) -> str:
    """Stable-ish key so re-running does not silently double every fact."""
    return str(abs(hash(str(content))))[:12]


if __name__ == "__main__":
    main()
