"""What the metering proxy accepts: spec 001's step 3 and step 4, and nothing else.

Pure: bytes in, a validated dict or a Refused out, no I/O. The proxy counts,
reserves against and forwards the object this returns, re-serialised, never
the raw bytes, so anything this did not look at cannot reach Anthropic.

Allowlists all the way down, like the spawner's: body fields, content blocks,
headers. Spec 004 C widened one of them on purpose. Sonnet 5.5 thinks by
default and Waku appends `response.content` to its history
(`waku/loop/agent.py`), so an assistant turn carries `thinking` and
`redacted_thinking` blocks back on the next call. Anthropic produced and
signed them; they carry no tool and no image. Refusing them would refuse the
second call of every multi-step turn.
"""

from __future__ import annotations

import json

BODY_FIELDS = frozenset({
    "model", "messages", "system", "tools", "tool_choice", "max_tokens", "stream",
    "temperature", "top_p", "top_k", "stop_sequences", "metadata",
})
USER_BLOCKS = frozenset({"text", "tool_result"})
ASSISTANT_BLOCKS = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})
FORWARDED_HEADERS = ("anthropic-version", "content-type")
MODEL_REFUSED = "This model is not in the free tier. Add your own key in Models."


class Refused(Exception):
    """An answer in Anthropic's error shape, so the tenant's SDK reads it."""

    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message

    def body(self) -> dict:
        return {"type": "error", "error": {"type": self.error_type, "message": self.message}}


def _bad(message: str) -> Refused:
    return Refused(400, "invalid_request_error", message)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _bad("the body repeats a key; send each key once (duplicate key)")
    return dict(pairs)


def _walk_for_refused_keys(value: object) -> None:
    if isinstance(value, dict):
        if "cache_control" in value:
            raise _bad("cache_control is not available on the free tier")
        for item in value.values():
            _walk_for_refused_keys(item)
    elif isinstance(value, list):
        for item in value:
            _walk_for_refused_keys(item)


def _check_blocks(role: str, content: object) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise _bad(f"{role} content must be a string or a list of blocks")
    allowed = USER_BLOCKS if role == "user" else ASSISTANT_BLOCKS
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in allowed:
            raise _bad(f"a {kind!r} block is not accepted in a {role} turn on the free tier")
        if kind == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list) and any(
                    not isinstance(b, dict) or b.get("type") != "text" for b in inner):
                raise _bad("tool_result content may only be text on the free tier")


def admit(raw: bytes, *, models: tuple[str, ...], ceiling: int) -> dict:
    """The body to count, reserve against and forward, or a Refused."""
    try:
        body = json.loads(raw, object_pairs_hook=_no_duplicates)
    except Refused:
        raise
    except ValueError as exc:
        raise _bad(f"the body is not valid JSON: {exc}") from None
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")

    unknown = sorted(set(body) - BODY_FIELDS)
    if unknown:
        raise _bad(f"the free tier does not accept {unknown}")
    _walk_for_refused_keys(body)

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise _bad("max_tokens must be a whole number of at least 1")
    body["max_tokens"] = min(max_tokens, ceiling)

    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or "type" in tool:
            raise _bad("a server tool is not available on the free tier; send custom tools only")

    system = body.get("system")
    if isinstance(system, list):
        if any(not isinstance(b, dict) or b.get("type") != "text" for b in system):
            raise _bad("system may only hold text blocks on the free tier")
    elif system is not None and not isinstance(system, str):
        raise _bad("system must be a string or a list of text blocks")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad("messages must be a non-empty list")
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        if role not in ("user", "assistant"):
            raise _bad(f"a message role must be user or assistant, not {role!r}")
        _check_blocks(role, message.get("content"))

    if body.get("model") not in models:
        raise Refused(403, "permission_error", MODEL_REFUSED)
    return body


def forward_headers(headers) -> dict[str, str]:
    """Only anthropic-version and content-type go upstream; the tenant's token,
    any beta header and anything else stay here."""
    lowered = {key.lower(): value for key, value in headers.items()}
    return {name: lowered[name] for name in FORWARDED_HEADERS if name in lowered}
