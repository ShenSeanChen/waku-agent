"""create_event — the flagship tool. "Did the meeting trigger?" is THE
deterministic eval: it either wrote the right row or it didn't.

Where events land:
  always      state.db (the eval asserts here) + calendar.ics (importable file)
  opt-in      Apple Calendar, in a dedicated "Waku" calendar, via AppleScript —
              set WAKU_APPLE_CALENDAR=1. First use makes macOS ask permission
              for your terminal to control Calendar; approve once.
  opt-in      Google Calendar via WAKU_GOOGLE_CALENDAR=1. Local files remain
              authoritative if credentials, the network, or Google fail.

The tool's return string always says exactly where the event went — the model
relays it, so Waku never over-claims what happened.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from waku.tools.registry import Tool

APPLE_CALENDAR_NAME = "Waku"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_CALENDAR_TIMEOUT = 30


def _write_ics(home: Path, title: str, start: str, end: str, attendees: str) -> None:
    """Append a minimal VEVENT. ISO timestamps like 2026-07-14T09:00 become
    ICS's compact 20260714T090000 form."""
    ics_path = home / "calendar.ics"

    def dt(s: str) -> str:
        return s.replace("-", "").replace(":", "") + ("00" if len(s) == 16 else "")

    event = (
        "BEGIN:VEVENT\n"
        f"SUMMARY:{title}\n"
        f"DTSTART:{dt(start)}\n"
        f"DTEND:{dt(end)}\n"
        f"DESCRIPTION:attendees: {attendees}\n"
        "END:VEVENT\n"
    )
    if ics_path.exists():
        body = ics_path.read_text(encoding="utf-8").replace("END:VCALENDAR\n", "")
    else:
        body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//waku-agent//EN\n"
    ics_path.write_text(body + event + "END:VCALENDAR\n", encoding="utf-8")


def _applescript_date(var: str, iso: str) -> str:
    """Build an AppleScript date from ISO parts — immune to system locale
    (never feed AppleScript a formatted date string; parsing is locale-bound)."""
    d = datetime.fromisoformat(iso)
    # set day to 1 BEFORE month/year: prevents the classic AppleScript overflow
    # (if today is the 31st, setting month to a 30-day month rolls into next month)
    return (
        f"set {var} to current date\nset day of {var} to 1\n"
        f"set year of {var} to {d.year}\nset month of {var} to {d.month}\n"
        f"set day of {var} to {d.day}\nset hours of {var} to {d.hour}\n"
        f"set minutes of {var} to {d.minute}\nset seconds of {var} to 0\n"
    )


def sync_to_apple_calendar(title: str, start: str, end: str, notes: str = "") -> str:
    """Create the event in Calendar.app under the 'Waku' calendar (created on
    first use). Returns a short human-readable outcome for the tool output."""
    if sys.platform != "darwin":
        return "Apple Calendar sync skipped (not macOS)."
    safe_title = title.replace("\\", "").replace('"', "'")
    safe_notes = notes.replace("\\", "").replace('"', "'")
    # Prefer a dedicated "Waku" calendar, but macOS can't create calendars in
    # iCloud-only accounts via AppleScript — fall back to the first writable
    # calendar and report which one was actually used.
    script = (
        _applescript_date("startDate", start)
        + _applescript_date("endDate", end)
        + f'''
tell application "Calendar"
  if not (exists calendar "{APPLE_CALENDAR_NAME}") then
    try
      make new calendar with properties {{name:"{APPLE_CALENDAR_NAME}"}}
      delay 1
    end try
  end if
  if exists calendar "{APPLE_CALENDAR_NAME}" then
    set targetCal to calendar "{APPLE_CALENDAR_NAME}"
  else
    set targetCal to first calendar whose writable is true
  end if
  tell targetCal
    make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate, description:"{safe_notes}"}}
  end tell
  return name of targetCal
end tell'''
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return (
            "Apple Calendar sync timed out — this usually means macOS is showing a "
            "permission dialog ('would like to add to your Calendar'). The event is safe "
            "in the local calendar; approve the dialog and ask me to create it again."
        )
    except OSError as exc:
        return f"Apple Calendar sync FAILED ({exc}) — the event is still in the local calendar."
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:120]
        return (
            f"Apple Calendar sync FAILED ({detail}) — the event is still in the local "
            "calendar. If this is a permissions error, allow your terminal to control "
            "Calendar in System Settings > Privacy & Security > Automation."
        )
    used = (result.stdout or "").strip() or APPLE_CALENDAR_NAME
    return f"Also added to Apple Calendar (calendar '{used}')."


