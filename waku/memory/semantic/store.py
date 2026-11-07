"""Semantic memory — durable facts, keyword-searched with SQLite FTS5.

The Hermes insight from the whiteboard: "keyword top-k, no embedding". For a
single user's facts, ranked keyword search (BM25) is fast, fully local, and —
crucially for teaching — you can read the whole index with sqlite3.
Want vectors? Set WAKU_SEMANTIC_STORE=supabase (see supabase_store.py).
"""

from __future__ import annotations

import re
import sqlite3

# The scripts unicode61 does not segment. It counts these as alphanumeric but
# puts no word boundary between them, so a whole run — 「阿历克斯喜欢游泳」 — is
# indexed as ONE term. An exact match on a name inside that run can therefore
# never hit, which is why these tokens, and only these, use FTS prefixes plus
# literal contains matching. Turning *every* token into `token*` would make "car" match
# "carpet": a worse failure than this one, because it still looks like it works.
_UNSEGMENTED = re.compile(
    "["
    "\u3040-\u309f"  # Hiragana
    "\u30a0-\u30ff"  # Katakana
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "]"
)


def _fts_query(text: str) -> str:
    r"""User text isn't a valid FTS5 query (quotes/punctuation break MATCH).
    Reduce it to `word OR word OR ...` over alphanumeric tokens.

    "Alphanumeric" has to mean what the *index* means by it. facts_fts and
    episodes_fts declare no tokenizer, so they get FTS5's default — unicode61,
    which keeps every Unicode alphanumeric and folds diacritics ("München" is
    stored as `munchen`). An ASCII-only `[a-zA-Z0-9]` disagreed with that
    index, and the index was not the side that was wrong:

      * accented Latin was truncated to a fragment — "Müller" became `ller`,
        which matches nothing, so German, French, Spanish, Portuguese, Turkish
        and Vietnamese users got silently wrong answers;
      * every non-Latin script reduced to "" — and an empty query is not a
        no-op. SqliteEpisodeStore.search() reads it as "just give me the recent
        ones", so a user asking about Сергей got an unrelated English episode
        handed to the model under the heading "Relevant memory". Waku wasn't
        skipping memory for those users, it was confidently supplying someone
        else's.

    `[^\W_]` is the same set unicode61 keeps. Underscore is excluded because
    unicode61 treats it as a separator, so `waku_agent` is two terms in the
    index and has to be two terms here too, or it matches nothing.

    Everything stays lowercased, and that is load-bearing beyond tidiness:
    FTS5's AND/OR/NOT/NEAR are operators only in UPPERCASE, so lowercasing is
    what stops a user's "Alex and Bob" from parsing as a boolean expression.
    """
    words = re.findall(r"[^\W_]{2,}", text.lower())
    if not words:
        return ""
    return " OR ".join(
        f"{w}*" if _UNSEGMENTED.search(w) else w for w in dict.fromkeys(words)
    )


def _cjk_contains(query: str, columns: tuple[str, ...]) -> tuple[str, list[str]]:
    """Literal contains terms for scripts unicode61 leaves unsegmented.

    Columns are internal SQL identifiers, never user input. At least two CJK
    characters are required, matching the query builder's minimum term length.
    This scans local rows only for CJK queries; other scripts keep FTS semantics.
    """
    terms = dict.fromkeys(re.findall(_UNSEGMENTED.pattern + "{2,}", query))
    predicates, params = [], []
    for term in terms:
        for column in columns:
            predicates.append(f"instr({column}, ?) > 0")
            params.append(term)
    return " OR ".join(predicates), params


class SqliteFactStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.conn.execute(
            "INSERT INTO facts (subject, content, source) VALUES (?,?,?)",
            (subject.lower().strip(), content, source),
        )
        self.conn.commit()

    # --- spec 006: kept facts on their way to Waku Memory. Stored unsynced
    # first and marked once the send succeeds, so a send that fails, or a
    # process that dies mid-send, leaves the fact waiting for the next try.
    def add_unsynced(self, subject: str, content: str, scope: str,
                     source: str = "consolidation") -> int:
        cur = self.conn.execute(
            "INSERT INTO facts (subject, content, source, synced, scope) VALUES (?,?,?,0,?)",
            (subject.lower().strip(), content, source, scope),
        )
        self.conn.commit()
        return cur.lastrowid

    def unsynced(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, content, scope FROM facts WHERE synced = 0 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, fact_id: int) -> None:
        self.conn.execute("UPDATE facts SET synced = 1 WHERE id = ?", (fact_id,))
        self.conn.commit()

    def search(self, query: str, top_k: int = 4) -> list[str]:
        rows = self._search_rows(query, top_k)
        return [f"[{r['subject']}] {r['content']}" for r in rows]

    def _search_rows(self, query: str, top_k: int) -> list[sqlite3.Row]:
        fts = _fts_query(query)
        if not fts:
            return []
        contains, params = _cjk_contains(query, ("f.subject", "f.content"))
        if contains:
            return self.conn.execute(
                "WITH matches AS (SELECT rowid, rank FROM facts_fts WHERE facts_fts MATCH ?) "
                "SELECT f.id, f.subject, f.content FROM facts f LEFT JOIN matches m ON m.rowid = f.id "
                f"WHERE m.rowid IS NOT NULL OR ({contains}) "
                "ORDER BY m.rowid IS NULL, m.rank, f.id DESC LIMIT ?",
                (fts, *params, top_k),
            ).fetchall()
        return self.conn.execute(
            "SELECT f.id, f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, top_k),
        ).fetchall()

    # --- CRUD: humans (dashboard) and the agent (manage_memory tool) edit memory.
    # The facts_au / facts_ad triggers keep the FTS index in sync automatically.
    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, content, source, created_at FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        fts = _fts_query(query)
        if not fts:
            return self.list(top_k)
        rows = self._search_rows(query, top_k)
        return [dict(r) for r in rows]

    def update(self, fact_id: int, content: str, subject: str | None = None) -> bool:
        if subject is None:
            cur = self.conn.execute("UPDATE facts SET content=? WHERE id=?", (content, fact_id))
        else:
            cur = self.conn.execute(
                "UPDATE facts SET content=?, subject=? WHERE id=?",
                (content, subject.lower().strip(), fact_id),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, fact_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def settle(self, timeout: float = 120.0) -> bool:
        """Already settled. The row and its FTS5 index land in one transaction,
        so a fact is searchable the instant add() returns. The hosted backends
        have to work for this."""
        return True
