"""What waits: timers that have not fired, calls that wait for the owner's
yes, and messages that are queued or dead (spec 024, decision 7).

`listing` reads the three tables and `cancel` takes one item off them. `waku
waiting`, the tools `list_waiting` and `cancel_waiting`, and the dashboard's
"Waiting" card all call these two functions, so they show the same thing.

An item's id says which table it is in:

  t3    a timer             timers.id = 3
  r7    a waiting call      runs.id = 7
  m12   a message           outbox.id = 12

A cancelled row keeps its record with the status `cancelled`. Nothing reads a
cancelled row again: the timer loop, the approval answer and the dispatcher
each take rows by their own status.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from waku.alwayson import approvals, outbox, runs, timers

CANCELLED = "cancelled"
_ID = re.compile(r"^([trm])([1-9]\d*)$")
_MAX_WORDS_CHARS = 300


class NotWaiting(LookupError):
    """The id names nothing that waits: it is not an id, or its item already
    fired, ran, was sent or was cancelled."""


@dataclass(frozen=True)
class Item:
    id: str        # t3, r7 or m12
    when: str      # UTC, ISO 8601: when the timer is due, when the owner was
                   # asked, or when the message is tried next
    what: str      # the timer's note, what the call will do, or the message
    where: str     # the conversation it belongs to, or where the message goes
    status: str    # pending, parked, queued or dead


@dataclass(frozen=True)
class Waiting:
    timers: list[Item] = field(default_factory=list)
    calls: list[Item] = field(default_factory=list)
    messages: list[Item] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.timers) + len(self.calls) + len(self.messages)


def _call_item(run: runs.Run) -> Item:
    call = run.pending_call or {}
    return Item(f"r{run.id}", run.ended_at,
                approvals.describe(call.get("tool", ""), call.get("args") or {}),
                run.session_id, run.status)


def listing(conn: sqlite3.Connection) -> Waiting:
    """Everything that waits, each list oldest first."""
    pending = conn.execute(
        "SELECT * FROM timers WHERE status = 'pending' ORDER BY due_at, id").fetchall()
    queued = conn.execute(
        "SELECT * FROM outbox WHERE status IN ('pending', 'dead') ORDER BY id").fetchall()
    return Waiting(
        timers=[Item(f"t{t.id}", t.due_at, t.note, t.session_id, t.status)
                for t in map(timers._timer, pending)],
        calls=[_call_item(run) for run in runs.parked(conn)],
        messages=[Item(f"m{m.id}", m.next_attempt_at, m.body[:_MAX_WORDS_CHARS],
                       outbox.format_address(m.channel, m.target),
                       "queued" if m.status == "pending" else m.status)
                  for m in map(outbox._message, queued)])


# For each kind of id: the table, and the statuses a row can be cancelled from.
_TABLES = {"t": ("timers", ("pending",)), "r": ("runs", ("parked",)),
           "m": ("outbox", ("pending", "dead"))}


def cancel(conn: sqlite3.Connection, item_id: str) -> Item:
    """Cancel one item and return it as it was. Raises NotWaiting when the id
    names nothing that waits. The status changes in one statement that names
    the status it expects, so an item that fires or is sent at the same moment
    is either cancelled or done, never both."""
    item_id = (item_id or "").strip().lower()
    found = _ID.match(item_id)
    now = listing(conn)
    item = next((i for i in (*now.timers, *now.calls, *now.messages) if i.id == item_id),
                None) if found else None
    if item is None:
        raise NotWaiting(f"'{item_id}' is not waiting. `waku waiting` lists what is.")
    table, statuses = _TABLES[found[1]]
    marks = ", ".join("?" for _ in statuses)
    cursor = conn.execute(
        f"UPDATE {table} SET status = ? WHERE id = ? AND status IN ({marks})",
        (CANCELLED, int(found[2]), *statuses))
    conn.commit()
    if not cursor.rowcount:
        raise NotWaiting(f"'{item_id}' is not waiting any more.")
    return item
