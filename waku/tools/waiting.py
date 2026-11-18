"""list_waiting and cancel_waiting: the agent sees and cancels what waits
(spec 024, decision 7).

"What reminders do I have?" and "cancel the Thursday one" work from chat. Both
tools call waku/alwayson/waiting.py, which `waku waiting` and the dashboard's
"Waiting" card call too.

Who sees what: the terminal, the dashboard and the owner's own chat see
everything. A run in anyone else's conversation sees and cancels only what
that conversation set, so Ana's thread cannot read or cancel Sean's timers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo
from pathlib import Path

from waku.tools.registry import Tool

NOTHING = "Nothing waits: no timer, no call that waits for a yes, and no unsent message."


def make_tools(home: Path, session_id: Callable[[], str] | None = None,
               tz: tzinfo | None = None) -> list[Tool]:
    """`tz` is for the evals: the zone the times are said in. None is the
    zone of this machine, which is the person's."""

    def visible():
        """(the connection, what this conversation may see)."""
        from waku.alwayson import outbox, waiting
        from waku.db import connect

        conn = connect(home)
        found = waiting.listing(conn)
        run = outbox.current_run(home)
        if run is None or run.address is None or run.address in outbox.owner_addresses():
            return conn, found
        own = {session_id() if session_id else "", outbox.format_address(*run.address)}
        return conn, waiting.Waiting(*([i for i in group if i.where in own]
                                       for group in (found.timers, found.calls,
                                                     found.messages)))

    def list_waiting() -> str:
        from waku.alwayson.waiting import spoken

        conn, found = visible()
        conn.close()
        if not len(found):
            return NOTHING
        lines = []
        if found.timers:
            lines.append("Timers:")
            lines += [f"  {i.id}: {spoken(i.when, tz)}. {i.what}" for i in found.timers]
        if found.calls:
            lines.append("Calls that wait for the owner's yes:")
            lines += [f"  {i.id}: {i.what} (asked {spoken(i.when, tz)})" for i in found.calls]
        if found.messages:
            lines.append("Messages that are not sent:")
            lines += [f"  {i.id}: to {i.where}, {i.status}. {i.what}" for i in found.messages]
        lines.append("To cancel one, call cancel_waiting with its id.")
        return "\n".join(lines)

    def cancel_waiting(id: str = "") -> str:  # noqa: A002  (the model's argument name)
        from waku.alwayson import waiting

        conn, found = visible()
        try:
            wanted = (id or "").strip().lower()
            if not any(i.id == wanted for i in (*found.timers, *found.calls, *found.messages)):
                return (f"Not cancelled: '{id}' is not waiting here. Call list_waiting for "
                        "the ids.")
            try:
                item = waiting.cancel(conn, wanted)
            except waiting.NotWaiting:
                return f"Not cancelled: '{id}' is not waiting any more."
            return f"Cancelled {item.id}: {item.what}"
        finally:
            conn.close()

    return [
        Tool(
            name="list_waiting",
            description=(
                "List what waits: timers you set that have not fired, calls that wait for "
                "the owner's yes, and messages that are not sent yet. Use when the user "
                "asks what reminders, timers or pending requests they have. Each item has "
                "an id for cancel_waiting."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=list_waiting,
        ),
        Tool(
            name="cancel_waiting",
            description=(
                "Cancel one timer, one call that waits for a yes, or one unsent message, "
                "by the id list_waiting gave (t3, r7 or m12). Use when the user asks to "
                "cancel a reminder or a pending request. Call list_waiting first when you "
                "do not have the id."
            ),
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string",
                                      "description": "The item's id, e.g. t3"}},
                "required": ["id"],
            },
            fn=cancel_waiting,
        ),
    ]
