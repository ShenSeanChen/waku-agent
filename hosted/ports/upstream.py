"""The model API behind the meter. Anthropic now."""

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


class Upstream(Protocol):
    async def count_tokens(self, body: dict) -> int:
        """Input tokens for the request's model, messages, system, tools and
        tool_choice -- the only fields POST /v1/messages/count_tokens takes.
        Raises on any failure; the proxy answers 529 and reserves nothing."""
        ...

    async def messages(self, body: dict, headers: dict) -> UpstreamResponse:
        """Forward a validated body with the platform key."""
        ...
