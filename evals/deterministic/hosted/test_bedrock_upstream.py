"""DETERMINISTIC EVAL -- the Bedrock fallback behind the meter (spec 043).

Offline: Anthropic and Bedrock are fakes, and one test runs a local aiohttp
server in Bedrock's place. What is pinned: the Anthropic-to-chat-completions
translation both ways, the routing (Anthropic first, Bedrock when Anthropic
refuses, ten minutes skipped), and that a Bedrock turn is charged at the price
of the model that ran it.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp import web

from evals.deterministic.hosted.test_proxy import MONTH, SONNET, TENANT, Rig, _chunks, _run
from hosted.ports.upstream import UpstreamResponse
from hosted.proxy import prices, translate
from hosted.proxy.app import _StreamUsage
from hosted.proxy.upstream import DOWN_SECONDS, BedrockUpstream, FallbackUpstream

GLM = "zai.glm-5"
LIMIT = json.dumps({"type": "error", "error": {
    "type": "invalid_request_error",
    "message": "You have reached your specified API usage limits. You will regain access "
               "on 2026-12-01 at 00:00 UTC."}}).encode()

# Sasha's turn from the spec: a system prompt, two tools, and a board tool that
# already ran once.
TURN = {
    "model": SONNET, "max_tokens": 1000, "stream": True,
    "system": [{"type": "text", "text": "You are Waku."}],
    "tools": [
        {"name": "board_list", "description": "List board messages.",
         "input_schema": {"type": "object", "properties": {"box": {"type": "string"}}}},
        {"name": "memory_search", "description": "Search memories.",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    ],
    "messages": [
        {"role": "user", "content": "What is waiting for me on the board?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking the board."},
            {"type": "tool_use", "id": "toolu_1", "name": "board_list", "input": {"box": "for_me"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "2 messages wait on you."}]},
    ],
}


def test_a_messages_turn_becomes_a_chat_completions_request():
    out = translate.to_chat_completions(TURN, GLM)
    assert out["model"] == GLM and out["max_tokens"] == 1000
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}
    assert out["messages"] == [
        {"role": "system", "content": "You are Waku."},
        {"role": "user", "content": "What is waiting for me on the board?"},
        {"role": "assistant", "content": "Checking the board.", "tool_calls": [
            {"id": "toolu_1", "type": "function",
             "function": {"name": "board_list", "arguments": json.dumps({"box": "for_me"})}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "2 messages wait on you."},
    ]
    assert [t["function"]["name"] for t in out["tools"]] == ["board_list", "memory_search"]
    assert out["tools"][0]["function"]["parameters"]["properties"] == {"box": {"type": "string"}}
    assert out["tool_choice"] == "auto"


def test_tool_choice_and_stop_sequences_carry_over():
    body = dict(TURN, tool_choice={"type": "tool", "name": "board_list"}, stop_sequences=["END"])
    out = translate.to_chat_completions(body, GLM)
    assert out["tool_choice"] == {"type": "function", "function": {"name": "board_list"}}
    assert out["stop"] == ["END"]
    assert translate.to_chat_completions(dict(TURN, tool_choice={"type": "any"}), GLM)[
        "tool_choice"] == "required"


# A GLM-5 stream as Bedrock sends it: reasoning, text, one tool call in two
# pieces, then the usage chunk include_usage adds.
GLM_STREAM = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Let me think."}}]},
    {"choices": [{"index": 0, "delta": {"content": "Two messages "}}]},
    {"choices": [{"index": 0, "delta": {"content": "wait on you."}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_9", "type": "function",
         "function": {"name": "memory_search", "arguments": "{\"q\":"}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "\"credit wall\"}"}}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 1500, "completion_tokens": 40}},
]


def _events(raw: bytes) -> list[dict]:
    return [json.loads(line[5:]) for line in raw.split(b"\n") if line.startswith(b"data:")]


def test_a_glm_stream_becomes_an_anthropic_stream_the_meter_reads():
    stream = translate.StreamTranslator(GLM)
    raw = b"".join(stream.feed(chunk) for chunk in GLM_STREAM) + stream.finish()
    events = _events(raw)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "message_start" and kinds[-2:] == ["message_delta", "message_stop"]
    assert events[0]["message"]["model"] == GLM
    text = "".join(e["delta"]["text"] for e in events
                   if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
    assert text == "Two messages wait on you."          # no reasoning in the answer
    tool = next(e for e in events if e["type"] == "content_block_start"
                and e["content_block"]["type"] == "tool_use")
    assert tool["content_block"]["name"] == "memory_search" and tool["index"] == 1
    arguments = "".join(e["delta"]["partial_json"] for e in events
                        if e["type"] == "content_block_delta"
                        and e["delta"]["type"] == "input_json_delta")
    assert json.loads(arguments) == {"q": "credit wall"}
    assert kinds.count("content_block_stop") == 2
    delta = events[-2]
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"] == {"input_tokens": 1500, "output_tokens": 40}
    usage = _StreamUsage()
    usage.feed(raw)
    assert usage.final and usage.usage == {"input_tokens": 1500, "output_tokens": 40}


def test_an_answer_that_was_not_streamed_becomes_a_message():
    answer = {"choices": [{"message": {"content": "Hi.", "tool_calls": [
        {"id": "c1", "function": {"name": "board_list", "arguments": "{\"box\":\"all\"}"}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}
    message = translate.to_message(answer, GLM)
    assert message["content"] == [{"type": "text", "text": "Hi."},
                                  {"type": "tool_use", "id": "c1", "name": "board_list",
                                   "input": {"box": "all"}}]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 9, "output_tokens": 3}


class Fake:
    """An upstream that answers with a fixed status and body."""

    def __init__(self, status=200, body=b"{}", model="", raises=False):
        self.status, self.body, self.model, self.raises = status, body, model, raises
        self.calls = 0

    async def count_tokens(self, body):
        return 10

    async def messages(self, body, headers):
        self.calls += 1
        if self.raises:
            raise aiohttp.ClientConnectionError("no route")
        return UpstreamResponse(status=self.status, content_type="application/json",
                                chunks=_chunks([self.body]), model=self.model)


def _route(anthropic, bedrock, clock, **kw):
    return FallbackUpstream(anthropic, bedrock, monotonic=lambda: clock[0], **kw)


def test_a_usage_limit_sends_the_turn_to_bedrock_and_skips_anthropic_for_ten_minutes():
    anthropic, bedrock, clock = Fake(400, LIMIT), Fake(200, model=GLM), [0.0]
    route = _route(anthropic, bedrock, clock)

    async def go():
        first = await route.messages(TURN, {})
        clock[0] = DOWN_SECONDS - 1
        await route.messages(TURN, {})
        clock[0] = DOWN_SECONDS + 1
        await route.messages(TURN, {})
        return first.model

    assert _run(go()) == GLM
    assert (anthropic.calls, bedrock.calls) == (2, 3)   # skipped once, tried again after


def test_an_outage_or_no_connection_also_falls_back():
    for anthropic in (Fake(529, b"{}"), Fake(401, b"{}"), Fake(raises=True)):
        bedrock = Fake(200, model=GLM)
        answer = _run(_route(anthropic, bedrock, [0.0]).messages(TURN, {}))
        assert answer.model == GLM and bedrock.calls == 1


def test_a_bad_request_is_passed_back_not_retried():
    bad = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                 "message": "messages: field required"}}).encode()
    anthropic, bedrock = Fake(400, bad), Fake(200, model=GLM)

    async def go():
        answer = await _route(anthropic, bedrock, [0.0]).messages(TURN, {})
        return answer.status, b"".join([c async for c in answer.chunks])

    assert _run(go()) == (400, bad)
    assert bedrock.calls == 0


def test_a_turn_anthropic_started_is_never_retried():
    # A 200 has started streaming to the tenant: whatever happens after, the
    # same turn is not sent again to a second model.
    anthropic, bedrock = Fake(200, b"{}"), Fake(200, model=GLM)
    _run(_route(anthropic, bedrock, [0.0]).messages(TURN, {}))
    assert (anthropic.calls, bedrock.calls) == (1, 0)


def test_the_operator_switch_sends_every_turn_to_bedrock():
    anthropic, bedrock = Fake(200), Fake(200, model=GLM)
    route = _route(anthropic, bedrock, [0.0], primary="bedrock")
    _run(route.messages(TURN, {}))
    assert (anthropic.calls, bedrock.calls) == (0, 1)


def test_a_bedrock_turn_is_charged_at_the_price_of_the_model_that_ran_it(tmp_path):
    usage = {"input_tokens": 1000, "output_tokens": 200}
    answer = json.dumps({"type": "message", "content": [], "usage": usage}).encode()
    route = FallbackUpstream(Fake(400, LIMIT), Fake(200, answer, model=GLM))

    async def go():
        async with Rig(tmp_path, route) as rig:
            status, _ = await rig.call()
            return status, rig.ledger.spend(TENANT, MONTH)

    status, (settled, reserved) = _run(go())
    assert status == 200 and reserved == 0
    assert settled == prices.cost(GLM, usage)
    assert settled < prices.cost(SONNET, usage)


def test_bedrock_is_called_with_its_bearer_key_and_answers_in_anthropic_events():
    seen: list[tuple[str, dict, dict]] = []

    async def go():
        async def handler(request):
            seen.append((request.path, dict(request.headers), await request.json()))
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for chunk in GLM_STREAM:
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            return response

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                bedrock = BedrockUpstream(session, f"http://127.0.0.1:{port}/v1", "bedrock-key", GLM)
                answer = await bedrock.messages(TURN, {})
                raw = b"".join([chunk async for chunk in answer.chunks])
                return answer, raw
        finally:
            await runner.cleanup()

    answer, raw = _run(go())
    path, headers, sent = seen[0]
    headers = {k.lower(): v for k, v in headers.items()}
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == "Bearer bedrock-key" and "x-api-key" not in headers
    assert sent["model"] == GLM and sent["messages"][0]["role"] == "system"
    assert answer.model == GLM and answer.content_type == "text/event-stream"
    events = _events(raw)
    assert events[-1]["type"] == "message_stop"
    assert events[-2]["usage"] == {"input_tokens": 1500, "output_tokens": 40}


def test_bedrock_has_no_count_so_the_proxy_estimates():
    async def go():
        async with aiohttp.ClientSession() as session:
            try:
                await BedrockUpstream(session, "http://127.0.0.1:9/v1", "k", GLM).count_tokens(TURN)
            except RuntimeError:
                return True
        return False

    assert asyncio.run(go())
