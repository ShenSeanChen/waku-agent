"""Semantic memory via LangGraph's store — the LangMem backend.

    pip install 'waku-agent[arena]'
    WAKU_SEMANTIC_STORE=langmem   OPENAI_API_KEY=sk-...   # embeddings
    WAKU_LANGMEM_POSTGRES=postgresql://...                # optional, see below

The odd one out, in a way worth saying on camera: LangMem is not a service. It
is LangChain's memory toolkit over LangGraph's BaseStore, so "using LangMem" is
really choosing a store — InMemoryStore, PostgresStore, Redis — and letting its
tools read and write through it. There is no account and no API key of its own;
what it bills you for is embeddings.

WHY IT IS IN THE ARENA ANYWAY, AND IT IS NOT FOR THE SCOREBOARD

LangMem splits memory into semantic, episodic and procedural — the same three
pillars Waku's whiteboard has taught since 2026-06-19, arrived at independently
by another team. That agreement is worth more than whichever way its numbers
land: a taxonomy two projects reach separately is a description of the problem,
not a design opinion.

THE DEFAULT IS EPHEMERAL, AND THAT IS DISCLOSED LOUDLY

Without WAKU_LANGMEM_POSTGRES this uses InMemoryStore, which LangGraph's own
docs describe as a reference implementation whose data dies with the process.
For a benchmark run that is fine — seed and probe happen in one process — but
it must never be mistaken for a persistence story, and `list()` after a restart
returning nothing is the store working as documented, not a bug in Waku.

Ids are the store's own string keys; `FactId = int | str` already allows them.
"""

from __future__ import annotations

import os
import uuid

from waku.config import Settings
from waku.memory.semantic.base import env_or

_NAMESPACE = ("waku", "facts")


class LangMemFactStore:
    def __init__(self, settings: Settings):
        self.top_k = settings.retrieval_top_k
        self.store = self._make_store()

    @staticmethod
    def _make_store():
        """Postgres when it is configured, in-process otherwise. The index block
        is what makes search() semantic rather than exact — without it the store
        is a plain key-value dict and every probe that rephrases a fact fails."""
        index = {"dims": 1536,
                 "embed": env_or("OPENAI_EMBED_MODEL", "openai:text-embedding-3-small")}
        dsn = os.getenv("WAKU_LANGMEM_POSTGRES", "").strip()
        if dsn:
            from langgraph.store.postgres import PostgresStore

            store = PostgresStore.from_conn_string(dsn, index=index)
            store.setup()
            return store
        from langgraph.store.memory import InMemoryStore

        return InMemoryStore(index=index)

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.store.put(_NAMESPACE, f"fact-{uuid.uuid4().hex[:12]}",
                       {"subject": subject.lower().strip(), "content": content, "source": source})

    def search(self, query: str, top_k: int = 4) -> list[str]:
        return [f"[{r['subject']}] {r['content']}" if r["subject"] else r["content"]
                for r in self.search_with_ids(query, top_k)]

    def list(self, limit: int = 200) -> list[dict]:
        # An empty query with a limit is BaseStore's documented way to page a
        # namespace; there is no separate list endpoint.
        return [self._row(item) for item in self.store.search(_NAMESPACE, limit=limit)]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        return [self._row(item) for item in self.store.search(_NAMESPACE, query=query, limit=top_k)]

    def update(self, fact_id: int | str, content: str, subject: str | None = None) -> bool:
        existing = self.store.get(_NAMESPACE, str(fact_id))
        if existing is None:
            return False
        value = dict(existing.value)
        value["content"] = content
        if subject is not None:
            value["subject"] = subject.lower().strip()
        # put() on an existing key overwrites, and re-embeds with it — a store
        # that kept the old vector would answer the old question and not the new.
        self.store.put(_NAMESPACE, str(fact_id), value)
        return True

    def delete(self, fact_id: int | str) -> bool:
        if self.store.get(_NAMESPACE, str(fact_id)) is None:
            return False
        self.store.delete(_NAMESPACE, str(fact_id))
        return True

    def settle(self, timeout: float = 120.0) -> bool:
        """Already settled. The store is in this process — InMemoryStore is a
        dict, PostgresStore is a synchronous write. There is no queue to drain
        because there is no server."""
        return True

    @staticmethod
    def _row(item) -> dict:
        value = getattr(item, "value", None) or {}
        return {"id": getattr(item, "key", ""), "subject": value.get("subject", ""),
                "content": value.get("content", ""), "source": value.get("source", ""),
                "created_at": getattr(item, "created_at", None)}
