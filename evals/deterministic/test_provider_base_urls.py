"""Regional Base URL choices for providers with separate domestic/global APIs."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from waku import integrations
from waku.config import Settings
from waku.loop import models
from waku.loop.models import PROVIDERS
from waku.ops import catalog

EXPECTED_ENDPOINTS = {
    "minimax": [
        ("China", "https://api.minimaxi.com/anthropic",
         "https://api.minimaxi.com/anthropic/v1/models"),
        ("Global", "https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/models"),
    ],
    "glm": [
        ("Global", "https://api.z.ai/api/anthropic", None),
        ("China", "https://open.bigmodel.cn/api/anthropic", None),
    ],
    "kimi": [
        ("Global", "https://api.moonshot.ai/anthropic",
         "https://api.moonshot.ai/v1/models"),
        ("China", "https://api.moonshot.cn/anthropic",
         "https://api.moonshot.cn/v1/models"),
    ],
}


def test_regional_provider_endpoint_choices_match_official_urls():
    for name, expected in EXPECTED_ENDPOINTS.items():
        provider = PROVIDERS[name]
        assert [(e.label, e.base_url, e.catalog_url) for e in provider.endpoints] == expected
        assert provider.base_url_env


@pytest.mark.parametrize(
    ("provider_name", "base_url_env", "selected_url"),
    [
        ("minimax", "MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"),
        ("glm", "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
        ("kimi", "MOONSHOT_BASE_URL", "https://api.moonshot.cn/anthropic"),
    ],
)
def test_client_uses_provider_specific_base_url(
    monkeypatch, tmp_path, provider_name, base_url_env, selected_url
):
    captured = {}

    class StubAnthropicClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    provider = PROVIDERS[provider_name]
    monkeypatch.setenv(provider.key_env, "test-key")
    monkeypatch.setenv(base_url_env, selected_url)
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        SimpleNamespace(Anthropic=StubAnthropicClient),
    )

    client = models.get_client(Settings(
        provider=provider_name, api_key="", base_url=None, model="",
        small_model="", home=tmp_path,
    ))

    assert isinstance(client, StubAnthropicClient)
    assert captured["base_url"] == selected_url


@pytest.mark.parametrize(
    ("provider_name", "base_url_env", "selected_url", "catalog_url"),
    [
        ("minimax", "MINIMAX_BASE_URL", "https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/models"),
        ("kimi", "MOONSHOT_BASE_URL", "https://api.moonshot.cn/anthropic",
         "https://api.moonshot.cn/v1/models"),
    ],
)
def test_catalog_follows_selected_provider_region(
    monkeypatch, provider_name, base_url_env, selected_url, catalog_url
):
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        return Response(json.dumps({"data": [{"id": "model-one"}]}).encode())

    provider = PROVIDERS[provider_name]
    monkeypatch.setenv("WAKU_PROVIDER", provider_name)
    monkeypatch.setenv(provider.key_env, "test-key")
    monkeypatch.setenv(base_url_env, selected_url)
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    catalog._models_cache.clear()

    result = catalog.list_models(provider_name, use_cache=False)

    assert result["listed"] is True
    assert captured["url"] == catalog_url


def test_provider_view_exposes_base_url_choice(monkeypatch):
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic")

    view = next(v for v in integrations.list_providers() if v.key == "minimax")
    base = next(field for field in view.fields if field.name == "MINIMAX_BASE_URL")

    assert base.kind is integrations.FieldKind.CHOICE
    assert base.value == "https://api.minimax.io/anthropic"
    assert base.options == (
        "https://api.minimaxi.com/anthropic",
        "https://api.minimax.io/anthropic",
    )
    assert base.option_labels == ("China", "Global")


def test_apply_provider_persists_scoped_base_url_and_clears_legacy_override(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("WAKU_BASE_URL='https://legacy.example'\n")
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    monkeypatch.setenv("WAKU_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("WAKU_BASE_URL", "https://legacy.example")
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: None)

    from waku.ops import browser_agent

    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    tracer = SimpleNamespace(event=lambda *args: None)
    monkeypatch.setattr(browser_agent, "current", lambda: SimpleNamespace(tracer=tracer))

    result = integrations.apply_provider(
        "minimax", base_url="https://api.minimax.io/anthropic"
    )

    assert result.ok
    assert os.environ["MINIMAX_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert os.environ.get("WAKU_BASE_URL", "") == ""
    contents = Path(".env").read_text()
    assert "MINIMAX_BASE_URL='https://api.minimax.io/anthropic'" in contents


def test_models_edit_modal_posts_selected_base_url():
    source = Path("waku/ops/static/js/models.js").read_text()

    assert 'id="pm-base-url"' in source
    assert "payload.base_url = baseUrl" in source
    assert "payload.activate = false" in source


@pytest.mark.parametrize(("current_provider", "expected_rebuilds"), [
    ("kimi", 0),
    ("minimax", 1),
])
def test_reusing_saved_provider_endpoint_skips_remote_probe_and_noop_rebuild(
    monkeypatch, tmp_path, current_provider, expected_rebuilds
):
    """Clicking either modal action with unchanged credentials stays local."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    monkeypatch.setenv("WAKU_PROVIDER", current_provider)
    monkeypatch.setenv("MOONSHOT_API_KEY", "saved-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/anthropic")
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)

    probes = []
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: probes.append(values))

    from waku.ops import browser_agent

    rebuilds = []
    monkeypatch.setattr(browser_agent, "rebuild", lambda: rebuilds.append(True))
    tracer = SimpleNamespace(event=lambda *args: None)
    monkeypatch.setattr(browser_agent, "current", lambda: SimpleNamespace(tracer=tracer))

    result = integrations.apply_provider(
        "kimi", base_url="https://api.moonshot.cn/anthropic"
    )

    assert result.ok
    assert probes == []
    assert len(rebuilds) == expected_rebuilds


def test_saving_noncurrent_provider_does_not_activate_or_rebuild(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    monkeypatch.setenv("WAKU_PROVIDER", "minimax")
    monkeypatch.setenv("MOONSHOT_API_KEY", "saved-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/anthropic")
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: None)

    from waku.ops import browser_agent

    rebuilds = []
    monkeypatch.setattr(browser_agent, "rebuild", lambda: rebuilds.append(True))

    result = integrations.apply_provider(
        "kimi",
        base_url="https://api.moonshot.cn/anthropic",
        activate=False,
    )

    assert result.ok
    assert os.environ["WAKU_PROVIDER"] == "minimax"
    assert os.environ["MOONSHOT_BASE_URL"] == "https://api.moonshot.cn/anthropic"
    assert rebuilds == []
