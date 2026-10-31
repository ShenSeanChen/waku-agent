"""Responses requests, output conversion, and tool history remain offline."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from evals.responses_helpers import (
    function_item,
    offline_adapter,
    response_payload,
    text_item,
)
from waku.config import Settings
from waku.loop.agent import run_loop
from waku.loop.models import OpenAICompatClient, get_client
from waku.loop.responses import OpenAIResponsesClient
from waku.tools.registry import Tool, ToolRegistry

TOOLS = [{"name": "lookup", "description": "Look up a city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
ARGS = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 128}


def test_configuration_selects_responses_and_preserves_endpoint(monkeypatch):
    captured = {}

    def sdk(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(openai, "OpenAI", sdk)
    monkeypatch.setenv("WAKU_OPENAI_API", "responses")
    settings = Settings(provider="openai", api_key="offline", base_url="https://proxy.test/v1",
                        model="gpt-6.1-sol", small_model="gpt-6.1-sol")
    assert settings.openai_api == "responses"
    client = get_client(settings)
    assert isinstance(client, OpenAIResponsesClient)
    assert captured["api_key"] == "offline"
    assert captured["base_url"] == "https://proxy.test/v1"
    assert settings.model == settings.small_model == "gpt-6.1-sol"


def test_chat_completions_remains_default(monkeypatch):
    monkeypatch.delenv("WAKU_OPENAI_API", raising=False)
    client = get_client(Settings(provider="openai", api_key="offline", base_url=None))
    assert isinstance(client, OpenAICompatClient)


@pytest.mark.parametrize("provider,api", [("openai", "typo"), ("anthropic", "responses")])
def test_invalid_protocol_fails_explicitly(provider, api):
    with pytest.raises(ValueError, match="WAKU_OPENAI_API"):
        get_client(Settings(provider=provider, api_key="offline", openai_api=api))


def test_create_uses_responses_wire_format_and_counts_tokens():
    adapter, calls = offline_adapter(response_payload([text_item()]))
    result = adapter.messages.create(**ARGS, system="Be brief", tools=TOOLS,
                                     tool_choice={"type": "none"})
    url, payload = calls[0]
    assert url == "https://proxy.test/v1/responses"
    assert payload["input"] == [{"role": "user", "content": "Hello"}]
    assert payload["model"] == "gpt-6.1-sol" and payload["instructions"] == "Be brief"
    assert payload["max_output_tokens"] == 128
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["tools"] == [{"type": "function", "name": "lookup",
                                 "description": "Look up a city", "strict": False,
                                 "parameters": TOOLS[0]["input_schema"]}]
    assert payload["tool_choice"] == "none"
    assert "messages" not in payload and "max_completion_tokens" not in payload
    assert result.content[0].text == "Done" and result.stop_reason == "end_turn"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)


def test_loop_replays_reasoning_parallel_calls_and_results_in_order():
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [],
                 "encrypted_content": "opaque-reasoning"}
    output = [reasoning, text_item("Checking"), function_item(),
              function_item("call_2", arguments='{"city":"Beijing"}')]
    adapter, calls = offline_adapter(response_payload(output), response_payload([text_item()]))
    registry = ToolRegistry()
    registry.register(Tool(name="lookup", description="city", input_schema=TOOLS[0]["input_schema"],
                           fn=lambda city: f"Weather for {city}"))
    result = run_loop(adapter, "gpt-6.1-sol", "Be brief", list(ARGS["messages"]), registry)
    assert result.reply == "Done" and result.iterations == 2
    assert [t["call_id"] for t in result.tool_calls] == ["call_1", "call_2"]
    replay = calls[1][1]["input"]
    assert replay[1:5] == output
    assert replay[5:] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "Weather for Shanghai"},
        {"type": "function_call_output", "call_id": "call_2", "output": "Weather for Beijing"},
    ]
    assert all(payload["store"] is False for _, payload in calls)
    assert "previous_response_id" not in calls[1][1]


def test_existing_history_without_responses_metadata_is_converted():
    adapter, calls = offline_adapter(response_payload([text_item()]))
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Find it"}]},
        {"role": "assistant", "content": [SimpleNamespace(type="text", text="Checking"),
         SimpleNamespace(type="tool_use", id="call_old", name="lookup", input={"city": "Shanghai"})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_old",
                                       "content": "Found"}]},
    ]
    adapter.messages.create(**{**ARGS, "messages": messages})
    items = calls[0][1]["input"]
    assert items[:2] == [{"role": "user", "content": "Find it"},
                         {"role": "assistant", "content": "Checking"}]
    assert items[2]["type"] == "function_call" and items[2]["call_id"] == "call_old"
    assert json.loads(items[2]["arguments"]) == {"city": "Shanghai"}
    assert items[3] == {"type": "function_call_output", "call_id": "call_old", "output": "Found"}


def test_limit_final_answer_disables_tools_on_responses_wire():
    adapter, calls = offline_adapter(response_payload([function_item()]),
                                     response_payload([text_item("Summary")]))
    registry = ToolRegistry()
    registry.register(Tool(name="lookup", description="city", input_schema=TOOLS[0]["input_schema"],
                           fn=lambda city: city))
    result = run_loop(adapter, "gpt-6.1-sol", "Brief", list(ARGS["messages"]), registry,
                      max_iterations=1)
    assert result.reply == "Summary" and result.limit_reached
    assert calls[1][1]["tool_choice"] == "none" and calls[1][1]["tools"]


@pytest.mark.parametrize("payload,match", [
    (response_payload([], status="failed", error={"code": "server_error", "message": "Down"}),
     "Down"),
    (response_payload([], status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
     "max_output_tokens"),
    (response_payload([]), "no text or function calls"),
    (response_payload([function_item(arguments="bad-json")]), "Expecting value"),
    (response_payload([function_item(arguments="[]")]), "JSON object"),
])
def test_invalid_responses_raise_instead_of_returning_success(payload, match):
    adapter, calls = offline_adapter(payload)
    with pytest.raises((RuntimeError, ValueError), match=match):
        adapter.messages.create(**ARGS)
    assert len(calls) == 1


def test_http_refusal_propagates_without_switching_protocols():
    adapter, calls = offline_adapter()

    def reject(request):
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": {"message": "Unsupported model", "type": "bad_request"}})

    adapter._client = openai.OpenAI(api_key="offline", base_url="https://proxy.test/v1",
                                   http_client=httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(openai.BadRequestError, match="Unsupported model"):
        adapter.messages.create(**ARGS)
    assert calls == ["https://proxy.test/v1/responses"]


def test_refusal_text_is_preserved():
    item = text_item()
    item["content"] = [{"type": "refusal", "refusal": "Cannot help"}]
    adapter, _ = offline_adapter(response_payload([item]))
    assert adapter.messages.create(**ARGS).content[0].text == "Cannot help"
