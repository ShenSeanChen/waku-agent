"""schedule_task: the agent sets its own follow-up (spec 023, decision 3).

The tool writes one row into the `timers` table of state.db and returns. The
timer loop in `waku serve` turns the row into an event when its time comes,
and the worker starts a run in the conversation that set it. That run reads
the note as the reason it woke. Without `waku serve` running the timer waits,
and it fires at the next start.

What the tool answers is for the person: "Timer 1 is set for Tuesday 14:09."
It names no process and no table, because the model repeats what it is given
(spec 024, decision 3).

`when` is an absolute time. A time without an offset is read in the time zone
of this machine, which is the person's. The tool refuses a time in the past
and a time more than one year away.

Without `repeat` a timer fires once. With `repeat` (spec 033) it is `daily` or
`weekdays`: `when` is the first time it fires, and the time on the person's
clock at `when` is the time of every later day. The timer loop sets the next
occurrence itself (waku/alwayson/timers.py), so the model sets a repeating
timer once and never again. A daily brief is such a timer whose note starts
with "Daily brief": the run it wakes is given the steps of the brief
(BRIEF_PROMPT in waku/alwayson/worker.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path

from waku.tools.registry import Tool

_EXAMPLE = "2026-11-19T09:00"
_FORM = f"`when` is a date and a time such as {_EXAMPLE}"


def _parse(when: str, tz: tzinfo | None) -> datetime | None:
    """The moment `when` names, or None when it is not a date with a time."""
    when = (when or "").strip()
    if "T" not in when and " " not in when:
        return None   # a date alone, or a time alone, names no moment
    try:
        moment = datetime.fromisoformat(when)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # tz=None is the machine's own zone; astimezone() reads a time without
        # an offset as local time.
        moment = moment.replace(tzinfo=tz) if tz else moment.astimezone()
    return moment


def _one_year_after(moment: datetime) -> datetime:
    try:
        return moment.replace(year=moment.year + 1)
    except ValueError:   # 29 February
        return moment.replace(year=moment.year + 1, day=28)


def _spoken(due: datetime, current: datetime) -> str:
    """The time as a person says it: "Tuesday 14:09" within the next six days,
    and with the date after that."""
    current = current.astimezone(due.tzinfo)
    if (due.date() - current.date()).days < 7:
        return f"{due:%A %H:%M}"
    year = f" {due.year}" if due.year != current.year else ""
    return f"{due:%A} {due.day} {due:%B}{year} {due:%H:%M}"


def make_tool(home: Path, session_id: Callable[[], str] | None = None,
              now: Callable[[], datetime] | None = None, tz: tzinfo | None = None) -> Tool:
    """`now` and `tz` are for the evals: a clock that a test moves, and a time
    zone that does not depend on the machine the test runs on."""

    def schedule_task(when: str = "", note: str = "", repeat: str = "") -> str:
        from waku.alwayson import outbox, timers
        from waku.alwayson.worker import serving
        from waku.db import connect

        note = (note or "").strip()
        repeat = (repeat or "").strip().lower()
        due = _parse(when, tz)
        if due is None:
            return f"Not set: Waku cannot read '{when}' as a time. {_FORM}."
        if not note:
            return "Not set: the note is empty. Say what to do when the timer is due."
        if repeat and repeat not in timers.REPEATS:
            return (f"Not set: `repeat` is 'daily' or 'weekdays', and '{repeat}' is neither. "
                    "Leave it out for a timer that fires once.")
        current = (now or outbox.utc_now)()
        if due <= current:
            return (f"Not set: {when} is in the past. It is now "
                    f"{current.astimezone(due.tzinfo):%Y-%m-%dT%H:%M}.")
        if due > _one_year_after(current):
            return f"Not set: {when} is more than one year away."
        zone = ""
        if repeat:
            # The zone's name keeps 09:00 at 09:00 when the clocks change. A
            # machine that does not say its zone's name gives its offset.
            zone = (timers.zone_name(tz) if tz else timers.local_zone_name()
                    or timers.zone_name(due.tzinfo))
            if not timers.repeats_on(due.astimezone(timers.zone_of(zone)).date(), repeat):
                # "Every weekday at 9" said on a Friday evening starts on Monday.
                due = timers.next_occurrence(
                    due, repeat, f"{due.astimezone(timers.zone_of(zone)):%H:%M}", zone)

        # The woken run answers in the conversation this run came from. A
        # timer set in the terminal or the dashboard has no such address, so
        # its run answers the owner.
        run = outbox.current_run(home)
        owners = outbox.owner_addresses()
        address = run.address if run and run.address else (owners[0] if owners else None)
        key = run.next_timer_key() if run else f"chat:{uuid.uuid4().hex}"
        conn = connect(home)
        try:
            timer_id = timers.insert(
                conn, due_at=due, session_id=session_id() if session_id else "default",
                note=note, address=address, idempotency_key=key, now=current,
                repeat=repeat, zone=zone)
            every = timers.get(conn, timer_id).every if timer_id and repeat else ""
        finally:
            conn.close()
        at = _spoken(due.astimezone(timers.zone_of(zone)) if zone else due, current)
        if timer_id is None:
            return f"The timer for {at} was already set by an earlier attempt of this run."
        said = f"Timer {timer_id} is set for {at}."
        if every:
            said = (f"Timer {timer_id} repeats {every}, first on {at}. It sets its next "
                    "time by itself, so do not set it again.")
        if not address:
            said += (" My reply then has nowhere to go: WAKU_OWNER is not set, so Waku does "
                     "not know where the owner is reached.")
        if run is None and not serving(home):
            said += " It waits until Waku's always-on mode is running, and fires then."
        return said

    return Tool(
        name="schedule_task",
        description=(
            "Set a timer for yourself: at `when` you are woken in this "
            "conversation with `note` as the reason. Use when you or the user want "
            "something done or checked later ('on Thursday', 'in two hours', 'tomorrow "
            "morning'). " + _FORM + ", in the user's time zone; work it out from the "
            "current date and time. Without `repeat` the timer fires once. With "
            "`repeat` it fires every day or every weekday at the time of day in `when`, "
            "and `when` is the first time: set a repeating timer once and never again. "
            "Write the note for your later self: it is all you will be told. For a "
            "regular brief ('send me a brief every morning at 9') set one repeating "
            "timer whose note starts with 'Daily brief', followed by anything the user "
            "wants it to cover; you are given the steps of the brief when it is due."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "when": {"type": "string",
                         "description": f"The absolute time, ISO 8601, e.g. {_EXAMPLE}"},
                "note": {"type": "string",
                         "description": "What to do or check then, with every name and "
                                        "number it needs"},
                "repeat": {"type": "string", "enum": ["daily", "weekdays"],
                           "description": "Leave out for a timer that fires once"},
            },
            "required": ["when", "note"],
        },
        fn=schedule_task,
    )
