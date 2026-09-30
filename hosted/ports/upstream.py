"""The model API behind the meter. Anthropic now."""

from __future__ import annotations

from typing import Protocol


class Upstream(Protocol):
    async def count_tokens(self, body: dict) -> int:
        """Input tokens for the request's model, messages, system, tools and
        tool_choice -- the only fields POST /v1/messages/count_tokens takes."""
        ...

    async def messages(self, body: dict, headers: dict) -> object:
        """Forward a validated body. Streams pass through unbuffered."""
        ...
