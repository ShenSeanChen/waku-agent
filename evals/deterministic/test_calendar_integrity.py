"""Calendar inputs must keep their instant and produce importable ICS text."""

import time
from datetime import UTC, datetime

import pytest

from waku.db import connect
from waku.tools import calendar


@pytest.mark.parametrize("start,end", [
    ("not a date", "also invalid"),
    ("2026-02-30T09:00", "2026-02-30T10:00"),
    ("2026-11-07", "2026-11-08"),
    ("2026-11-07T10:00", "2026-11-07T09:00"),
    ("2026-11-07T10:00", "2026-11-07T10:00"),
])
def test_invalid_dates_leave_no_event_or_export(tmp_path, start, end):
    conn = connect(tmp_path)
    result = calendar.make_tool(conn, tmp_path).fn(title="invalid", start=start, end=end)
    assert "create_event" in result and "Saved" not in result
    assert conn.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 0
    assert not (tmp_path / "calendar.ics").exists()
    conn.close()


def test_offsets_keep_the_instant_and_deduplicate_equivalent_times(tmp_path):
    conn = connect(tmp_path)
    create = calendar.make_tool(conn, tmp_path).fn
    create(title="meeting", start="2026-11-07T09:00:00Z")
    row = conn.execute('SELECT start, "end" FROM calendar_events').fetchone()
    start, end = [datetime.fromisoformat(value) for value in row]
    assert start.tzinfo is not None
    assert start == datetime(2026, 11, 7, 9, tzinfo=UTC)
    assert (end - start).total_seconds() == 3600
    assert "already exists" in create(title="meeting", start="2026-11-07T17:00:00+08:00")
    assert "Event created" in create(title="meeting", start="2026-11-07T09:00:00+08:00")
    assert conn.execute("SELECT count(*) FROM calendar_events").fetchone()[0] == 2
    data = (tmp_path / "calendar.ics").read_bytes()
    assert b"DTSTART:20261107T090000Z\r\n" in data
    assert b"DTSTART:20261107T010000Z\r\n" in data
    conn.close()


def test_old_naive_events_still_deduplicate(tmp_path):
    conn = connect(tmp_path)
    create = calendar.make_tool(conn, tmp_path).fn
    create(title="legacy", start="2026-11-07T09:00")
    aware = datetime(2026, 11, 7, 9).astimezone().isoformat()
    assert "already exists" in create(title="legacy", start=aware)
    assert conn.execute("SELECT start FROM calendar_events").fetchone()[0] == "2026-11-07T09:00"
    conn.close()


def test_export_escapes_text_and_folds_utf8_without_splitting_characters(tmp_path):
    conn = connect(tmp_path)
    title = "Review, phase; one\\two\r\nnext steps " + "上海" * 50
    create = calendar.make_tool(conn, tmp_path).fn
    create(title=title, start="2026-11-07T09:00", attendees="Alex,Bob;team\nremote")
    create(title="second", start="2026-11-08T09:00")
    data = (tmp_path / "calendar.ics").read_bytes()
    lines = data.split(b"\r\n")
    assert all(len(line) <= 75 for line in lines)
    assert all(line.decode("utf-8") is not None for line in lines)
    unfolded = data.replace(b"\r\n ", b"").decode("utf-8")
    expected = "Review\\, phase\\; one\\\\two\\nnext steps " + "上海" * 50
    assert f"SUMMARY:{expected}\r\n" in unfolded
    assert "DESCRIPTION:attendees: Alex\\,Bob\\;team\\nremote\r\n" in unfolded
    assert unfolded.count("BEGIN:VCALENDAR") == unfolded.count("END:VCALENDAR") == 1
    assert unfolded.count("BEGIN:VEVENT") == unfolded.count("END:VEVENT") == 2
    conn.close()


def test_last_minute_of_day_includes_offset_and_seconds(tmp_path):
    conn = connect(tmp_path)
    local = datetime(2026, 11, 7, 23, 59, 30).astimezone()
    calendar.make_tool(conn, tmp_path).fn(title="late", start=local.isoformat())
    result = calendar.make_list_tool(conn).fn(start="2026-11-07", end="2026-11-07")
    assert "late" in result
    conn.close()


def test_google_sync_receives_the_original_instant(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(calendar, "sync_to_google_calendar", lambda *args, **kwargs: captured.append(args) or "synced")
    conn = connect(tmp_path)
    calendar.make_tool(conn, tmp_path, google_calendar=True).fn(
        title="UTC meeting", start="2026-11-07T09:00:00Z"
    )
    assert datetime.fromisoformat(captured[0][1]) == datetime(2026, 11, 7, 9, tzinfo=UTC)
    assert datetime.fromisoformat(captured[0][2]) == datetime(2026, 11, 7, 10, tzinfo=UTC)
    conn.close()


@pytest.fixture
def sydney_timezone(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("the process timezone cannot be changed on this platform")
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Australia/Sydney")
            time.tzset()
            yield
    finally:
        time.tzset()


def test_list_orders_repeated_dst_hour_by_instant(tmp_path, sydney_timezone):
    conn = connect(tmp_path)
    create = calendar.make_tool(conn, tmp_path).fn
    create(title="later", start="2026-04-05T02:30:00+10:00")
    create(title="earlier", start="2026-04-05T02:30:00+11:00")
    result = calendar.make_list_tool(conn).fn(start="2026-04-05", end="2026-04-05")
    assert result.index("earlier") < result.index("later")
    conn.close()


def test_list_projects_saved_offset_into_current_local_day(tmp_path, monkeypatch, sydney_timezone):
    conn = connect(tmp_path)
    calendar.make_tool(conn, tmp_path).fn(title="traveler", start="2026-11-07T20:00:00Z")
    # Sydney stores this as November 8. After changing local timezone, it belongs to November 7.
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "UTC")
        time.tzset()
        try:
            result = calendar.make_list_tool(conn).fn(start="2026-11-07", end="2026-11-07")
            assert "traveler" in result
        finally:
            conn.close()
