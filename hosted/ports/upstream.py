"""The model API behind the meter: Anthropic, and Bedrock when it cannot (spec 043)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class UpstreamResponse:
    """What came back, before the proxy has read the body: the status and the
    content type to answer with, and the body as it arrives, so a stream passes
    through unbuffered."""
    status: int
    content_type: str
    chunks: AsyncIterator[bytes]
    release: object = field(default=None)   # called once the body is done with
    # The model that answered, when it is not the one the tenant asked for: a
    # turn GLM-5 ran on Bedrock is priced and charged as GLM-5 (spec 043).
    model: str = ""


class Upstream(Protocol):
    async def count_tokens(self, body: dict) -> int:
        """Input tokens for the request's model, messages, system, tools and
        tool_choice -- the only fields POST /v1/messages/count_tokens takes.
        Raises on any failure; the proxy then reserves an estimate (#445)."""
        ...

    async def messages(self, body: dict, headers: dict) -> UpstreamResponse:
        """Forward a validated body with the platform key."""
        ...
