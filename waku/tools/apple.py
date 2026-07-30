"""Apple-ecosystem tools (macOS) so Waku can brief you on your real week —
reading your actual Calendar.app (including email-invited events) and Mail, and
writing Reminders/Notes. Opt-in via WAKU_APPLE_TOOLS=1; first use triggers the
system Automation permission prompts. All AppleScript runs with a timeout and
returns honest error text so a slow/denied call never hangs a turn.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from waku.tools.registry import Tool

_TIMEOUT = 30
_cache: dict[str, tuple[float, str]] = {}


def _osa(script: str, timeout: int = _TIMEOUT) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Apple tools are macOS-only."
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "timed out — the app may be showing a permission dialog; approve it and retry."
    except OSError as exc:
        return False, f"could not run osascript ({exc})"
    if r.returncode != 0:
        return False, (r.stderr or "failed").strip()[:200]
    return True, r.stdout.strip()


def _cached(key: str, ttl: int, producer) -> str:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = producer()
    _cache[key] = (now, val)
    return val


def read_apple_calendar(days_ahead: int = 7) -> str:
    """Events from Calendar.app between now and days_ahead.

    Set WAKU_APPLE_CALENDARS=Work,Home to name the calendars you actually care
    about. That is not just a filter, it is the difference between working and
    timing out: querying one calendar costs ~4 seconds, and a real Mac easily
    has 30+ once holiday and subscribed calendars pile up. Walking all of them
    blows past any sane timeout, so with a list we address each calendar BY NAME
    and never enumerate the rest.

    Without the list we still enumerate everything, because a first-run user has
    no idea which names exist — but we say so in the timeout message rather than
    leaving them guessing.
    """
    def go() -> str:
        cals = [c.strip() for c in os.getenv("WAKU_APPLE_CALENDARS", "").split(",") if c.strip()]
        # BULK property access, never per-event. `summary of (every event ...)`
        # is ONE Apple Event returning a list; reading `summary of e` inside a
        # repeat loop is one round-trip per event per property. Measured on a
        # real Mac: bulk = 1.9s per calendar, per-event = 70s+ for five.
        def block(cal_expr: str, label: str) -> str:
            # `summary of (every event whose ...)` must be ONE expression. Binding
            # the events to a variable first and then asking for `summary of _ev`
            # fails with -1728 ("Can't get summary of {event id ...}") because the
            # variable holds a list of references, not a queryable specifier.
            return f'''
  try
    tell {cal_expr}
      set _su to summary of (every event whose start date ≥ startDate and start date ≤ endDate)
      set _st to start date of (every event whose start date ≥ startDate and start date ≤ endDate)
      repeat with i from 1 to (count of _su)
        set out to out & {label} & " | " & (item i of _su) & " | " & ((item i of _st) as string) & linefeed
      end repeat
    end tell
  end try'''

        if cals:
            blocks = "\n".join(block(f'calendar "{c}"', f'"{c}"') for c in cals)
            budget = 15 + 8 * len(cals)
        else:
            blocks = f'''
  repeat with cal in calendars{block("cal", "(name of cal)")}
  end repeat'''
            budget = 90
        # `launch` starts Calendar without stealing focus; without it a quit
        # Calendar.app answers -600 "Application isn't running" instead of
        # auto-starting, which read as a permissions problem for an hour.
        script = f'''
set out to ""
set startDate to current date
set endDate to (current date) + ({int(days_ahead)} * days)
launch application "Calendar"
tell application "Calendar"{blocks}
end tell
return out'''
        ok, res = _osa(script, timeout=budget)
        if ok:
            return res
        hint = "" if cals else " Set WAKU_APPLE_CALENDARS=Work,Home to read only the calendars you use."
        return f"Calendar unavailable: {res}{hint}"
    return _cached(f"cal:{days_ahead}", 600, go) or "No events in that window."


def read_apple_mail(hours: int = 48, limit: int = 20) -> str:
    """Recent Mail messages: subject, sender, date, and a message:// link that
    opens Mail at that exact message."""
    def go() -> str:
        script = f'''
set out to ""
set cutoff to (current date) - ({int(hours)} * hours)
tell application "Mail"
  set box to inbox
  set msgs to (messages of box whose date received ≥ cutoff)
  set n to 0
  repeat with m in msgs
    if n ≥ {int(limit)} then exit repeat
    set out to out & (subject of m) & " | " & (sender of m) & " | " & ((date received of m) as string) & " | message://%3c" & (message id of m) & "%3e" & linefeed
    set n to n + 1
  end repeat
end tell
return out'''
        ok, res = _osa(script, timeout=45)
        return res if ok else f"Mail unavailable: {res}"
    return _cached(f"mail:{hours}:{limit}", 300, go) or "No recent mail."


def create_reminder(title: str, due: str = "") -> str:
    props = f'name:"{title}"'
    if due:
        props += f', due date:(date "{due}")'
    ok, res = _osa(f'tell application "Reminders" to make new reminder with properties {{{props}}}')
    return f"Reminder created: {title}" if ok else f"Reminder failed: {res}"


def create_note(title: str, body: str = "") -> str:
    safe = (title + "\n" + body).replace('"', "'")
    ok, res = _osa(f'tell application "Notes" to make new note at folder "Notes" with properties {{body:"{safe}"}}')
    return f"Note created: {title}" if ok else f"Note failed: {res}"


def make_tools() -> list[Tool]:
    return [
        Tool("read_apple_calendar",
             "Read the user's real macOS Calendar for the next N days (includes events invited by email). Use for weekly/daily briefings and 'what's on my calendar'.",
             {"type": "object", "properties": {"days_ahead": {"type": "integer", "description": "default 7"}}},
             lambda days_ahead=7: read_apple_calendar(int(days_ahead))),
        Tool("read_apple_mail",
             "Read the user's recent Apple Mail (subject, sender, date, and a message:// link to open each). Use to brief the user on what needs attention.",
             {"type": "object", "properties": {"hours": {"type": "integer", "description": "look-back window, default 48"}}},
             lambda hours=48: read_apple_mail(int(hours))),
        Tool("create_reminder",
             "Create a reminder in Apple Reminders.",
             {"type": "object", "properties": {"title": {"type": "string"}, "due": {"type": "string", "description": 'optional, e.g. "Monday 9:00 AM"'}}, "required": ["title"]},
             lambda title, due="": create_reminder(title, due)),
        Tool("create_note",
             "Create a note in Apple Notes.",
             {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}}, "required": ["title"]},
             lambda title, body="": create_note(title, body)),
    ]
