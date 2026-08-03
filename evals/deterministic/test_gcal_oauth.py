"""DETERMINISTIC EVAL — Google Calendar InstalledAppFlow OAuth flow."""

from __future__ import annotations

import sys
from types import ModuleType

from waku.tools import calendar, google_calendar


def _install_fake_oauth_modules(monkeypatch, *, execute_error: Exception | None = None):
    captured = {}

    google = ModuleType("google")

    # google.auth
    google_auth = ModuleType("google.auth")
    google_auth_requests = ModuleType("google.auth.transport.requests")

    class Request:
        pass

    google_auth_requests.Request = Request
    google_auth.transport = ModuleType("google.auth.transport")
    google_auth.transport.requests = google_auth_requests
    google.auth = google_auth

    # google.oauth2.credentials
    google_oauth2 = ModuleType("google.oauth2")
    google_oauth2_credentials = ModuleType("google.oauth2.credentials")

    class FakeCredentials:
        def __init__(self, valid=True, expired=False, refresh_token="ref-token"):
            self.valid = valid
            self.expired = expired
            self.refresh_token = refresh_token
            self.refreshed = False

        @classmethod
        def from_authorized_user_file(cls, path, scopes=None):
            captured["loaded_token_path"] = path
            captured["loaded_scopes"] = scopes
            return FakeCredentials(valid=captured.get("creds_valid", True), expired=captured.get("creds_expired", False))

        def refresh(self, request):
            captured["refreshed"] = True
            self.valid = True
            self.expired = False

        def to_json(self):
            return '{"token": "fake-oauth-token"}'

    google_oauth2_credentials.Credentials = FakeCredentials
    google_oauth2.credentials = google_oauth2_credentials
    google.oauth2 = google_oauth2

    # google_auth_oauthlib.flow
    google_auth_oauthlib = ModuleType("google_auth_oauthlib")
    google_auth_oauthlib_flow = ModuleType("google_auth_oauthlib.flow")

    class FakeInstalledAppFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes=None):
            captured["client_secrets_path"] = path
            captured["flow_scopes"] = scopes
            return FakeInstalledAppFlow()

        @classmethod
        def from_client_config(cls, config, scopes=None):
            captured["client_config"] = config
            captured["flow_scopes"] = scopes
            return FakeInstalledAppFlow()

        def run_local_server(self, port=0):
            captured["ran_local_server_port"] = port
            return FakeCredentials()

    google_auth_oauthlib_flow.InstalledAppFlow = FakeInstalledAppFlow
    google_auth_oauthlib.flow = google_auth_oauthlib_flow

    # httplib2 & google_auth_httplib2
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

    # googleapiclient.discovery
    class FakeRequest:
        def execute(self, *, num_retries):
            captured["num_retries"] = num_retries
            if execute_error is not None:
                raise execute_error
            return {"id": "google-event"}

    class FakeEvents:
        def insert(self, **kwargs):
            captured["insert"] = kwargs
            return FakeRequest()

    class FakeService:
        def events(self):
            return FakeEvents()

    discovery = ModuleType("googleapiclient.discovery")

    def build(name, version, **kwargs):
        captured["build"] = (name, version, kwargs)
        return FakeService()

    discovery.build = build
    googleapiclient = ModuleType("googleapiclient")
    googleapiclient.discovery = discovery

    for name, module in {
        "google": google,
        "google.auth": google_auth,
        "google.auth.transport": google_auth.transport,
        "google.auth.transport.requests": google_auth_requests,
        "google.oauth2": google_oauth2,
        "google.oauth2.credentials": google_oauth2_credentials,
        "google_auth_oauthlib": google_auth_oauthlib,
        "google_auth_oauthlib.flow": google_auth_oauthlib_flow,
        "httplib2": httplib2,
        "google_auth_httplib2": google_auth_httplib2,
        "googleapiclient": googleapiclient,
        "googleapiclient.discovery": discovery,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return captured


def test_missing_credentials_json_reports_honest_failure(tmp_path, monkeypatch):
    _install_fake_oauth_modules(monkeypatch)

    result = calendar.sync_to_google_calendar(
        title="Sync test",
        start="2026-07-28T10:00+08:00",
        end="2026-07-28T11:00+08:00",
        home=tmp_path,
    )

    assert "credentials.json not found" in result
    assert "still in the local calendar" in result


def test_oauth_flow_runs_and_caches_token(tmp_path, monkeypatch):
    captured = _install_fake_oauth_modules(monkeypatch)
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text('{"installed": {}}', encoding="utf-8")

    result = calendar.sync_to_google_calendar(
        title="Planning",
        start="2026-07-28T10:00+08:00",
        end="2026-07-28T11:00+08:00",
        home=tmp_path,
    )

    assert captured["client_secrets_path"] == str(creds_file)
    assert captured["ran_local_server_port"] == 0
    token_file = tmp_path / "token.json"
    assert token_file.exists()
    assert 'fake-oauth-token' in token_file.read_text()
    assert "Also added to Google Calendar" in result


def test_valid_cached_token_is_reused_without_reauth(tmp_path, monkeypatch):
    captured = _install_fake_oauth_modules(monkeypatch)
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "existing"}', encoding="utf-8")

    result = calendar.sync_to_google_calendar(
        title="Planning",
        start="2026-07-28T10:00+08:00",
        end="2026-07-28T11:00+08:00",
        home=tmp_path,
    )

    assert captured["loaded_token_path"] == str(token_file)
    assert "client_secrets_path" not in captured
    assert "Also added to Google Calendar" in result


