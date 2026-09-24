"""The waku-platform row: hidden locally, routed through the proxy hosted.

Acceptance 7 and 8 of spec 001. Everything here runs offline.
"""

from __future__ import annotations

import pytest

from waku.loop.models import PROVIDERS, REGISTRY

PLATFORM = "waku-platform"
ENV = ("WAKU_PLATFORM_BASE_URL", "WAKU_PLATFORM_TOKEN",
       "WAKU_PLATFORM_MODEL", "WAKU_PLATFORM_SMALL_MODEL")


@pytest.fixture
def local(monkeypatch):
    """A local user's machine: no platform variable set anywhere."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def hosted(monkeypatch):
    """A tenant container: every platform variable set."""
    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", "http://proxy.local:8080")
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "tenant-token-abc")
    monkeypatch.setenv("WAKU_PLATFORM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("WAKU_PLATFORM_SMALL_MODEL", "claude-haiku-4-5-20251001")
    return monkeypatch


def test_the_row_exists_and_is_anthropic_wire():
    assert PLATFORM in REGISTRY
    assert REGISTRY[PLATFORM]["kind"] == "anthropic"
    assert REGISTRY[PLATFORM]["key_env"] == "WAKU_PLATFORM_TOKEN"
    assert REGISTRY[PLATFORM]["base_url_env"] == "WAKU_PLATFORM_BASE_URL"


def test_it_is_invisible_without_its_base_url(local):
    assert PROVIDERS[PLATFORM].is_visible() is False


def test_it_is_visible_once_the_base_url_is_set(hosted):
    assert PROVIDERS[PLATFORM].is_visible() is True


def test_the_model_override_is_read_at_call_time(hosted):
    """PROVIDERS is built once at import; the override must not be baked in."""
    assert PROVIDERS[PLATFORM].models_now() == (
        "claude-sonnet-5", "claude-haiku-4-5-20251001")
    hosted.setenv("WAKU_PLATFORM_MODEL", "claude-opus-5")
    assert PROVIDERS[PLATFORM].models_now()[0] == "claude-opus-5"


def test_without_the_override_the_row_keeps_its_placeholders(local):
    row = REGISTRY[PLATFORM]
    assert PROVIDERS[PLATFORM].models_now() == (row["model"], row["small_model"])


def test_an_ordinary_row_is_unaffected(local):
    """The seven fields are opt-in. anthropic sets none of them."""
    anthropic = PROVIDERS["anthropic"]
    assert anthropic.is_visible() is True
    assert anthropic.models_now() == (anthropic.model, anthropic.small_model)
