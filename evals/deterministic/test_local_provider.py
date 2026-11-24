"""Spec 034: one provider row for a model server on this computer.

Ollama, LM Studio, llama.cpp's server and vLLM all speak the OpenAI API, so a
single `local` row reaches any of them. It needs no key, takes its address
from WAKU_LOCAL_BASE_URL, and must never take a model family away from the
provider that sells it.
"""

import pytest

from waku.config import Settings
from waku.integrations import _provider_ready
from waku.loop.models import LOCAL_PLACEHOLDER_KEY, PROVIDERS, get_client, models_for

LOCAL = PROVIDERS["local"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("WAKU_LOCAL_API_KEY", "WAKU_LOCAL_BASE_URL", "WAKU_PROVIDER",
                "WAKU_MODEL", "WAKU_SMALL_MODEL", "WAKU_BASE_URL", "WAKU_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _settings(**kw):
    return Settings(provider="local", model=kw.get("model", ""), small_model="",
                    api_key="", base_url=None)


def test_the_local_row_needs_no_key_and_builds_a_client():
    settings = _settings()
    client = get_client(settings)
    assert client._client.api_key == LOCAL_PLACEHOLDER_KEY
    assert str(client._client.base_url).rstrip("/") == "http://localhost:11434/v1"
    assert settings.model == LOCAL.model


def test_the_address_comes_from_waku_local_base_url(monkeypatch):
    monkeypatch.setenv("WAKU_LOCAL_BASE_URL", "http://localhost:1234/v1")
    client = get_client(_settings())
    assert str(client._client.base_url).rstrip("/") == "http://localhost:1234/v1"


def test_a_key_given_for_a_guarded_server_is_used(monkeypatch):
    monkeypatch.setenv("WAKU_LOCAL_API_KEY", "server-token")
    client = get_client(_settings())
    assert client._client.api_key == "server-token"


def test_a_keyless_row_never_says_no_api_key():
    for name, provider in PROVIDERS.items():
        if provider.key_optional:
            get_client(Settings(provider=name, model="", small_model="", api_key="", base_url=None))


def test_the_model_the_person_chose_is_kept(monkeypatch):
    monkeypatch.setenv("WAKU_MODEL", "qwen3:14b")
    assert models_for("local", "qwen3:14b", "")[0] == "qwen3:14b"


def test_a_model_left_over_from_another_provider_is_dropped(monkeypatch):
    monkeypatch.setenv("WAKU_MODEL", "claude-sonnet-5")
    assert models_for("local", "claude-sonnet-5", "")[0] == LOCAL.model


def test_the_local_row_does_not_claim_model_families(monkeypatch):
    # A local server often serves deepseek-* or qwen* models. Claiming those
    # names would strip a deliberate DeepSeek choice on the DeepSeek row.
    assert LOCAL.claims_families is False
    monkeypatch.setenv("WAKU_MODEL", "deepseek-v4-flash")
    assert models_for("deepseek", "deepseek-v4-flash", "")[0] == "deepseek-v4-flash"


def test_the_row_is_ready_once_chosen_or_given_an_address(monkeypatch):
    assert LOCAL.has_credentials("local") is False
    assert _provider_ready({}, "local", LOCAL) is False
    monkeypatch.setenv("WAKU_PROVIDER", "local")
    assert LOCAL.has_credentials("local") is True
    assert _provider_ready({"WAKU_PROVIDER": "local"}, "local", LOCAL) is True
    assert _provider_ready({"WAKU_LOCAL_BASE_URL": "http://localhost:1234/v1"}, "local", LOCAL) is True


def test_a_keyed_row_is_still_not_ready_without_its_key(monkeypatch):
    monkeypatch.setenv("WAKU_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert PROVIDERS["openai"].has_credentials("openai") is False
