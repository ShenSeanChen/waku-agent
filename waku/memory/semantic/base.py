"""The contract every semantic-memory backend has to honour.

Waku is meant to be indifferent to *where* facts live — SQLite on your laptop,
pgvector in Supabase, a hosted memory API. Everything upstream just says "store
this" and "find that". That indifference only works if every backend can do the
same six things, and until this file existed, nothing checked.

It didn't check, so it broke. `SqliteFactStore` implemented six methods;
`SupabaseFactStore` implemented two. Switching backends then produced three
different failures, and the first one is the kind Waku keeps shipping:

  * `list()`      → AttributeError, dashboard memory page 500s
  * `update()`
    `delete()`    → AttributeError, from both the dashboard and manage_memory
  * `search_with_ids()` → worse. The call site guarded it with
    `hasattr(...) else []`, so the agent received an empty list and told the
    user "no matching facts" — while the facts sat in the database. No error,
    no trace, no way to notice. A guard around a missing method doesn't prevent
    a bug; it converts a loud one into a silent lie.

That is the third silent failure in this codebase in two weeks (see
consolidation.py's max_tokens note and store.py's `_fts_query` docstring for
the other two). The pattern is always the same shape: a fallback that looks
defensive and quietly answers wrong. A Protocol is how you stop writing them —
the conformance suite in evals/deterministic/test_fact_store_conformance.py
runs against *every* backend, so a store that can't do the job fails in CI
instead of in front of a user.

Ids are `int | str` on purpose. SQLite rows are integers, but the Supabase
table is keyed by a `chunk_id` string (`fact-a1b2c3…`) inherited from
launch-rag, and a hosted API will hand back whatever it likes. The episodic
side already settled this — `memory_admin.py` coerces by shape — so the
semantic side matches rather than inventing a second convention.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


def env_or(name: str, default: str) -> str:
    """`os.getenv` that treats an EMPTY value as absent.

    Backend authors reach for `os.getenv(name, default)` and it is wrong here.
    The Connections form writes every optional field it is shown, so leaving one
    blank does not omit it — it stores `NAME=''`, and os.getenv only falls back
    to its default when the variable is MISSING. So a blank "Settle seconds" box
    produced `float("")` and took every Zep call down with it, while a blank
    "User id" would have silently scoped the graph to "" instead of "waku".

    Found the first time a real key was saved through the dashboard, which is
    the only way to hit it — a hand-edited .env just omits the line.
    """
    return os.getenv(name, "").strip() or default

# A row id as its backend chose to express it: sqlite counts, Supabase and
# hosted APIs hand out opaque strings. Never parse it — pass it back verbatim.
FactId = int | str


@runtime_checkable
class FactStore(Protocol):
    """Durable facts about the user, their people, projects and preferences.

    Six methods, split by who calls them:

      add / search              — the agent, every turn
      list / search_with_ids
      update / delete           — humans in the dashboard, and the agent's
                                  manage_memory tool when a fact goes stale

    Implementations must never raise on a miss. Return an empty list or False;
    a memory backend that throws takes the whole turn down with it.
    """

    def add(self, subject: str, content: str, source: str = "user") -> None:
        """Store one fact. `subject` is the who/what it is about and is
        lowercased by convention; `source` records who claimed it ("user",
        "consolidation") so the dashboard can show provenance."""
        ...

    def search(self, query: str, top_k: int = 4) -> list[str]:
        """Facts relevant to `query`, best first, formatted for the prompt as
        `[subject] content`. Returns [] when nothing matches — and [] must mean
        "nothing matched", never "I couldn't run the search"."""
        ...

    def list(self, limit: int = 200) -> list[dict]:
        """Recent facts, newest first, for the dashboard. Each dict carries at
        least `id`, `subject` and `content`."""
        ...

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        """Like `search`, but each dict carries the `id` needed to `update` or
        `delete` it. This is the lookup step of every correction, which is why
        returning [] on failure instead of raising is so damaging: the agent
        reports the memory does not exist and moves on."""
        ...

    def update(self, fact_id: FactId, content: str, subject: str | None = None) -> bool:
        """Replace a fact's text. True if a row changed, False if `fact_id` is
        unknown. `subject=None` leaves the subject alone."""
        ...

    def delete(self, fact_id: FactId) -> bool:
        """Forget a fact. True if a row went away, False if `fact_id` is
        unknown."""
        ...
