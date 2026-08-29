import os
from types import SimpleNamespace

import pytest

from evals.helpers import HAS_KEY
from waku.config import Settings
from waku.loop import models

RUN_LIVE_EVALS = os.getenv("WAKU_RUN_LIVE_EVALS") == "1"


def test_xai_grok_provider_uses_expected_key_endpoint_and_models(monkeypatch, tmp_path):
    captured = {}

    class StubOpenAICompatClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setattr(models, "OpenAICompatClient", StubOpenAICompatClient)
    settings = Settings(provider="xai", api_key="", base_url=None, model="",
                        small_model="", home=tmp_path)

    client = models.get_client(settings)

    assert isinstance(client, StubOpenAICompatClient)
    assert captured["api_key"] == "test-xai-key"
    assert captured["base_url"] == "https://api.x.ai/v1"
    assert settings.model == "grok-4"


def test_opencode_provider_sends_client_identification_headers(monkeypatch, tmp_path):
    """Regression: bare opencode.ai/zen requests (no identifying headers) land in
    the anonymous per-IP fallback pool and die with FreeUsageLimitError 429 while
    the TUI works. The opencode providers must identify as an opencode client via
    a User-Agent containing 'opencode' plus x-opencode-* ids, and non-opencode
    providers must NOT receive them."""
    captured = []

    class StubOpenAICompatClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-zen-key")
    monkeypatch.setattr(models, "OpenAICompatClient", StubOpenAICompatClient)
    settings = Settings(provider="opencode_zen", api_key="", base_url=None, model="",
                        small_model="", home=tmp_path)

    models.get_client(settings)
    kwargs = captured[0]

    assert "default_headers" in kwargs
    headers = kwargs["default_headers"]
    assert "opencode" in headers["User-Agent"]
    assert headers["x-opencode-client"] == "tui"
    assert headers["x-opencode-session"].startswith("ses_")
    assert headers["x-opencode-request"].startswith("usr_")
    assert headers["x-opencode-project"].startswith("prj_")

    # one stable session id per process, fresh request/project ids per client
    models.get_client(settings)
    assert captured[1]["default_headers"]["x-opencode-session"] == headers["x-opencode-session"]
    assert captured[1]["default_headers"]["x-opencode-request"] != headers["x-opencode-request"]


def test_non_opencode_provider_gets_no_identification_headers(monkeypatch, tmp_path):
    captured = {}

    class StubOpenAICompatClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(models, "OpenAICompatClient", StubOpenAICompatClient)
    settings = Settings(provider="deepseek", api_key="", base_url=None, model="",
                        small_model="", home=tmp_path)

    models.get_client(settings)

    assert "default_headers" not in captured


def test_openai_default_is_tool_capable(tmp_path):
    """Regression: bare 'gpt-5.6' isn't callable, and the gpt-5.6 REASONING
    variants (luna/sol/terra) can't use function tools on /v1/chat/completions
    (they 400). The default must be a NON-reasoning, tool-capable chat model."""
    from waku.loop.models import PROVIDERS
    assert PROVIDERS["openai"].model == "gpt-5.5"
    assert PROVIDERS["openai"].default_pair() == ["gpt-5.5", "gpt-4.1-mini"]


def test_no_default_model_is_a_moving_alias():
    """The whole gpt-5.x-chat-latest line went 404 at once (issue #132), and
    waku shipped a default pointing into it. An alias is what made that a silent
    break rather than a caught one: the id in the repo never changed, so nothing
    local could tell it had stopped resolving.

    Pinning concrete ids does not stop a provider deprecating a model — nothing
    offline can. It stops the OTHER half of the failure, where a benchmark drifts
    under you because the name quietly started meaning something else. Catching
    an actual deprecation needs the live probe below.
    """
    from waku.loop.models import PROVIDERS

    offenders = [f"{name}: {m}"
                 for name, prov in PROVIDERS.items()
                 for m in prov.default_pair()
                 if m.endswith("-latest")]
    assert not offenders, (
        "default models must be concrete ids, not moving aliases — "
        f"{', '.join(offenders)}. See issue #132.")