def _google_event_body(
    title: str, start: str, end: str, attendees: str = "", notes: str = ""
) -> dict:
    """Map the stable create_event fields to Google Calendar's event shape."""

    def rfc3339(value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.isoformat()

    body = {
        "summary": title,
        "start": {"dateTime": rfc3339(start)},
        "end": {"dateTime": rfc3339(end)},
    }
    if notes:
        body["description"] = notes
    emails = [parseaddr(item.strip())[1] for item in attendees.split(",")]
    emails = [email for email in emails if "@" in email]
    if emails:
        body["attendees"] = [{"email": email} for email in emails]
    return body


def sync_to_google_calendar(
    title: str,
    start: str,
    end: str,
    attendees: str = "",
    notes: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create one Google Calendar event without changing the local-first contract."""
    try:
        import google.auth
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build
    except ImportError:
        return (
            "Google Calendar sync FAILED (support is not installed; "
            "install with pip install -e '.[gcal]') — the event is still in the "
            "local calendar."
        )

    try:
        credentials, _ = google.auth.default(scopes=[GOOGLE_CALENDAR_SCOPE])
        bounded_http = httplib2.Http(timeout=GOOGLE_CALENDAR_TIMEOUT)
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=bounded_http
        )
        service = build(
            "calendar",
            "v3",
            http=authorized_http,
            cache_discovery=False,
            static_discovery=True,
        )
        (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=_google_event_body(title, start, end, attendees, notes),
                sendUpdates="none",
            )
            .execute(num_retries=0)
        )
    except Exception as exc:
        detail = (str(exc).strip() or type(exc).__name__)[:160]
        return (
            f"Google Calendar sync FAILED ({detail}) — the event is still in the "
            "local calendar."
        )
    return (
        f"Also added to Google Calendar (calendar '{calendar_id}'; "
        "attendee notifications suppressed)."
    )


def make_tool(
    conn: sqlite3.Connection,
    home: Path,
    apple_calendar: bool = False,
    google_calendar: bool = False,
    google_calendar_id: str = "primary",
) -> Tool:
    def create_event(
        title: str = "",
        start: str = "",
        end: str = "",
        attendees: str = "",
        notes: str = "",
    ) -> str:
        # Defensive: models sometimes emit an empty/partial tool call. Return a
        # helpful message the model can recover from, not a raw Python TypeError.
        if not title or not start:
            return ("create_event needs at least a title and a start time "
                    "(ISO 8601, e.g. 2026-07-14T09:00). Please call it again with both.")
        if not end:
            # default: one hour
            from datetime import timedelta
            end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(timespec="minutes")

        # idempotence guard: same title+start = same event. A confused model
        # (or an impatient user) must not be able to triple-book a meeting.
        start = start[:16]  # normalize 2026-07-11T17:00:00 → 2026-07-11T17:00
        end = end[:16]
        existing = conn.execute(
            "SELECT id FROM calendar_events WHERE title = ? AND start = ?", (title, start)
        ).fetchone()
        if existing:
            return f"Event '{title}' at {start} already exists (not duplicated)."

        conn.execute(
            'INSERT INTO calendar_events (title, start, "end", attendees, notes) VALUES (?,?,?,?,?)',
            (title, start, end, attendees, notes),
        )
        conn.commit()
        _write_ics(home, title, start, end, attendees)

        where = f"Saved to the local calendar ({home / 'calendar.ics'})."
        if apple_calendar:
            where += " " + sync_to_apple_calendar(title, start, end, notes)
        if google_calendar:
            where += " " + sync_to_google_calendar(
                title,
                start,
                end,
                attendees,
                notes,
                calendar_id=google_calendar_id,
            )
        if not apple_calendar and not google_calendar:
            where += (
                " Not synced to any calendar app (enable with WAKU_APPLE_CALENDAR=1 "
                "or WAKU_GOOGLE_CALENDAR=1, "
                f"or import manually: open {home / 'calendar.ics'})."
            )
        return (
            f"Event created: '{title}' {start} → {end}"
            + (f" with {attendees}" if attendees else "")
            + f". {where}"
        )

    return Tool(
        name="create_event",
        description=(
            "Create a calendar event on the user's local calendar. Use whenever the user "
            "wants to schedule, book, or plan something at a specific time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short event title"},
                "start": {"type": "string", "description": "Start time, ISO 8601, e.g. 2026-07-14T09:00"},
                "end": {"type": "string", "description": "End time, ISO 8601. Defaults to start + 1h."},
                "attendees": {"type": "string", "description": "Comma-separated names/emails"},
                "notes": {"type": "string", "description": "Optional context for the event"},
            },
            "required": ["title", "start"],
        },
        fn=create_event,
    )


def make_list_tool(conn: sqlite3.Connection, home: Path | None = None) -> Tool:
    """list_events — the read side of the calendar, across EVERY connected source.

    One tool, not one per backend. Two calendar tools with overlapping names is
    how the agent ends up answering "your calendar is clear" from a database of
    demo events while the user is staring at a full Thursday in Calendar.app —
    which is exactly what happened on 2026-07-30. The model should not have to
    guess which calendar means "mine".

    Order matters: Google first, because for anyone signed in that IS their real
    schedule; the local SQLite calendar second, because it only ever holds what
    waku itself created. Every source is LABELLED in the output, so the agent can
    say where an answer came from instead of implying it saw everything.

    Apple Calendar is deliberately not read here: going through AppleScript to
    reach Google-synced calendars measured ~51 seconds on a real Mac (472 events,
    two `whose` queries). It stays available as its own opt-in tool for genuinely
    local calendars. Google's API answers the same question in ~0.4s.
    """
    def local_events(start: str = "", end: str = "", limit: int = 20) -> str:
        query = 'SELECT title, start, "end", attendees FROM calendar_events'
        clauses, params = [], []
        if start:
            clauses.append("start >= ?")
            params.append(start[:10])                 # inclusive from the start of that day
        if end:
            clauses.append("start <= ?")
            params.append(end[:10] + "T23:59")        # inclusive through the end of that day
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY start LIMIT ?"
        params.append(max(1, min(int(limit or 20), 100)))
        rows = conn.execute(query, params).fetchall()
        if not rows:
            return ""
        lines = []
        for r in rows:
            who = f" with {r['attendees']}" if r["attendees"] else ""
            lines.append(f"- {r['title']}: {r['start']} → {r['end']}{who}")
        return "\n".join(lines)

    def list_events(start: str = "", end: str = "", limit: int = 20) -> str:
        sections: list[str] = []
        checked: list[str] = []

        if home is not None:
            from waku.tools.google_calendar import is_connected, list_google_events

            if is_connected(home):
                checked.append("Google Calendar")
                g = list_google_events(home, start, end, limit)
                # A "no events" sentence is not a section; only real rows are.
                if g and not g.startswith(("No Google", "Google Calendar unavailable")):
                    sections.append("From Google Calendar:\n" + g)

        checked.append("waku's local calendar")
        local = local_events(start, end, limit)
        if local:
            sections.append("From waku's local calendar (events waku created):\n" + local)

        if sections:
            return "\n\n".join(sections)
        window = f" between {start} and {end}" if (start or end) else ""
        # Name every source that was actually consulted. "Your calendar is clear"
        # is only honest if the user knows WHICH calendars that covers.
        return f"No events found{window}. Checked: {', '.join(checked)}."

    return Tool(
        name="list_events",
        description=(
            "Read the user's calendar across every connected source (Google Calendar "
            "when signed in, plus waku's own local calendar). "
            "Use whenever the user asks what's on their calendar / schedule for a day, "
            "week, yesterday, etc. Dates are ISO (e.g. 2026-07-10); omit both to list "
            "everything upcoming. For 'yesterday'/'today' resolve the date from the "
            "current time given in your system prompt. The result labels which "
            "calendar each event came from — repeat that when it matters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "earliest date to include, ISO (e.g. 2026-07-10)"},
                "end": {"type": "string", "description": "latest date to include, ISO (e.g. 2026-07-10)"},
                "limit": {"type": "integer", "description": "max events to return (default 20)"},
            },
            "required": [],
        },
        fn=list_events,
    )
