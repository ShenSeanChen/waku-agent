"""Timers: one row in `timers` per follow-up the agent set (spec 023, decision 3).

A timer is an event that arrives later. `schedule_task` calls `insert`. The
loop in `waku serve` turns each due row into one row in `events` with `source
= timer`, the timer's session and its note, and the worker starts a run for
it like for any other message.

The loop sleeps until the earliest timer is due, or for 30 seconds, whichever
is shorter, and `insert` wakes it at once (spec 024, decision 9). A timer set
in this process therefore fires within 2 seconds of its time. A timer set by
another process, the dashboard for example, is found at the next look, at most
30 seconds later.

  pending   waiting for its time (due_at)
  fired     its event is in the inbox
  cancelled someone cancelled it before its time (waiting.py)

v1 has no recurring timer. A run that wants to check again sets a new one.

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM timers'
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from waku.alwayson import events, outbox

SOURCE = "timer"
# The longest the loop sleeps between two looks at the table.
CHECK_SECONDS = 30.0
# The wake flag of every loop that runs in this process. `insert` sets them.
_LOOPS: set[threading.Event] = set()
_LOOPS_LOCK = threading.Lock()


def _wake_loops() -> None:
    with _LOOPS_LOCK:
        for wake in _LOOPS:
            wake.set()


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
    _wake_loops()   # the new timer may be due before the loop's sleep ends
    return cursor.lastrowid if cursor.rowcount else None


def next_due(conn: sqlite3.Connection) -> datetime | None:
    """When the earliest pending timer is due, or None without one."""
    row = conn.execute("SELECT MIN(due_at) AS due FROM timers WHERE status = 'pending'").fetchone()
    return datetime.fromisoformat(row["due"]) if row and row["due"] else None


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


def fire(conn: sqlite3.Connection, timer: Timer, now: datetime) -> None:
    """Write the timer's event, then mark the timer fired. The event's
    external id is `timer:<id>`, so a process that dies between the two writes
    writes no second event at its next start: UNIQUE(source, external_id)
    drops it, and the timer is marked then."""
    payload: dict = {"text": timer.note, "set_at": timer.created_at, "due_at": timer.due_at}
    if timer.address:
        payload["channel"], payload["reply_to"] = timer.address
    events.insert(conn, source=SOURCE, external_id=f"timer:{timer.id}",
                  session_id=timer.session_id, payload=payload,
                  received_at=outbox._stamp(now))
    mark_fired(conn, timer.id)


class _Stop(threading.Event):
    """The loop's stop flag. Setting it also ends the loop's sleep."""

    def __init__(self, wake: threading.Event) -> None:
        super().__init__()
        self._wake = wake

    def set(self) -> None:
        super().set()
        self._wake.set()


class TimerLoop:
    """The loop in `waku serve` that turns each due timer into an event."""

    def __init__(self, conn: sqlite3.Connection, *,
                 now: Callable[[], datetime] = outbox.utc_now,
                 log: Callable[[str], None] = print) -> None:
        self.conn, self._now, self._log = conn, now, log
        self._wake = threading.Event()
        self.stop = _Stop(self._wake)

    def step(self) -> int:
        """Fire every due timer. Returns how many fired."""
        now = self._now()
        found = due(self.conn, now)
        for timer in found:
            fire(self.conn, timer, now)
        return len(found)

    def sleep_seconds(self, interval: float = CHECK_SECONDS) -> float:
        """How long the loop may sleep: until the earliest timer is due, and
        `interval` at most."""
        earliest = next_due(self.conn)
        if earliest is None:
            return interval
        return min(interval, max(0.0, (earliest - self._now()).total_seconds()))

    def run_forever(self, interval: float = CHECK_SECONDS,
                    wait: Callable[[float], object] | None = None) -> None:
        """`wait` is for the evals: a sleep that moves a fake clock."""
        wait = wait or self._wake.wait
        with _LOOPS_LOCK:
            _LOOPS.add(self._wake)
        try:
            while not self.stop.is_set():
                # Cleared before the look, so an insert during the look is
                # not lost: it sets the flag again and the sleep ends at once.
                self._wake.clear()
                seconds = interval
                try:
                    self.step()
                    seconds = self.sleep_seconds(interval)
                except Exception as exc:
                    self._log(f"(serve) the timer loop hit an error: {type(exc).__name__}")
                if not self.stop.is_set():
                    wait(seconds)
        finally:
            with _LOOPS_LOCK:
                _LOOPS.discard(self._wake)
