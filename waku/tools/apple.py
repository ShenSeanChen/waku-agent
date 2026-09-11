"""Apple-ecosystem tools (macOS) so Waku can brief you on your real week —
reading your actual Calendar.app (including email-invited events) and Mail, and
writing Reminders/Notes. Opt-in via WAKU_APPLE_TOOLS=1; first use triggers the
system Automation permission prompts. All AppleScript runs with a timeout and
returns honest error text so a slow/denied call never hangs a turn.

THE PERFORMANCE RULE, learned the hard way on 2026-07-30 when every one of these
timed out on a real Mac while the tests stayed green:

  NEVER use a `whose` filter on a large collection.

Measured against a 472-event Google-synced calendar:

  every event whose start date ≥ X    ~25s   (and the tool needs two)
  start date of every event            ~6s   (then filter in Python)

`whose` makes Calendar.app evaluate a predicate per item across an Apple Event
bridge. Pulling the raw column once and filtering locally is 4x faster and gets
slower far more gracefully. Same rule applies to Mail.

Reminders is a separate problem: `name of list 1` — a trivial query — measured
22.8s on the same machine. Nothing to optimise there, it is simply slow, so it
gets a timeout that reflects reality instead of a misleading "timed out" string.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

from waku.tools.registry import Tool

_TIMEOUT = 30
_PROBE_TIMEOUT = 8
_PROBE_APPS = ("Calendar", "Mail", "Reminders", "Notes")
# A tool that takes 75s to FAIL is worse than no tool: it blocks a chat turn and
# then apologises. These budgets are set so a broken app gives up quickly with an
# actionable message. Measured on a real Mac 2026-07-31:
#   Calendar  `count of calendars`        instant   -> usable
#   Reminders `count of lists`            7.8s      -> slow but alive
#   Mail      `count of messages of inbox` TIMED OUT at 120s (-1712)
_SLOW_APP_TIMEOUT = 25
_cache: dict[str, tuple[float, str]] = {}



def _is_running(app: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", app], capture_output=True,
                              check=False, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell — assume it is up and let _osa report the truth


def ensure_running(app: str, timeout: float = 12.0) -> None:
    """Start `app` before talking to it, because AppleScript cannot.

    Both of the obvious idioms answer -600 "Application isn't running" when the
    app is not already running — the launch line is itself what raises, which is
    why the error's character offset points straight at it. We shipped one of
    them believing it fixed -600; it was the line producing it, so a closed
    Calendar failed exactly as before. Mail, Reminders and Notes never tried.

    Only a shell-level open can cold-start the app. `-g` keeps it behind the
    user's windows and `-j` starts it hidden, so reading the calendar does not
    steal focus mid-sentence.
    """
    if sys.platform != "darwin" or _is_running(app):
        return
    try:
        subprocess.run(["open", "-gj", "-a", app], capture_output=True,
                       check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return  # cannot launch it; _osa will say what actually went wrong
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_running(app):
            return
        time.sleep(0.25)


def _osa(script: str, timeout: int = _TIMEOUT, app: str | None = None) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Apple tools are macOS-only."
    if app:
        ensure_running(app)
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, (
            f"timed out after {timeout}s. The app is either slow (Reminders and large "
            "synced calendars routinely take 20s+) or waiting on a macOS permission "
            "dialog — check for one, approve it, and ask again."
        )
    except OSError as exc:
        return False, f"could not run osascript ({exc})"
    if r.returncode != 0:
        return False, (r.stderr or "failed").strip()[:200]
    return True, r.stdout.strip()


def probe_apple_tools() -> None:
    """Verify Automation access to every app without reading or writing user data."""
    failures = []
    for app in _PROBE_APPS:
        # Without the launch a closed app answers -600 and the probe reports it
        # as an Automation-permission failure — a different problem with a
        # different fix, and a trip to System Settings for nothing.
        ok, detail = _osa(
            f'tell application "{app}" to return version',
            timeout=_PROBE_TIMEOUT,
            app=app,
        )
        if not ok:
            failures.append(f"{app}: {detail}")
    if failures:
        raise RuntimeError("Apple Tools probe failed: " + "; ".join(failures))


def _parse_applescript_date(raw: str) -> datetime | None:
    """AppleScript renders dates as 'Thursday, July 30, 2026 at 1:00:00 PM' (with
    a locale-dependent prefix we ignore). Returns None rather than raising: one
    unparseable row must not lose the whole calendar."""
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{1,2}:\d{2}:\d{2}\s*[AP]M)", raw)
    if not m:
        return None
    try:
        # Naive on purpose: Calendar.app renders wall-clock local time with no
        # offset, and every comparison below is against local `now`. Attaching a
        # tz here would invent information AppleScript never gave us.
        return datetime.strptime(  # noqa: DTZ007
            f"{m.group(1)} {m.group(2).replace(chr(8239), ' ')}", "%B %d, %Y %I:%M:%S %p")
    except ValueError:
        return None


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

    Without the list we refuse and tell you how to find the names, because
    enumerating 37 calendars takes minutes and a wall of silence is worse than
    an instruction.
    """
    def go() -> str:
        cals = [c.strip() for c in os.getenv("WAKU_APPLE_CALENDARS", "").split(",") if c.strip()]
        if not cals:
            return (
                "Calendar not configured. Set WAKU_APPLE_CALENDARS to the calendars you "
                "actually use (e.g. WAKU_APPLE_CALENDARS=Work,Home) — a typical Mac has "
                "30+ once holiday and subscribed calendars pile up, and reading them all "
                "takes minutes. Run `osascript -e 'tell application \"Calendar\" to return "
                "name of every calendar'` to see the names."
            )

        # NO `whose` filter — see the module docstring. We pull two raw columns
        # per calendar (~6s each) and match them up in Python. The filter itself
        # is free here; it is the Apple Event predicate that costs 25s.
        blocks = "\n".join(f'''
  try
    tell calendar "{c}"
      set _su to summary of every event
      set _st to start date of every event
      repeat with i from 1 to (count of _su)
        set out to out & "{c}" & "\\t" & (item i of _su) & "\\t" & ((item i of _st) as string) & linefeed
      end repeat
    end tell
  end try''' for c in cals)

        script = f'''
set out to ""
tell application "Calendar"{blocks}
end tell
return out'''
        ok, res = _osa(script, timeout=20 + 12 * len(cals), app="Calendar")
        if not ok:
            return f"Calendar unavailable: {res}"

        lo = datetime.now()
        hi = lo + timedelta(days=int(days_ahead))
        rows = []
        for line in res.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            when = _parse_applescript_date(parts[2])
            if when and lo.date() <= when.date() <= hi.date():
                rows.append((when, f"{parts[0]} | {parts[1]} | {when:%Y-%m-%d %H:%M}"))
        rows.sort()
        return "\n".join(r[1] for r in rows)
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
  set msgs to messages of box
  set n to 0
  repeat with m in msgs
    if n ≥ {int(limit)} then exit repeat
    set out to out & (subject of m) & " | " & (sender of m) & " | " & ((date received of m) as string) & " | message://%3c" & (message id of m) & "%3e" & linefeed
    set n to n + 1
  end repeat
end tell
return out'''
        ok, res = _osa(script, timeout=20, app="Mail")
        if ok:
            return res
        return (
            f"Mail unavailable: {res} Note: on this machine Mail.app could not even "
            "count its own inbox within two minutes, which usually means a very large "
            "mailbox or an in-progress sync. Apple Mail reading is effectively "
            "unavailable until that settles."
        )
    return _cached(f"mail:{hours}:{limit}", 300, go) or "No recent mail."


def create_reminder(title: str, due: str = "") -> str:
    props = f'name:"{title}"'
    if due:
        props += f', due date:(date "{due}")'
    ok, res = _osa(f'tell application "Reminders" to make new reminder with properties {{{props}}}',
                   timeout=_SLOW_APP_TIMEOUT, app="Reminders")
    return f"Reminder created: {title}" if ok else f"Reminder failed: {res}"


def create_note(title: str, body: str = "") -> str:
    safe = (title + "\n" + body).replace('"', "'")
    ok, res = _osa(f'tell application "Notes" to make new note at folder "Notes" with properties {{body:"{safe}"}}',
                   timeout=_SLOW_APP_TIMEOUT, app="Notes")
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
