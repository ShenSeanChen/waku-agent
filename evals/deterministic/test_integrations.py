"""Deterministic coverage for the shared Connections registry."""

from __future__ import annotations

import os
import socket

import pytest

from waku import integrations
from waku.integrations import IntegrationState, IntegrationStatus
from waku.loop.models import PROVIDERS
from waku.ops import browser_agent
from waku.tools import apple, calendar


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    integrations._HEALTH = None
    integrations._reset_import_cache()


def test_registry_contract():
    items = integrations.registry()
    assert len(items) == 21
    assert len({item.key for item in items}) == len(items)
    assert {item.key for item in items if item.group == "AI Providers"} == set(PROVIDERS)
    for item in items:
        assert callable(item.enabled)
        for field in item.env:
            if field.kind is integrations.FieldKind.CHOICE:
                assert field.options
            if field.secret:
                assert field.kind is integrations.FieldKind.TEXT


def test_status_masking_health_persistence_and_invalidation(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-1234")
    view = next(view for view in integrations.list_integrations() if view.key == "openai")
    # A key is present and nothing required is missing, so this is CONFIGURED —
    # not INSTALLED_BUT_UNCONFIGURED, which the dashboard renders as "needs
    # setup". See test_configured_is_not_confused_with_needing_setup below.
    assert view.status.state is IntegrationState.CONFIGURED
    assert view.fields[0].value == ""
    assert view.fields[0].last4 == "1234"
    integrations.record_health("openai", IntegrationStatus(IntegrationState.CONNECTED))
    integrations._HEALTH = None
    assert next(view for view in integrations.list_integrations() if view.key == "openai").status.state is IntegrationState.CONNECTED
    integrations.invalidate_health("openai")
    after = next(view for view in integrations.list_integrations() if view.key == "openai")
    assert after.status.state is IntegrationState.CONFIGURED
    assert after.status.message == "configured — not tested yet"


def test_otel_probe_checks_configured_collector_tcp_port(monkeypatch):
    captured = {}

    class Connection:
        def close(self):
            captured["closed"] = True

    def connect(address, timeout):
        captured.update(address=address, timeout=timeout)
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)

    integrations._otel_probe({"OTEL_EXPORTER_OTLP_ENDPOINT": "localhost:4317"})

    assert captured == {"address": ("localhost", 4317), "timeout": 3, "closed": True}


def test_otel_probe_rejects_endpoint_without_tcp_port():
    with pytest.raises(ValueError, match="host:port"):
        integrations._otel_probe({"OTEL_EXPORTER_OTLP_ENDPOINT": "localhost"})


def test_otel_probe_rejects_endpoint_with_a_signal_path(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: None)

    with pytest.raises(ValueError, match="host:port"):
        integrations._otel_probe({"OTEL_EXPORTER_OTLP_ENDPOINT": "localhost:4317/v1/traces"})


def test_otel_test_connection_records_connected_after_tcp_probe(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317")
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)
    monkeypatch.setattr(integrations, "_otel_probe", lambda values: None)

    view = integrations.test_integration("otel")

    assert view.status.state is IntegrationState.CONNECTED


