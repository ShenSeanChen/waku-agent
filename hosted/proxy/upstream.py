"""The model APIs behind the meter: the one place the platform keys are used.

The keys live in config/proxy.env and in this process's memory, and nowhere
else on the VM (spec 001, acceptance 2). Anthropic's goes on every Anthropic
request as `x-api-key`; Bedrock's goes on every Bedrock request as a bearer
token (spec 043). The tenant's own token never goes upstream.

`FallbackUpstream` sends a turn to Anthropic first and to Bedrock when
Anthropic cannot answer: a usage limit, a bad key, an outage or no connection.
It switches only before anything has streamed, and it skips Anthropic for ten
minutes after a failure, so the turns after it do not wait on the same refusal.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable

import aiohttp

from hosted import log
from hosted.ports.upstream import Upstream, UpstreamResponse
from hosted.proxy import translate

_LOG = log.get(__name__)

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
        # The session does not decompress (__main__: auto_decompress=False), so
        # the stream reaches the tenant byte for byte and _StreamUsage can read
        # it. aiohttp still offers gzip by default, and Anthropic takes it:
        # count_tokens then failed to decode byte 0x8b on every call (measured
        # on agent.waku.one 2026-10-02) and a gzipped stream would pass through
        # with no Content-Encoding the tenant could undo. Ask for plain bytes.
        out["accept-encoding"] = "identity"
        return out

    async def count_tokens(self, body: dict) -> int:
        payload = {k: body[k] for k in COUNT_FIELDS if k in body}
        async with self._session.post(
                f"{self._base}/v1/messages/count_tokens", data=json.dumps(payload),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=COUNT_TIMEOUT_SECONDS)) as response:
            if response.status != 200:
                # The error body says why (a bad field, an unknown model, a
                # billing problem); it holds no key, so the log keeps it.
                detail = (await response.text())[:300]
                raise RuntimeError(f"count_tokens answered {response.status}: {detail}")
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


async def _from_bytes(data: bytes) -> AsyncIterator[bytes]:
    yield data


class BedrockUpstream:
    """GLM-5 (or another chat-completions model) on Amazon Bedrock, behind the
    Anthropic Messages format the containers speak (spec 043, F1 and F2)."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, key: str,
                 model: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._key = key
        self.model = model

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._key}", "content-type": "application/json",
                "accept-encoding": "identity"}   # the gzip note in AnthropicUpstream

    async def count_tokens(self, body: dict) -> int:
        # The key may not call bedrock-mantle:CountTokens (spec 043, F4), and
        # chat completions has no count. The proxy reserves an estimate.
        raise RuntimeError("Bedrock has no count_tokens for this model")

    async def messages(self, body: dict, headers: dict) -> UpstreamResponse:
        payload = translate.to_chat_completions(body, self.model)
        response = await self._session.post(
            f"{self._base}/chat/completions", data=json.dumps(payload),
            headers=self._headers(), timeout=MESSAGES_TIMEOUT)
        if response.status != 200:
            detail = (await response.text())[:300]
            response.release()
            error = {"type": "error", "error": {"type": "api_error",
                     "message": f"Bedrock answered {response.status}: {detail}"}}
            return UpstreamResponse(status=response.status, content_type="application/json",
                                    chunks=_from_bytes(json.dumps(error).encode()),
                                    model=self.model)
        if not payload.get("stream"):
            answer = await response.json(content_type=None)
            response.release()
            message = translate.to_message(answer, self.model)
            return UpstreamResponse(status=200, content_type="application/json",
                                    chunks=_from_bytes(json.dumps(message).encode()),
                                    model=self.model)
        return UpstreamResponse(status=200, content_type="text/event-stream",
                                chunks=self._stream(response), release=response.release,
                                model=self.model)

    async def _stream(self, response: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
        translator = translate.StreamTranslator(self.model)
        pending = b""
        async for chunk in response.content.iter_any():
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    out = translator.feed(parsed)
                    if out:
                        yield out
        yield translator.finish()


#: How long Anthropic is skipped after it fails.
DOWN_SECONDS = 600
#: Anthropic statuses that send the turn to Bedrock. A 400 does too, but only
#: when it says the usage limit or the credit balance ran out; any other 400 is
#: a bad request that Bedrock would refuse as well.
FALLBACK_STATUSES = frozenset({401, 403, 429, 500, 502, 503, 504, 529})
LIMIT_WORDS = ("usage limit", "credit balance")


class FallbackUpstream:
    """Anthropic first, Bedrock when Anthropic cannot answer (spec 043).

    `primary="bedrock"` is the operator's switch (WAKU_UPSTREAM_PRIMARY): every
    turn goes to Bedrock, with no call to Anthropic at all.
    """

    def __init__(self, anthropic: Upstream, bedrock: Upstream, *, primary: str = "anthropic",
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._anthropic = anthropic
        self._bedrock = bedrock
        self._primary = primary
        self._monotonic = monotonic
        self._down_until = 0.0

    def _anthropic_first(self) -> bool:
        return self._primary == "anthropic" and self._monotonic() >= self._down_until

    def _mark_down(self, why: str) -> None:
        self._down_until = self._monotonic() + DOWN_SECONDS
        _LOG.warning("Anthropic failed (%s); turns go to Bedrock for %d s", why, DOWN_SECONDS)

    async def count_tokens(self, body: dict) -> int:
        if self._anthropic_first():
            return await self._anthropic.count_tokens(body)
        return await self._bedrock.count_tokens(body)

    async def messages(self, body: dict, headers: dict) -> UpstreamResponse:
        if not self._anthropic_first():
            return await self._bedrock.messages(body, headers)
        try:
            answer = await self._anthropic.messages(body, headers)
        except Exception as exc:
            self._mark_down(f"unreachable: {exc}")
            return await self._bedrock.messages(body, headers)
        if answer.status < 300:
            return answer
        # A refusal is small and nothing has reached the tenant yet, so read it
        # whole and decide.
        held = b"".join([chunk async for chunk in answer.chunks])
        if callable(answer.release):
            answer.release()
        text = held.decode("utf-8", "replace").lower()
        if answer.status in FALLBACK_STATUSES or (
                answer.status == 400 and any(word in text for word in LIMIT_WORDS)):
            self._mark_down(f"{answer.status}: {text[:200]}")
            return await self._bedrock.messages(body, headers)
        return UpstreamResponse(status=answer.status, content_type=answer.content_type,
                                chunks=_from_bytes(held), model=answer.model)
