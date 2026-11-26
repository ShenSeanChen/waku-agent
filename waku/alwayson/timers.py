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

A REPEATING TIMER (spec 033) is one row that never leaves `pending`. Its
`repeat` is `daily` or `weekdays`, `local_time` is the wall-clock time it
fires at ("09:00") and `zone` is the person's time zone ("Europe/Madrid").
When it is due, the loop writes one event and moves `due_at` to the next
occurrence. The loop does this itself and the model is never asked to, so a
run that fails, parks or says nothing cannot end the series. Because the row
stays `pending` with the next time in `due_at`, everything that reads pending
timers needs no second rule: the loop's sleep, `waku waiting`, the cancel path
and the report a hosted container gives before it stops (handover.next_work).

  one event per occurrence   the event's external id is `timer:<id>:<date>`,
                             the occurrence's date in the timer's zone, and
                             UNIQUE(source, external_id) drops a second one
  no backlog                 a loop that was stopped for a week fires the
                             latest occurrence it missed, and only when that
                             one is less than LATE_HOURS old. Every older one
                             is skipped. The next fires at its own time
  daylight saving            the next occurrence is worked out from the local
                             date and time in the zone, never by adding 24
                             hours, so 09:00 stays 09:00 when the clocks change

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM timers'
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo

from waku.alwayson import events, outbox

SOURCE = "timer"
# A timer whose note starts with "PR desk" wakes the PR desk's morning run
# (spec 035). Its event carries the pull requests GitHub reported since this
# timer last fired, and it runs in a conversation of its own, `pr-desk:<id>`,
# so a review waiting there never blocks a call in the chat that set it: one
# call waits per conversation.
PR_DESK_NOTE = "pr desk"
# The longest the loop sleeps between two looks at the table.
CHECK_SECONDS = 30.0
# What `repeat` may hold, and the weekdays each fires on (Monday is 0).
REPEATS = {"daily": frozenset(range(7)), "weekdays": frozenset(range(5))}
EVERY = {"daily": "every day", "weekdays": "every weekday"}
# A missed occurrence still fires when it is younger than this. Two
# occurrences are 24 hours apart, so a late one and the next one on time are
# at least 11 hours apart, on a day the clocks change too.
LATE_HOURS = 12
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
    repeat: str = ""     # '' fires once; 'daily' or 'weekdays' repeats
    local_time: str = ""  # a repeating timer's wall-clock time, "09:00"
    zone: str = ""       # a repeating timer's time zone, "Europe/Madrid" or "+01:00"

    @property
    def every(self) -> str:
        """How a person says the repeat: "every day at 09:00", or ""."""
        return f"{EVERY[self.repeat]} at {self.local_time}" if self.repeat in EVERY else ""


def _timer(row: sqlite3.Row) -> Timer:
    reply_to = json.loads(row["reply_to"]) if row["reply_to"] else None
    address = (reply_to["channel"], reply_to["target"]) if reply_to else None
    return Timer(row["id"], row["created_at"], row["due_at"], row["session_id"],
                 row["note"], address, row["status"], row["repeat"], row["local_time"],
                 row["zone"])


# ---- the time zone and the occurrences of a repeating timer ------------------

def local_zone_name() -> str:
    """The name of this machine's time zone ("Europe/Madrid"), or "" when the
    machine does not say. A hosted container is started with TZ set to its
    person's zone. A Mac and most Linux machines link /etc/localtime into the
    zone database, and the name is the end of that path."""
    for name in (os.getenv("TZ", "").lstrip(":"),
                 os.path.realpath("/etc/localtime").partition("zoneinfo/")[2]):
        if name and zone_name(name):
            return name
    return ""


def zone_name(value: tzinfo | str | None) -> str:
    """The text stored in `zone` for a time zone: the name of a zone in the
    zone database, or a fixed offset as "+01:00". "" when `value` is neither."""
    if isinstance(value, str):
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            return ""
        return value
    if isinstance(value, zoneinfo.ZoneInfo):
        return value.key
    offset = value.utcoffset(None) if value is not None else None
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    return f"{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def zone_of(name: str) -> tzinfo:
    """The time zone `zone_name` wrote. A fixed offset has no daylight saving,
    so a timer stored with one drifts by an hour against the wall clock when
    the clocks change. A name this machine no longer knows is read as UTC."""
    if name[:1] in "+-" and len(name) == 6:
        sign = -1 if name[0] == "-" else 1
        return timezone(sign * timedelta(hours=int(name[1:3]), minutes=int(name[4:6])))
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def _on(day: date, local_time: str, zone: tzinfo) -> datetime:
    """The moment `local_time` is on the clock on `day` in `zone`, in UTC. On
    the day the clocks go forward, a time in the missing hour is read with the
    offset from before the change, so 02:30 fires when the clock says 03:30.
    On the day they go back, a time that comes twice fires at the first."""
    hour, minute = (int(part) for part in local_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(UTC)


def repeats_on(day: date, repeat: str) -> bool:
    return day.weekday() in REPEATS[repeat]


def next_occurrence(after: datetime, repeat: str, local_time: str, zone: str) -> datetime:
    """The first occurrence later than `after`."""
    tz = zone_of(zone)
    day = after.astimezone(tz).date()
    while True:
        moment = _on(day, local_time, tz)
        if moment > after and repeats_on(day, repeat):
            return moment
        day += timedelta(days=1)


def latest_occurrence(until: datetime, repeat: str, local_time: str, zone: str) -> datetime:
    """The last occurrence that is not later than `until`."""
    tz = zone_of(zone)
    day = until.astimezone(tz).date()
    while True:
        moment = _on(day, local_time, tz)
        if moment <= until and repeats_on(day, repeat):
            return moment
        day -= timedelta(days=1)


def insert(conn: sqlite3.Connection, *, due_at: datetime, session_id: str, note: str,
           address: tuple[str, dict] | None, idempotency_key: str,
           now: datetime | None = None, repeat: str = "", zone: str = "") -> int | None:
    """Write one pending row. Returns its id, or None when a row with this
    idempotency_key exists already. `due_at` must carry a time zone.

    With `repeat`, `due_at` is the first occurrence and `zone` is the person's
    time zone as zone_name wrote it. The time on the clock in that zone at
    `due_at` is the time every later occurrence fires at."""
    reply_to = json.dumps({"channel": address[0], "target": address[1]}) if address else ""
    local_time = ""
    if repeat:
        if repeat not in REPEATS:
            raise ValueError(f"'{repeat}' is not a repeat: {', '.join(REPEATS)}")
        if not zone or (zone[:1] not in "+-" and not zone_name(zone)):
            raise ValueError(f"a repeating timer needs a time zone, and '{zone}' is not one")
        local_time = f"{due_at.astimezone(zone_of(zone)):%H:%M}"
    cursor = conn.execute(
        "INSERT OR IGNORE INTO timers (created_at, due_at, session_id, note, reply_to, "
        "idempotency_key, repeat, local_time, zone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (outbox._stamp(now or outbox.utc_now()), outbox._stamp(due_at), session_id, note,
         reply_to, idempotency_key, repeat, local_time, zone if repeat else ""))
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


