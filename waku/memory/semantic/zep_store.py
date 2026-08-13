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
from waku.memory.semantic.base import env_or

_DEFAULT_USER = "waku"

# graph.add() is async: it returns an Episode with processed=False in ~0.2s,
# and the text only becomes searchable once Zep has decomposed it into nodes
# and edges.
#
# This started as a blind two-second sleep, on my claim that the SDK offered no
# readiness signal. That claim was wrong — Episode.processed is exactly that
# signal — and the sleep was far too short besides: a first live run found the
# data still unsearchable, which I nearly reported as "Zep ingested nothing".
# It had ingested everything; I had asked too early and then misread an empty
# result as an empty store.
#
# So: poll until Zep says it is done, and cap it. A benchmark must wait for a
# contestant to be ready — anything else measures queue depth — but it must
# also not hang forever on one that never finishes.
_MAX_WAIT = float(env_or("ZEP_MAX_WAIT_SECONDS", "120"))
_POLL_EVERY = 2.0


class ZepFactStore:
    def __init__(self, settings: Settings):
        from zep_cloud import Zep

        self.client = Zep(api_key=os.environ["ZEP_API_KEY"])
        self.user_id = env_or("ZEP_USER_ID", _DEFAULT_USER)
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
        episode = self.client.graph.add(
            data=f"{subject.lower().strip()}: {content}",
            type="text",
            user_id=self.user_id,
        )
        self._wait_processed(getattr(episode, "uuid_", None))

    def _wait_processed(self, uuid: str | None) -> bool:
        """Block until Zep has turned this episode into graph, or give up.

        Polls the user's episode list rather than episode.get(uuid_) — the
        per-uuid fetch did not reflect the processed flag when this was written,
        and trusting it made a fully-ingested graph look empty for two minutes.
        Returns whether it settled, so a caller can report a timeout as a
        timeout instead of as a memory that was never stored.
        """
        if not uuid:
            return False
        deadline = time.monotonic() + _MAX_WAIT
        while time.monotonic() < deadline:
            try:
                found = self.client.graph.episode.get_by_user_id(user_id=self.user_id, lastn=20)
                for ep in (getattr(found, "episodes", None) or []):
                    if getattr(ep, "uuid_", None) == uuid:
                        if getattr(ep, "processed", False):
                            return True
                        break
            except Exception:  # noqa: S110 — transient; the deadline is the real bound
                pass
            time.sleep(_POLL_EVERY)
        return False

    def settle(self, timeout: float = 120.0) -> bool:
        """Wait until the GRAPH has stopped growing, not until the episodes say
        they are done.

        `_wait_processed` above already blocks per add(), and it is not enough.
        A live run on a clean project had every episode reporting
        processed=True while the launch facts had produced no nodes at all, so
        asking "when is the launch?" returned an unrelated fact about a meetup.
        `processed` describes the EPISODE; the nodes and edges derived from it
        arrive afterwards, and nothing announces them.

        A benchmark that seeds and immediately probes needs the second event,
        so: every episode processed AND a node count that has held still.
        """
        started, last, stable = time.monotonic(), -1, 0
        while time.monotonic() - started < timeout:
            try:
                found = self.client.graph.episode.get_by_user_id(
                    user_id=self.user_id, lastn=50)
                episodes = getattr(found, "episodes", None) or []
                done = bool(episodes) and all(
                    getattr(ep, "processed", False) for ep in episodes)
                nodes = len(self.client.graph.node.get_by_user_id(
                    user_id=self.user_id, limit=200) or [])
            except Exception:  # noqa: S110 — transient; the deadline is the real bound
                nodes, done = -1, False
            stable = stable + 1 if done and nodes == last and nodes > 0 else 0
            last = nodes
            if stable >= 3:
                return True
            time.sleep(_POLL_EVERY)
        return False

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
        away the history that makes that work.

        But the id still has to exist. This used to return True unconditionally,
        so `update(999999, ...)` reported success on a row that was never there —
        caught by the conformance suite on the first live run, which is the
        entire reason that suite is parametrized over every backend."""
        if not self._exists(fact_id):
            return False
        self.add(subject or "", content, source="correction")
        return True

    def _exists(self, fact_id: int | str) -> bool:
        for get in (self.client.graph.edge.get, self.client.graph.node.get):
            try:
                if get(uuid_=str(fact_id)) is not None:
                    return True
            except Exception:  # noqa: S110 — wrong kind for this uuid, or unknown
                pass
        return False

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
