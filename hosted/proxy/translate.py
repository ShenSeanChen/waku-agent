"""Anthropic Messages in, OpenAI chat completions out, and back (spec 043).

Tenant containers speak only the Anthropic Messages format to the proxy. GLM-5
on Amazon Bedrock speaks only OpenAI chat completions (spec 043, F2). This
module turns one into the other so a turn can run on Bedrock without any
change in the containers:

- `to_chat_completions` turns a validated Messages body into a chat-completions
  body: the system prompt, the turns, the tool definitions, the tool calls and
  the tool results.
- `StreamTranslator` turns the chat-completions stream into the Anthropic event
  stream a container reads (message_start, content blocks, message_delta with
  usage, message_stop), so the proxy's own meter (_StreamUsage) reads it as it
  reads Anthropic's.
- `to_message` does the same for an answer that was not streamed.

Reasoning text that a model returns beside its answer (`reasoning_content`,
`reasoning`) is dropped, as spec 036 drops it in the local harness.
"""

from __future__ import annotations

import json
import uuid

STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "content_filter": "end_turn"}


def _text_of(content) -> str:
    """The text of a content field: a string, or a list of blocks with text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_choice(choice):
    kind = choice.get("type") if isinstance(choice, dict) else None
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return "auto"


def to_chat_completions(body: dict, model: str) -> dict:
    """The chat-completions body for a validated Anthropic Messages body."""
    messages: list[dict] = []
    system = _text_of(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for turn in body.get("messages", []):
        role, content = turn.get("role"), turn.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = [b for b in content or [] if isinstance(b, dict)]
        if role == "assistant":
            calls = [{"id": b.get("id", ""), "type": "function",
                      "function": {"name": b.get("name", ""),
                                   "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            message: dict = {"role": "assistant", "content": _text_of(blocks) or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            continue
        # A user turn: tool results become tool messages, in order, and any text
        # after them is one user message.
        for block in blocks:
            if block.get("type") == "tool_result":
                result = block.get("content")
                text = result if isinstance(result, str) else _text_of(result)
                if block.get("is_error"):
                    text = f"Error: {text}"
                messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                                 "content": text})
        text = _text_of(blocks)
        if text:
            messages.append({"role": "user", "content": text})
    out: dict = {"model": model, "messages": messages, "max_tokens": body["max_tokens"]}
    tools = body.get("tools") or []
    if tools:
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name", ""), "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools]
        out["tool_choice"] = _tool_choice(body.get("tool_choice"))
    for name in ("temperature", "top_p"):
        if name in body:
            out[name] = body[name]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def _usage(usage: dict | None) -> dict:
    usage = usage or {}
    return {"input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0)}


def _event(kind: str, payload: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n".encode()


class StreamTranslator:
    """Turns chat-completions stream chunks into Anthropic stream events.

    Feed it each parsed `data:` object with `feed`, and call `finish` once the
    upstream stream ends. Both return the bytes to send to the container.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._started = False
        self._index = -1             # the Anthropic block now open, -1 for none
        self._open_kind = ""         # "text" or "tool"
        self._tools: dict[int, int] = {}   # upstream tool index -> Anthropic block
        self._usage: dict = {}
        self._finish = ""

    def _start(self) -> bytes:
        if self._started:
            return b""
        self._started = True
        return _event("message_start", {"message": {
            "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self) -> bytes:
        if self._index < 0 or not self._open_kind:
            return b""
        self._open_kind = ""
        return _event("content_block_stop", {"index": self._index})

    def _open(self, block: dict, kind: str) -> bytes:
        out = self._close()
        self._index += 1
        self._open_kind = kind
        return out + _event("content_block_start", {"index": self._index, "content_block": block})

    def feed(self, chunk: dict) -> bytes:
        out = self._start()
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if self._open_kind != "text":
                    out += self._open({"type": "text", "text": ""}, "text")
                out += _event("content_block_delta", {"index": self._index,
                                                      "delta": {"type": "text_delta", "text": text}})
            for call in delta.get("tool_calls") or []:
                slot = call.get("index", 0)
                function = call.get("function") or {}
                if slot not in self._tools:
                    out += self._open({"type": "tool_use",
                                       "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                       "name": function.get("name", ""), "input": {}}, "tool")
                    self._tools[slot] = self._index
                arguments = function.get("arguments")
                if arguments:
                    out += _event("content_block_delta", {
                        "index": self._tools[slot],
                        "delta": {"type": "input_json_delta", "partial_json": arguments}})
            if choice.get("finish_reason"):
                self._finish = choice["finish_reason"]
        return out

    def finish(self) -> bytes:
        out = self._start() + self._close()
        out += _event("message_delta", {
            "delta": {"stop_reason": STOP_REASONS.get(self._finish, "end_turn"),
                      "stop_sequence": None},
            "usage": _usage(self._usage)})
        return out + _event("message_stop", {})


def to_message(answer: dict, model: str) -> dict:
    """The Anthropic message for a chat-completions answer that was not streamed."""
    choice = (answer.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        content.append({"type": "tool_use", "id": call.get("id", ""),
                        "name": function.get("name", ""), "input": arguments})
    return {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": model, "content": content,
            "stop_reason": STOP_REASONS.get(choice.get("finish_reason") or "", "end_turn"),
            "stop_sequence": None, "usage": _usage(answer.get("usage"))}
