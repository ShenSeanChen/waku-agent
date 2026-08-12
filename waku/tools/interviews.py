"""log_interview — records/updates one interview process (company + role) as
it moves through rounds, so one process stays one row instead of fragmenting.

See docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls

from waku.tools.registry import Tool

OPEN_STATUSES = ("进行中", "待跟进")
VALID_STATUSES = ("进行中", "通过", "失败", "待跟进")


def make_tool(conn: sqlite3.Connection) -> Tool:
    def log_interview(
        company: str,
        role: str,
        status: str,
        round: str | None = None,
        date: str | None = None,
        notes: str | None = None,
    ) -> str:
        if status not in VALID_STATUSES:
            return f"Error: unknown status '{status}'. Valid statuses: {', '.join(VALID_STATUSES)}"
        entry_date = date or date_cls.today().isoformat()
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        existing = conn.execute(
            f"SELECT id, round, notes FROM interview_entries WHERE lower(company)=lower(?) AND lower(role)=lower(?) "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (company, role, *OPEN_STATUSES),
        ).fetchone()
        if existing:
            new_round = round if round else existing["round"]
            new_notes = notes if notes else existing["notes"]
            conn.execute(
                "UPDATE interview_entries SET round=?, date=?, status=?, notes=?, "
                "updated_at=datetime('now') WHERE id=?",
                (new_round, entry_date, status, new_notes, existing["id"]),
            )
            verb = "Updated"
            final_round = new_round
        else:
            conn.execute(
                "INSERT INTO interview_entries (company, role, round, date, status, notes) "
                "VALUES (?,?,?,?,?,?)",
                (company, role, round or "", entry_date, status, notes or ""),
            )
            verb = "Logged"
            final_round = round or ""
        conn.commit()
        return f"{verb} {company} — {role} ({final_round or 'no round given'}): {status} (state.db, interview_entries)"

    return Tool(
        name="log_interview",
        description=(
            "Record or update an interview. If the same company+role already has an "
            "open entry (进行中 or 待跟进), this UPDATES it in place with the new round/"
            "status/notes instead of creating a duplicate — call it again for each new "
            "round of the same process. Use when the user reports an interview happened, "
            "a result came in, or gives a recap/notes to remember."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "round": {"type": "string", "description": "e.g. 一面, 二面, HR面"},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                "date": {"type": "string", "description": "ISO date; defaults to today"},
                "notes": {"type": "string", "description": "Recap: questions asked, self-assessment, etc."},
            },
            "required": ["company", "role", "status"],
        },
        fn=log_interview,
    )