def is_pr_desk(note: str) -> bool:
    """Whether a timer's note asks for the PR desk: it starts with "PR desk"."""
    return (note or "").strip().lower().startswith(PR_DESK_NOTE)


def mark_fired(conn: sqlite3.Connection, timer_id: int) -> None:
    conn.execute("UPDATE timers SET status = 'fired' WHERE id = ?", (timer_id,))
    conn.commit()


def _last_event(conn: sqlite3.Connection, timer_id: int) -> int:
    """The id of the newest event this timer wrote, or 0."""
    row = conn.execute(
        "SELECT MAX(id) AS last FROM events WHERE source = ? "
        "AND (external_id = ? OR external_id LIKE ?)",
        (SOURCE, f"timer:{timer_id}", f"timer:{timer_id}:%")).fetchone()
    return row["last"] or 0


def _session(conn: sqlite3.Connection, timer: Timer, payload: dict) -> str:
    """The conversation a timer's run is in. A PR desk timer's payload also
    gets every quiet pull request since the timer last fired."""
    if not is_pr_desk(timer.note):
        return timer.session_id
    payload["prs"] = [row["pr"] for row in events.quiet_since(conn, _last_event(conn, timer.id))
                      if isinstance(row.get("pr"), dict)]
    return f"pr-desk:{timer.id}"


def fire(conn: sqlite3.Connection, timer: Timer, now: datetime) -> bool:
    """Write the timer's event, then mark the timer fired. The event's
    external id is `timer:<id>`, so a process that dies between the two writes
    writes no second event at its next start: UNIQUE(source, external_id)
    drops it, and the timer is marked then.

    A repeating timer is not marked: `_fire_repeating` moves it to its next
    occurrence. Returns False when no event was written for it."""
    if timer.repeat:
        return _fire_repeating(conn, timer, now)
    payload: dict = {"text": timer.note, "set_at": timer.created_at, "due_at": timer.due_at}
    if timer.address:
        payload["channel"], payload["reply_to"] = timer.address
    events.insert(conn, source=SOURCE, external_id=f"timer:{timer.id}",
                  session_id=_session(conn, timer, payload), payload=payload,
                  received_at=outbox._stamp(now))
    mark_fired(conn, timer.id)
    return True


def _fire_repeating(conn: sqlite3.Connection, timer: Timer, now: datetime) -> bool:
    """Write one event for the latest occurrence, then move the timer to the
    first occurrence after `now`. The two writes are in this order for the
    same reason as in `fire`: a process that dies between them finds the timer
    still due at its next start, writes the same external id, which the inbox
    drops, and moves the timer then.

    The move names the status it expects, so a timer that was cancelled a
    moment ago stays cancelled."""
    rule = (timer.repeat, timer.local_time, timer.zone)
    occurrence = max(latest_occurrence(now, *rule), datetime.fromisoformat(timer.due_at))
    on_time = now - occurrence < timedelta(hours=LATE_HOURS)
    if on_time:
        payload: dict = {"text": timer.note, "set_at": timer.created_at,
                         "due_at": outbox._stamp(occurrence), "repeat": timer.every}
        if timer.address:
            payload["channel"], payload["reply_to"] = timer.address
        day = occurrence.astimezone(zone_of(timer.zone)).date().isoformat()
        events.insert(conn, source=SOURCE, external_id=f"timer:{timer.id}:{day}",
                      session_id=_session(conn, timer, payload), payload=payload,
                      received_at=outbox._stamp(now))
    conn.execute("UPDATE timers SET due_at = ? WHERE id = ? AND status = 'pending'",
                 (outbox._stamp(next_occurrence(now, *rule)), timer.id))
    conn.commit()
    return on_time


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
        """Fire every due timer. Returns how many fired. A repeating timer
        whose missed occurrence is too old to send is moved on and not
        counted."""
        now = self._now()
        return sum(fire(self.conn, timer, now) for timer in due(self.conn, now))

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
