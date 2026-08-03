"""Deterministic coverage for the shared Connections registry."""

from __future__ import annotations

import os

from waku import integrations
from waku.integrations import IntegrationState, IntegrationStatus
from waku.loop.models import PROVIDERS


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
    assert view.status.state is IntegrationState.INSTALLED_BUT_UNCONFIGURED
    assert view.fields[0].value == ""
    assert view.fields[0].last4 == "1234"
    integrations.record_health("openai", IntegrationStatus(IntegrationState.CONNECTED))
    integrations._HEALTH = None
    assert next(view for view in integrations.list_integrations() if view.key == "openai").status.state is IntegrationState.CONNECTED
    integrations.invalidate_health("openai")
    assert next(view for view in integrations.list_integrations() if view.key == "openai").status.message == "Configured, not tested"


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