@pytest.mark.skipif(not (HAS_KEY and RUN_LIVE_EVALS),
                    reason="set WAKU_RUN_LIVE_EVALS=1 and configure an API key to run live evals")
def test_openai_defaults_still_resolve_live():
    """The only check that can catch a deprecation, because only the provider
    knows. Calls both defaults in the exact shape OpenAICompatClient._to_openai
    sends — chat.completions WITH a function tool attached — because that is the
    combination gpt-5.6-luna passes as a bare call and 400s on.

    Off by default; `make gate` with WAKU_RUN_LIVE_EVALS=1 is where this bites.
    """
    import openai

    from waku.loop.models import PROVIDERS

    key = os.getenv("OPENAI_API_KEY")
    if not key:
        pytest.skip("no OPENAI_API_KEY")
    client = openai.OpenAI(api_key=key, timeout=60)
    tool = [{"type": "function",
             "function": {"name": "ping", "description": "ping",
                          "parameters": {"type": "object", "properties": {}}}}]
    for model in PROVIDERS["openai"].default_pair():
        client.chat.completions.create(
            model=model, tools=tool, max_completion_tokens=32,
            messages=[{"role": "user", "content": "call ping"}])


def test_gemini_thought_signature_round_trips():
    """Gemini thinking models attach a thought_signature to each tool call and
    REQUIRE it echoed back next turn, or the follow-up 400s. The OpenAI-compat
    adapter must capture it on parse (_create) and put it back on serialize
    (_to_openai). Verified end-to-end without a network call."""
    from waku.loop.models import OpenAICompatClient

    client = OpenAICompatClient.__new__(OpenAICompatClient)   # skip __init__ (no network)
    sig = {"google": {"thought_signature": "ABC123"}}
    toolcall = SimpleNamespace(id="t1", extra_content=sig,
                               function=SimpleNamespace(name="create_event", arguments='{"title":"x"}'))
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[toolcall]))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    client._call = lambda kwargs, **extra: resp

    parsed = client._create(model="gemini-3.5-flash", messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    block = next(b for b in parsed.content if b.type == "tool_use")
    assert block.extra == sig                                  # captured on parse

    kwargs = client._to_openai(model="gemini-3.5-flash", max_tokens=10,
                               messages=[{"role": "assistant", "content": parsed.content}])
    assert kwargs["messages"][0]["tool_calls"][0]["extra_content"] == sig   # echoed back


def test_deepseek_provider_uses_expected_key_endpoint_and_models(monkeypatch, tmp_path):
    captured = {}

    class StubOpenAICompatClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(models, "OpenAICompatClient", StubOpenAICompatClient)
    settings = Settings(
        provider="deepseek",
        api_key="",
        base_url=None,
        model="",
        small_model="",
        home=tmp_path,
    )

    client = models.get_client(settings)

    assert isinstance(client, StubOpenAICompatClient)
    assert captured["api_key"] == "test-deepseek-key"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert settings.model == "deepseek-v4-pro"
    assert settings.small_model == "deepseek-v4-pro"


def test_minimax_provider_uses_expected_key_endpoint_and_models(monkeypatch, tmp_path):
    captured = {}

    class StubAnthropicClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)
            self.messages = None

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        SimpleNamespace(Anthropic=StubAnthropicClient),
    )
    settings = Settings(
        provider="minimax",
        api_key="",
        base_url=None,
        model="",
        small_model="",
        home=tmp_path,
    )

    client = models.get_client(settings)

    assert isinstance(client, StubAnthropicClient)
    assert captured["api_key"] == "test-minimax-key"
    assert captured["base_url"] == "https://api.minimaxi.com/anthropic"
    assert captured["timeout"] == 120.0
    assert settings.model == "MiniMax-M3"
    assert settings.small_model == "MiniMax-M2"
