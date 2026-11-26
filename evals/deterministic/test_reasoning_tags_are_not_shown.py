"""A reasoning block written into the answer text stays out of the reply.

gpt-oss on Amazon Bedrock's OpenAI-compatible address puts its reasoning in
the answer itself, as <reasoning>...</reasoning> before the reply (spec 036).
Any OpenAI-style row gets the same treatment: a block at the very start of the
reply is moved into a thinking block, which the turn (and so the trace) keeps.
A reply that only mentions the tag is left exactly as the model wrote it.
"""

import json
from types import SimpleNamespace

import openai
import pytest

from waku.loop.agent import run_loop
from waku.loop.models import OpenAICompatClient

try:
    import httpx
except ImportError:
    # New SDK releases depend on httpx2; neither transport is a Waku extra.
    import httpx2 as httpx

MODEL = "openai.gpt-oss-120b-1:0"
TOOL = {"name": "save_note", "description": "Save a note", "input_schema": {
    "type": "object", "properties": {}}}


def client_answering(content: str, bodies: list):
    """A client whose endpoint always answers `content`, streamed in small
    pieces so a tag is split across chunks the way a real stream splits it."""

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        common = {"id": "test", "created": 1, "model": MODEL}
        if not body.get("stream"):
            return httpx.Response(200, json={**common, "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}]})
        pieces = [content[i:i + 4] for i in range(0, len(content), 4)]
        chunks = [{**common, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
        chunks.append({**common, "object": "chat.completion.chunk", "choices": [],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
        data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    client = OpenAICompatClient(api_key="offline", base_url="https://example.invalid/v1")
    client._client.close()
    client._client = openai.OpenAI(
        api_key="offline", base_url="https://example.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return client


def run_turn(content: str, stream: bool):
    bodies: list = []
    client = client_answering(content, bodies)
    tools = SimpleNamespace(schemas=lambda: [TOOL], execute=lambda *a, **kw: "Saved")
    messages = [{"role": "user", "content": "Summarise the open PRs"}]
    events = []
    result = run_loop(client, MODEL, "", messages, tools, stream=stream,
                      observer=lambda kind, event: events.append((kind, event)))
    shown = "".join(e["delta"] for k, e in events if k == "text")
    turn = messages[-1]["content"]
    client._client.close()
    return result, shown, turn, client, bodies


@pytest.mark.parametrize("stream", [False, True])
def test_a_leading_reasoning_block_is_moved_out_of_the_reply(stream):
    result, shown, turn, _, _ = run_turn("<reasoning>x</reasoning>Answer", stream)
    assert result.reply == "Answer"
    if stream:
        assert shown == "Answer"
    thinking = [b.thinking for b in turn if b.type == "thinking"]
    assert thinking == ["x"], "the turn the trace records keeps the reasoning"
    assert [b.text for b in turn if b.type == "text"] == ["Answer"]


@pytest.mark.parametrize("stream", [False, True])
def test_whitespace_around_the_block_is_not_shown(stream):
    content = "\n<reasoning>Read all five PRs first.</reasoning>\n\nFive PRs are open."
    result, shown, turn, _, _ = run_turn(content, stream)
    assert result.reply == "Five PRs are open."
    if stream:
        assert shown == "Five PRs are open."
    assert [b.thinking for b in turn if b.type == "thinking"] == ["Read all five PRs first."]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", [
    "Models such as gpt-oss write a <reasoning> tag before they answer.",
    "Answer first. <reasoning>x</reasoning> stays because it is not at the start.",
    "<reasoning>never closed, so nothing is removed",
    "<reason>a different tag</reason>Answer",
])
def test_a_reply_that_only_mentions_the_tag_is_unchanged(stream, content):
    result, shown, turn, _, _ = run_turn(content, stream)
    assert result.reply == content
    if stream:
        assert shown == content
    assert not [b for b in turn if b.type == "thinking"]


@pytest.mark.parametrize("stream", [False, True])
def test_moved_reasoning_is_not_sent_back_as_a_new_field(stream):
    """The endpoint never sent reasoning_content, so the next request must not
    start sending it: a strict endpoint would refuse the unknown field."""
    _, _, turn, client, _ = run_turn("<reasoning>x</reasoning>Answer", stream)
    wire = client._to_openai(model=MODEL, max_tokens=32, tools=[TOOL], messages=[
        {"role": "user", "content": "Hi"}, {"role": "assistant", "content": turn},
        {"role": "user", "content": "Next"}])
    assistant = wire["messages"][1]
    assert assistant["content"] == "Answer"
    assert "reasoning_content" not in assistant
    assert MODEL not in getattr(client, "_reasoning_models", set())
