"""Semantic memory, hosted — Mem0 behind the same six methods as everything else.

    pip install 'waku-agent[arena]'
    WAKU_SEMANTIC_STORE=mem0   MEM0_API_KEY=m0-...

Waku's own store is FTS5 keyword search; Supabase is embeddings you host. Mem0
is a memory service: you hand it text, it decides what is worth keeping, and it
retrieves semantically. Having all three behind `FactStore` is what makes the
Memory arena a fair fight — one agent, one model, one loop, and the only thing
that changes is where facts live.

ONE HONEST CAVEAT, AND IT MATTERS FOR ANY BENCHMARK RUN

Mem0's headline feature is `infer=True`: an LLM reads what you send and decides
what is worth remembering. This adapter passes `infer=False`.

That is not a slight — it is what the contract requires. `FactStore.add()`
promises "store this fact", and a caller that stores something must be able to
find it again. With inference on, Mem0 may reasonably decide a given sentence is
not worth keeping, and `add()` would silently become a no-op. That breaks the
conformance suite for a good reason.

So a bake-off using this adapter measures Mem0's RETRIEVAL, not its extraction.
Say so out loud when reporting numbers; a benchmark that quietly disables half a
product and then scores it is worse than no benchmark.

IDS are strings here — Mem0 hands out UUIDs. `FactId` is `int | str` precisely
so a hosted backend can pass its own back untouched (see semantic/base.py).
"""

from __future__ import annotations

import os

from waku.config import Settings
from waku.memory.semantic.base import env_or

# Every fact Waku stores belongs to the one person running it, and Mem0 scopes
# by user_id. A stable default keeps a single-user install from scattering its
# memory across generated ids; override when several people share a project.
_DEFAULT_USER = "waku"


class Mem0FactStore:
    def __init__(self, settings: Settings):
        from mem0 import MemoryClient

        self.client = MemoryClient(api_key=os.environ["MEM0_API_KEY"])
        self.user_id = env_or("MEM0_USER_ID", _DEFAULT_USER)
        self.top_k = settings.retrieval_top_k

    # Waku keeps subject and content apart; Mem0 stores one blob. Round-tripping
    # through "subject: content" keeps `[subject] content` renderable on the way
    # out without a second field or a second call.
    @staticmethod
    def _pack(subject: str, content: str) -> str:
        return f"{subject.lower().strip()}: {content}"

    @staticmethod
    def _unpack(text: str) -> tuple[str, str]:
        subject, _, content = (text or "").partition(": ")
        return (subject, content) if content else ("", text or "")

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.client.add(
            [{"role": "user", "content": self._pack(subject, content)}],
            user_id=self.user_id,
            metadata={"source": source},
            infer=False,   # see the module docstring — the contract needs this
        )

    def search(self, query: str, top_k: int = 4) -> list[str]:
        rows = self.client.search(query, filters={"user_id": self.user_id}, top_k=top_k)
        out = []
        for row in self._rows(rows):
            subject, content = self._unpack(row.get("memory") or row.get("text") or "")
            out.append(f"[{subject}] {content}" if subject else content)
        return out

    def list(self, limit: int = 200) -> list[dict]:
        rows = self._rows(self.client.get_all(filters={"user_id": self.user_id}))
        return [self._row(row) for row in rows[:limit]]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        rows = self.client.search(query, filters={"user_id": self.user_id}, top_k=top_k)
        return [self._row(row) for row in self._rows(rows)]

    def update(self, fact_id: int | str, content: str, subject: str | None = None) -> bool:
        # Mem0 stores one blob, so a subject-only edit still has to rewrite the
        # whole thing — read the current subject back rather than dropping it.
        if subject is None:
            current = next((r for r in self.list(500) if str(r["id"]) == str(fact_id)), None)
            subject = current["subject"] if current else ""
        try:
            self.client.update(memory_id=str(fact_id), text=self._pack(subject, content))
        except Exception:
            return False   # unknown id — False, never an exception (base.py)
        return True

    def delete(self, fact_id: int | str) -> bool:
        try:
            self.client.delete(memory_id=str(fact_id))
        except Exception:
            return False
        return True

    # --- shapes -------------------------------------------------------------
    # The hosted API has returned both a bare list and {"results": [...]} across
    # versions. Accept either rather than pinning to whichever shipped the day
    # this was written; a benchmark that breaks on a minor SDK bump is useless.
    @staticmethod
    def _rows(payload) -> list[dict]:
        if isinstance(payload, dict):
            payload = payload.get("results") or payload.get("memories") or []
        return [row for row in (payload or []) if isinstance(row, dict)]

    def _row(self, row: dict) -> dict:
        subject, content = self._unpack(row.get("memory") or row.get("text") or "")
        return {"id": row.get("id", ""), "subject": subject, "content": content,
                "source": (row.get("metadata") or {}).get("source", ""),
                "created_at": row.get("created_at")}
