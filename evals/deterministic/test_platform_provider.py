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


def test_it_appears_in_no_local_list(local):
    from waku.integrations import render_env_example_block
    from waku.ops.settings_api import settings_info

    assert PLATFORM not in [p["name"] for p in settings_info()["providers"]]
    block = render_env_example_block()
    assert PLATFORM not in block and "WAKU_PLATFORM_" not in block


def test_env_example_never_mentions_it_even_when_hosted(hosted):
    """The file is committed and identical on every machine."""
    from waku.integrations import render_env_example_block

    block = render_env_example_block()
    assert PLATFORM not in block and "WAKU_PLATFORM_" not in block


def test_it_does_not_take_the_claude_family(local, monkeypatch):
    """Without claims_families = false, the row's claude-* placeholder would
    take the family from anthropic and a local WAKU_MODEL would be dropped."""
    from waku.loop.models import _belongs_elsewhere

    assert _belongs_elsewhere("claude-opus-4-8", "anthropic") is False


def test_get_client_refuses_the_row_without_its_endpoint(local):
    from waku.config import Settings
    from waku.loop.models import get_client

    with pytest.raises(SystemExit) as exc:
        get_client(Settings(provider=PLATFORM, model="", small_model=""))
    assert "WAKU_PLATFORM_BASE_URL" in str(exc.value)


def test_scoped_credentials_ignore_a_leftover_custom_key(hosted, monkeypatch):
    """A tenant who once saved a custom key keeps WAKU_API_KEY in .env, and a
    stale WAKU_BASE_URL from that earlier save. Neither must outrank the
    platform token/endpoint after switching back -- this is how a BYOK save
    sent the platform token to api.anthropic.com on 0.1.8.

    Controller ruling 2: the original draft's assertion could not fail
    (`or out.get("listed") is not None`). This asserts on the values the code
    actually used for a model call AND for model listing, not the shape of a
    response.
    """
    import io
    import json
    import urllib.request

    monkeypatch.setenv("WAKU_API_KEY", "a-user-key")
    monkeypatch.setenv("WAKU_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("WAKU_PROVIDER", PLATFORM)

    from waku.config import Settings
    from waku.loop.models import get_client
    from waku.ops import catalog

    # --- a model call: the client is built from the row's own key/endpoint ---
    settings = Settings(provider=PLATFORM, model="", small_model="",
                        api_key="a-user-key", base_url="https://api.anthropic.com")
    client = get_client(settings)
    assert client.api_key == "tenant-token-abc"
    assert str(client.base_url).rstrip("/") == "http://proxy.local:8080"

    # --- model listing: same rule, checked on the wire request itself ---
    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        body = io.BytesIO(json.dumps({"data": []}).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: None
        return body

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    catalog._models_cache.clear()
    catalog.list_models(PLATFORM, use_cache=False)
    catalog._models_cache.clear()

    assert captured["url"] == "http://proxy.local:8080/v1/models"
    assert captured["headers"]["authorization"] == "Bearer tenant-token-abc"
    assert captured["headers"]["x-api-key"] == "tenant-token-abc"


def test_a_stale_pin_is_dropped_when_the_override_moves(hosted, tmp_path, monkeypatch):
    """A pin saved under an earlier WAKU_PLATFORM_MODEL must not survive a
    platform-side model change -- the switcher must never offer a model id
    that the container will no longer actually call."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))

    from waku.ops import catalog

    catalog.save_pinned([f"{PLATFORM}:claude-old-id", "anthropic:claude-sonnet-5"])
    specs = catalog.pinned_specs()
    assert f"{PLATFORM}:claude-old-id" not in specs
    assert "anthropic:claude-sonnet-5" in specs
