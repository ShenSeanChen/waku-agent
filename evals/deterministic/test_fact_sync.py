"""DETERMINISTIC EVAL -- which facts still have to reach Waku Memory (spec 006 A2).

The facts table carries `synced` and `scope`. A fact consolidation keeps while
Waku Memory is connected is stored at 0 and set to 1 once the send succeeds;
everything else is 1. The case that matters most is the upgrade: a state.db
from before this column must not re-send its whole backlog, which the capture
shim already imports from memory/<id>.md.
"""

from __future__ import annotations

import sqlite3

from waku.db import connect
from waku.memory.semantic.store import SqliteFactStore


def test_an_existing_database_keeps_its_backlog_synced(tmp_path):
    old = sqlite3.connect(tmp_path / "state.db")
    old.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT NOT NULL, "
                "content TEXT NOT NULL, source TEXT DEFAULT 'user', "
                "created_at TEXT DEFAULT (datetime('now')))")
    old.execute("INSERT INTO facts (subject, content) VALUES ('acme', 'Acme cut its price to $19.')")
    old.commit()
    old.close()

    conn = connect(tmp_path)
    row = conn.execute("SELECT synced, scope FROM facts").fetchone()
    assert (row["synced"], row["scope"]) == (1, "global")
    assert SqliteFactStore(conn).unsynced() == []
    connect(tmp_path)   # migrating twice is a no-op, not a duplicate-column error


def test_a_fact_the_user_states_has_nothing_to_send(tmp_path):
    facts = SqliteFactStore(connect(tmp_path))
    facts.add("alex", "Alex prefers morning meetings.")
    assert facts.unsynced() == []


def test_an_unsynced_fact_waits_until_it_is_marked(tmp_path):
    facts = SqliteFactStore(connect(tmp_path))
    fact_id = facts.add_unsynced("Notion AI", "Notion AI launched agents on 2026-09-30.",
                                 scope="project:Company brain")
    assert facts.unsynced() == [{"id": fact_id, "subject": "notion ai",
                                 "content": "Notion AI launched agents on 2026-09-30.",
                                 "scope": "project:Company brain"}]
    facts.mark_synced(fact_id)
    assert facts.unsynced() == []
    assert facts.search("notion") == ["[notion ai] Notion AI launched agents on 2026-09-30."], (
        "marking a fact synced rewrites its row; the FTS index must still find it")
