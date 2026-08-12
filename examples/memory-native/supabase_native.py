"""Supabase pgvector on its own terms: roll your own, and see what you get.

    pip install supabase openai   # or, from the repo root: uv pip install -e '.[arena]'
    export SUPABASE_URL=... SUPABASE_KEY=... OPENAI_API_KEY=...
    python examples/memory-native/supabase_native.py

SDK: supabase-py 2.x + openai 2.51.0. NOT RUN BY US, and deliberately so: the
only Supabase project on this account is a production database, and a demo
table does not belong in one. Kept because plenty of readers already have a
spare project, and this is the honest baseline the other three are measured
against. Run it yourself and the header should say so.

WHY THIS FILE EXISTS

Most people reading this already have a Supabase project open in another tab,
and the honest question about the three products in this folder is: what do
they give you that thirty lines of pgvector does not?

This file is the baseline that makes that question answerable. It is a table,
an embedding call, and a cosine search. It works, and it will answer the
paraphrase and the Chinese question better than a keyword index will.

What it does NOT do is the whole point:

  - nothing decides what is worth keeping (mem0 does)
  - nothing notices that fact 3 contradicts fact 2 (Zep does)
  - nothing ever forgets, so contradictions accumulate as neighbours forever

A vector table is retrieval. A memory system is retrieval plus judgement about
what to store and what is no longer true. Step 4 shows the gap.

SETUP -- run this once in the Supabase SQL editor:

    create extension if not exists vector;
    create table quickstart_memories (
      id bigserial primary key,
      content text not null,
      embedding vector(1536),
      created_at timestamptz default now()
    );
    create or replace function match_quickstart_memories(
      query_embedding vector(1536), match_count int
    ) returns table (id bigint, content text, similarity float)
    language sql stable as $$
      select id, content, 1 - (embedding <=> query_embedding) as similarity
      from quickstart_memories
      order by embedding <=> query_embedding
      limit match_count;
    $$;

Nothing here imports waku.
"""

from __future__ import annotations

import os

import openai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()  # your .env at the repo root, same keys waku uses

TABLE = os.environ.get("SUPABASE_QUICKSTART_TABLE", "quickstart_memories")
EMBED_MODEL = "text-embedding-3-small"

# The same three sentences in all four quickstarts. The third CONTRADICTS the
# second -- that is the whole test.
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
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    oai = openai.OpenAI()
    print(f"table     : {TABLE}\n")

    def embed(text: str) -> list[float]:
        return oai.embeddings.create(model=EMBED_MODEL, input=[text]).data[0].embedding

    # Start clean so re-runs do not stack duplicates and flatter the search.
    supabase.table(TABLE).delete().neq("id", 0).execute()

    # 1. WRITE. Every sentence is stored verbatim. There is no extraction step
    #    because there is nothing here to do it -- that is the deal you made.
    print("-- telling it three things ------------------------------------")
    for fact in FACTS:
        supabase.table(TABLE).insert({"content": fact, "embedding": embed(fact)}).execute()
        print(f"  said : {fact}")

    # 2. READ BACK RAW. Unlike mem0 and Zep, this list is IDENTICAL to the one
    #    above -- same words, same count. Nothing judged anything.
    print("\n-- what it actually kept --------------------------------------")
    for row in supabase.table(TABLE).select("content").order("id").execute().data:
        print(f"  kept : {row['content']}")

    # 3. SEARCH, three ways. This is where pgvector earns its place: real
    #    embeddings handle the paraphrase and the Chinese question that a
    #    keyword index (waku's FTS5) has to have the query rewritten for.
    print("\n-- asking ------------------------------------------------------")
    for label, question in QUESTIONS:
        hits = supabase.rpc(
            f"match_{TABLE}", {"query_embedding": embed(question), "match_count": 3}
        ).execute().data or []
        top = f"{hits[0]['content']}  (sim {hits[0]['similarity']:.3f})" if hits else "(nothing found)"
        print(f"  {label} : {question}\n              -> {top}")

    # 4. THE CONTRADICTION -- and the gap. Both launch dates are still here,
    #    both still retrievable, sitting next to each other with nearly
    #    identical similarity. Whichever wins is an accident of the embedding,
    #    not a decision about what is true.
    print("\n-- the superseded fact ----------------------------------------")
    hits = supabase.rpc(
        f"match_{TABLE}", {"query_embedding": embed("product launch date"), "match_count": 5}
    ).execute().data or []
    for hit in hits:
        print(f"  sim {hit['similarity']:.3f}  {hit['content']}")
    print("  Both dates survive, ranked by similarity. Nothing here knows one")
    print("  of them stopped being true. That judgement is what you are buying")
    print("  when you pay for a memory layer instead of a vector table.")

    # 5. WHERE TO LOOK.
    print("\n-- see it yourself --------------------------------------------")
    print(f"  Supabase -> your project -> Table editor -> {TABLE}")
    print("  Rows and embeddings, exactly as written. The most transparent of")
    print("  the four, and the least opinionated -- those are the same fact.")


if __name__ == "__main__":
    main()
