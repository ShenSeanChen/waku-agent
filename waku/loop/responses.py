"""Responses API adapter for the loop's existing Messages interface."""

from __future__ import annotations

import json
from types import SimpleNamespace

import openai


class OpenAIResponsesClient:
    """Translate messages and tools without changing the agent loop."""

    def __init__(self, *, api_key: str, base_url: str | None = None, timeout: float = 120):
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _request(self, *, model, messages, max_tokens, system=None, tools=None,
                 tool_choice=None) -> dict:
        """Translate request inputs."""
        items = []
        for message in messages:
            role, content = message["role"], message["content"]
            if isinstance(content, str):
                items.append({"role": role, "content": content})
                continue
            if role == "assistant":
                # Replay reasoning items.
                saved = next((b.responses_output for b in content
                              if hasattr(b, "responses_output")), None)
                if saved is not None:
                    items.extend(saved)
                    continue
            for block in content:
                if role == "assistant" and block.type == "text":
                    items.append({"role": role, "content": block.text})
                elif role == "assistant" and block.type == "tool_use":
                    items.append({"type": "function_call", "call_id": block.id,
                                  "name": block.name, "arguments": json.dumps(block.input)})
                elif role == "user" and block["type"] == "tool_result":
                    items.append({"type": "function_call_output",
                                  "call_id": block["tool_use_id"], "output": block["content"]})
                elif role == "user" and block["type"] == "text":
                    items.append({"role": role, "content": block["text"]})
                else:
                    raise ValueError(f"Unsupported Responses input block: {block!r}")

        kwargs = {"model": model, "input": items, "max_output_tokens": max_tokens,
                  "store": False, "include": ["reasoning.encrypted_content"]}
        if system is not None:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = [
                {"type": "function", "name": t["name"], "description": t["description"],
                 "parameters": t["input_schema"], "strict": False} for t in tools
            ]
        if tool_choice is not None:
            if tool_choice["type"] != "none":
                raise ValueError("Responses adapter only accepts tool_choice type none")
            kwargs["tool_choice"] = "none"
        return kwargs

    def _create(self, **kwargs):
        # Collect one streamed request.
        with self._stream(**kwargs) as stream:
            for _ in stream.text_stream:
                pass
            return stream.get_final_message()

    def _stream(self, **kwargs):
        return _ResponsesStream(self._client, self._request(**kwargs))


def _message(response):
    """Normalize completed output."""
    if response.status != "completed":
        detail = response.error or response.incomplete_details or response.status
        raise RuntimeError(f"Responses request {response.status}: {detail}")
    blocks = []
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    blocks.append(SimpleNamespace(type="text", text=part.text))
                elif part.type == "refusal":
                    blocks.append(SimpleNamespace(type="text", text=part.refusal))
                else:
                    raise ValueError(f"Unsupported Responses content: {part.type}")
        elif item.type == "function_call":
            arguments = json.loads(item.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Responses function arguments must be a JSON object")
            blocks.append(SimpleNamespace(type="tool_use", id=item.call_id,
                                          name=item.name, input=arguments))
        elif item.type != "reasoning":
            raise ValueError(f"Unsupported Responses output: {item.type}")
    if not blocks:
        raise RuntimeError("Responses request returned no text or function calls")
    # Preserve ordered output.
    blocks[0].responses_output = [item.model_dump(exclude_none=True) for item in response.output]
    return SimpleNamespace(
        content=blocks,
        stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn",
        usage=SimpleNamespace(input_tokens=response.usage.input_tokens,
                              output_tokens=response.usage.output_tokens),
    )


class _ResponsesStream:
    """Expose text deltas and the completed message to the loop."""

    def __init__(self, client, kwargs):
        self._client, self._kwargs = client, kwargs
        self._response = None
        self._items = {}

    def __enter__(self):
        self._events = self._client.responses.create(**self._kwargs, stream=True)
        return self

    def __exit__(self, *exc):
        self._events.close()
        return False

    @property
    def text_stream(self):
        for event in self._events:
            if event.type in ("response.output_text.delta", "response.refusal.delta"):
                yield event.delta
            elif event.type == "response.output_item.done":
                # Keep completed output items.
                self._items[event.output_index] = event.item
            elif event.type == "response.completed":
                self._response = event.response
                self._items.update(enumerate(event.response.output))
            elif event.type in ("response.failed", "response.incomplete"):
                _message(event.response)
            elif event.type == "error":
                raise RuntimeError(f"Responses stream error: {event.code}: {event.message}")
        if self._response is None:
            raise RuntimeError("Responses stream ended before response.completed")

    def get_final_message(self):
        if self._response is None:
            raise RuntimeError("Responses stream has no completed response")
        if sorted(self._items) != list(range(len(self._items))):
            raise RuntimeError("Responses stream is missing completed output items")
        # Assemble ordered stream output.
        self._response.output = [self._items[i] for i in range(len(self._items))]
        return _message(self._response)
