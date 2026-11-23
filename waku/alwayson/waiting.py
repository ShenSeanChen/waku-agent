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
from datetime import datetime, tzinfo

from waku.alwayson import approvals, outbox, runs, timers

CANCELLED = "cancelled"
# The zone `waku waiting` prints times in. None is the zone of this machine.
LOCAL: tzinfo | None = None
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
    repeat: str = ""   # a repeating timer's rule: "every day at 09:00". Its
                       # `when` is the next time it fires
    zone: str = ""     # the time zone that rule's time of day is in


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
        timers=[Item(f"t{t.id}", t.due_at, t.note, t.session_id, t.status, t.every, t.zone)
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


# ---- `waku waiting` ---------------------------------------------------------

USAGE = """\
waku waiting               list the timers, calls and messages that wait
waku waiting cancel <id>   cancel one: t3 is a timer, r7 a waiting call, m12 a message"""
NOTHING = "Nothing waits."
# Said after a repeating timer is cancelled: one cancel ends every later day.
STOPS = " It does not repeat any more."


def spoken(stamp: str, tz: tzinfo | None = None) -> str:
    """A stored UTC time as a person reads it: "Thursday 19 November 09:00"."""
    try:
        moment = datetime.fromisoformat(stamp).astimezone(tz)
    except ValueError:
        return stamp
    return f"{moment:%A} {moment.day} {moment:%B %H:%M}"


def timer_when(item: Item, tz: tzinfo | None = None) -> str:
    """When a timer fires, as a person reads it. A repeating timer says its
    rule first: "every day at 09:00, next Friday 20 November 09:00". Its next
    time is said in its own zone, the one its time of day is in."""
    if not item.repeat:
        return spoken(item.when, tz)
    return f"{item.repeat}, next {spoken(item.when, timers.zone_of(item.zone))}"


def render(found: Waiting, tz: tzinfo | None = None) -> str:
    """What `waku waiting` prints."""
    if not len(found):
        return NOTHING
    lines: list[str] = []

    def section(title: str, rows: list[str]) -> None:
        if rows:
            lines.extend([title, *rows, ""])

    section("Timers that have not fired",
            [f"  {i.id:<5} {timer_when(i, tz)}   {i.what}" for i in found.timers])
    section("Calls that wait for your yes",
            [f"  {i.id:<5} asked {spoken(i.when, tz)}   {i.what}" for i in found.calls])
    section("Messages that are queued or could not be sent",
            [f"  {i.id:<5} {i.status}   to {i.where}   {i.what}" for i in found.messages])
    lines.append("Cancel one with: waku waiting cancel <id>")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    from waku.config import load_settings
    from waku.db import connect

    if argv and (argv[0] != "cancel" or len(argv) != 2):
        print(USAGE)
        return 1
    settings = load_settings()
    settings.ensure_home()
    conn = connect(settings.home)
    try:
        if not argv:
            print(render(listing(conn), LOCAL))
            return 0
        try:
            item = cancel(conn, argv[1])
        except NotWaiting as exc:
            print(exc)
            return 1
        print(f"Cancelled {item.id}: {item.what}" + (STOPS if item.repeat else ""))
        return 0
    finally:
        conn.close()
