"""Semantic memory, as a temporal graph — Zep behind the same six methods.

    pip install 'waku-agent[arena]'
    WAKU_SEMANTIC_STORE=zep   ZEP_API_KEY=z_...

This is the most architecturally different backend in the arena, and it is the
one worth watching. Waku's FTS5 store and Mem0 both hold facts as rows: writing
a newer fact leaves the older one sitting there, equally retrievable, and
"which is true now" is left to whatever ranks higher. Zep builds a temporal
knowledge graph instead — entities and relationships with validity intervals,
so a fact can be marked superseded AT A POINT IN TIME rather than merely
outranked.

That is exactly the update test ("the design review moved to Wednesday"), which
is the probe waku is most likely to lose. Losing it to a system that was built
for it is a real finding and belongs in the results, not in a footnote.

WHAT DOESN'T MAP CLEANLY, STATED PLAINLY

`graph.add()` ingests text and derives nodes and edges from it — the fact you
put in is not the row you get back. So:

  * `add()` is a graph ingest, not an insert. Zep decides how to decompose it.
  * `search()` returns graph results whose text is Zep's rendering of an edge or
    node, not the sentence Waku wrote. Answers will read differently from the
    other backends even when they are equally correct — that is the product
    working, not a bug, and a scoreboard reporting it should say so.
  * `update()` has no counterpart. A graph does not edit a fact in place; you
    add the correction and the older edge is superseded by validity dates. So
    update() adds the new text and reports success — honestly the closest true
    behaviour, and documented here rather than faked with a delete+add that
    would destroy the very history Zep exists to keep.
  * ids are node/edge UUIDs (strings), which `FactId = int | str` already allows.

Ingestion is also ASYNCHRONOUS: graph.add returns before the episode has been
processed into nodes and edges. A benchmark that seeds and immediately queries
will read an empty graph and score Zep as having forgotten everything, which
would be a lie about the product. `_SETTLE_SECONDS` is the crude, honest
mitigation — see the note there.
"""

from __future__ import annotations

import os
import time

from waku.config import Settings

_DEFAULT_USER = "waku"

# graph.add() is async: the episode is queued, then decomposed into nodes and
# edges. There is no "is it ready" call in the SDK, so the only options are to
# wait or to report a graph that has not finished thinking as empty. A fixed
# pause is unsatisfying and it is disclosed rather than hidden: a bake-off that
# quietly out-waits one contestant is not measuring what it claims to.
_SETTLE_SECONDS = float(os.getenv("ZEP_SETTLE_SECONDS", "2.0"))


class ZepFactStore:
    def __init__(self, settings: Settings):
        from zep_cloud import Zep

        self.client = Zep(api_key=os.environ["ZEP_API_KEY"])
        self.user_id = os.getenv("ZEP_USER_ID", _DEFAULT_USER)
        self.top_k = settings.retrieval_top_k
        self._ensure_user()

    def _ensure_user(self) -> None:
        """Zep scopes a graph to a user, and add() fails on an unknown one.
        Creating it is idempotent in effect — an already-exists error is the
        success case, not something to propagate."""
        try:
            self.client.user.add(user_id=self.user_id)
        except Exception:
            pass

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.client.graph.add(
            data=f"{subject.lower().strip()}: {content}",
            type="text",
            user_id=self.user_id,
        )
        if _SETTLE_SECONDS:
            time.sleep(_SETTLE_SECONDS)

    def search(self, query: str, top_k: int = 4) -> list[str]:
        return [row["content"] for row in self._search_rows(query, top_k)]

    def list(self, limit: int = 200) -> list[dict]:
        try:
            nodes = self.client.graph.node.get_by_user_id(user_id=self.user_id, limit=limit)
        except Exception:
            return []
        return [self._row(node) for node in (nodes or [])][:limit]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        return self._search_rows(query, top_k)

    def update(self, fact_id: int | str, content: str, subject: str | None = None) -> bool:
        """A graph has no in-place edit — see the module docstring. The
        correction is ingested and the older edge is superseded by its validity
        dates, which is the mechanism Zep exists for. Deleting first would throw
        away the history that makes that work."""
        self.add(subject or "", content, source="correction")
        return True

    def delete(self, fact_id: int | str) -> bool:
        """A uuid can name a node or an edge and the caller has no way to know
        which, so both are tried. Failure of the first is the ordinary case, not
        an error worth reporting — only failing BOTH means the id is unknown,
        and that is what False says."""
        deleted = False
        for target in (self.client.graph.node, self.client.graph.edge):
            if deleted:
                break
            try:
                target.delete(uuid_=str(fact_id))
                deleted = True
            except Exception:  # noqa: S110 — wrong kind for this uuid; try the other
                pass
        return deleted

    # --- shapes -------------------------------------------------------------
    def _search_rows(self, query: str, top_k: int) -> list[dict]:
        try:
            found = self.client.graph.search(query=query, user_id=self.user_id, limit=top_k)
        except Exception:
            return []
        rows = []
        # A search returns edges and/or nodes depending on scope; both carry a
        # uuid and some rendered text, under different attribute names.
        for group in ("edges", "nodes"):
            for item in (getattr(found, group, None) or []):
                rows.append(self._row(item))
        return rows[:top_k]

    @staticmethod
    def _row(item) -> dict:
        text = (getattr(item, "fact", None) or getattr(item, "summary", None)
                or getattr(item, "name", None) or "")
        subject, _, content = text.partition(": ")
        if not content:
            subject, content = "", text
        return {"id": getattr(item, "uuid_", None) or getattr(item, "uuid", "") or "",
                "subject": subject, "content": content, "source": "zep",
                "created_at": getattr(item, "created_at", None)}
