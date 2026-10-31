"""Gemini tool signatures survive the real SDK, loop and follow-up request.

Only the HTTP transport is faked. No provider credentials or network are used.
"""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from waku.loop.agent import run_loop
from waku.loop.models import OpenAICompatClient
from waku.tools.registry import Tool, ToolRegistry


def _tool_call(index, name, extra=None):
    call = {"index": index, "id": f"call_{index}", "type": "function",
            "function": {"name": name, "arguments": '{"value":"'}}
    if extra is not None:
        call["extra_content"] = extra
    return call


def _sse(deltas, finish):
    chunks = [{"id": "offline", "object": "chat.completion.chunk", "created": 1,
               "model": "gemini-test", "choices": [
                   {"index": 0, "delta": delta, "finish_reason": None}]}
              for delta in deltas]
    chunks.append({"id": "offline", "object": "chat.completion.chunk", "created": 1,
                   "model": "gemini-test", "choices": [
                       {"index": 0, "delta": {}, "finish_reason": finish}]})
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode("utf-8")


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("scenario", ["single", "signature_first", "signature_last",
                                      "multiple", "no_extra"])
def test_tool_metadata_survives_follow_up(stream, scenario):
    signatures = [{"google": {"thought_signature": f"opaque-signature-{i}"},
                   "provider_metadata": {"keep": [i, "unchanged"]}} for i in range(2)]
    names = ["get_time", "get_date"] if scenario == "multiple" else ["get_time"]
    expected = [None if scenario == "no_extra" else signatures[i]
                for i in range(len(names))]
    calls = [{"id": f"call_{i}", "type": "function",
              "function": {"name": name, "arguments": json.dumps({"value": name})},
              **({"extra_content": expected[i]} if expected[i] is not None else {})}
             for i, name in enumerate(names)]
    if scenario == "single":
        deltas = [{"tool_calls": [{"index": 0, **calls[0]}]}]
    else:
        deltas = [{"tool_calls": [_tool_call(i, name, None if scenario == "signature_last"
                                             else expected[i])]}
                  for i, name in enumerate(names)]
        # Interleave multiple calls; omitted, null and empty extras must not
        # erase metadata already received on an earlier chunk.
        for i in reversed(range(len(names))):
            deltas.append({"tool_calls": [{"index": i, "function": {
                "arguments": names[i] + '"}'}}]})
            if scenario == "signature_last":
                deltas.append({"tool_calls": [{"index": i, "extra_content": expected[i]}]})
            else:
                for extra in (None, {}):
                    deltas.append({"tool_calls": [{"index": i, "extra_content": extra}]})

    requests = []
    executions = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body.get("stream", False) == stream
        if len(requests) == 1:
            message = {"content": None, "tool_calls": calls}
            response_deltas, finish = deltas, "tool_calls"
        else:
            assistant = next(m for m in body["messages"] if m["role"] == "assistant")
            replayed = assistant["tool_calls"]
            if [call.get("extra_content") for call in replayed] != expected:
                return httpx.Response(400, json={"error": {
                    "message": "Function call is missing a thought_signature in functionCall parts.",
                    "type": "invalid_request_error"}})
            assert replayed == calls
            results = [m for m in body["messages"] if m["role"] == "tool"]
            assert [(m["tool_call_id"], m["content"]) for m in results] == [
                (f"call_{i}", name) for i, name in enumerate(names)]
            message = {"content": "Done."}
            response_deltas, finish = [{"content": "Done."}], "stop"
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_sse(response_deltas, finish))
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 1, "model": "gemini-test",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": finish}]})

    tools = ToolRegistry()
    for name in names:
        tools.register(Tool(name=name, description="Return the supplied value.",
                            input_schema={"type": "object", "properties": {
                                "value": {"type": "string"}}, "required": ["value"]},
                            fn=lambda value: executions.append(value) or value))
    # Supply the SDK client directly so every request uses MockTransport.
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    client.messages = SimpleNamespace(create=client._create, stream=client._stream)
    with openai.OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1",
                       max_retries=0, http_client=httpx.Client(
                           transport=httpx.MockTransport(handler))) as sdk:
        client._client = sdk
        result = run_loop(client, "gemini-test", "You are Waku.",
                          [{"role": "user", "content": "Call the tools."}], tools, stream=stream)
    assert result.reply == "Done."
    assert result.iterations == 2
    assert executions == names
    assert len(requests) == 2
