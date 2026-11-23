"""Reasoning survives the real SDK wire format, tool loop, and text history."""

import json
from types import SimpleNamespace

import openai
import pytest

from waku.config import Settings
from waku.loop.agent import run_loop
from waku.loop.models import OpenAICompatClient
from waku.runtime.session import Session

try:
    import httpx
except ImportError:
    # New SDK releases depend on httpx2; neither transport is a Waku extra.
    import httpx2 as httpx

MODEL = "deepseek-v4-pro"
REASONING = "Save the preference."
TOOL = {"name": "save_note", "description": "Save a note", "input_schema": {
    "type": "object", "properties": {}}}


def client_with_transport(handler):
    client = OpenAICompatClient(api_key="offline", base_url="https://example.invalid/v1")
    client._client.close()
    client._client = openai.OpenAI(
        api_key="offline", base_url="https://example.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client


def completion(body, message):
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    common = {"id": "test", "created": 1, "model": MODEL}
    if not body.get("stream"):
        return httpx.Response(200, json={**common, "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": finish}]})
    # Split reasoning across chunks; do not expose it as user-visible text.
    deltas = []
    if "reasoning_content" in message:
        reasoning = message["reasoning_content"]
        deltas.extend({"reasoning_content": part} for part in (reasoning[:5], reasoning[5:]))
    delta = {k: v for k, v in message.items() if k != "reasoning_content"}
    if delta.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **t} for i, t in enumerate(delta["tool_calls"])]
    deltas.append(delta)
    chunks = [{**common, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    chunks.append({**common, "object": "chat.completion.chunk", "choices": [],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
    data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


def refusal(message="Missing reasoning_content", status=400):
    return httpx.Response(status, json={"error": {
        "message": message, "type": "invalid_request_error"}})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("first_message", ["Remember", "Hi"])
def test_tool_turn_and_followup_history(stream, first_message, tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        if any("reasoning_content" not in m for m in assistants):
            return refusal()
        if body["messages"][-1]["content"] == "Remember":
            return completion(body, {"content": None, "reasoning_content": REASONING,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "save_note", "arguments": "{}"}}]})
        return completion(body, {"content": "Done.", "reasoning_content": "Answer briefly."})

    client = client_with_transport(handler)
    executed = []
    tools = SimpleNamespace(schemas=lambda: [TOOL], execute=lambda *a, **kw: executed.append(a) or "Saved")
    messages = [{"role": "user", "content": first_message}]
    events = []
    result = run_loop(client, MODEL, "", messages, tools, stream=stream,
                      observer=lambda kind, event: events.append((kind, event)))
    assert result.reply == "Done."
    expected_calls = 2 if first_message == "Remember" else 1
    assert len(executed) == expected_calls - 1
    assert len(bodies) == expected_calls  # No repair request is needed inside a turn.
    if first_message == "Remember":
        assistant = next(m for m in bodies[1]["messages"] if m["role"] == "assistant")
        assert assistant["reasoning_content"] == REASONING
        assert assistant["tool_calls"][0]["id"] == "call_1"
    if stream:
        assert "".join(e["delta"] for k, e in events if k == "text") == "Done."

    session = Session(Settings(home=tmp_path))
    session.add_exchange(first_message, result.reply)
    result = run_loop(client, MODEL, "", session.history + [{"role": "user", "content": "Next"}],
                      tools, stream=stream)
    assert result.reply == "Done."
    assert bodies[-1]["messages"][1]["reasoning_content"] == ""
    assert len(bodies) == expected_calls + 1
    client._client.close()


@pytest.mark.parametrize("stream", [False, True])
def test_restored_history_repairs_once_without_mutation(stream, tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_content" not in body["messages"][1]:
            return refusal()
        return completion(body, {"content": "Done.", "reasoning_content": ""})

    session = Session(Settings(home=tmp_path), memory=SimpleNamespace(
        session_history=lambda _: [("Hi", "Hello")]))
    session.switch("restored")
    client = client_with_transport(handler)
    kwargs = {"model": MODEL, "max_tokens": 32, "messages": session.history, "tools": [TOOL]}
    if stream:
        with client.messages.stream(**kwargs) as s:
            assert list(s.text_stream) == ["Done."]
            result = s.get_final_message()
    else:
        result = client.messages.create(**kwargs)
    assert len(bodies) == 2
    assert bodies[1]["messages"][1]["reasoning_content"] == ""
    assert session.history == [{"role": "user", "content": "Hi"},
                               {"role": "assistant", "content": "Hello"}]
    blocks = [b for b in result.content if b.type == "thinking"]
    assert len(blocks) == 1 and blocks[0].thinking == ""
    client._client.close()


@pytest.mark.parametrize("message,status", [("Invalid tools", 400), ("reasoning_content", 401)])
def test_unrelated_errors_are_not_retried(message, status):
    requests = []

    def handler(request):
        requests.append(request)
        return refusal(message, status)

    client = client_with_transport(handler)
    with pytest.raises(openai.APIStatusError):
        client.messages.create(model=MODEL, max_tokens=32, tools=[TOOL],
                               messages=[{"role": "assistant", "content": "Hi"}])
    assert len(requests) == 1
    client._client.close()


def test_reasoning_support_is_scoped_to_model_and_preserves_existing_values():
    def handler(request):
        return completion(json.loads(request.content), {"content": "Hi", "reasoning_content": ""})

    client = client_with_transport(handler)
    client.messages.create(model=MODEL, max_tokens=32, messages=[{"role": "user", "content": "Hi"}])
    history = [{"role": "assistant", "content": "Hello"}]
    kwargs = {"max_tokens": 32, "messages": history, "tools": [TOOL]}
    assert client._to_openai(model=MODEL, **kwargs)["messages"][0]["reasoning_content"] == ""
    for model in ("gpt-4.1", "gemini-3.5-flash"):
        assert client._to_openai(model=model, **kwargs)["messages"] == history
    blocks = [SimpleNamespace(type="thinking", thinking=REASONING)]
    wire = client._to_openai(model=MODEL, max_tokens=32, tools=[TOOL], messages=[
        {"role": "assistant", "content": blocks}])
    assert wire["messages"][0]["reasoning_content"] == REASONING
    client._client.close()


def test_reasoning_repair_is_bounded():
    requests = []

    def handler(request):
        requests.append(request)
        return refusal()

    client = client_with_transport(handler)
    with pytest.raises(openai.BadRequestError):
        client.messages.create(model=MODEL, max_tokens=32, tools=[TOOL],
                               messages=[{"role": "assistant", "content": "Hi"}])
    assert len(requests) == 2
    client._client.close()


@pytest.mark.parametrize("token_error_first", [False, True])
def test_reasoning_repair_and_token_fallback_preserve_existing_reasoning(token_error_first):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        missing = "reasoning_content" not in body["messages"][0]
        if "max_completion_tokens" in body and (token_error_first or not missing):
            return refusal("Unsupported parameter max_completion_tokens; use max_tokens")
        if missing:
            return refusal()
        return completion(body, {"content": "Done."})

    client = client_with_transport(handler)
    client.messages.create(model=MODEL, max_tokens=32, tools=[TOOL], messages=[
        {"role": "assistant", "content": "Old reply"},
        {"role": "assistant", "content": [SimpleNamespace(type="thinking", thinking=REASONING)]}])
    assert len(bodies) == 3
    assert bodies[-1]["max_tokens"] == 32
    assert bodies[-1]["messages"][0]["reasoning_content"] == ""
    assert bodies[-1]["messages"][1]["reasoning_content"] == REASONING
    client._client.close()
