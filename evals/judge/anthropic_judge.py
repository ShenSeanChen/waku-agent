"""A DeepEval judge model backed by the same Anthropic-compatible endpoint
Milli itself uses — no separate OpenAI key needed for judging.

DeepEval calls generate() with an optional pydantic schema when it wants
structured verdicts; we ask the model for JSON and validate it back.
"""

from __future__ import annotations

import json

from deepeval.models import DeepEvalBaseLLM

from milli.config import load_settings
from milli.loop.models import get_client


class AnthropicJudge(DeepEvalBaseLLM):
    def __init__(self, model: str | None = None):
        self.settings = load_settings()
        self.client = get_client(self.settings)  # fills provider-default model ids
        self.model = model or self.settings.small_model

    def load_model(self):
        return self.client

    def generate(self, prompt: str, schema=None):
        if schema is not None:
            prompt += (
                "\n\nReply with ONLY a JSON object matching this schema, no prose:\n"
                + json.dumps(schema.model_json_schema())
            )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        if schema is not None:
            return schema.model_validate_json(text[text.index("{") : text.rindex("}") + 1])
        return text

    async def a_generate(self, prompt: str, schema=None):
        return self.generate(prompt, schema)

    def get_model_name(self):
        return f"AnthropicJudge({self.model})"
