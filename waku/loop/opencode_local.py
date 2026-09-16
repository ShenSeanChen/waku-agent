"""OpenCode's local HTTP server as a Waku model transport (stdlib only).

Waku owns conversation history and tool execution. Each attempt creates a
temporary session, disables the server's discovered tools, submits once, and
polls for a completed assistant message. Network failures never replay a prompt.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace


class OpenCodeError(RuntimeError):
    """The local server or its selected model could not complete the call."""


class OpenCodeTimeout(OpenCodeError):
    """The response deadline expired; the owned session was stopped."""


class OpenCodeQuotaExceeded(OpenCodeError):
    """The provider reported a free-tier limit; no fallback is attempted."""

    def __init__(self, next_retry=None):
        self.next_retry = next_retry
        when = next_retry
        if isinstance(when, (int, float)) and not isinstance(when, bool):
            try:
                when = datetime.fromtimestamp(when / 1000, UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        detail = f" Reported next retry: {when}." if when is not None else ""
        super().__init__("OpenCode free-tier limit reached (free_tier_limit)." + detail)


class InvalidProtocolError(OpenCodeError):
    """A completed response did not contain a valid Waku action."""


def _http_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OpenCode HTTP deadline exceeded")
    return remaining


class _DeadlineReader(io.RawIOBase):
    """Bound every socket receive, including HTTP headers and error bodies.

    A socket timeout alone resets on each receive, so a trickling response can
    keep HTTPResponse.read() blocked indefinitely. Keep one absolute deadline.
    """

    def __init__(self, sock, raw, deadline):
        self.sock, self.raw, self.deadline = sock, raw, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_http_remaining(self.deadline))
        count = self.raw.readinto(buffer)
        _http_remaining(self.deadline)
        return count

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        # HTTPResponse has created its file but has not read the headers yet.
        self.fp = io.BufferedReader(_DeadlineReader(sock, self.fp.detach(), deadline))


class _DeadlineHandler(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def _open(self, request, connection_type, **options):
        def connect(host, **kwargs):
            connection = connection_type(host, **kwargs)
            connection.response_class = partial(_DeadlineResponse, deadline=self.deadline)
            send = connection.send

            def bounded_send(data):
                connection.timeout = _http_remaining(self.deadline)
                if connection.sock is None:
                    connection.connect()
                connection.sock.settimeout(_http_remaining(self.deadline))
                send(data)
                _http_remaining(self.deadline)

            connection.send = bounded_send
            return connection

        return self.do_open(connect, request, **options)

    def http_open(self, request):
        return self._open(request, http.client.HTTPConnection)

    def https_open(self, request):
        return self._open(request, http.client.HTTPSConnection, context=self._context)


def _number(name, value, *, zero=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if isinstance(value, bool) or not math.isfinite(number) or number < 0 or (
        number == 0 and not zero
    ):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return number


def _check_quota(value, next_retry=None):
    if isinstance(value, dict):
        next_retry = value.get("next", next_retry)
        for child in value.values():
            _check_quota(child, next_retry)
    elif isinstance(value, list):
        for child in value:
            _check_quota(child, next_retry)
    elif isinstance(value, str) and "free_tier_limit" in value.lower():
        raise OpenCodeQuotaExceeded(next_retry)


def _model_selection(model):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a nonempty model ID")
    provider, model_id = model.split("/", 1) if "/" in model else ("opencode", model)
    if not provider.strip() or not model_id.strip():
        raise ValueError("model must have nonempty provider and model IDs")
    return provider, model_id


def _field(block, name, default=""):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def _transcript(messages):
    """Render both SDK content blocks and the loop's tool-result dictionaries."""
    rendered = []
    for message in messages:
        role, content = message["role"].upper(), message["content"]
        if isinstance(content, str):
            rendered.append(f"{role}:\n{content}")
            continue
        for block in content:
            kind = _field(block, "type")
            if kind == "text":
                rendered.append(f"{role}:\n{_field(block, 'text')}")
            elif kind in ("tool_use", "tool_result"):
                keys = ("id", "name", "input") if kind == "tool_use" else (
                    "tool_use_id", "content", "is_error"
                )
                label = "ASSISTANT TOOL CALL" if kind == "tool_use" else "TOOL RESULT"
                data = {key: _field(block, key, None) for key in keys}
                rendered.append(f"{label}:\n{json.dumps(data, ensure_ascii=False)}")
            elif kind not in ("thinking", "redacted_thinking", "reasoning"):
                raise ValueError(f"Unsupported OpenCode message block: {kind!r}")
    return "\n\n".join(rendered)


