"""The "[tools used: ...]" note an assistant turn carries in history.

After a turn that called tools, Session.add_exchange appends one line to the
assistant's record:

    Booked it.
    [tools used: create_event({'title': 'X'}) -> Created "X" on Mon 9:00 ...]

The note has one job: the next turn's model must see that it already acted,
or it re-runs the tool (the triple-booked-meeting bug). It used to carry each
tool's FULL output. That record is the history the model is sent on every
later turn of the conversation and the row chat_log keeps, so one
treg_catalog_search (tens of kilobytes of JSON) was re-sent to the model on
every turn after it, fed whole to consolidation, and drawn in the chat as a
block thousands of lines tall when the thread was reopened. The full output
already lives where it belongs: in the turn's trace and in the live tool row.

So the note is compact: the tool's name, its arguments cut short, and its
output cut to OUTPUT_MAX characters, which keeps a status line ("Created
...", "error: ...") and the start of any JSON. Rows written before this keep
their long notes; clip() caps those wherever old history is read back, and
the chat strips the note before drawing a reply (stripToolNote in render.js).
"""

from __future__ import annotations

import re

PREFIX = "[tools used: "
ARGS_MAX = 120
OUTPUT_MAX = 300
# A whole note read back from an old row (one or several calls) is held to this.
NOTE_MAX = 600

_SPACE = re.compile(r"\s+")
# The tool each call in a note names: `name(` after the prefix or after "; ".
_NAMED = re.compile(r"(?:^\[tools used: |; )(\w+)\(")
# What the model reads when its reply held the note of a call it did not make.
NO_CALL_WAS_MADE = (
    "[Nobody sent this message. Your last answer ended with a \"[tools used: ...]\" line "
    "that you wrote yourself, and it names a tool you did not call in this turn. Waku "
    "writes that line after a call, never you. Nothing was done. If the request needs "
    "the tool, call it now. Then answer again, without that line, and say only what "
    "the tools of this turn answered.]")


def _short(text: object, limit: int) -> str:
    """One line, at most `limit` characters, saying how much was cut."""
    flat = _SPACE.sub(" ", str(text)).strip()
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}... [{len(flat) - limit} more chars]"


def note(tool_calls: list[dict]) -> str:
    """The compact note for one turn's tool calls, or "" when there were none."""
    if not tool_calls:
        return ""
    calls = "; ".join(
        f"{c['tool']}({_short(c.get('args'), ARGS_MAX)}) -> {_short(c.get('output', ''), OUTPUT_MAX)}"
        for c in tool_calls)
    return f"{PREFIX}{calls}]"


def strip(record: str) -> str:
    """The reply alone: the record with its note removed (for display)."""
    i = (record or "").find(PREFIX)
    return (record or "").strip() if i < 0 else record[:i].strip()


def clip(record: str) -> str:
    """An old record with its note held to NOTE_MAX characters, so a row
    written before notes were compact is not re-sent to a model whole."""
    i = (record or "").find(PREFIX)
    if i < 0 or len(record) - i <= NOTE_MAX:
        return record
    return record[:i] + _short(record[i:], NOTE_MAX - 1) + "]"


def claimed(reply: str) -> list[str]:
    """The tools a note in a reply names, in order. [] without a note."""
    i = (reply or "").find(PREFIX)
    return [] if i < 0 else _NAMED.findall(reply[i:])


def imitated(reply: str, tool_calls: list[dict]) -> bool:
    """Whether a reply holds a note that names a call the turn did not make.
    The model wrote that note itself: it copied the form from its history."""
    made = {call["tool"] for call in tool_calls}
    return any(name not in made for name in claimed(reply))
