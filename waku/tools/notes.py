"""save_note — writes a durable fact into semantic memory, on request.

This is the *explicit* memory path ("remember that Alex prefers mornings").
The *implicit* path is consolidation (waku/memory/consolidation.py), which
distills facts out of chat history without being asked.

Spec 018: with Waku Memory connected, the fact is also sent there, through the
same `remember` callable consolidation uses, and the answer names where it
went. Until then the answer said "Saved to memory" while the fact stayed on this
agent, and the model told the person it was saved.
"""

from __future__ import annotations

import sqlite3

from waku.tools.registry import Tool

SCOPE = "global"
REASON_MAX = 80


def _reason(exc: Exception) -> str:
    """One short line a person can read: the first line of the error, cut."""
    first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return first[:REASON_MAX] or type(exc).__name__


def make_tool(conn: sqlite3.Connection, memory=None) -> Tool:
    def save_note(subject: str, content: str) -> str:
        subject = subject.lower().strip()
        # Read at call time: app.py sets `memory.remember` after the tool
        # registry is built, once the Waku Memory connection exists.
        remember = getattr(memory, "remember", None) if memory is not None else None
        if remember is None:
            conn.execute(
                "INSERT INTO facts (subject, content, source) VALUES (?,?,'user')",
                (subject, content),
            )
            conn.commit()
            return "Saved here. Waku Memory is not connected."

        # Stored unsynced first and marked once the send works, so a failed
        # send, or a process that dies mid-send, leaves the fact for the next
        # consolidation to retry (spec 006).
        cur = conn.execute(
            "INSERT INTO facts (subject, content, source, synced, scope) VALUES (?,?,'user',0,?)",
            (subject, content, SCOPE),
        )
        conn.commit()
        try:
            memory_id = remember(content, SCOPE)
        except Exception as exc:
            return f"Saved here. Waku Memory did not take it ({_reason(exc)}); it is sent again later."
        conn.execute("UPDATE facts SET synced = 1 WHERE id = ?", (cur.lastrowid,))
        conn.commit()
        suffix = f" (id {memory_id})" if memory_id else ""
        return f"Saved here and in Waku Memory{suffix}."

    return Tool(
        name="save_note",
        description=(
            "Save a durable fact to long-term memory. Use when the user tells you something "
            "worth remembering about themselves, a person, or a project — especially if they "
            "say 'remember' or share a preference. The answer says where the fact was saved; "
            "repeat it to the user exactly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Who/what this is about, e.g. 'alex' or 'acme-project'"},
                "content": {"type": "string", "description": "The fact, one sentence"},
            },
            "required": ["subject", "content"],
        },
        fn=save_note,
    )
