"""Semantic memory, vector edition — the Supabase pgvector upgrade path.

Same interface as SqliteFactStore, different retrieval: real embeddings and
cosine similarity instead of keyword BM25. Uses the exact schema and
`match_chunks` RPC from launch-rag / launch-agentic-rag
(github.com/ShenSeanChen/launch-agentic-rag) — if you followed those videos,
this is the same table. Run sql/init_supabase.sql on a fresh project, then:

    pip install 'milli-agent[supabase]'
    MILLI_SEMANTIC_STORE=supabase  SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...
    OPENAI_API_KEY=...   # embeddings only (text-embedding-3-small, 1536d)

When is this worth it over FTS5? When phrasing diverges from wording:
"my business partner" should find "Alex is my cofounder". Keywords can't;
vectors can. For a few hundred personal facts, both are instant.
"""

from __future__ import annotations

import os
import uuid

from milli.config import Settings


class SupabaseFactStore:
    def __init__(self, settings: Settings):
        import openai
        from supabase import create_client

        self.supabase = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
        )
        self.openai = openai.OpenAI()  # reads OPENAI_API_KEY
        self.embed_model = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.top_k = settings.retrieval_top_k

    def _embed(self, text: str) -> list[float]:
        return self.openai.embeddings.create(model=self.embed_model, input=[text]).data[0].embedding

    def add(self, subject: str, content: str, source: str = "user") -> None:
        # launch-rag column mapping: source=subject, text=the fact
        self.supabase.table("rag_chunks").upsert(
            {
                "chunk_id": f"fact-{uuid.uuid4().hex[:12]}",
                "source": subject.lower().strip(),
                "text": content,
                "embedding": self._embed(f"{subject}: {content}"),
            },
            on_conflict="chunk_id",
        ).execute()

    def search(self, query: str, top_k: int = 4) -> list[str]:
        result = self.supabase.rpc(
            "match_chunks",
            {"query_embedding": self._embed(query), "match_count": top_k},
        ).execute()
        return [f"[{row['source']}] {row['text']}" for row in (result.data or [])]
