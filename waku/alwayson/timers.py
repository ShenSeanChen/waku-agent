"""Timers: one row in `timers` per follow-up the agent set (spec 023, decision 3).

A timer is an event that arrives later. `schedule_task` calls `insert`. The
loop in `waku serve` calls `fire_due` every 30 seconds: each due row becomes
one row in `events` with `source = timer`, the timer's session and its note,
and the worker starts a run for it like for any other message.

  pending   waiting for its time (due_at)
  fired     its event is in the inbox

v1 has no recurring timer. A run that wants to check again sets a new one.

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM timers'
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from waku.alwayson import outbox


@dataclass(frozen=True)
class Timer:
    id: int
    created_at: str      # UTC, ISO 8601
    due_at: str          # UTC, ISO 8601
    session_id: str      # the conversation that set it
    note: str            # what the agent told itself to do then
    address: tuple[str, dict] | None   # where the woken run's reply goes
    status: str


def _timer(row: sqlite3.Row) -> Timer:
    reply_to = json.loads(row["reply_to"]) if row["reply_to"] else None
    address = (reply_to["channel"], reply_to["target"]) if reply_to else None
    return Timer(row["id"], row["created_at"], row["due_at"], row["session_id"],
                 row["note"], address, row["status"])


def insert(conn: sqlite3.Connection, *, due_at: datetime, session_id: str, note: str,
           address: tuple[str, dict] | None, idempotency_key: str,
           now: datetime | None = None) -> int | None:
    """Write one pending row. Returns its id, or None when a row with this
    idempotency_key exists already. `due_at` must carry a time zone."""
    reply_to = json.dumps({"channel": address[0], "target": address[1]}) if address else ""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO timers (created_at, due_at, session_id, note, reply_to, "
        "idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
        (outbox._stamp(now or outbox.utc_now()), outbox._stamp(due_at), session_id, note,
         reply_to, idempotency_key))
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else None


def get(conn: sqlite3.Connection, timer_id: int) -> Timer | None:
    row = conn.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)).fetchone()
    return _timer(row) if row else None


def due(conn: sqlite3.Connection, now: datetime) -> list[Timer]:
    """Every pending row whose time has passed, oldest first."""
    rows = conn.execute(
        "SELECT * FROM timers WHERE status = 'pending' AND due_at <= ? ORDER BY due_at, id",
        (outbox._stamp(now),)).fetchall()
    return [_timer(row) for row in rows]


def mark_fired(conn: sqlite3.Connection, timer_id: int) -> None:
    conn.execute("UPDATE timers SET status = 'fired' WHERE id = ?", (timer_id,))
    conn.commit()