def _protocol(system, tools):
    return (system + "\n\n" + """Waku owns and executes all tools. OpenCode tools are disabled.
Return exactly one JSON object, without markdown fences or other text:
{"type":"tool_use","name":"<exact Waku tool name>","input":{...}}
or {"type":"final","text":"<answer>"}.
Request only one tool at a time. Its input must satisfy the supplied schema.
Use existing TOOL RESULT messages when deciding the next action.
Available Waku tools:
""" + json.dumps(tools, ensure_ascii=False)).strip()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON number is out of range")
    return result


def _action(text, allowed):
    try:
        action = json.loads(text, object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant, parse_float=_json_float)
    except ValueError as exc:
        raise InvalidProtocolError("Expected one complete JSON action") from exc
    if not isinstance(action, dict):
        raise InvalidProtocolError("Expected a JSON object")
    kind = action.get("type")
    if kind == "tool_use":
        name, args = action.get("name"), action.get("input")
        if set(action) != {"type", "name", "input"}:
            raise InvalidProtocolError("Tool action requires exactly type, name and input")
        if not isinstance(name, str) or name not in allowed:
            raise InvalidProtocolError("Response requested an unknown Waku tool")
        if not isinstance(args, dict):
            raise InvalidProtocolError("Tool input must be a JSON object")
        return SimpleNamespace(type="tool_use", id="oc_" + uuid.uuid4().hex, name=name, input=args)
    if kind == "final" and set(action) == {"type", "text"}:
        answer = action["text"]
        if isinstance(answer, str) and answer.strip():
            return SimpleNamespace(type="text", text=answer)
    raise InvalidProtocolError("Expected a tool action or a final action with nonempty text")


