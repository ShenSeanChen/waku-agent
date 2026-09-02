"""log_interview — records/updates one interview process at a company as it
moves through rounds, so one process stays one row instead of fragmenting.

Matching is by company alone (not company+role): role is treated as a
correctable field of the process, not part of its identity. Regression:
matching on company+role meant fixing a wrong/placeholder role (e.g. the
model logging "未指定职位" before it knew the real title) silently created a
second row instead of correcting the first — see
test_log_interview_correcting_role_updates_same_row. Trade-off: two
simultaneously open interviews for different roles at the same company are
treated as one process (out of scope for v1, see the design spec).

See docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls

from waku.tools.registry import Tool

OPEN_STATUSES = ("已投递", "进行中", "通过", "待跟进")
VALID_STATUSES = ("已投递", "进行中", "通过", "失败", "待跟进")


def make_tool(conn: sqlite3.Connection) -> Tool:
    def log_interview(
        company: str,
        role: str,
        status: str,
        round: str | None = None,
        date: str | None = None,
        notes: str | None = None,
        channel: str | None = None,
    ) -> str:
        if status not in VALID_STATUSES:
            return f"Error: unknown status '{status}'. Valid statuses: {', '.join(VALID_STATUSES)}"
        entry_date = date or date_cls.today().isoformat()
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        existing = conn.execute(
            f"SELECT id, round, notes, channel FROM interview_entries WHERE lower(company)=lower(?) "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (company, *OPEN_STATUSES),
        ).fetchone()
        if existing:
            new_round = round if round else existing["round"]
            new_notes = notes if notes else existing["notes"]
            new_channel = channel if channel else existing["channel"]
            conn.execute(
                "UPDATE interview_entries SET role=?, round=?, date=?, status=?, notes=?, channel=?, "
                "updated_at=datetime('now') WHERE id=?",
                (role, new_round, entry_date, status, new_notes, new_channel, existing["id"]),
            )
            verb = "Updated"
            final_round = new_round
        else:
            conn.execute(
                "INSERT INTO interview_entries (company, role, round, date, status, notes, channel) "
                "VALUES (?,?,?,?,?,?,?)",
                (company, role, round or "", entry_date, status, notes or "", channel or ""),
            )
            verb = "Logged"
            final_round = round or ""
        conn.commit()
        return f"{verb} {company} — {role} ({final_round or 'no round given'}): {status} (state.db, interview_entries)"

    return Tool(
        name="log_interview",
        description=(
            "Record or update a job application/interview. Use status 已投递 the moment "
            "a resume is submitted, before any interview round has happened. If the same "
            "company already has an open entry (已投递, 进行中, or 待跟进), this UPDATES it "
            "in place with the new round/status/role/notes/channel instead of creating a "
            "duplicate — call it again as the process moves from application to interview "
            "rounds to a result, or to correct a wrong/placeholder role. Use when the user "
            "reports a resume was submitted, an interview happened, a result came in, or "
            "gives a recap/notes to remember."
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
                "channel": {"type": "string", "description": "How the application was submitted, e.g. 官网, 猎聘, 内推, LinkedIn"},
            },
            "required": ["company", "role", "status"],
        },
        fn=log_interview,
    )
