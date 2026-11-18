"""Runs: one row in `runs` per run the worker starts (spec 023, decision 4).

The worker calls `start` before a run and `finish` after it. The row says
which events the run read, how it ended, and the id of its turn (spec 011), so
a run joins its receipt and its trace.

  running   the run is in flight, or the process died while it was
  done      it ended
  failed    it raised
  parked    it ended with one tool call waiting for the owner's yes
            (approvals.py); `pending_call` holds that call
  expired   nobody answered for 72 hours
  cancelled the owner cancelled the waiting call (waiting.py)

A run that is replayed after a crash starts with the same first event, so
`start` returns the row the first attempt wrote. The run's id is the same, and
so is every idempotency key built from it.

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM runs'
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from waku.alwayson import outbox

STATUSES = ("running", "done", "failed", "parked", "expired", "cancelled")
EXPIRE_HOURS = 72
# The owner is reminded once, this long before a waiting call expires.
REMIND_HOURS_BEFORE = 24


@dataclass(frozen=True)
class Run:
    id: int
    session_id: str
    started_at: str            # UTC, ISO 8601
    ended_at: str              # UTC, ISO 8601, or "" while the run is in flight
    status: str
    event_ids: list[int]       # the events the run read, oldest first
    pending_call: dict | None  # the tool call that waits for approval
    pending_hash: str          # SHA-256 of that call, or ""
    turn_id: str               # spec 011's id for the run's turn, or ""


@dataclass
class Outcome:
    """What a run hands back to the worker. A run may also return its reply
    as plain text, or None."""
    reply: str | None
    turn_id: str = ""


def _run(row: sqlite3.Row) -> Run:
    return Run(row["id"], row["session_id"], row["started_at"], row["ended_at"], row["status"],
               json.loads(row["event_ids"]),
               json.loads(row["pending_call"]) if row["pending_call"] else None,
               row["pending_hash"], row["turn_id"])


def get(conn: sqlite3.Connection, run_id: int) -> Run | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run(row) if row else None


def find(conn: sqlite3.Connection, session_id: str, first_event_id: int) -> Run | None:
    """The run whose first event is this one, or None."""
    row = conn.execute(
        "SELECT * FROM runs WHERE session_id = ? AND json_extract(event_ids, '$[0]') = ? "
        "ORDER BY id DESC LIMIT 1", (session_id, first_event_id)).fetchone()
    return _run(row) if row else None


def start(conn: sqlite3.Connection, session_id: str, event_ids: list[int],
          now: datetime | None = None) -> Run:
    """The row for the run these events start, `running`. An event starts one
    run, so a row whose first event is this batch's first event is an earlier
    attempt of the same run, and it is used again."""
    stamp, ids = outbox._stamp(now or outbox.utc_now()), json.dumps(event_ids)
    earlier = find(conn, session_id, event_ids[0])
    if earlier:
        run_id = earlier.id
        conn.execute("UPDATE runs SET status = 'running', started_at = ?, ended_at = '', "
                     "event_ids = ? WHERE id = ?", (stamp, ids, run_id))
    else:
        run_id = conn.execute(
            "INSERT INTO runs (session_id, started_at, event_ids) VALUES (?, ?, ?)",
            (session_id, stamp, ids)).lastrowid
    conn.commit()
    return get(conn, run_id)


def finish(conn: sqlite3.Connection, run_id: int, status: str, turn_id: str = "",
           now: datetime | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"'{status}' is not a run status: {', '.join(STATUSES)}")
    conn.execute("UPDATE runs SET status = ?, ended_at = ?, turn_id = ? WHERE id = ?",
                 (status, outbox._stamp(now or outbox.utc_now()), turn_id, run_id))
    conn.commit()


def set_pending(conn: sqlite3.Connection, run_id: int, call: dict, call_hash: str) -> None:
    """Store the one tool call this run leaves waiting for approval."""
    conn.execute("UPDATE runs SET pending_call = ?, pending_hash = ? WHERE id = ?",
                 (json.dumps(call), call_hash, run_id))
    conn.commit()


def resolve(conn: sqlite3.Connection, run_id: int, status: str, *, approved: bool = False,
            now: datetime | None = None) -> None:
    """Close a parked run once the owner answered. The stored call stays in
    the row as a record of what was asked, with the answer and its time."""
    answered = json.dumps({"approved": approved,
                           "at": outbox._stamp(now or outbox.utc_now())})
    conn.execute(
        "UPDATE runs SET status = ?, pending_call = json_set(pending_call, '$.answered', "
        "json(?)) WHERE id = ? AND status = 'parked'", (status, answered, run_id))
    conn.commit()


def ran_at(conn: sqlite3.Connection, call_hash: str, since: datetime) -> datetime | None:
    """When the owner last approved a call with this hash, if that was after
    `since` and the call ran. None otherwise."""
    row = conn.execute(
        "SELECT json_extract(pending_call, '$.answered.at') AS at FROM runs "
        "WHERE pending_hash = ? AND status = 'done' "
        "AND json_extract(pending_call, '$.answered.approved') = 1 "
        "AND json_extract(pending_call, '$.answered.at') >= ? "
        "ORDER BY at DESC LIMIT 1", (call_hash, outbox._stamp(since))).fetchone()
    return datetime.fromisoformat(row["at"]) if row else None


def parked(conn: sqlite3.Connection, session_id: str | None = None) -> list[Run]:
    """Every parked run, oldest first; of one session when one is named."""
    where, values = ("AND session_id = ?", (session_id,)) if session_id else ("", ())
    rows = conn.execute(f"SELECT * FROM runs WHERE status = 'parked' {where} ORDER BY id",
                        values).fetchall()
    return [_run(row) for row in rows]


def overdue(run: Run, now: datetime, hours: int = EXPIRE_HOURS) -> bool:
    """Whether a parked run has waited `hours` or longer."""
    return run.ended_at <= outbox._stamp(now - timedelta(hours=hours))


def expire(conn: sqlite3.Connection, now: datetime) -> list[Run]:
    """Mark every run that has been parked for 72 hours `expired`, and return
    those runs as they were. Their calls can no longer be approved. The worker
    is the one caller: it tells the owner and the conversation that asked
    (spec 024, decision 8)."""
    found = [run for run in parked(conn) if overdue(run, now)]
    for run in found:
        conn.execute("UPDATE runs SET status = 'expired' WHERE id = ? AND status = 'parked'",
                     (run.id,))
    conn.commit()
    return found


def to_remind(conn: sqlite3.Connection, now: datetime) -> list[Run]:
    """The parked runs that have waited 48 hours and are not yet reminded."""
    return [run for run in parked(conn)
            if overdue(run, now, EXPIRE_HOURS - REMIND_HOURS_BEFORE)
            and not (run.pending_call or {}).get("reminded")]


def mark_reminded(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE runs SET pending_call = json_set(pending_call, '$.reminded', "
                 "json('true')) WHERE id = ?", (run_id,))
    conn.commit()
