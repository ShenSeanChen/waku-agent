"""Anthropic, behind the meter: the one place the platform key is used.

The key lives in config/proxy.env and in this process's memory, and nowhere
else on the VM (spec 001, acceptance 2). It goes on every upstream request as
`x-api-key`; the tenant's own token never does.
"""

from __future__ import annotations

import json

import aiohttp

from hosted.ports.upstream import UpstreamResponse

COUNT_FIELDS = ("model", "messages", "system", "tools", "tool_choice")
API_VERSION = "2023-06-01"
COUNT_TIMEOUT_SECONDS = 15
# A turn can stream for minutes; only the connect is bounded tightly.
MESSAGES_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=300)


class AnthropicUpstream:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, key: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._key = key

    def _headers(self, headers: dict | None = None) -> dict[str, str]:
        out = {"anthropic-version": API_VERSION, "content-type": "application/json"}
        out.update(headers or {})
        out["x-api-key"] = self._key      # last, so nothing forwarded can replace it
        return out

    async def count_tokens(self, body: dict) -> int:
        payload = {k: body[k] for k in COUNT_FIELDS if k in body}
        async with self._session.post(
                f"{self._base}/v1/messages/count_tokens", data=json.dumps(payload),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=COUNT_TIMEOUT_SECONDS)) as response:
            if response.status != 200:
                raise RuntimeError(f"count_tokens answered {response.status}")
            answer = await response.json()
        tokens = answer.get("input_tokens")
        if not isinstance(tokens, int) or tokens < 0:
            raise RuntimeError("count_tokens answered no input_tokens")
        return tokens

    async def messages(self, body: dict, headers: dict) -> UpstreamResponse:
        response = await self._session.post(
            f"{self._base}/v1/messages", data=json.dumps(body),
            headers=self._headers(headers), timeout=MESSAGES_TIMEOUT)
        return UpstreamResponse(
            status=response.status,
            content_type=response.headers.get("Content-Type", "application/json"),
            chunks=response.content.iter_any(),
            release=response.release)