def test_expired_token_is_refreshed(tmp_path, monkeypatch):
    captured = _install_fake_oauth_modules(monkeypatch)
    captured["creds_valid"] = False
    captured["creds_expired"] = True

    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "expired"}', encoding="utf-8")

    result = calendar.sync_to_google_calendar(
        title="Planning",
        start="2026-07-28T10:00+08:00",
        end="2026-07-28T11:00+08:00",
        home=tmp_path,
    )

    assert captured.get("refreshed") is True
    assert "client_secrets_path" not in captured
    assert "Also added to Google Calendar" in result


def test_google_calendar_uses_bundled_client_and_readonly_scope_by_default(
    tmp_path, monkeypatch
):
    captured = _install_fake_oauth_modules(monkeypatch)
    bundled_config = {
        "installed": {
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
        }
    }
    monkeypatch.setattr(
        google_calendar, "BUNDLED_CLIENT_CONFIG", bundled_config
    )

    result = google_calendar.connect(tmp_path)

    assert captured["client_config"] == bundled_config
    assert captured["flow_scopes"] == google_calendar.DEFAULT_SCOPES
    assert captured["flow_scopes"] == [google_calendar.READONLY_SCOPE]
    assert "client_secrets_path" not in captured
    assert captured["ran_local_server_port"] == 0
    token_file = tmp_path / "google-token.json"
    assert token_file.exists()
    assert "fake-oauth-token" in token_file.read_text(encoding="utf-8")
    assert "read-only via bundled OAuth client" in result


def test_google_calendar_reports_when_bundled_client_is_not_configured(
    tmp_path, monkeypatch
):
    captured = _install_fake_oauth_modules(monkeypatch)

    result = google_calendar.connect(tmp_path)

    assert "bundled OAuth client is not configured" in result
    assert ".waku/credentials.json" in result
    assert "client_config" not in captured
    assert "ran_local_server_port" not in captured
    assert not (tmp_path / "google-token.json").exists()


def test_google_calendar_credentials_json_overrides_client_not_scope(
    tmp_path, monkeypatch
):
    captured = _install_fake_oauth_modules(monkeypatch)
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text('{"installed": {}}', encoding="utf-8")

    result = google_calendar.connect(tmp_path)

    assert captured["client_secrets_path"] == str(creds_file)
    assert captured["flow_scopes"] == google_calendar.DEFAULT_SCOPES
    assert captured["flow_scopes"] == [google_calendar.READONLY_SCOPE]
    assert "client_config" not in captured
    assert (tmp_path / "google-token.json").exists()
    assert "read-only via credentials.json override" in result


def test_google_calendar_token_loading_uses_default_readonly_scopes(
    tmp_path, monkeypatch
):
    captured = _install_fake_oauth_modules(monkeypatch)
    token_file = tmp_path / "google-token.json"
    token_file.write_text('{"token": "existing"}', encoding="utf-8")

    creds = google_calendar._load_credentials(
        tmp_path, google_calendar.DEFAULT_SCOPES
    )

    assert creds is not None
    assert captured["loaded_token_path"] == str(token_file)
    assert captured["loaded_scopes"] == google_calendar.DEFAULT_SCOPES
    assert captured["loaded_scopes"] == [google_calendar.READONLY_SCOPE]
