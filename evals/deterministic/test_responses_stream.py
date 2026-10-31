"""Responses streaming must preserve tool output and reject partial streams."""

import pytest

from evals.responses_helpers import function_item, offline_adapter, response_payload, sse, text_item
from waku.loop.agent import run_loop
from waku.tools.registry import Tool, ToolRegistry

ARGS = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 128}


@pytest.mark.parametrize("streaming", [True, False])
def test_done_items_survive_an_empty_completion_snapshot(streaming):
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [],
                 "encrypted_content": "opaque"}
    output = [reasoning, text_item("Hello"), function_item()]
    adapter, calls = offline_adapter(sse(
        {"type": "response.output_text.delta", "delta": "Hello"},
        *({"type": "response.output_item.done", "output_index": i, "item": item}
          for i, item in enumerate(output)),
        {"type": "response.completed", "response": response_payload([])},
    ))
    if streaming:
        with adapter.messages.stream(**ARGS) as stream:
            assert list(stream.text_stream) == ["Hello"]
            result = stream.get_final_message()
    else:
        result = adapter.messages.create(**ARGS)
    assert result.content[0].text == "Hello"
    assert result.content[0].responses_output == output
    assert result.content[1].input == {"city": "Shanghai"}
    assert result.stop_reason == "tool_use"
    assert result.usage.output_tokens == 7
    assert len(calls) == 1 and calls[0][1]["stream"] is True


@pytest.mark.parametrize("events,match", [
    (sse({"type": "response.output_text.delta", "delta": "Unfinished"},
         {"type": "response.completed", "response": response_payload([])}),
     "no text or function calls"),
    (sse({"type": "response.output_item.done", "output_index": 1, "item": text_item()},
         {"type": "response.completed", "response": response_payload([])}),
     "missing completed output items"),
    (sse({"type": "response.output_item.done", "output_index": 0, "item": text_item()},
         {"type": "response.incomplete", "response": response_payload(
             [], status="incomplete", incomplete_details={"reason": "max_output_tokens"})}),
     "max_output_tokens"),
])
def test_stream_does_not_accept_unfinished_output(events, match):
    adapter, calls = offline_adapter(events)
    with pytest.raises(RuntimeError, match=match):
        adapter.messages.create(**ARGS)
    assert len(calls) == 1


def test_completion_snapshot_does_not_duplicate_done_items():
    output = [text_item()]
    adapter, _ = offline_adapter(sse(
        {"type": "response.output_item.done", "output_index": 0, "item": output[0]},
        {"type": "response.completed", "response": response_payload(output)}))
    result = adapter.messages.create(**ARGS)
    assert len(result.content) == 1
    assert result.content[0].responses_output == output


def test_stream_yields_text_and_keeps_reasoning_calls_and_usage():
    output = [{"id": "rs_1", "type": "reasoning", "summary": [],
               "encrypted_content": "opaque"}, text_item("Hello world"), function_item()]
    events = sse(
        {"type": "response.output_text.delta", "delta": "Hello "},
        {"type": "response.output_text.delta", "delta": "world"},
        {"type": "response.completed", "response": response_payload(output)},
    )
    adapter, calls = offline_adapter(events)
    with adapter.messages.stream(**ARGS) as stream:
        assert list(stream.text_stream) == ["Hello ", "world"]
        result = stream.get_final_message()
    assert calls[0][0] == "https://proxy.test/v1/responses"
    assert calls[0][1]["stream"] is True and "stream_options" not in calls[0][1]
    assert result.content[0].text == "Hello world"
    assert result.content[0].responses_output == output
    assert result.content[1].id == "call_1" and result.content[1].input == {"city": "Shanghai"}
    assert result.stop_reason == "tool_use"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)
    assert stream._events.response.is_closed


def test_dashboard_loop_streams_then_executes_tool_and_replays_history():
    first = response_payload([function_item()])
    second = response_payload([text_item("Found")])
    adapter, calls = offline_adapter(
        sse({"type": "response.completed", "response": first}),
        sse({"type": "response.output_text.delta", "delta": "Found"},
            {"type": "response.completed", "response": second}),
    )
    registry = ToolRegistry()
    registry.register(Tool(name="lookup", description="city",
                           input_schema={"type": "object", "properties": {}}, fn=lambda city: city))
    events = []
    result = run_loop(adapter, "gpt-6.1-sol", "Brief", list(ARGS["messages"]), registry,
                      stream=True, observer=lambda kind, ev: events.append((kind, ev)))
    assert result.reply == "Found" and result.iterations == 2
    assert [event["delta"] for kind, event in events if kind == "text"] == ["Found"]
    assert [event["usage"] for kind, event in events if kind == "llm"] == [
        {"in": 11, "out": 7}, {"in": 11, "out": 7}]
    assert calls[1][1]["input"][-1] == {
        "type": "function_call_output", "call_id": "call_1", "output": "Shanghai"}


@pytest.mark.parametrize("events,match", [
    (sse({"type": "response.output_text.delta", "delta": "Partial"}), "before response.completed"),
    (sse({"type": "error", "code": "server_error", "message": "Offline"}), "Offline"),
    (sse({"type": "response.failed", "response": response_payload(
        [], status="failed", error={"code": "server_error", "message": "Down"})}), "Down"),
    (sse({"type": "response.incomplete", "response": response_payload(
        [], status="incomplete", incomplete_details={"reason": "max_output_tokens"})}),
     "max_output_tokens"),
])
def test_stream_failures_propagate_and_close_the_connection(events, match):
    adapter, calls = offline_adapter(events)
    with pytest.raises(RuntimeError, match=match), adapter.messages.stream(**ARGS) as stream:
        list(stream.text_stream)
    assert len(calls) == 1 and stream._events.response.is_closed


def test_stream_requires_completion_before_reading_final_message():
    adapter, _ = offline_adapter(sse({"type": "response.completed",
                                     "response": response_payload([text_item()])}))
    with (adapter.messages.stream(**ARGS) as stream,
          pytest.raises(RuntimeError, match="no completed response")):
        stream.get_final_message()


def test_stream_refusal_delta_is_visible():
    item = text_item()
    item["content"] = [{"type": "refusal", "refusal": "Cannot help"}]
    adapter, _ = offline_adapter(sse(
        {"type": "response.refusal.delta", "delta": "Cannot help"},
        {"type": "response.completed", "response": response_payload([item])}))
    with adapter.messages.stream(**ARGS) as stream:
        assert list(stream.text_stream) == ["Cannot help"]
        assert stream.get_final_message().content[0].text == "Cannot help"
