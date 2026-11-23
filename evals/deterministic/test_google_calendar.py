"""DETERMINISTIC EVAL — Google Calendar is an opt-in write target only."""

from __future__ import annotations

import sys
from types import ModuleType

from evals.helpers import ScriptedClient, make_waku
from waku.config import Settings
from waku.db import connect
from waku.tools import calendar


def _install_fake_google_modules(monkeypatch, *, execute_error: Exception | None = None):
    captured = {}

    google = ModuleType("google")
    google_auth = ModuleType("google.auth")

    def default(*, scopes):
        captured["scopes"] = scopes
        return "credentials", None

    google_auth.default = default
    google.auth = google_auth

    httplib2 = ModuleType("httplib2")

    def http(*, timeout):
        captured["timeout"] = timeout
        return "bounded-http"

    httplib2.Http = http

    google_auth_httplib2 = ModuleType("google_auth_httplib2")

    def authorized_http(credentials, *, http):
        captured["credentials"] = credentials
        captured["http"] = http
        return "authorized-http"

    google_auth_httplib2.AuthorizedHttp = authorized_http

    class Request:
        def execute(self, *, num_retries):
            captured["num_retries"] = num_retries
            if execute_error is not None:
                raise execute_error
            return {"id": "google-event"}

    class Events:
        def insert(self, **kwargs):
            captured["insert"] = kwargs
            return Request()

    class Service:
        def events(self):
            return Events()

    discovery = ModuleType("googleapiclient.discovery")

    def build(name, version, **kwargs):
        captured["build"] = (name, version, kwargs)
        return Service()

    discovery.build = build
    googleapiclient = ModuleType("googleapiclient")
    googleapiclient.discovery = discovery

    for name, module in {
        "google": google,
        "google.auth": google_auth,
        "httplib2": httplib2,
        "google_auth_httplib2": google_auth_httplib2,
        "googleapiclient": googleapiclient,
        "googleapiclient.discovery": discovery,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return captured


def test_google_calendar_settings_are_opt_in(monkeypatch):
    monkeypatch.delenv("WAKU_GOOGLE_CALENDAR", raising=False)
    monkeypatch.delenv("WAKU_GOOGLE_CALENDAR_ID", raising=False)
    assert Settings().google_calendar is False
    assert Settings().google_calendar_id == "primary"

    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR", "yes")
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR_ID", "team@example.com")
    assert Settings().google_calendar is True
    assert Settings().google_calendar_id == "team@example.com"


def test_google_insert_uses_bounded_transport_and_suppresses_invites(monkeypatch):
    captured = _install_fake_google_modules(monkeypatch)

    result = calendar.sync_to_google_calendar(
        title="Planning",
        start="2026-07-24T09:00+08:00",
        end="2026-07-24T10:00+08:00",
        attendees="Alex, alice@example.com, Bob <bob@example.com>",
        notes="Quarterly plan",
        calendar_id="team@example.com",
    )

    assert captured["timeout"] == calendar.GOOGLE_CALENDAR_TIMEOUT
    assert captured["http"] == "bounded-http"
    assert captured["build"] == (
        "calendar",
        "v3",
        {
            "http": "authorized-http",
            "cache_discovery": False,
            "static_discovery": True,
        },
    )
    assert captured["num_retries"] == 0
    request = captured["insert"]
    assert request["calendarId"] == "team@example.com"
    assert request["sendUpdates"] == "none"
    assert request["body"] == {
        "summary": "Planning",
        "description": "Quarterly plan",
        "start": {"dateTime": "2026-07-24T09:00:00+08:00"},
        "end": {"dateTime": "2026-07-24T10:00:00+08:00"},
        "attendees": [
            {"email": "alice@example.com"},
            {"email": "bob@example.com"},
        ],
    }
    assert "Also added to Google Calendar" in result
    assert "notifications suppressed" in result


def test_google_api_error_is_reported_without_raising(monkeypatch):
    _install_fake_google_modules(monkeypatch, execute_error=TimeoutError("request timed out"))

    result = calendar.sync_to_google_calendar(
        title="Still local",
        start="2026-07-24T09:00+08:00",
        end="2026-07-24T10:00+08:00",
    )

    assert "Google Calendar sync FAILED (request timed out)" in result
    assert "still in the local calendar" in result


def test_missing_google_extra_is_an_honest_partial_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "google", None)

    result = calendar.sync_to_google_calendar(
        title="Still local",
        start="2026-07-24T09:00+08:00",
        end="2026-07-24T10:00+08:00",
    )

    assert "support is not installed" in result
    assert "still in the local calendar" in result


def test_google_failure_keeps_local_event_and_reports_partial_success(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    monkeypatch.setattr(
        calendar,
        "sync_to_google_calendar",
        lambda *args, **kwargs: (
            "Google Calendar sync FAILED (network timed out) — "
            "the event is still in the local calendar."
        ),
    )

    tool = calendar.make_tool(conn, tmp_path, google_calendar=True)
    result = tool.fn(
        title="Local first",
        start="2026-07-24T09:00",
        attendees="alice@example.com",
    )

    row = conn.execute("SELECT title, start FROM calendar_events").fetchone()
    assert dict(row) == {"title": "Local first", "start": "2026-07-24T09:00"}
    assert "SUMMARY:Local first" in (tmp_path / "calendar.ics").read_text(encoding="utf-8")
    assert "Saved to the local calendar" in result
    assert "Google Calendar sync FAILED" in result
    assert "Local first" in calendar.make_list_tool(conn).fn()
    conn.close()


def test_default_mock_never_calls_google_and_schema_is_unchanged(tmp_path, monkeypatch):
    conn = connect(tmp_path)

    def unexpected_sync(*args, **kwargs):
        raise AssertionError("the default local mock must not call Google")

    monkeypatch.setattr(calendar, "sync_to_google_calendar", unexpected_sync)
    local_tool = calendar.make_tool(conn, tmp_path)
    google_tool = calendar.make_tool(conn, tmp_path, google_calendar=True)

    assert google_tool.input_schema == local_tool.input_schema
    result = local_tool.fn(title="Offline", start="2026-07-24T11:00")
    assert "Not synced to any calendar app" in result
    conn.close()


def test_eval_factory_disables_google_even_when_environment_enables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR", "1")
    app = make_waku(tmp_path, client=ScriptedClient([]))
    try:
        assert app.settings.google_calendar is False
    finally:
        app.close()
