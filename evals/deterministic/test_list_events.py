"""DETERMINISTIC EVAL — list_events tool logic."""

from __future__ import annotations

import sqlite3

import pytest

from waku.tools.calendar import make_list_tool


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY,
            title TEXT,
            start TEXT,
            "end" TEXT,
            attendees TEXT,
            notes TEXT
        )
        """
    )
    yield connection
    connection.close()


@pytest.fixture
def home(tmp_path):
    # Mock home directory
    return tmp_path


def seed_local(conn, title, start, end, attendees=""):
    conn.execute(
        'INSERT INTO calendar_events (title, start, "end", attendees, notes) VALUES (?,?,?,?,?)',
        (title, start, end, attendees, "")
    )
    conn.commit()


def test_list_events_merges_sources_and_labels_them(conn, home, monkeypatch):
    """Events from two sources are merged into one list, each labelled with its source."""
    seed_local(conn, "Local Sync", "2026-08-15T10:00", "2026-08-15T11:00")
    
    monkeypatch.setattr("waku.tools.google_calendar.is_connected", lambda h: True)
    monkeypatch.setattr("waku.tools.google_calendar.list_google_events", lambda h, s, e, l: "- Google Sync: 2026-08-15T14:00 → 2026-08-15T15:00")
    
    tool = make_list_tool(conn, home)
    result = tool.fn()
    
    assert "From Google Calendar:" in result
    assert "Google Sync: 2026-08-15T14:00 → 2026-08-15T15:00" in result
    assert "From waku's local calendar" in result
    assert "Local Sync: 2026-08-15T10:00 → 2026-08-15T11:00" in result


def test_list_events_empty_names_calendars_checked(conn, home, monkeypatch):
    """An empty result names the calendars that were checked, rather than just saying 'nothing'."""
    monkeypatch.setattr("waku.tools.google_calendar.is_connected", lambda h: True)
    monkeypatch.setattr("waku.tools.google_calendar.list_google_events", lambda h, s, e, l: "No Google events found.")
    
    tool = make_list_tool(conn, home)
    result = tool.fn()
    
    assert "No events found" in result
    assert "Checked: Google Calendar, waku's local calendar" in result


def test_list_events_graceful_failure_of_one_source(conn, home, monkeypatch):
    """One source failing still returns the other source's events rather than an error."""
    seed_local(conn, "Local Only", "2026-08-16T09:00", "2026-08-16T10:00")
    
    monkeypatch.setattr("waku.tools.google_calendar.is_connected", lambda h: True)
    monkeypatch.setattr("waku.tools.google_calendar.list_google_events", lambda h, s, e, l: "Google Calendar unavailable: credentials invalid.")
    
    tool = make_list_tool(conn, home)
    result = tool.fn()
    
    # Should not include the google failure as a section
    assert "Google Calendar unavailable" not in result 
    assert "From waku's local calendar" in result
    assert "Local Only: 2026-08-16T09:00 → 2026-08-16T10:00" in result


def test_list_events_respects_date_boundaries(conn, home, monkeypatch):
    """The date window is respected at the boundaries."""
    seed_local(conn, "Way Before", "2026-08-10T09:00", "2026-08-10T10:00")
    seed_local(conn, "On Start Day", "2026-08-15T00:00", "2026-08-15T01:00")
    seed_local(conn, "Middle", "2026-08-16T12:00", "2026-08-16T13:00")
    seed_local(conn, "On End Day", "2026-08-17T23:59", "2026-08-18T00:59")
    seed_local(conn, "Way After", "2026-08-20T09:00", "2026-08-20T10:00")
    
    monkeypatch.setattr("waku.tools.google_calendar.is_connected", lambda h: False)
    
    tool = make_list_tool(conn, home)
    result = tool.fn(start="2026-08-15", end="2026-08-17")
    
    assert "Way Before" not in result
    assert "On Start Day" in result
    assert "Middle" in result
    assert "On End Day" in result
    assert "Way After" not in result


def test_list_events_no_home_does_not_crash(conn):
    """list_events should work even if home is not provided (e.g., just local DB check)."""
    seed_local(conn, "Local Event", "2026-08-15T10:00", "2026-08-15T11:00")
    
    tool = make_list_tool(conn, home=None)
    result = tool.fn()
    
    assert "From waku's local calendar" in result
    assert "Local Event" in result