class OpenCodeLocalClient:
    """Anthropic-shaped ``messages.create`` backed by a running OpenCode server.

    Unqualified model IDs use providerID ``opencode``; ``provider/model`` selects
    an explicit provider (split at the first slash). There is no model fallback.
    ``timeout`` bounds the response wait and protocol repairs; 0 means no response
    deadline. Each HTTP operation has a finite ``http_timeout``. Best-effort
    cleanup is separate and can take up to two additional HTTP timeouts.
    Only completed invalid tool-protocol replies may be regenerated, up to
    ``protocol_retries`` times, in new sessions. No tool is executed here.
    Usage includes those attempts; the result's ``protocol_retries`` counts them.
    Action validation checks JSON shape, known tool names and object inputs;
    Waku's tools remain responsible for validating their argument values.
    A quota denial holds that explicit model until a reported future retry time,
    or until ``clear_quota_hold(model)`` / a new client when no such time exists.
    ``max_tokens`` is accepted but not sent: OpenCode's prompt API has no such
    field, so the provider's output limit applies. Truncated output is rejected.
    """

    def __init__(self, base_url="http://127.0.0.1:4096", timeout=1800,
                 poll_interval=2, http_timeout=30, protocol_retries=2):
        self.base_url = base_url.rstrip("/")
        self.timeout = _number("timeout", timeout, zero=True)
        self.poll_interval = _number("poll_interval", poll_interval)
        self.http_timeout = _number("http_timeout", http_timeout)
        if isinstance(protocol_retries, bool) or not isinstance(protocol_retries, int) \
                or protocol_retries < 0:
            raise ValueError("protocol_retries must be a nonnegative integer")
        self.protocol_retries = protocol_retries
        self.messages = SimpleNamespace(create=self._create)
        self._quota_holds = {}

    def clear_quota_hold(self, model):
        """Explicitly allow another attempt for this model after a quota denial."""
        self._quota_holds.pop(_model_selection(model), None)

    def _remaining(self, deadline):
        if deadline is None:
            return math.inf
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeTimeout(f"OpenCode response timeout after {self.timeout:g}s")
        return remaining

    def _request(self, method, path, payload=None, *, deadline=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
        limit = min(self.http_timeout, self._remaining(deadline))
        opener = urllib.request.build_opener(_DeadlineHandler(time.monotonic() + limit))
        try:
            try:
                with opener.open(request, timeout=limit) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                with exc:
                    raw = exc.read().decode("utf-8", errors="replace")
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = raw
                _check_quota(detail)
                raise OpenCodeError(f"OpenCode HTTP {exc.code} on {method} {path}: {raw}") from exc
        except (urllib.error.URLError, OSError) as exc:
            self._remaining(deadline)
            raise OpenCodeError(f"OpenCode request failed: {method} {path}: {exc}") from exc
        try:
            return json.loads(raw) if raw else None
        except (ValueError, UnicodeError) as exc:
            raise OpenCodeError(f"OpenCode returned invalid JSON on {method} {path}") from exc

    def _disabled_tools(self, deadline):
        tools = self._request("GET", "/experimental/tool/ids", deadline=deadline)
        if isinstance(tools, dict) and len(tools) == 1:
            tools = tools.get("data", tools.get("tools"))
        if not isinstance(tools, list) or not tools or any(
            not isinstance(tool, str) or not tool.strip() or tool != tool.strip() for tool in tools
        ):
            raise OpenCodeError("OpenCode tool discovery must return a nonempty list of tool IDs")
        return dict.fromkeys(tools, False)

    def _poll(self, sid, deadline):
        while True:
            messages = self._request("GET", f"/session/{sid}/message", deadline=deadline)
            self._remaining(deadline)
            if not isinstance(messages, list):
                raise OpenCodeError("OpenCode returned a malformed message list")
            latest = None
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
                    raise OpenCodeError("OpenCode returned a malformed message")
                info = message["info"]
                if info.get("sessionID", sid) != sid:
                    raise OpenCodeError("OpenCode returned a message for another session")
                if info.get("role") != "assistant":
                    continue
                parts = message.get("parts")
                if not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts):
                    raise InvalidProtocolError("OpenCode returned malformed content parts")
                if any(part.get("type") == "tool" for part in parts):
                    raise OpenCodeError("OpenCode attempted a native tool despite disabled tools")
                if not info.get("summary"):
                    latest = message
            if latest is not None:
                info = latest["info"]
                if info.get("error") is not None:
                    _check_quota(info["error"])
                    raise OpenCodeError(f"OpenCode model error: {json.dumps(info['error'])}")
                timing = info.get("time")
                if not isinstance(timing, dict):
                    raise OpenCodeError("OpenCode returned malformed assistant completion time")
                completed = timing.get("completed")
                if completed is not None:
                    if isinstance(completed, bool) or not isinstance(completed, (int, float)) \
                            or not math.isfinite(completed) or completed < 0:
                        raise OpenCodeError("OpenCode returned malformed assistant completion time")
                    return latest
            statuses = self._request("GET", "/session/status", deadline=deadline)
            if not isinstance(statuses, dict):
                raise OpenCodeError("OpenCode returned malformed session status")
            state = statuses.get(sid)
            if state is not None and not isinstance(state, dict):
                raise OpenCodeError("OpenCode returned malformed status for the owned session")
            _check_quota(state)  # Other callers' sessions are not ours to inspect.
            time.sleep(min(self.poll_interval, self._remaining(deadline)))

    def _attempt(self, model, system, prompt, deadline):
        disabled_tools = self._disabled_tools(deadline)
        session = self._request("POST", "/session", {"title": "Waku model call"}, deadline=deadline)
        sid = session.get("id") if isinstance(session, dict) else None
        if not isinstance(sid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
            raise OpenCodeError("OpenCode session creation returned an invalid session ID")
        completed = False
        failed = False
        try:
            self._request("POST", f"/session/{sid}/prompt_async", {
                "model": model, "system": system, "tools": disabled_tools,
                "parts": [{"type": "text", "text": prompt}],
            }, deadline=deadline)
            response = self._poll(sid, deadline)
            completed = True
            return response
        except BaseException:
            failed = True
            raise
        finally:
            cancellation = None
            operations = [] if completed else [("POST", f"/session/{sid}/abort")]
            for method, path in operations + [("DELETE", f"/session/{sid}")]:
                try:
                    self._request(method, path)
                except BaseException as exc:
                    # Finish cleanup without replacing an existing failure. A fresh
                    # cancellation during successful cleanup must still reach the caller.
                    if not failed and not isinstance(exc, Exception) and cancellation is None:
                        cancellation = exc
            if cancellation is not None:
                raise cancellation

    def _create(self, *, model, messages, system=None, tools=None, max_tokens=None):
        provider_id, model_id = key = _model_selection(model)
        hold = self._quota_holds.get(key)
        if hold is not None:
            reported, retry_at = hold
            if retry_at is None or time.time() < retry_at:
                raise OpenCodeQuotaExceeded(reported)
            # Leave expired evidence inert; don't erase a newer concurrent denial.
        selection = {"providerID": provider_id, "modelID": model_id}
        allowed = set()
        for tool in tools or []:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not name.strip() or name in allowed:
                raise ValueError("Waku tools must have unique, nonempty names")
            allowed.add(name)
        if isinstance(system, list):
            system = "\n".join(_field(block, "text") for block in system)
        system = _protocol(system or "", tools) if tools else system or ""
        prompt = _transcript(messages)
        deadline = time.monotonic() + self.timeout if self.timeout else None
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        correction = ""
        for attempt in range(self.protocol_retries + 1):
            try:
                response = self._attempt(selection, system, prompt + correction, deadline)
            except OpenCodeQuotaExceeded as exc:
                retry = exc.next_retry
                retry_at = retry / 1000 if isinstance(retry, (int, float)) \
                    and not isinstance(retry, bool) and math.isfinite(retry) else None
                if retry_at is not None and retry_at <= time.time():
                    retry_at = None
                self._quota_holds[key] = (retry, retry_at)
                exc.usage = usage
                raise
            except OpenCodeError as exc:
                exc.usage = usage
                raise
            info, parts = response["info"], response.get("parts")
            tokens = info.get("tokens", {})
            if not isinstance(tokens, dict):
                raise OpenCodeError("OpenCode returned malformed usage")
            try:
                usage.input_tokens += _number("input tokens", tokens.get("input", 0), zero=True)
                usage.output_tokens += _number("output tokens", tokens.get("output", 0), zero=True)
            except ValueError as exc:
                raise OpenCodeError("OpenCode returned malformed usage") from exc
            text = ""
            try:
                if not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts):
                    raise InvalidProtocolError("OpenCode returned malformed content parts")
                reasons = [info.get(key) for key in ("finish", "finishReason", "stopReason")]
                reasons += [part.get("reason") for part in parts if part.get("type") == "step-finish"]
                for reason in reasons:
                    if reason is not None and reason not in ("stop", "end_turn"):
                        raise InvalidProtocolError(f"OpenCode response did not finish normally: {reason}")
                text_parts = [part.get("text") for part in parts if part.get("type") == "text"
                              and not part.get("ignored") and not part.get("synthetic")]
                if any(not isinstance(part, str) for part in text_parts):
                    raise InvalidProtocolError("OpenCode returned a malformed text part")
                text = "".join(text_parts).strip()
                if not text:
                    raise InvalidProtocolError("OpenCode returned empty assistant text")
                block = _action(text, allowed) if tools else SimpleNamespace(type="text", text=text)
            except InvalidProtocolError as exc:
                exc.usage = usage
                if not tools or attempt == self.protocol_retries:
                    raise
                correction = ("\n\nThe previous response was rejected: " + str(exc)
                              + ". No Waku tool was executed. Return one complete valid JSON action."
                              + "\nPrevious response (quoted): " + json.dumps(text, ensure_ascii=False))
                continue
            except OpenCodeError as exc:
                exc.usage = usage
                raise
            return SimpleNamespace(content=[block], usage=usage, protocol_retries=attempt,
                                   stop_reason="tool_use" if block.type == "tool_use" else "end_turn")
