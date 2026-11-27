"""DETERMINISTIC EVAL -- what the metering proxy accepts (spec 004 C2, spec 001 step 3).

The proxy is the one service tenant code can reach with the platform key
behind it. Everything it forwards is the parsed, validated object
re-serialised, never the raw bytes, and anything outside the allowlists is a
400 in Anthropic's own error shape. Waku's real request shapes pass: a
streamed turn with tools, and a second call carrying the first call's
thinking blocks.
"""

from __future__ import annotations

import json

import pytest

from hosted.proxy.admission import Refused, admit, forward_headers, strip_cache_control

MODELS = ("claude-sonnet-5-5", "claude-haiku-4-5")


def _admit(body: dict | bytes, ceiling: int = 8192) -> dict:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return admit(raw, models=MODELS, ceiling=ceiling)


def _turn(**extra) -> dict:
    return {"model": "claude-sonnet-5-5", "max_tokens": 8192, "stream": True,
            "system": "You are Waku.",
            "messages": [{"role": "user", "content": "what did Acme ship?"}],
            "tools": [{"name": "remember", "description": "keep a fact",
                       "input_schema": {"type": "object", "properties": {}}}],
            **extra}


def test_wakus_streamed_turn_with_tools_is_admitted_unchanged():
    assert _admit(_turn()) == _turn()


def test_a_second_call_carrying_thinking_blocks_is_admitted():
    """Sonnet 5.5 thinks by default and Waku appends response.content to history."""
    body = _turn(messages=[
        {"role": "user", "content": "what did Acme ship?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "t1", "name": "remember", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ])
    assert _admit(body)["messages"] == body["messages"]


def test_max_tokens_is_clamped_to_the_ceiling():
    assert _admit(_turn(max_tokens=64000), ceiling=8192)["max_tokens"] == 8192
    assert _admit(_turn(max_tokens=600))["max_tokens"] == 600


@pytest.mark.parametrize("body,needle", [
    (b"{not json", "JSON"),
    (b'{"model": "claude-sonnet-5-5", "model": "claude-haiku-4-5"}', "duplicate"),
    (b"[]", "object"),
])
def test_malformed_bodies_are_refused(body, needle):
    with pytest.raises(Refused) as caught:
        _admit(body)
    assert caught.value.status == 400 and needle in caught.value.message


@pytest.mark.parametrize("extra,needle", [
    ({"thinking": {"type": "enabled"}}, "thinking"),
    ({"betas": ["x"]}, "betas"),
    ({"service_tier": "priority"}, "service_tier"),
    ({"tools": [{"type": "web_search_20260209", "name": "web_search"}]}, "server tool"),
    ({"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://x"}}]}]}, "image"),
    ({"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "image", "source": {}}]}]}]}, "tool_result"),
    ({"messages": [{"role": "user", "content": [
        {"type": "thinking", "thinking": "", "signature": "s"}]}]}, "thinking"),
    ({"messages": [{"role": "system", "content": "x"}]}, "role"),
    ({"max_tokens": 0}, "max_tokens"),
])
def test_anything_outside_the_allowlists_is_refused(extra, needle):
    with pytest.raises(Refused) as caught:
        _admit(_turn(**extra))
    assert caught.value.status == 400 and needle in caught.value.message


def test_a_model_off_the_allowlist_is_a_403_with_the_spec_sentence():
    with pytest.raises(Refused) as caught:
        _admit(_turn(model="claude-opus-5-5"))
    assert caught.value.status == 403
    assert caught.value.message == "This model is not in the free tier. Add your own key in Models."


def test_the_error_body_is_anthropics_shape():
    with pytest.raises(Refused) as caught:
        _admit(b"{not json")
    assert caught.value.body() == {"type": "error", "error": {
        "type": "invalid_request_error", "message": caught.value.message}}


def test_only_two_headers_go_upstream():
    assert forward_headers({"anthropic-version": "2023-06-01", "Content-Type": "application/json",
                            "x-api-key": "tenant-token", "anthropic-beta": "x",
                            "Authorization": "Bearer y"}) == {
        "anthropic-version": "2023-06-01", "content-type": "application/json"}


# --- spec 039 B1: prompt caching -------------------------------------------------------

EPHEMERAL = {"type": "ephemeral"}


def test_a_cache_breakpoint_on_the_system_prompt_is_admitted_unchanged():
    """Waku's hosted turn marks the end of its stable system prompt (spec 039
    B3). The proxy refused every cache_control before (F5), which turned
    prompt caching off for every tenant."""
    body = _turn(system=[{"type": "text", "text": "You are Waku.", "cache_control": EPHEMERAL},
                         {"type": "text", "text": "Right now it is Tuesday."}])
    assert _admit(body) == body


@pytest.mark.parametrize("where", ["tool", "message"])
def test_a_cache_breakpoint_on_a_tool_or_a_message_block_is_admitted(where):
    if where == "tool":
        body = _turn(tools=[{"name": "remember", "description": "keep a fact",
                             "input_schema": {"type": "object", "properties": {}},
                             "cache_control": EPHEMERAL}])
    else:
        body = _turn(messages=[{"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": EPHEMERAL}]}])
    assert _admit(body) == body


@pytest.mark.parametrize("control", [
    {"type": "ephemeral", "ttl": "1h"},          # a one-hour write costs twice the input price
    {"type": "persistent"},
    {"type": "ephemeral", "extra": 1},
    "ephemeral",
    None,
])
def test_only_the_five_minute_ephemeral_breakpoint_is_admitted(control):
    body = _turn(system=[{"type": "text", "text": "x", "cache_control": control}])
    with pytest.raises(Refused) as caught:
        _admit(body)
    assert caught.value.status == 400 and "cache_control" in caught.value.message


def test_cache_control_does_not_open_any_other_refused_field():
    """Every field the allowlist refused before is still refused when the
    same body also carries a cache breakpoint."""
    cached = [{"type": "text", "text": "x", "cache_control": EPHEMERAL}]
    for extra, needle in [({"thinking": {"type": "enabled"}}, "thinking"),
                          ({"betas": ["x"]}, "betas"),
                          ({"service_tier": "priority"}, "service_tier"),
                          ({"tools": [{"type": "web_search_20260209", "name": "web_search"}]},
                           "server tool")]:
        with pytest.raises(Refused) as caught:
            _admit(_turn(system=cached, **extra))
        assert needle in caught.value.message
    with pytest.raises(Refused) as caught:
        _admit(_turn(system=cached, messages=[{"role": "user", "content": [
            {"type": "image", "source": {}, "cache_control": EPHEMERAL}]}]))
    assert "image" in caught.value.message


def test_strip_cache_control_leaves_everything_else():
    """The seam for an upstream that does not take Anthropic's field (spec 039
    F16): every breakpoint goes, and nothing else changes."""
    body = _turn(system=[{"type": "text", "text": "You are Waku.", "cache_control": EPHEMERAL}],
                 tools=[{"name": "remember", "description": "d",
                         "input_schema": {"type": "object", "properties": {}},
                         "cache_control": EPHEMERAL}],
                 messages=[{"role": "user", "content": [
                     {"type": "text", "text": "hi", "cache_control": EPHEMERAL}]}])
    stripped = strip_cache_control(body)
    assert "cache_control" not in json.dumps(stripped)
    assert stripped["system"] == [{"type": "text", "text": "You are Waku."}]
    assert stripped["tools"][0]["name"] == "remember"
    assert stripped["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "cache_control" in json.dumps(body), "the body it was given is not changed"
