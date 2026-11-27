"""DETERMINISTIC EVAL: provider fallback routing on 429, 5xx, and timeouts.

Offline: fake clients in place of Anthropic/OpenAI SDKs. Verifies that
`FallbackClient` fails over on retryable outages, cools down the failed
provider, rewrites model/small_model per target, refuses to fail over on
non-retryable 4xx errors or mid-stream, and integrates with `get_client`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from waku.config import Settings
from waku.loop.agent import run_loop
from waku.loop.models import (
    FALLBACK_DOWN_SECONDS,
    FallbackClient,
    FallbackTarget,
    get_client,
)
from waku.tools.registry import ToolRegistry


class _ApiError(Exception):
    def __init__(self, status_code: int, message: str = "error") -> None:
        super().__init__(message)
        self.status_code = status_code


def _reply(text: str):
    return SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[SimpleNamespace(type="text", text=text)],
    )


class _FakeStream:
    def __init__(self, chunks: list[str], reply_text: str, exc: BaseException | None = None,
                 enter_exc: BaseException | None = None):
        self._chunks = chunks
        self._reply_text = reply_text
        self._exc = exc
        self._enter_exc = enter_exc
        self.exited = False

    def __enter__(self):
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    @property
    def text_stream(self):
        yield from self._chunks
        if self._exc is not None:
            raise self._exc

    def get_final_message(self):
        assert not self.exited, "inner stream was closed before get_final_message()"
        return _reply(self._reply_text)


class _FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            return _FakeStream([], "", exc=item)
        return item


@pytest.mark.parametrize("exc", [
    _ApiError(503, "high demand"),
    _ApiError(429, "rate limit"),
    _ApiError(500, "internal"),
    _ApiError(529, "overloaded"),
    _ApiError(400, "You have reached your specified API usage limits"),
    TimeoutError("timed out"),
])
def test_create_fails_over_on_retryable_errors_and_rewrites_models(exc):
    primary = _FakeClient([exc])
    backup = _FakeClient([_reply("from backup"), _reply("gate from backup")])
    clock = [100.0]

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
        monotonic=lambda: clock[0],
    )

    first = client.messages.create(model="gemini-2.5-flash", messages=[])
    assert first.content[0].text == "from backup"
    assert backup.calls[0]["model"] == "gpt-5.2"

    # Cooldown: primary is skipped on the second call, and small_model maps to backup's small_model.
    second = client.messages.create(model="gemini-2.5-flash-lite", messages=[])
    assert second.content[0].text == "gate from backup"
    assert len(primary.calls) == 1
    assert backup.calls[1]["model"] == "gpt-5-nano"


def test_primary_recovers_after_cooldown_expires():
    primary = _FakeClient([_ApiError(503, "down"), _reply("primary back")])
    backup = _FakeClient([_reply("backup")])
    clock = [0.0]

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
        monotonic=lambda: clock[0],
    )

    assert client.messages.create(model="gemini-2.5-flash", messages=[]).content[0].text == "backup"
    clock[0] += FALLBACK_DOWN_SECONDS + 1.0
    assert client.messages.create(model="gemini-2.5-flash", messages=[]).content[0].text == "primary back"
    assert len(primary.calls) == 2


@pytest.mark.parametrize("exc", [
    _ApiError(400, "invalid tool schema"),
    _ApiError(401, "invalid api key"),
    _ApiError(403, "forbidden"),
    _ApiError(404, "model not found"),
])
def test_non_retryable_client_errors_do_not_fail_over(exc):
    primary = _FakeClient([exc])
    backup = _FakeClient([_reply("should not run")])

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
    )

    with pytest.raises(_ApiError) as raised:
        client.messages.create(model="gemini-2.5-flash", messages=[])
    assert raised.value.status_code == exc.status_code
    assert backup.calls == []


def test_all_targets_failing_raises_last_retryable_error():
    primary = _FakeClient([_ApiError(503, "primary down")])
    backup = _FakeClient([_ApiError(429, "backup limited")])

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
    )

    with pytest.raises(_ApiError) as raised:
        client.messages.create(model="gemini-2.5-flash", messages=[])
    assert raised.value.status_code == 429


def test_stream_fails_over_before_first_delta_and_never_splices_mid_stream():
    primary = _FakeClient([
        _ApiError(503, "down before first byte"),
        _FakeStream(["hel"], "hel", exc=_ApiError(503, "dropped mid-stream")),
    ])
    backup = _FakeClient([_FakeStream(["ok ", "done"], "ok done")])
    clock = [0.0]

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
        monotonic=lambda: clock[0],
    )

    with client.messages.stream(model="gemini-2.5-flash", messages=[]) as s:
        assert "".join(s.text_stream) == "ok done"
        assert s.get_final_message().content[0].text == "ok done"

    # After cooldown expires, a mid-stream failure after yielding text must raise, not splice.
    clock[0] += FALLBACK_DOWN_SECONDS + 1.0
    with pytest.raises(_ApiError), client.messages.stream(model="gemini-2.5-flash", messages=[]) as s:
        list(s.text_stream)

    with pytest.raises(RuntimeError, match="before stream succeeded"):
        client.messages.stream(model="gemini-2.5-flash", messages=[]).get_final_message()


def test_stream_fails_over_when_enter_raises_like_anthropic_sdk():
    primary = _FakeClient([_FakeStream([], "", enter_exc=_ApiError(503, "overloaded in __enter__"))])
    backup = _FakeClient([_FakeStream(["from ", "backup"], "from backup")])
    client = FallbackClient(
        FallbackTarget("anthropic", primary, "claude-sonnet-5", "claude-haiku-4-5-20251001"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
    )

    with client.messages.stream(model="claude-sonnet-5", messages=[]) as s:
        assert "".join(s.text_stream) == "from backup"
        assert s.get_final_message().content[0].text == "from backup"


def test_multi_hop_fallback_and_all_in_cooldown():
    primary = _FakeClient([_ApiError(503, "primary down"), _ApiError(503, "still down")])
    first_backup = _FakeClient([_ApiError(429, "first backup limited"), _reply("first recovered")])
    second_backup = _FakeClient([_reply("second backup")])
    clock = [0.0]

    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [
            FallbackTarget("groq", first_backup, "llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
            FallbackTarget("openai", second_backup, "gpt-5.2", "gpt-5-nano"),
        ],
        monotonic=lambda: clock[0],
    )

    assert client.messages.create(model="gemini-2.5-flash", messages=[]).content[0].text == "second backup"

    # When all targets are in cooldown, _candidates() still tries them in order.
    second_backup._responses.append(_ApiError(503, "second down"))
    # Put second_backup into cooldown too, then advance nothing: all 3 are down, so it tries primary -> first_backup.
    with pytest.raises(_ApiError):
        client.messages.create(model="gemini-2.5-flash", messages=[])
    # Now first_backup has "first recovered" queued; primary fails 503 and first_backup answers.
    assert client.messages.create(model="gemini-2.5-flash", messages=[]).content[0].text == "first recovered"


def test_equal_model_and_small_model_maps_to_target_main_model():
    primary = _FakeClient([_ApiError(503, "down")])
    backup = _FakeClient([_reply("ok")])
    client = FallbackClient(
        FallbackTarget("deepseek", primary, "deepseek-v4-pro", "deepseek-v4-pro"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
    )
    client.messages.create(model="deepseek-v4-pro", messages=[])
    assert backup.calls[0]["model"] == "gpt-5.2"


def test_run_loop_succeeds_when_primary_hits_503():
    primary = _FakeClient([_ApiError(503, "overloaded")])
    backup = _FakeClient([_reply("backup answered")])
    client = FallbackClient(
        FallbackTarget("gemini", primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"),
        [FallbackTarget("openai", backup, "gpt-5.2", "gpt-5-nano")],
    )

    result = run_loop(
        client=client, model="gemini-2.5-flash", system="sys",
        messages=[{"role": "user", "content": "hi"}], tools=ToolRegistry(),
    )
    assert result.reply == "backup answered"


def test_get_client_builds_fallback_only_when_configured_and_keyed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", "https://proxy.example.com")
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "plat-tok")

    plain = get_client(Settings(provider="gemini", fallback_providers=()))
    assert not isinstance(plain, FallbackClient)

    # 'groq' has no key (skipped), 'deepseek' is disabled (skipped),
    # 'waku-platform' is scoped_credentials (skipped), 'openai' is keyed (kept).
    # Primary WAKU_API_KEY and WAKU_BASE_URL must not leak into fallback targets.
    wrapped = get_client(Settings(
        provider="gemini",
        api_key="custom-primary-key",
        base_url="https://custom-primary.example.com/v1",
        fallback_providers=("groq", "deepseek", "waku-platform", "openai", "gemini"),
        disabled_providers=frozenset({"deepseek"}),
    ))
    assert isinstance(wrapped, FallbackClient)
    assert [t.name for t in wrapped._targets] == ["gemini", "openai"]
    openai_target = wrapped._targets[1]
    assert openai_target.client._client.api_key == "oai-key"
    assert str(openai_target.client._client.base_url).rstrip("/") == "https://api.openai.com/v1"
