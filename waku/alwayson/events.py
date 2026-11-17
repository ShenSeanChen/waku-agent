"""The event inbox: one row in `events` per incoming message (spec 023).

A handler calls `insert` and returns. The worker calls `claim_next`, then
`claim_session`, then `mark`. Every function here is one short transaction on
state.db, so a row is `pending`, `claimed`, `done` or `failed` on disk at every
moment, and a process that dies loses nothing.

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM events'
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUSES = ("pending", "claimed", "done", "failed")


@dataclass(frozen=True)
class Event:
    id: int
    received_at: str     # UTC, ISO 8601
    source: str          # telegram, slack, github, timer or approval
    external_id: str     # the sender's own id for this message
    session_id: str      # the conversation the message belongs to
    payload: dict        # the text, the sender, and where a reply goes
    status: str


def _event(row: sqlite3.Row) -> Event:
    return Event(row["id"], row["received_at"], row["source"], row["external_id"],
                 row["session_id"], json.loads(row["payload"]), row["status"])


def insert(conn: sqlite3.Connection, *, source: str, external_id: str, session_id: str,
           payload: dict, received_at: str | None = None) -> int | None:
    """Write one pending row. Returns its id, or None when this source already
    delivered this external_id: Slack and GitHub resend a message whose answer
    was slow, and UNIQUE(source, external_id) makes the second delivery a no-op."""
    received_at = received_at or datetime.now(UTC).isoformat(timespec="seconds")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO events (received_at, source, external_id, session_id, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (received_at, source, str(external_id), session_id, json.dumps(payload)))
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else None


def get(conn: sqlite3.Connection, event_id: int) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _event(row) if row else None


def claim_next(conn: sqlite3.Connection) -> Event | None:
    """Claim the oldest pending row, or return None when the inbox is empty."""
    row = conn.execute(
        "UPDATE events SET status = 'claimed' WHERE id = "
        "(SELECT id FROM events WHERE status = 'pending' ORDER BY id LIMIT 1) "
        "RETURNING *").fetchone()
    conn.commit()
    return _event(row) if row else None


def claim_session(conn: sqlite3.Connection, session_id: str) -> list[Event]:
    """Claim every pending row of one session, oldest first."""
    rows = conn.execute(
        "UPDATE events SET status = 'claimed' WHERE status = 'pending' AND session_id = ? "
        "RETURNING *", (session_id,)).fetchall()
    conn.commit()
    return sorted((_event(r) for r in rows), key=lambda e: e.id)


def mark(conn: sqlite3.Connection, event_ids: list[int], status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"'{status}' is not an event status: {', '.join(STATUSES)}")
    conn.executemany("UPDATE events SET status = ? WHERE id = ?",
                     [(status, event_id) for event_id in event_ids])
    conn.commit()


def recover(conn: sqlite3.Connection) -> int:
    """Return every claimed row to pending, and say how many. The worker calls
    this once at startup: a claimed row then belongs to a run that died."""
    cursor = conn.execute("UPDATE events SET status = 'pending' WHERE status = 'claimed'")
    conn.commit()
    return cursor.rowcount


class Inbox:
    """What a handler holds: `add` writes one row from whichever thread the
    handler runs on. SQLite connections stay on the thread that opened them,
    so each thread gets its own."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self._local = threading.local()

    def add(self, *, source: str, external_id: str, session_id: str, payload: dict) -> bool:
        """True when a row was written, False for a redelivery."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from waku.db import connect

            conn = self._local.conn = connect(self.home)
        return insert(conn, source=source, external_id=external_id,
                      session_id=session_id, payload=payload) is not None
