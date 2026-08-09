"""Semantic memory, vector edition — the Supabase pgvector upgrade path.

Same interface as SqliteFactStore, different retrieval: real embeddings and
cosine similarity instead of keyword BM25. Uses the exact schema and
`match_chunks` RPC from launch-rag / launch-agentic-rag
(github.com/ShenSeanChen/launch-agentic-rag) — if you followed those videos,
this is the same table. Run sql/init_supabase.sql on a fresh project, then:

    pip install 'waku-agent[supabase]'
    WAKU_SEMANTIC_STORE=supabase  SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...
    OPENAI_API_KEY=...   # embeddings only (text-embedding-3-small, 1536d)

When is this worth it over FTS5? When phrasing diverges from wording:
"my business partner" should find "Alex is my cofounder". Keywords can't;
vectors can. For a few hundred personal facts, both are instant.
"""

from __future__ import annotations

import os
import uuid

from waku.config import Settings
from waku.memory.semantic.base import env_or


class SupabaseFactStore:
    def __init__(self, settings: Settings):
        import openai
        from supabase import create_client

        self.supabase = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
        )
        self.openai = openai.OpenAI()  # reads OPENAI_API_KEY
        self.embed_model = env_or("OPENAI_EMBED_MODEL", "text-embedding-3-small")
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

    # --- CRUD ----------------------------------------------------------------
    # These four went missing until 2026-08-08 and nothing noticed, because
    # nothing described what a fact store owes its callers. See
    # semantic/base.py: the dashboard's memory page and manage_memory's
    # update/delete raised AttributeError here, and search_with_ids was worse —
    # a `hasattr` guard upstream turned it into "no matching facts" while the
    # facts sat in this table.
    #
    # Two schema notes, both inherited from launch-rag and neither obvious:
    #
    #   * `source` is the SUBJECT here, not provenance. SqliteFactStore keeps
    #     them apart (subject="alex", source="consolidation"); rag_chunks has
    #     one column and add() puts the subject in it. So `list()` below cannot
    #     report who claimed a fact. The protocol only promises id/subject/
    #     content, so this is honest rather than broken — but it is why the
    #     dashboard's provenance column is blank on this backend.
    #   * ids: rag_chunks has BOTH `id BIGSERIAL` and `chunk_id TEXT`. We expose
    #     the integer, because dashboard.py does `int(payload["id"])` and would
    #     reject a string outright. Strings are still accepted on the way back
    #     in (matched against chunk_id) the same way memory_admin coerces
    #     Notion's page ids — by shape, not by parsing.

    def _column_for(self, fact_id: int | str) -> str:
        return "id" if isinstance(fact_id, int) or str(fact_id).isdigit() else "chunk_id"

    def list(self, limit: int = 200) -> list[dict]:
        rows = (
            self.supabase.table("rag_chunks")
            .select("id, source, text, created_at")
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            {"id": r["id"], "subject": r["source"], "content": r["text"],
             "source": "", "created_at": r.get("created_at")}
            for r in (rows.data or [])
        ]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        """Semantic search that also returns the ids update/delete need.

        match_chunks() returns chunk_id but not id, so this is two round trips:
        rank first, then resolve. Changing the SQL function would be one trip,
        but it ships in launch-rag and anyone who followed those videos already
        has it — silently requiring them to re-run a migration is a worse trade
        than an extra select on a query that already paid for an embedding."""
        ranked = self.supabase.rpc(
            "match_chunks",
            {"query_embedding": self._embed(query), "match_count": top_k},
        ).execute()
        chunk_ids = [r["chunk_id"] for r in (ranked.data or [])]
        if not chunk_ids:
            return []
        rows = (
            self.supabase.table("rag_chunks")
            .select("id, chunk_id, source, text")
            .in_("chunk_id", chunk_ids)
            .execute()
        )
        by_chunk = {r["chunk_id"]: r for r in (rows.data or [])}
        # re-impose the RPC's similarity order; the `in_` select does not keep it
        return [
            {"id": by_chunk[c]["id"], "subject": by_chunk[c]["source"],
             "content": by_chunk[c]["text"]}
            for c in chunk_ids
            if c in by_chunk
        ]

    def update(self, fact_id: int | str, content: str, subject: str | None = None) -> bool:
        """Re-embeds. A vector store where update() rewrote the text but left
        the old embedding would keep answering the old question correctly and
        the new one not at all — the silent-wrong-answer failure this whole
        file is a reaction to."""
        column = self._column_for(fact_id)
        current = (
            self.supabase.table("rag_chunks").select("source").eq(column, fact_id).execute()
        )
        if not current.data:
            return False
        new_subject = (subject or current.data[0]["source"]).lower().strip()
        result = (
            self.supabase.table("rag_chunks")
            .update({
                "source": new_subject,
                "text": content,
                "embedding": self._embed(f"{new_subject}: {content}"),
            })
            .eq(column, fact_id)
            .execute()
        )
        return bool(result.data)

    def delete(self, fact_id: int | str) -> bool:
        result = (
            self.supabase.table("rag_chunks")
            .delete()
            .eq(self._column_for(fact_id), fact_id)
            .execute()
        )
        return bool(result.data)