def test_apply_rejects_unknown_and_secret_clear(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=old-secret\n")
    monkeypatch.setenv("TAVILY_API_KEY", "old-secret")
    assert not integrations.apply_integration("tavily", {"NOPE": "x"}).ok
    # Force avoids the remote Tavily probe; clear is explicit and removes both stores.
    result = integrations.apply_integration("tavily", {}, ("TAVILY_API_KEY",), force=True)
    assert result.ok
    assert "TAVILY_API_KEY" not in os.environ
    assert "TAVILY_API_KEY" not in (tmp_path / ".env").read_text()


def _configure_google(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR", "1")
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR_ID", "team@example.com")
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)


def test_google_test_connection_records_connected(monkeypatch, tmp_path):
    _configure_google(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(
        calendar,
        "probe_google_calendar",
        lambda home, calendar_id: captured.update(home=home, calendar_id=calendar_id),
    )

    view = integrations.test_integration("google_calendar")

    assert captured == {"home": tmp_path, "calendar_id": "team@example.com"}
    assert view.status.state is IntegrationState.CONNECTED
    assert view.status.checked_at is not None


def _configure_apple(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setenv("WAKU_APPLE_CALENDAR", "1")


def test_apple_test_connection_records_connected(monkeypatch, tmp_path):
    _configure_apple(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(calendar, "probe_apple_calendar", lambda: called.append(True))

    view = integrations.test_integration("apple_calendar")

    assert called == [True]
    assert view.status.state is IntegrationState.CONNECTED
    assert view.status.checked_at is not None


def test_apple_save_probes_before_recording_connected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    called = []
    monkeypatch.setattr(calendar, "probe_apple_calendar", lambda: called.append(True))

    result = integrations.apply_integration(
        "apple_calendar", {"WAKU_APPLE_CALENDAR": "1", "WAKU_APPLE_CALENDARS": ""}
    )

    assert result.ok
    assert called == [True]
    assert result.view is not None
    assert result.view.status.state is IntegrationState.CONNECTED


def test_apple_probe_failure_records_error_and_can_recover(monkeypatch, tmp_path):
    _configure_apple(monkeypatch, tmp_path)
    monkeypatch.setattr(
        calendar,
        "probe_apple_calendar",
        lambda: (_ for _ in ()).throw(RuntimeError("automation denied")),
    )

    view = integrations.test_integration("apple_calendar")
    assert view.status.state is IntegrationState.ERROR
    assert view.status.message == "automation denied"

    monkeypatch.setattr(calendar, "probe_apple_calendar", lambda: None)
    view = integrations.test_integration("apple_calendar")
    assert view.status.state is IntegrationState.CONNECTED


def test_apple_force_save_records_error_without_probe(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)

    def unexpected_probe():
        raise AssertionError("force save must skip the probe")

    monkeypatch.setattr(calendar, "probe_apple_calendar", unexpected_probe)

    result = integrations.apply_integration(
        "apple_calendar", {"WAKU_APPLE_CALENDAR": "1"}, force=True
    )

    assert result.ok
    assert result.view is not None
    assert result.view.status.state is IntegrationState.ERROR
    assert result.view.status.message == "Saved without a successful test"


def _configure_apple_tools(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setenv("WAKU_APPLE_TOOLS", "1")


def test_apple_tools_save_probes_and_records_connected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    called = []
    monkeypatch.setattr(apple, "probe_apple_tools", lambda: called.append(True))

    result = integrations.apply_integration(
        "apple_tools", {"WAKU_APPLE_TOOLS": "1"}
    )

    assert result.ok
    assert called == [True]
    assert os.environ["WAKU_APPLE_TOOLS"] == "1"
    assert result.view is not None
    assert result.view.status.state is IntegrationState.CONNECTED
    assert result.view.status.checked_at is not None


def test_apple_tools_probe_failure_records_error_and_recovers(monkeypatch, tmp_path):
    _configure_apple_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(
        apple,
        "probe_apple_tools",
        lambda: (_ for _ in ()).throw(RuntimeError("Mail: Not authorized")),
    )

    view = integrations.test_integration("apple_tools")

    assert view.status.state is IntegrationState.ERROR
    assert view.status.message == "Mail: Not authorized"
    assert view.status.checked_at is not None

    monkeypatch.setattr(apple, "probe_apple_tools", lambda: None)
    view = integrations.test_integration("apple_tools")

    assert view.status.state is IntegrationState.CONNECTED
    assert view.status.checked_at is not None


def test_apple_tools_force_save_skips_probe(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)

    def unexpected_probe():
        raise AssertionError("force save must skip the probe")

    monkeypatch.setattr(apple, "probe_apple_tools", unexpected_probe)

    result = integrations.apply_integration(
        "apple_tools", {"WAKU_APPLE_TOOLS": "1"}, force=True
    )

    assert result.ok
    assert result.view is not None
    assert result.view.status.state is IntegrationState.ERROR
    assert result.view.status.message == "Saved without a successful test"


def test_disabling_apple_clears_connected_health(monkeypatch, tmp_path):
    _configure_apple(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("WAKU_APPLE_CALENDAR=1\n")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    integrations.record_health("apple_calendar", IntegrationStatus(IntegrationState.CONNECTED))

    result = integrations.apply_integration(
        "apple_calendar", {"WAKU_APPLE_CALENDAR": ""}
    )

    assert result.ok
    assert result.view is not None
    assert result.view.status.state is IntegrationState.NOT_CONFIGURED


def test_google_save_probes_candidate_and_records_connected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    captured = {}
    monkeypatch.setattr(
        calendar,
        "probe_google_calendar",
        lambda home, calendar_id: captured.update(home=home, calendar_id=calendar_id),
    )

    result = integrations.apply_integration(
        "google_calendar",
        {
            "WAKU_GOOGLE_CALENDAR": "1",
            "WAKU_GOOGLE_CALENDAR_ID": "candidate@example.com",
        },
    )

    assert result.ok
    assert captured == {"home": tmp_path, "calendar_id": "candidate@example.com"}
    assert result.view is not None
    assert result.view.status.state is IntegrationState.CONNECTED


def test_google_probe_failure_records_error(monkeypatch, tmp_path):
    _configure_google(monkeypatch, tmp_path)
    monkeypatch.setattr(
        calendar,
        "probe_google_calendar",
        lambda home, calendar_id: (_ for _ in ()).throw(RuntimeError("access denied")),
    )

    view = integrations.test_integration("google_calendar")

    assert view.status.state is IntegrationState.ERROR
    assert view.status.message == "access denied"
    assert view.status.checked_at is not None


def test_google_save_failure_can_force_without_writing_first(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR", "")
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR_ID", "old@example.com")
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)
    monkeypatch.setattr(
        calendar,
        "probe_google_calendar",
        lambda home, calendar_id: (_ for _ in ()).throw(RuntimeError("not authorized")),
    )

    result = integrations.apply_integration(
        "google_calendar",
        {
            "WAKU_GOOGLE_CALENDAR": "1",
            "WAKU_GOOGLE_CALENDAR_ID": "team@example.com",
        },
    )

    assert not result.ok
    assert result.can_force
    assert result.error == "not authorized"
    assert not (tmp_path / ".env").exists()
    assert os.environ["WAKU_GOOGLE_CALENDAR"] == ""
    assert os.environ["WAKU_GOOGLE_CALENDAR_ID"] == "old@example.com"


def test_google_force_save_skips_probe_and_records_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)

    def unexpected_probe(home, calendar_id):
        raise AssertionError("force save must skip the probe")

    monkeypatch.setattr(calendar, "probe_google_calendar", unexpected_probe)

    result = integrations.apply_integration(
        "google_calendar",
        {
            "WAKU_GOOGLE_CALENDAR": "1",
            "WAKU_GOOGLE_CALENDAR_ID": "team@example.com",
        },
        force=True,
    )

    assert result.ok
    assert result.view is not None
    assert result.view.status.state is IntegrationState.ERROR
    assert result.view.status.message == "Saved without a successful test"


def test_disabling_google_skips_probe(monkeypatch, tmp_path):
    _configure_google(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "WAKU_GOOGLE_CALENDAR=1\nWAKU_GOOGLE_CALENDAR_ID=team@example.com\n"
    )
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)

    def unexpected_probe(home, calendar_id):
        raise AssertionError("disabled integrations must not be probed")

    monkeypatch.setattr(calendar, "probe_google_calendar", unexpected_probe)

    result = integrations.apply_integration(
        "google_calendar", {"WAKU_GOOGLE_CALENDAR": ""}
    )

    assert result.ok


def test_disabled_google_test_connection_skips_probe(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR", "")
    monkeypatch.setenv("WAKU_GOOGLE_CALENDAR_ID", "team@example.com")
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)

    def unexpected_probe(home, calendar_id):
        raise AssertionError("disabled integrations must not be probed")

    monkeypatch.setattr(calendar, "probe_google_calendar", unexpected_probe)

    view = integrations.test_integration("google_calendar")

    assert view.status.state is not IntegrationState.CONNECTED
    assert view.status.message == "integration is disabled"


def test_configured_is_not_confused_with_needing_setup(monkeypatch, tmp_path):
    """THE regression. Both of these used to return INSTALLED_BUT_UNCONFIGURED,
    which the dashboard renders as "needs setup" — so a working Tavily key
    reported itself as broken. The health store is empty on a fresh checkout,
    so EVERY user met that on their first visit to Connections, about
    integrations that were already working.

    The two situations are not the same and now cannot collapse:
      missing a required value -> installed_but_unconfigured ("needs setup")
      complete but never probed -> configured ("configured, not tested")
    """
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-working-key")
    tavily = next(v for v in integrations.list_integrations() if v.key == "tavily")
    assert tavily.status.state is IntegrationState.CONFIGURED, (
        "a filled-in integration must not tell the user it needs setting up"
    )

    monkeypatch.delenv("TAVILY_API_KEY")
    integrations._HEALTH = None
    empty = next(v for v in integrations.list_integrations() if v.key == "tavily")
    assert empty.status.state is IntegrationState.NOT_CONFIGURED


def test_a_genuinely_missing_required_field_still_says_needs_setup(monkeypatch, tmp_path):
    """The other half: widening the states must not swallow a real problem.
    Telegram with a token but no allowed-chat id is incomplete, and has to keep
    saying so."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations, "_extra_installed", lambda name: True)
    # Notion, not Telegram: Telegram has a single required field, so "half
    # filled in" is not expressible there and the test would prove nothing.
    notion = next(i for i in integrations.registry() if i.key == "notion")
    required = [f.name for f in notion.env if f.required]
    assert len(required) > 1, "this test needs an integration with 2+ required fields"
    monkeypatch.setenv(required[0], "set")
    for name in required[1:]:
        monkeypatch.delenv(name, raising=False)
    view = next(v for v in integrations.list_integrations() if v.key == "notion")
    assert view.status.state is IntegrationState.INSTALLED_BUT_UNCONFIGURED
    assert required[1] in view.status.message
