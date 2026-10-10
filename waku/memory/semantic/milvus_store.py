"""Semantic memory, vector edition — Milvus as a self-hosted upgrade path.

Same interface as SqliteFactStore / SupabaseFactStore, different home for the
vectors: a Milvus collection instead of pgvector. Connects only through
`MilvusClient(uri=..., token=..., db_name=...)` — no Lite, no Docker helpers,
no K8s branches. Point `MILVUS_URI` at whatever is already running.

    pip install 'waku-agent[milvus]'
    WAKU_SEMANTIC_STORE=milvus  MILVUS_URI=http://127.0.0.1:19530
    OPENAI_API_KEY=...   # embeddings only (text-embedding-3-small, 1536d)

Optional: MILVUS_TOKEN, MILVUS_COLLECTION (default waku_facts), MILVUS_DB_NAME,
MILVUS_EMBED_DIM (default 1536), OPENAI_EMBED_MODEL.

When is this worth it over FTS5? Same answer as Supabase: when phrasing
diverges from wording. The difference is where the index lives — your LAN
instead of a hosted Postgres.
"""

from __future__ import annotations

import os
import time
import uuid

from waku.config import Settings
from waku.memory.semantic.base import env_or


class MilvusFactStore:
    def __init__(self, settings: Settings):
        import openai
        from pymilvus import DataType, MilvusClient

        uri = os.environ.get("MILVUS_URI", "").strip()
        if not uri:
            raise ValueError(
                "MILVUS_URI is required when WAKU_SEMANTIC_STORE=milvus "
                "(e.g. http://127.0.0.1:19530 or https://… for Zilliz Cloud)"
            )

        # token / db_name only when set — empty string is not "no auth", it is a
        # blank credential the Connections form writes for an unused field.
        client_kwargs: dict = {"uri": uri}
        token = os.environ.get("MILVUS_TOKEN", "").strip()
        if token:
            client_kwargs["token"] = token
        db_name = os.environ.get("MILVUS_DB_NAME", "").strip()
        if db_name:
            client_kwargs["db_name"] = db_name

        self.client = MilvusClient(**client_kwargs)
        self.openai = openai.OpenAI()  # reads OPENAI_API_KEY
        self.embed_model = env_or("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.dim = int(env_or("MILVUS_EMBED_DIM", "1536"))
        self.collection = env_or("MILVUS_COLLECTION", "waku_facts")
        self.top_k = settings.retrieval_top_k
        self._DataType = DataType
        self._MilvusClient = MilvusClient
        self._ensure_collection()

    def _embed(self, text: str) -> list[float]:
        return self.openai.embeddings.create(model=self.embed_model, input=[text]).data[0].embedding

    def _ensure_collection(self) -> None:
        """Create the collection if missing; refuse to alter an existing one.

        Schema is fixed: string PK, subject/content/source, FLOAT_VECTOR(dim),
        created_at. If the collection already exists we only check that the
        embedding dim matches — silently rewriting schema would orphan whatever
        was already indexed under a different width."""
        DataType = self._DataType
        MilvusClient = self._MilvusClient
        if self.client.has_collection(self.collection):
            existing = self._embedding_dim()
            if existing is not None and existing != self.dim:
                raise ValueError(
                    f"Milvus collection {self.collection!r} has embedding dim "
                    f"{existing}, but MILVUS_EMBED_DIM={self.dim}. Refusing to "
                    f"alter the schema — use a new collection name, or set "
                    f"MILVUS_EMBED_DIM={existing} to match what is already there."
                )
            self.client.load_collection(self.collection)
            return

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("subject", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=8192)
        schema.add_field("source", DataType.VARCHAR, max_length=64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field("created_at", DataType.INT64)

        index_params = MilvusClient.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        # Passing index_params at create time indexes + loads in one shot —
        # without it, load_collection refuses a collection with no vector index.
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )

    def _embedding_dim(self) -> int | None:
        info = self.client.describe_collection(self.collection)
        for field in info.get("fields") or []:
            if field.get("name") != "embedding":
                continue
            params = field.get("params") or {}
            dim = params.get("dim")
            return int(dim) if dim is not None else None
        return None

    def add(self, subject: str, content: str, source: str = "user") -> None:
        subject = subject.lower().strip()
        fact_id = f"fact-{uuid.uuid4().hex[:12]}"
        self.client.upsert(
            self.collection,
            data=[{
                "id": fact_id,
                "subject": subject,
                "content": content,
                "source": source,
                "embedding": self._embed(f"{subject}: {content}"),
                "created_at": int(time.time()),
            }],
        )

    def search(self, query: str, top_k: int = 4) -> list[str]:
        hits = self._search_hits(query, top_k)
        return [f"[{h['subject']}] {h['content']}" for h in hits]

    def _search_hits(self, query: str, top_k: int) -> list[dict]:
        """Run a Strong-consistency vector search; [] on empty / no match.

        Hits come back nested under `entity` (pymilvus SearchResult). We flatten
        to {id, subject, content, source} so callers never have to know that."""
        if top_k <= 0:
            return []
        raw = self.client.search(
            collection_name=self.collection,
            data=[self._embed(query)],
            limit=top_k,
            output_fields=["subject", "content", "source"],
            search_params={"metric_type": "COSINE"},
            consistency_level="Strong",
        )
        rows = raw[0] if raw else []
        out = []
        for hit in rows:
            entity = hit.get("entity") if hasattr(hit, "get") else hit["entity"]
            entity = entity or {}
            out.append({
                "id": hit.get("id") if hasattr(hit, "get") else hit["id"],
                "subject": entity.get("subject", ""),
                "content": entity.get("content", ""),
                "source": entity.get("source", ""),
            })
        return out

    # --- CRUD ----------------------------------------------------------------
    # Same six methods as every other FactStore. String ids (fact-a1b2c3…) are
    # the primary key — there is no integer twin the way rag_chunks has both
    # `id` and `chunk_id`. Pass them back verbatim.

    def list(self, limit: int = 200) -> list[dict]:
        # Empty filter + limit is the documented "give me N rows" shape on
        # MilvusClient 2.4+. Sort in Python: query does not ORDER BY.
        rows = self.client.query(
            collection_name=self.collection,
            filter="",
            output_fields=["id", "subject", "content", "source", "created_at"],
            limit=max(limit, 1),
        )
        rows = sorted(rows, key=lambda r: r.get("created_at") or 0, reverse=True)
        return [
            {"id": r["id"], "subject": r.get("subject", ""), "content": r.get("content", ""),
             "source": r.get("source", ""), "created_at": r.get("created_at")}
            for r in rows[:limit]
        ]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        return [
            {"id": h["id"], "subject": h["subject"], "content": h["content"]}
            for h in self._search_hits(query, top_k)
        ]

    def update(self, fact_id: int | str, content: str, subject: str | None = None) -> bool:
        """Re-embeds. Same reason as SupabaseFactStore: a vector store that
        rewrote the text but left the old embedding would keep answering the
        old question correctly and the new one not at all."""
        fact_id = str(fact_id)
        current = self.client.get(
            self.collection, ids=[fact_id],
            output_fields=["subject", "source", "created_at"],
        )
        if not current:
            return False
        row = current[0]
        new_subject = (subject if subject is not None else row.get("subject", "")).lower().strip()
        self.client.upsert(
            self.collection,
            data=[{
                "id": fact_id,
                "subject": new_subject,
                "content": content,
                "source": row.get("source") or "user",
                "embedding": self._embed(f"{new_subject}: {content}"),
                "created_at": row.get("created_at") or int(time.time()),
            }],
        )
        return True

    def delete(self, fact_id: int | str) -> bool:
        fact_id = str(fact_id)
        current = self.client.get(self.collection, ids=[fact_id], output_fields=["id"])
        if not current:
            return False
        self.client.delete(self.collection, ids=[fact_id])
        return True

    def settle(self, timeout: float = 120.0) -> bool:
        """Already settled. Upsert is synchronous and search uses Strong
        consistency, so a just-written row is searchable as soon as upsert
        returns. Nothing is inferred here and nothing happens later."""
        return True
