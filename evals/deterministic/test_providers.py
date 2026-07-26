"""OFFLINE provider-table checks: every PROVIDERS entry must build the right
client, fill its default model ids, and be covered by the dashboard's pricing
and model-listing fallbacks. No network, no real keys (fakes via monkeypatch).

Born from a live regression hunt: adding a provider touches shared paths
(get_client, HAS_KEY, /api/models, PRICING), and nothing offline proved the
other five still worked. Now something does.
"""

from __future__ import annotations

import anthropic
import pytest

from waku.config import Settings
from waku.loop.models import PROVIDERS, OpenAICompatClient, get_client


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch):
    for provider in PROVIDERS.values():
        monkeypatch.setenv(provider.key_env, "fake-key-for-tests")
    # a stray custom-endpoint override must not leak into these checks
    monkeypatch.delenv("WAKU_API_KEY", raising=False)
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_get_client_builds_the_right_wire(name):
    provider = PROVIDERS[name]
    settings = Settings(provider=name, model="", small_model="", api_key="", base_url=None)
    client = get_client(settings)
    expected = anthropic.Anthropic if provider.kind == "anthropic" else OpenAICompatClient
    assert isinstance(client, expected)
    # defaults must be filled in so the loop never sends model=""
    assert settings.model == provider.model
    assert settings.small_model == provider.small_model


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_missing_key_exits_with_the_key_name(name, monkeypatch):
    monkeypatch.delenv(PROVIDERS[name].key_env, raising=False)
    settings = Settings(provider=name, model="", small_model="", api_key="", base_url=None)
    with pytest.raises(SystemExit, match=PROVIDERS[name].key_env):
        get_client(settings)


def test_unknown_provider_names_the_choices():
    settings = Settings(provider="not-a-provider", model="", small_model="",
                        api_key="", base_url=None)
    with pytest.raises(SystemExit, match="openrouter"):
        get_client(settings)


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_dashboard_pricing_covers_every_provider(name):
    from waku.ops.dashboard import PRICING

    assert name in PRICING


@pytest.mark.parametrize("name", [n for n, p in PROVIDERS.items()
                                  if p.catalog_url is None
                                  and (p.kind == "anthropic" or not p.base_url)])
def test_model_listing_falls_back_without_a_catalog(name, monkeypatch):
    """Providers with no listable catalog still give the picker their defaults
    (and never make a network call to get them)."""
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_PROVIDER", name)
    monkeypatch.delenv("WAKU_MODEL", raising=False)
    monkeypatch.delenv("WAKU_SMALL_MODEL", raising=False)
    result = dashboard.list_models()
    assert result["listed"] is False
    ids = [m["id"] for m in result["models"]]
    assert PROVIDERS[name].model in ids
    # the flagship (showcase) model is offered too, not just the loop default
    if PROVIDERS[name].flagship:
        assert PROVIDERS[name].flagship in ids


def test_bad_key_gives_a_fixable_error_not_a_codec_crash(monkeypatch):
    """A key with a stray non-latin-1 char (a mis-pasted arrow/smart-quote) must
    NOT crash the whole catalog with an opaque codec error — it should return a
    fixable message AND still offer the flagship so opus-4.8/fable-5 aren't lost.
    (Regression: a cloned repo whose ANTHROPIC_API_KEY had a '→' dropped the
    picker to two defaults with a 'latin-1 codec' error.)"""
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 100 + "→bad")
    monkeypatch.delenv("WAKU_MODEL", raising=False)
    dashboard._models_cache.clear()
    result = dashboard.list_models("anthropic")
    assert result["listed"] is False
    assert "ANTHROPIC_API_KEY" in result["error"] and "non-ASCII" in result["error"]
    assert "claude-opus-4-8" in [m["id"] for m in result["models"]]


def test_catalog_url_is_used_with_both_auth_styles(monkeypatch):
    """kimi chats on the anthropic wire but LISTS models on its OpenAI-compat
    endpoint — catalog_url must win, carrying both auth header styles, so the
    picker offers the real menu instead of two hardcoded defaults."""
    import io
    import json
    import urllib.request

    from waku.ops import dashboard

    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        body = io.BytesIO(json.dumps(
            {"data": [{"id": "kimi-k3"}, {"id": "kimi-k2.7"}, {"id": "kimi-k1.5"}]}
        ).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: None
        return body

    monkeypatch.setenv("WAKU_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "fake-key-for-tests")
    monkeypatch.delenv("WAKU_MODEL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dashboard._models_cache.clear()

    result = dashboard.list_models()
    assert captured["url"] == PROVIDERS["kimi"].catalog_url
    assert captured["headers"]["authorization"] == "Bearer fake-key-for-tests"
    assert captured["headers"]["x-api-key"] == "fake-key-for-tests"
    assert result["listed"] is True
    assert "kimi-k3" in [m["id"] for m in result["models"]]
    dashboard._models_cache.clear()


def test_price_for_layers_model_over_provider():
    """Receipts correctness: a kimi-k3 run must be priced at K3's $3/$15, not
    the kimi provider's K2.7 rate — and unknown models still fall back to the
    provider estimate. (Live-catalog and :free paths are covered above.)"""
    from waku.ops.dashboard import MODEL_PRICING, PRICING, price_for

    assert price_for("kimi", "kimi-k3") == MODEL_PRICING["kimi-k3"] == (3.0, 15.0)
    assert price_for("kimi", "kimi-k2.7") == (0.95, 4.0)
    assert price_for("kimi", "some-future-model") == PRICING["kimi"]
    assert price_for("openrouter", "whatever:free") == (0.0, 0.0)

    # Regression: within a provider, models diverge hugely — fable-5 is priced at
    # $10/$50, ~2x opus's $5/$25. A provider-level fallback once made fable-5 look
    # CHEAPER than opus on the scoreboard; each must carry its own per-model rate.
    assert price_for("anthropic", "claude-fable-5") == (10.0, 50.0)
    assert price_for("anthropic", "claude-opus-4-8") == (5.0, 25.0)
    fable_in, fable_out = price_for("anthropic", "claude-fable-5")
    opus_in, opus_out = price_for("anthropic", "claude-opus-4-8")
    assert fable_in > opus_in and fable_out > opus_out   # fable is never cheaper


def test_every_priced_model_has_a_knowledge_cutoff():
    """Arena honesty: the Compare arena discloses each model's knowledge cutoff
    so stale world knowledge isn't misread as low capability (gemini-3.1-pro
    confidently denies 2026 models exist — its cutoff is 2025-01). Every model
    in MODEL_PRICING must have a MODEL_CUTOFF entry. None is a valid value
    (vendor hasn't published a cutoff; the UI shows a dash) — a MISSING key
    means someone added a model without deciding, which is what this catches."""
    import re

    from waku.ops.dashboard import MODEL_CUTOFF, MODEL_PRICING, cutoff_for

    missing = set(MODEL_PRICING) - set(MODEL_CUTOFF)
    assert not missing, f"models priced but missing a MODEL_CUTOFF entry: {sorted(missing)}"

    for model, cutoff in MODEL_CUTOFF.items():
        if cutoff is not None:
            assert re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", cutoff), \
                f"{model}: cutoff {cutoff!r} is not YYYY-MM"

    # The motivating case, plus the unknown-model path (no guessing).
    assert cutoff_for("gemini-3.1-pro-preview") == "2025-01"
    assert cutoff_for("some-future-model") is None
