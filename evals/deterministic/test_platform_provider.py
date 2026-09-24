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
def local(tmp_path, monkeypatch):
    """A local user's machine: no platform variable set anywhere, and no real
    .waku directory in reach — several tests below read/write models.json and
    connections_health.json, and must never touch the developer's own home."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    """A tenant container: every platform variable set."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
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


# --- C1: the override must reach every reader the spec names, not just get_client --

def test_default_pair_uses_the_override_not_the_placeholder(hosted):
    """default_pair() feeds default_pinned_specs() and _known_default_ids().
    Regression: it used to fall back to the raw model/small_model TOML fields,
    so it handed out the placeholder even with an override active — the exact
    pin _stale_platform_pin would then delete on the very next read."""
    hosted.setenv("WAKU_PLATFORM_MODEL", "claude-opus-5")
    hosted.setenv("WAKU_PLATFORM_SMALL_MODEL", "claude-haiku-9")

    assert PROVIDERS[PLATFORM].default_pair() == ["claude-opus-5", "claude-haiku-9"]


def test_default_pinned_specs_pins_the_override_not_the_placeholder(hosted):
    hosted.setenv("WAKU_PLATFORM_MODEL", "claude-opus-5")
    hosted.setenv("WAKU_PLATFORM_SMALL_MODEL", "claude-haiku-9")

    from waku.ops import catalog

    specs = catalog.default_pinned_specs()
    platform_specs = [s for s in specs if s.startswith(f"{PLATFORM}:")]
    assert platform_specs == [f"{PLATFORM}:claude-opus-5", f"{PLATFORM}:claude-haiku-9"]
    # the pins this function just generated must not be the ones the stale
    # filter deletes on the next read — that was the C1 contradiction.
    assert not any(catalog._stale_platform_pin(s) for s in platform_specs)


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
        # io.BytesIO already implements __enter__/__exit__ on the type, so it
        # works directly as the `with` target urlopen callers expect.
        return io.BytesIO(json.dumps({"data": []}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    catalog._models_cache.clear()
    catalog.list_models(PLATFORM, use_cache=False)
    catalog._models_cache.clear()

    assert captured["url"] == "http://proxy.local:8080/v1/models"
    assert captured["headers"]["authorization"] == "Bearer tenant-token-abc"
    assert captured["headers"]["x-api-key"] == "tenant-token-abc"


# --- I2/I3: the stale-pin filter must bite on a real move, and only on the
# placeholder id -- never on a real id the row's own live catalog offers. ----

def test_a_stale_pin_is_dropped_when_the_override_moves(hosted):
    """A pin saved under an earlier state (no override, or a superseded one)
    must not survive a platform-side model change. Deliberately uses an
    override value that DIFFERS from the TOML placeholder -- the `hosted`
    fixture's own WAKU_PLATFORM_MODEL equals the placeholder, which is why the
    first draft of this test could not fail (I2): a no-op filter
    (`return bool(prov.model_env)`, which deletes every platform pin) also
    passed it."""
    hosted.setenv("WAKU_PLATFORM_MODEL", "claude-opus-5")   # a genuine move

    from waku.ops import catalog

    catalog.save_pinned([
        f"{PLATFORM}:claude-sonnet-5",     # the placeholder -- now stale
        f"{PLATFORM}:claude-opus-5",       # the live override -- must survive
        f"{PLATFORM}:claude-sonnet-4-6",   # a real id from the live catalog (I3) -- must survive
        "anthropic:claude-sonnet-5",       # an ordinary row's pin -- untouched
    ])
    specs = catalog.pinned_specs()

    assert f"{PLATFORM}:claude-sonnet-5" not in specs
    assert f"{PLATFORM}:claude-opus-5" in specs
    assert f"{PLATFORM}:claude-sonnet-4-6" in specs
    assert "anthropic:claude-sonnet-5" in specs


def test_a_pin_equal_to_the_placeholder_survives_without_an_override(hosted, monkeypatch):
    """No override set (WAKU_PLATFORM_MODEL unset) -> the placeholder IS the
    current model, so a pin naming it is not stale."""
    monkeypatch.delenv("WAKU_PLATFORM_MODEL", raising=False)
    monkeypatch.delenv("WAKU_PLATFORM_SMALL_MODEL", raising=False)

    from waku.ops import catalog

    catalog.save_pinned([f"{PLATFORM}:claude-sonnet-5"])
    assert catalog.pinned_specs() == [f"{PLATFORM}:claude-sonnet-5"]


# --- I4: claims_families = false means the row is skipped entirely, not just
# excluded from ownership -- a claude-* choice under it must survive. --------

def test_a_claude_model_survives_under_the_platform_row(hosted, monkeypatch):
    """Regression: anthropic owns the "claude" family, so a claude-* WAKU_MODEL
    under waku-platform was discarded by _belongs_elsewhere and silently
    replaced by the override/placeholder -- meaning no claude-* id from the
    row's own live catalog could ever take effect."""
    monkeypatch.setenv("WAKU_MODEL", "claude-sonnet-4-6")
    monkeypatch.delenv("WAKU_SMALL_MODEL", raising=False)

    from waku.config import Settings
    from waku.loop.models import get_client

    settings = Settings(provider=PLATFORM, model="claude-sonnet-4-6", small_model="")
    get_client(settings)

    assert settings.model == "claude-sonnet-4-6"


# --- I5: offline coverage for the remaining consumers this task changed. ----

def test_no_key_message_omits_the_hidden_row(local, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from waku.config import Settings
    from waku.loop.models import get_client

    with pytest.raises(SystemExit) as exc:
        get_client(Settings(provider="anthropic", model="", small_model="", api_key=""))
    assert PLATFORM not in str(exc.value)


def test_unknown_provider_error_omits_the_hidden_row(local):
    """test_providers.py's fixture sets WAKU_PLATFORM_BASE_URL globally, which
    would make the row visible here too -- this test controls the variable
    itself (via `local`) rather than relying on that other file's state."""
    from waku.config import Settings
    from waku.loop.models import get_client

    with pytest.raises(SystemExit) as exc:
        get_client(Settings(provider="not-a-provider", model="", small_model="", api_key=""))
    assert PLATFORM not in str(exc.value)


def test_settings_info_masks_scoped_credentials(hosted, monkeypatch):
    """A leftover WAKU_API_KEY/WAKU_BASE_URL from an earlier BYOK save must
    not read back through settings_info as if it belonged to the scoped
    platform row -- the same rule get_client and catalog.list_models follow."""
    monkeypatch.setenv("WAKU_PROVIDER", PLATFORM)
    monkeypatch.setenv("WAKU_API_KEY", "leftover-custom-key")
    monkeypatch.setenv("WAKU_BASE_URL", "https://api.anthropic.com")

    from waku.ops.settings_api import settings_info

    info = settings_info()
    assert info["base_url"] == ""
    assert info["custom_key_set"] is False


def test_default_pinned_specs_skips_the_hidden_row_even_with_a_key(local, monkeypatch):
    """A key alone (no base_url) must not be enough to surface the hidden row
    in the starter shortlist -- is_visible() gates it, not just key_env."""
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "tenant-token-abc")   # key set...
    monkeypatch.delenv("WAKU_PLATFORM_BASE_URL", raising=False)     # ...but not visible

    from waku.ops.catalog import default_pinned_specs

    specs = default_pinned_specs()
    assert not any(s.startswith(f"{PLATFORM}:") for s in specs)


# --- M6: label_text() must actually reach the UI-facing integration title. --

def test_label_text_is_wired_into_the_integration_title(hosted):
    from waku.integrations import provider_integrations

    row = next(i for i in provider_integrations() if i.key == PLATFORM)
    assert row.name == "Hosted free tier"
