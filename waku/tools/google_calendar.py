"""Google Calendar — read your real schedule, with a sign-in you can actually do.

Two OAuth-client sources, with the same read-only access:

  DEFAULT   waku's own OAuth client, shipped below. Click, approve in the
            browser, done. No files to download.

  OVERRIDE  your own `.waku/credentials.json`, when present. This replaces the
            OAuth client configuration only; it does not expand permissions.

Why the read client can live in a public repo: for Google's "Desktop app" OAuth
client type the secret is not confidential, and Google says so. It identifies
the app, it does not authorise anything. Nobody can read your calendar with it;
every user still approves in their own browser and their token is stored only on
their machine. This is what gcloud and rclone do. The blast radius of a leak is
brand impersonation and quota, not data — which is exactly why READ is shipped
and WRITE is not.

Everything here is best-effort and honest: a missing dependency, a refused
consent or an expired token returns a sentence explaining what to do, never an
exception, because a calendar hiccup must not take down a chat turn.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
DEFAULT_SCOPES = [READONLY_SCOPE]
TIMEOUT = 30

# waku's own OAuth client (Desktop app).
# TODO: Fill in the project-owned Google OAuth client ID and secret before
# release. Desktop-app client secrets are identifiers, not confidential
# credentials; every user must still grant access in their own browser.
BUNDLED_CLIENT_CONFIG = {
    "installed": {
        "client_id": "",
        "client_secret": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

_INSTALL_HINT = (
    "Google Calendar support is not installed — run: pip install -e '.[gcal]'"
)
_SETUP_HINT = (
    "Google Calendar is not connected yet. Run `waku connect google` (or click "
    "Connect in the dashboard's Connections tab) to sign in — it opens your "
    "browser and takes about ten seconds."
)


def _token_path(home: Path) -> Path:
    return home / "google-token.json"


def _load_credentials(home: Path, scopes: list[str]):
    """Cached token → refresh if stale → None. Never launches a browser: that
    only happens in connect(), so a chat turn can never block on a consent
    screen the user cannot see."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    path = _token_path(home)
    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), scopes=scopes)
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            return None
    return creds if (creds and creds.valid) else None


def connect(home: Path) -> str:
    """Open the browser, get consent, cache the token. The ONE place a browser
    window is allowed to appear — called from the CLI/dashboard, never mid-turn.

    Uses waku's bundled client by default. If you dropped your own
    `.waku/credentials.json` in place, that OAuth client configuration wins.
    Both paths request the same read-only scope."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return _INSTALL_HINT

    own = home / "credentials.json"
    try:
        if own.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(own), scopes=DEFAULT_SCOPES
            )
            client_source = "credentials.json override"
        else:
            bundled = BUNDLED_CLIENT_CONFIG.get("installed", {})
            if not bundled.get("client_id") or not bundled.get("client_secret"):
                return (
                    "Google Calendar's bundled OAuth client is not configured "
                    "yet. Add .waku/credentials.json to use your own Desktop "
                    "OAuth client."
                )
            flow = InstalledAppFlow.from_client_config(
                BUNDLED_CLIENT_CONFIG,
                scopes=DEFAULT_SCOPES,
            )
            client_source = "bundled OAuth client"
        # port=0 = any free local port. The redirect lands back on this machine,
        # so no server and no public URL are involved anywhere.
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        return f"Google sign-in failed or was cancelled ({type(exc).__name__}). Nothing was saved."

    path = _token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    return (
        f"Google Calendar connected (read-only via {client_source}). "
        f"Token cached at {path}."
    )


def list_google_events(home: Path, start: str = "", end: str = "", limit: int = 20) -> str:
    """Events from the user's PRIMARY Google calendar between two dates.

    `start`/`end` are YYYY-MM-DD; empty means today. Returns one event per line
    as `title | start | attendees`, or a sentence saying why it could not.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return _INSTALL_HINT

    creds = _load_credentials(home, DEFAULT_SCOPES)
    if creds is None:
        return _SETUP_HINT

    today = _dt.date.today().isoformat()
    s = (start or today)[:10]
    e = (end or s)[:10]
    # Google wants RFC3339 with an offset; local midnight to local midnight+1d
    # so "today" means the user's today, not UTC's.
    tz = _dt.datetime.now().astimezone().tzinfo
    lo = _dt.datetime.fromisoformat(s).replace(tzinfo=tz)
    hi = _dt.datetime.fromisoformat(e).replace(tzinfo=tz) + _dt.timedelta(days=1)

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        resp = service.events().list(
            calendarId="primary",
            timeMin=lo.isoformat(),
            timeMax=hi.isoformat(),
            singleEvents=True,          # expand recurring series into instances
            orderBy="startTime",
            maxResults=max(1, min(int(limit or 20), 100)),
        ).execute()
    except Exception as exc:
        return f"Google Calendar unavailable ({type(exc).__name__}: {str(exc)[:120]})"

    items = resp.get("items", [])
    if not items:
        return f"No Google Calendar events between {s} and {e}."
    lines = []
    for ev in items:
        when = ev.get("start", {})
        at = when.get("dateTime") or when.get("date") or "?"
        who = ", ".join(a.get("email", "") for a in ev.get("attendees", []) if a.get("email"))
        lines.append(f"{ev.get('summary', '(no title)')} | {at}" + (f" | {who}" if who else ""))
    return "\n".join(lines)


def is_connected(home: Path) -> bool:
    """True when a usable token exists. Cheap and side-effect free: callers use
    this to decide whether to even mention Google, so it must never trigger a
    browser or a network call."""
    return _token_path(home).exists()
