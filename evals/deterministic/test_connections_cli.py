"""Connections CLI reads the same persisted health state as Dashboard."""

from __future__ import annotations

from waku import integrations
from waku.integrations import IntegrationState, IntegrationStatus


def test_cli_returns_failure_for_configured_error(monkeypatch, tmp_path):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    integrations._HEALTH = None
    integrations.record_health("openai", IntegrationStatus(IntegrationState.ERROR, "bad key"))
    assert integrations.cli_main() == 1
    integrations.invalidate_health("openai")
    assert integrations.cli_main() == 0
