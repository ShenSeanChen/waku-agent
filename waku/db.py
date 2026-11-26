"""One SQLite file (state.db) holds everything Waku remembers and does.

This mirrors the Hermes approach on the whiteboard: SQLite + FTS5, no server.
Open it yourself anytime:  sqlite3 ~/.waku/state.db '.tables'
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- Flagship-task artifact: events the calendar tool creates. The deterministic
-- eval asserts directly on rows in this table ("did the meeting trigger?").
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,           -- ISO 8601
    "end" TEXT,
    attendees TEXT DEFAULT '',     -- comma-separated
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Semantic memory: durable facts about you, your people, your projects.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,         -- who/what the fact is about, e.g. 'alex'
    content TEXT NOT NULL,         -- the fact itself
    source TEXT DEFAULT 'user',    -- 'user' (told directly) or 'consolidation'
    created_at TEXT DEFAULT (datetime('now')),
    -- Spec 006: 0 while a kept fact still has to reach Waku Memory, and the
    -- scope it goes to there. Every other fact is 1: nothing to send.
    synced INTEGER DEFAULT 1,
    scope TEXT DEFAULT 'global'
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, content, content=facts, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content) VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO facts_fts(rowid, subject, content) VALUES (new.id, new.subject, new.content);
END;

-- Episodic memory: dated things that happened (past chats, distilled).
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    happened_at TEXT NOT NULL,     -- ISO 8601 date of the episode
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, content=episodes, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary) VALUES (new.id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
END;

-- Raw chat log ("save the messages" box). Consolidation reads from here.
-- session_id tags each row with which conversation it belongs to, so the
-- dashboard can offer "New chat" and switch between past sessions (like a
-- chat app). Everything shares this one table — sessions are just a label.
CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0,
    session_id TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now'))
);

-- The event inbox (spec 023). A handler under `waku serve` writes one row per
-- incoming message and returns; the worker claims rows and starts a run.
-- UNIQUE(source, external_id) makes a redelivered message a no-op.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,     -- UTC, ISO 8601: when the handler saw it
    source TEXT NOT NULL,          -- 'telegram' | 'slack' | 'github' | 'timer' | 'approval'
    external_id TEXT NOT NULL,     -- the sender's own id for this message
    session_id TEXT NOT NULL,      -- the conversation, e.g. 'telegram:<chat id>'
    payload TEXT NOT NULL,         -- JSON: the text, the sender, where a reply goes
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | done | failed
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS events_status ON events(status, id);

-- The outbox (spec 023). A run writes one row per outgoing message and the
-- dispatcher in `waku serve` sends it. UNIQUE(idempotency_key) drops the
-- second row when a run is replayed after a crash.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,          -- the run that wrote it
    channel TEXT NOT NULL,         -- 'telegram' | 'slack' | 'github'
    target TEXT NOT NULL,          -- JSON: a chat id, a channel and thread, or an issue
    body TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | dead | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,     -- failed deliveries so far
    next_attempt_at TEXT NOT NULL,           -- UTC, ISO 8601
    last_error TEXT NOT NULL DEFAULT '',
    receipt TEXT NOT NULL DEFAULT ''         -- spec 035: what the channel answered, a Slack message's ts
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt_at);

-- Timers (spec 023). schedule_task writes one row, and a loop in `waku serve`
-- turns each due row into an event with source 'timer'. UNIQUE(idempotency_key)
-- drops the second row when a run is replayed after a crash.
CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,      -- UTC, ISO 8601
    due_at TEXT NOT NULL,          -- UTC, ISO 8601: when the agent wakes
    session_id TEXT NOT NULL,      -- the conversation that set it
    note TEXT NOT NULL,            -- what the agent told itself to do then
    reply_to TEXT NOT NULL DEFAULT '',       -- JSON: channel and target, or ''
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | fired | cancelled
    -- Spec 033. A repeating timer stays pending, and the loop moves due_at to
    -- the next occurrence each time it fires.
    repeat TEXT NOT NULL DEFAULT '',         -- '' | daily | weekdays
    local_time TEXT NOT NULL DEFAULT '',     -- the time on the clock, "09:00"
    zone TEXT NOT NULL DEFAULT ''            -- "Europe/Madrid", or "+01:00"
);
CREATE INDEX IF NOT EXISTS timers_due ON timers(status, due_at);

-- Runs (spec 023). The worker in `waku serve` writes one row per run. A run
-- that called a tool whose mode is `ask` ends `parked`, with that call in
-- pending_call until the owner answers.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,      -- the conversation the run answered
    started_at TEXT NOT NULL,      -- UTC, ISO 8601
    ended_at TEXT NOT NULL DEFAULT '',       -- '' while the run is in flight
    status TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | parked | expired | cancelled
    event_ids TEXT NOT NULL,       -- JSON list: the events the run read
    pending_call TEXT NOT NULL DEFAULT '',   -- JSON: the tool call that waits, or ''
    pending_hash TEXT NOT NULL DEFAULT '',   -- SHA-256 of the tool name and arguments
    turn_id TEXT NOT NULL DEFAULT ''         -- spec 011: joins the receipt and the trace
);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, status);

-- Approval rules the owner stored (spec 025). One row lets every tool of one
-- connector run without a question: `linear_*` for the server named linear.
-- waku/alwayson/approvals.py reads it after WAKU_APPROVE_ASK and _ALLOW.
CREATE TABLE IF NOT EXISTS approvals (
    pattern TEXT PRIMARY KEY,      -- `<server>_*`
    mode TEXT NOT NULL,            -- 'allow'
    created_at TEXT NOT NULL       -- UTC, ISO 8601
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column upgrades for databases created before a
    column existed. SQLite has no 'ADD COLUMN IF NOT EXISTS', so we check."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_log)").fetchall()}
    if "session_id" not in cols:
        conn.execute("ALTER TABLE chat_log ADD COLUMN session_id TEXT DEFAULT 'default'")
        conn.commit()
    if "source" not in cols:
        # which gateway a message came in through (cli / voice / telegram / dashboard)
        conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT DEFAULT 'cli'")
        conn.commit()
    if "meta" not in cols:
        # per-turn telemetry as JSON on the assistant row (gate decision,
        # latency, iterations, tools) — so reopening a thread still shows how
        # each answer was produced, not just the plain text.
        conn.execute("ALTER TABLE chat_log ADD COLUMN meta TEXT")
        conn.commit()
    facts_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "synced" not in facts_cols:
        # Spec 006. Existing rows default to 1, so the backlog is not re-sent
        # to Waku Memory: the capture shim imports that from memory/<id>.md.
        conn.execute("ALTER TABLE facts ADD COLUMN synced INTEGER DEFAULT 1")
        conn.commit()
    if "scope" not in facts_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN scope TEXT DEFAULT 'global'")
        conn.commit()
    # Spec 033. A timer set before these columns existed fires once, as before.
    timer_cols = {r[1] for r in conn.execute("PRAGMA table_info(timers)").fetchall()}
    for column in ("repeat", "local_time", "zone"):
        if column not in timer_cols:
            conn.execute(f"ALTER TABLE timers ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            conn.commit()
    # Spec 035. A row sent before this column existed has no receipt.
    outbox_cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()}
    if "receipt" not in outbox_cols:
        conn.execute("ALTER TABLE outbox ADD COLUMN receipt TEXT NOT NULL DEFAULT ''")
        conn.commit()


def connect(home: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    # check_same_thread=False lets the dashboard's threaded HTTP server reuse
    # one agent connection across worker threads (guarded by a lock). busy_timeout
    # avoids "database is locked" when the dashboard reads while a chat writes.
    conn = sqlite3.connect(home / "state.db", check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    # WAL (spec 023): `waku serve` and the dashboard are two processes on this
    # file. In the default journal a reader blocks the writer, so a dashboard
    # page could stall a handler that has to store a message. The mode is
    # stored in the file; state.db-wal and state.db-shm appear beside it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
