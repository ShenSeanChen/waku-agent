"""update_event — reschedule an existing calendar event in place.

Regression: create_event only ever INSERTs. Asking to move an interview
("EPAM's interview got pushed to next Tuesday") had no update path, so the
model called create_event again and the old event just sat there unchanged
alongside a brand-new one — two events for what should be one process. See
CLAUDE.md / the 2026-08-19 bug report.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from waku.tools.calendar import make_tool, make_update_tool


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        'CREATE TABLE calendar_events (id INTEGER PRIMARY KEY, title TEXT, start TEXT, '
        '"end" TEXT, attendees TEXT DEFAULT \'\', notes TEXT DEFAULT \'\', '
        "created_at TEXT DEFAULT (datetime('now')));"
    )
    return c


@pytest.fixture
def home():
    return Path(tempfile.mkdtemp())


def test_update_event_moves_existing_event_in_place_without_duplicating(conn, home):
    create = make_tool(conn, home).fn
    update = make_update_tool(conn, home).fn

    create(title="EPAM 面试 - 第一轮", start="2026-08-18T10:00", end="2026-08-18T11:00")

    out = update(old_title="EPAM", new_start="2026-08-25T09:00")

    rows = conn.execute("SELECT title, start, \"end\" FROM calendar_events").fetchall()
    assert len(rows) == 1, "reschedule must update the row in place, not insert a new one"
    assert rows[0]["start"] == "2026-08-25T09:00"
    # duration (1h) preserved from the original event
    assert rows[0]["end"] == "2026-08-25T10:00"
    assert "2026-08-25T09:00" in out

    ics = (home / "calendar.ics").read_text(encoding="utf-8")
    assert ics.count("BEGIN:VEVENT") == 1, "ics must reflect the move, not accumulate a duplicate"
    assert "20260818T100000" not in ics
    assert "20260825T090000" in ics


def test_update_event_no_match_reports_error_and_touches_nothing(conn, home):
    update = make_update_tool(conn, home).fn

    out = update(old_title="EPAM", new_start="2026-08-25T09:00")

    assert "No event found" in out
    assert conn.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 0


def test_update_event_ambiguous_match_asks_to_be_more_specific(conn, home):
    create = make_tool(conn, home).fn
    update = make_update_tool(conn, home).fn

    create(title="EPAM 面试 - 第一轮", start="2026-08-18T10:00")
    create(title="EPAM 二面", start="2026-08-20T10:00")

    out = update(old_title="EPAM", new_start="2026-08-25T09:00")

    assert "multiple events match" in out.lower() or "more specific" in out.lower()
    rows = conn.execute("SELECT start FROM calendar_events ORDER BY id").fetchall()
    assert [r["start"] for r in rows] == ["2026-08-18T10:00", "2026-08-20T10:00"], (
        "ambiguous match must not touch either row"
    )
