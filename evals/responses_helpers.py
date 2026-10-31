"""Responses fixtures decoded by the real SDK over an offline transport."""

import json
from types import SimpleNamespace

import httpx
import openai

from waku.loop.responses import OpenAIResponsesClient


def response_payload(output, **overrides):
    """Build a completed response."""
    return {"id": "resp_1", "object": "response", "created_at": 1,
            "model": "gpt-6.1-sol", "status": "completed", "error": None,
            "incomplete_details": None, "output": output,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            **overrides}


def text_item(text="Done"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def function_item(call_id="call_1", name="lookup", arguments='{"city":"Shanghai"}'):
    return {"id": "fc_1", "type": "function_call", "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def offline_adapter(*replies):
    """Capture real SDK requests."""
    requests, pending = [], iter(replies)

    def handle(request):
        requests.append((str(request.url), json.loads(request.content)))
        reply = next(pending)
        if isinstance(reply, str):
            return httpx.Response(200, text=reply, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=reply)

    adapter = OpenAIResponsesClient.__new__(OpenAIResponsesClient)
    adapter._client = openai.OpenAI(
        api_key="offline", base_url="https://proxy.test/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    adapter.messages = SimpleNamespace(create=adapter._create, stream=adapter._stream)
    return adapter, requests
