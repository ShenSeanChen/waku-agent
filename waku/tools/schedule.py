"""schedule_task: the agent sets its own follow-up (spec 023, decision 3).

The tool writes one row into the `timers` table of state.db and returns. The
timer loop in `waku serve` turns the row into an event when its time comes,
and the worker starts a run in the conversation that set it. That run reads
the note as the reason it woke. Without `waku serve` running the timer waits,
and it fires at the next start.

`when` is an absolute time. A time without an offset is read in the time zone
of this machine, which is the person's. The tool refuses a time in the past
and a time more than one year away. A timer fires once: a run that wants to
check again sets a new one.
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


def make_tool(home: Path, session_id: Callable[[], str] | None = None,
              now: Callable[[], datetime] | None = None, tz: tzinfo | None = None) -> Tool:
    """`now` and `tz` are for the evals: a clock that a test moves, and a time
    zone that does not depend on the machine the test runs on."""

    def schedule_task(when: str = "", note: str = "") -> str:
        from waku.alwayson import outbox, timers
        from waku.db import connect

        note = (note or "").strip()
        due = _parse(when, tz)
        if due is None:
            return f"Not set: Waku cannot read '{when}' as a time. {_FORM}."
        if not note:
            return "Not set: the note is empty. Say what to do when the timer is due."
        current = (now or outbox.utc_now)()
        if due <= current:
            return (f"Not set: {when} is in the past. It is now "
                    f"{current.astimezone(due.tzinfo):%Y-%m-%dT%H:%M}.")
        if due > _one_year_after(current):
            return f"Not set: {when} is more than one year away."

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
                note=note, address=address, idempotency_key=key, now=current)
        finally:
            conn.close()
        at = f"{due:%A %Y-%m-%d %H:%M} (UTC{due:%z})"
        if timer_id is None:
            return f"The timer for {at} was already set by an earlier attempt of this run."
        if address:
            where = f"My reply then goes to {outbox.format_address(*address)}."
        else:
            where = ("My reply then has nowhere to go: WAKU_OWNER is not set, so Waku does "
                     "not know where the owner is reached.")
        return (f"Timer {timer_id} is set for {at}. `waku serve` wakes me then with this "
                "note; while `waku serve` is not running the timer waits. " + where)

    return Tool(
        name="schedule_task",
        description=(
            "Set a timer for yourself: at `when`, `waku serve` wakes you in this "
            "conversation with `note` as the reason. Use when you or the user want "
            "something done or checked later ('on Thursday', 'in two hours', 'tomorrow "
            "morning'). " + _FORM + ", in the user's time zone; work it out from the "
            "current date and time. The timer fires once, so a recurring check sets a "
            "new timer each time. Write the note for your later self: it is all you "
            "will be told."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "when": {"type": "string",
                         "description": f"The absolute time, ISO 8601, e.g. {_EXAMPLE}"},
                "note": {"type": "string",
                         "description": "What to do or check then, with every name and "
                                        "number it needs"},
            },
            "required": ["when", "note"],
        },
        fn=schedule_task,
    )
