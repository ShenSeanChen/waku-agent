"""OpenCode transport checks: fake HTTP and clock, no server or model calls."""

from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from waku.loop import opencode_local
from waku.loop.agent import run_loop
from waku.loop.opencode_local import (
    InvalidProtocolError,
    OpenCodeError,
    OpenCodeLocalClient,
    OpenCodeQuotaExceeded,
    OpenCodeTimeout,
)
from waku.tools.registry import Tool, ToolRegistry

MODEL = "muse-spark-1.3-contributor-free"
TOOLS = [{"name": "echo", "description": "Echo a value", "input_schema": {
    "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
}}]
TOOL_ACTION = '{"type":"tool_use","name":"echo","input":{"value":"hello"}}'
FINAL_ACTION = '{"type":"final","text":"Done."}'


def reply(text="Done.", *, completed=1, finish="stop", tokens=None, parts=None):
    return [{"info": {"role": "assistant", "time": {"completed": completed},
                      "finish": finish, "tokens": tokens or {"input": 11, "output": 7}},
             "parts": parts if parts is not None else [{"type": "text", "text": text}]}]


class FakeServer:
    """A transport fake with one independently scripted response per session."""

    def __init__(self, monkeypatch, replies=None):
        self.replies = replies if replies is not None else [reply()]
        self.calls = []
        self.sessions = {}
        self.discovery = ["bash", "read", "plugin_search"]
        self.status = {}
        self.overrides = {}
        self.now = 0
        self.interrupt = None
        monkeypatch.setattr(opencode_local.urllib.request, "build_opener",
                            lambda *handlers: SimpleNamespace(open=self.urlopen))
        monkeypatch.setattr(opencode_local.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(opencode_local.time, "time", lambda: 1_000_000_000 + self.now)
        monkeypatch.setattr(opencode_local.time, "sleep", self.sleep)

    def sleep(self, seconds):
        if self.interrupt is not None:
            raise self.interrupt
        self.now += seconds

    def urlopen(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data else None
        self.calls.append({"method": method, "path": path, "payload": payload, "timeout": timeout})
        if (method, path) in self.overrides:
            data = self.overrides[method, path]
        elif path == "/experimental/tool/ids":
            data = self.discovery
        elif (method, path) == ("POST", "/session"):
            sid = f"ses_{len(self.sessions) + 1}"
            self.sessions[sid] = self.replies[len(self.sessions)]
            data = {"id": sid}
        elif path == "/session/status":
            data = self.status
        elif method == "GET" and path.endswith("/message"):
            data = self.sessions[path.split("/")[2]]
        elif (method == "POST" and path.endswith(("/prompt_async", "/abort"))) \
                or method == "DELETE":
            data = None
        else:
            raise AssertionError(f"Unexpected HTTP request: {method} {path}")
        if callable(data):
            data = data()
        if isinstance(data, BaseException):
            raise data
        raw = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else b""
        return io.BytesIO(raw)

    def requests(self, method, suffix):
        return [call for call in self.calls
                if call["method"] == method and call["path"].endswith(suffix)]

    def assert_cleanup(self, *, aborted, sessions=("ses_1",)):
        assert [call["path"] for call in self.requests("POST", "/abort")] == (
            [f"/session/{sid}/abort" for sid in sessions] if aborted else []
        )
        assert [call["path"] for call in self.requests("DELETE", "")] == [
            f"/session/{sid}" for sid in sessions
        ]


def create(client=None, **kwargs):
    return (client or OpenCodeLocalClient()).messages.create(
        model=kwargs.pop("model", MODEL), messages=kwargs.pop("messages", [
            {"role": "user", "content": "Hello 🌍"}
        ]), max_tokens=4096, **kwargs,
    )


def test_async_payload_disables_every_discovered_tool(monkeypatch):
    server = FakeServer(monkeypatch, [reply(FINAL_ACTION)])
    result = create(tools=TOOLS, system="Be helpful.")
    submits = server.requests("POST", "/prompt_async")
    assert len(submits) == 1
    payload = submits[0]["payload"]
    assert set(payload) == {"model", "system", "parts", "tools"}
    assert payload["model"] == {"providerID": "opencode", "modelID": MODEL}
    assert payload["tools"] == {"bash": False, "read": False, "plugin_search": False}
    assert payload["parts"] == [{"type": "text", "text": "USER:\nHello 🌍"}]
    assert payload["system"].startswith("Be helpful.\n\n")
    assert '"name": "echo"' in payload["system"]
    assert "max_tokens" not in payload
    assert server.requests("POST", "/message") == []
    assert result.stop_reason == "end_turn" and result.content[0].text == "Done."
    assert result.usage.input_tokens == 11 and result.usage.output_tokens == 7
    assert all(0 < call["timeout"] <= 30 for call in server.calls)
    server.assert_cleanup(aborted=False)


def test_wait_longer_than_120_seconds_without_duplicate_submission(monkeypatch):
    server = FakeServer(monkeypatch)
    server.replies = [lambda: reply(FINAL_ACTION, completed=1 if server.now >= 130 else None)]
    # Another caller's quota must not abort our session.
    server.status = {"ses_someone_else": {"type": "retry", "action": {"reason": "free_tier_limit"}}}
    result = create(tools=TOOLS)
    assert result.content[0].text == "Done."
    assert server.now == 130
    assert len(server.requests("POST", "/prompt_async")) == 1
    assert len(server.requests("GET", "/message")) > 60
    assert len(server.requests("GET", "/session/status")) > 60
    server.assert_cleanup(aborted=False)


@pytest.mark.parametrize("model, expected", [
    ("explicit-model", {"providerID": "opencode", "modelID": "explicit-model"}),
    ("custom/vendor/model", {"providerID": "custom", "modelID": "vendor/model"}),
])
def test_explicit_model_is_preserved(monkeypatch, model, expected):
    server = FakeServer(monkeypatch)
    create(model=model)
    assert server.requests("POST", "/prompt_async")[0]["payload"]["model"] == expected


def test_own_quota_status_fails_immediately_and_reports_retry(monkeypatch):
    server = FakeServer(monkeypatch, [reply(completed=None)])
    server.status = {"ses_1": {"type": "retry", "action": {"reason": "free_tier_limit"},
                               "next": 2_000_000_000_000}}
    with pytest.raises(OpenCodeQuotaExceeded, match="Reported next retry: 2033") as caught:
        create()
    assert caught.value.next_retry == 2_000_000_000_000
    assert server.now == 0
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_http_quota_failure_is_not_resubmitted(monkeypatch):
    server = FakeServer(monkeypatch)
    body = io.BytesIO(b'{"error":{"reason":"free_tier_limit","next":2000000000000}}')
    server.overrides["POST", "/session/ses_1/prompt_async"] = urllib.error.HTTPError(
        "http://localhost/session/ses_1/prompt_async", 429, "Limited", {}, body,
    )
    with pytest.raises(OpenCodeQuotaExceeded):
        create()
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_quota_hold_covers_qualified_alias_until_reported_retry(monkeypatch):
    server = FakeServer(monkeypatch, [reply(completed=None), reply()])
    server.status = {"ses_1": {"action": {"reason": "free_tier_limit"},
                               "next": 1_000_000_010_000}}
    client = OpenCodeLocalClient()
    with pytest.raises(OpenCodeQuotaExceeded):
        create(client)
    previous_calls = list(server.calls)
    server.now = 9
    with pytest.raises(OpenCodeQuotaExceeded):
        create(client, model="opencode/" + MODEL)
    assert server.calls == previous_calls
    server.now = 10
    assert create(client).content[0].text == "Done."
    assert len(server.requests("POST", "/prompt_async")) == 2


@pytest.mark.parametrize("retry", [None, True, float("inf"), float("nan"), "soon", 0, 999_999_999_000])
def test_quota_without_usable_future_retry_requires_explicit_reset(monkeypatch, retry):
    server = FakeServer(monkeypatch, [reply(completed=None), reply()])
    server.status = {"ses_1": {"action": {"reason": "free_tier_limit"}, "next": retry}}
    client = OpenCodeLocalClient()
    with pytest.raises(OpenCodeQuotaExceeded):
        create(client)
    previous_calls = list(server.calls)
    server.now = 1_000_000
    with pytest.raises(OpenCodeQuotaExceeded):
        create(client)
    assert server.calls == previous_calls
    client.clear_quota_hold("opencode/" + MODEL)
    assert create(client).content[0].text == "Done."
    assert len(server.requests("POST", "/prompt_async")) == 2


def test_quota_hold_does_not_change_an_explicit_different_model(monkeypatch):
    server = FakeServer(monkeypatch, [reply(completed=None), reply()])
    server.status = {"ses_1": {"action": {"reason": "free_tier_limit"}}}
    client = OpenCodeLocalClient()
    with pytest.raises(OpenCodeQuotaExceeded):
        create(client)
    create(client, model="custom/explicit-model")
    assert server.requests("POST", "/prompt_async")[1]["payload"]["model"] == {
        "providerID": "custom", "modelID": "explicit-model",
    }


@pytest.mark.parametrize("discovery", [
    None, [], {}, "bash", [None], [""], ["bash", " "], [" bash "], {"data": []},
    {"unknown": ["bash"]}, {"tools": ["bash"], "data": ["read"]}, b"not JSON",
    urllib.error.URLError("server unavailable"),
])
def test_invalid_tool_discovery_fails_before_creating_or_submitting(monkeypatch, discovery):
    server = FakeServer(monkeypatch)
    server.discovery = discovery
    with pytest.raises(OpenCodeError):
        create()
    assert server.requests("POST", "") == []
    assert server.requests("DELETE", "") == []


@pytest.mark.parametrize("discovery", [{"data": ["bash"]}, {"tools": ["bash"]}])
def test_known_tool_discovery_wrappers(monkeypatch, discovery):
    server = FakeServer(monkeypatch)
    server.discovery = discovery
    create()
    assert server.requests("POST", "/prompt_async")[0]["payload"]["tools"] == {"bash": False}


@pytest.mark.parametrize("cancel", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_cancellation_aborts_and_deletes_only_owned_session(monkeypatch, cancel):
    server = FakeServer(monkeypatch, [reply(completed=None)])
    server.interrupt = cancel
    with pytest.raises(type(cancel)):
        create()
    server.assert_cleanup(aborted=True)
    assert len(server.requests("POST", "/prompt_async")) == 1


def test_response_timeout_has_a_separate_finite_http_timeout(monkeypatch):
    server = FakeServer(monkeypatch, [reply(completed=None)])
    with pytest.raises(OpenCodeTimeout):
        create(OpenCodeLocalClient(timeout=5, http_timeout=3))
    assert server.now == 5
    assert all(0 < call["timeout"] <= 3 for call in server.calls)
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_unlimited_response_wait_still_bounds_each_http_request(monkeypatch):
    server = FakeServer(monkeypatch)
    server.replies = [lambda: reply(completed=1 if server.now >= 2000 else None)]
    create(OpenCodeLocalClient(timeout=0, poll_interval=100, http_timeout=4))
    assert server.now == 2000
    assert all(call["timeout"] == 4 for call in server.calls)
    assert len(server.requests("GET", "/message")) == 21
    assert len(server.requests("POST", "/prompt_async")) == 1


@pytest.mark.parametrize("method, path", [
    ("POST", "/session/ses_1/prompt_async"),
    ("GET", "/session/ses_1/message"),
    ("GET", "/session/status"),
])
def test_uncertain_submission_or_poll_outage_is_never_replayed(monkeypatch, method, path):
    server = FakeServer(monkeypatch, [reply(completed=None)])
    server.overrides[method, path] = urllib.error.URLError("connection lost")
    with pytest.raises(OpenCodeError, match="connection lost"):
        create()
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_cleanup_failure_preserves_original_error_and_attempts_deletion(monkeypatch):
    server = FakeServer(monkeypatch)
    server.overrides["POST", "/session/ses_1/prompt_async"] = TimeoutError("submit timed out")
    server.overrides["POST", "/session/ses_1/abort"] = KeyboardInterrupt()
    server.overrides["DELETE", "/session/ses_1"] = urllib.error.URLError("delete unavailable")
    with pytest.raises(OpenCodeError, match="submit timed out"):
        create()
    server.assert_cleanup(aborted=True)


@pytest.mark.parametrize("cancel", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_fresh_cancellation_during_successful_cleanup_propagates(monkeypatch, cancel):
    server = FakeServer(monkeypatch)
    server.overrides["DELETE", "/session/ses_1"] = cancel
    with pytest.raises(type(cancel)):
        create()
    server.assert_cleanup(aborted=False)
    assert len(server.requests("POST", "/prompt_async")) == 1


def test_cleanup_has_separate_finite_limits_after_response_timeout(monkeypatch):
    server = FakeServer(monkeypatch, [reply(completed=None)])

    def unavailable():
        server.now += 3
        raise urllib.error.URLError("cleanup unavailable")

    server.overrides["POST", "/session/ses_1/abort"] = unavailable
    server.overrides["DELETE", "/session/ses_1"] = unavailable
    with pytest.raises(OpenCodeTimeout):
        create(OpenCodeLocalClient(timeout=5, http_timeout=3))
    assert server.now == 11
    assert [call["timeout"] for call in server.calls[-2:]] == [3, 3]
    server.assert_cleanup(aborted=True)


@pytest.mark.parametrize("phase", ["headers", "body", "error_body"])
@pytest.mark.parametrize("deadline_kind", ["response", "http"])
def test_absolute_deadlines_bound_trickling_http_without_real_network(
    monkeypatch, phase, deadline_kind,
):
    now = [0.0]
    body = b'["read"]'
    status = b"429 Limited" if phase == "error_body" else b"200 OK"
    headers = b"HTTP/1.1 " + status + b"\r\nContent-Length: 8\r\nConnection: close\r\n\r\n"
    wire = headers + body

    class TrickleRaw(io.RawIOBase):
        position = 0

        def readable(self):
            return True

        def readinto(self, buffer):
            if self.position == len(wire):
                return 0
            is_header = self.position < len(headers)
            wait = 0.04 if is_header == (phase == "headers") else 0
            now[0] += min(wait, sock.timeout)
            if wait >= sock.timeout:
                raise TimeoutError("socket receive timed out")
            buffer[0] = wire[self.position]
            self.position += 1
            return 1

    class Socket:
        def __init__(self):
            self.timeout = 1.0
            self.closes = 0
            self.sent = []

        def settimeout(self, value):
            self.timeout = value

        def setsockopt(self, *args):
            pass

        def sendall(self, data):
            self.sent.append(data)

        def makefile(self, mode):
            assert mode == "rb"
            return io.BufferedReader(raw)

        def close(self):
            self.closes += 1

    raw, sock = TrickleRaw(), Socket()
    connections = []

    def connect(address, *args, **kwargs):
        connections.append(address)
        return sock

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(opencode_local.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    client = OpenCodeLocalClient(timeout=0.07 if deadline_kind == "response" else 0,
                                 http_timeout=1 if deadline_kind == "response" else 0.07)
    expected = OpenCodeTimeout if deadline_kind == "response" else OpenCodeError
    with pytest.raises(expected):
        client._request("GET", "/experimental/tool/ids",
                        deadline=0.07 if deadline_kind == "response" else None)
    assert now[0] == pytest.approx(0.07)
    assert connections == [("127.0.0.1", 4096)]
    assert len(sock.sent) == 1
    assert sock.closes and raw.closed


def test_completed_message_arriving_after_deadline_is_rejected(monkeypatch):
    server = FakeServer(monkeypatch)

    def late_reply():
        server.now += 6
        return reply()

    server.replies = [late_reply]
    with pytest.raises(OpenCodeTimeout):
        create(OpenCodeLocalClient(timeout=5))
    server.assert_cleanup(aborted=True)


def test_late_session_creation_still_cleans_up_its_returned_id(monkeypatch):
    server = FakeServer(monkeypatch)

    def late_session():
        server.now += 6
        return {"id": "ses_owned"}

    server.overrides["POST", "/session"] = late_session
    with pytest.raises(OpenCodeTimeout):
        create(OpenCodeLocalClient(timeout=5))
    assert server.requests("POST", "/prompt_async") == []
    server.assert_cleanup(aborted=True, sessions=("ses_owned",))


def test_requires_completion_of_latest_assistant_even_when_status_is_idle(monkeypatch):
    server = FakeServer(monkeypatch)
    server.status = {"ses_1": {"type": "idle"}}

    def messages():
        user = reply("User text is not the answer.")[0]
        user["info"]["role"] = "user"
        latest = reply("Final answer.", completed=1 if server.now >= 2 else None)[0]
        return [user, reply("Older completed answer.")[0], latest]

    server.replies = [messages]
    result = create()
    assert result.content[0].text == "Final answer." and server.now == 2
    assert len(server.requests("POST", "/prompt_async")) == 1


@pytest.mark.parametrize("mutate", [
    lambda message: message["info"].update(time=None),
    lambda message: message["info"].update(time={"completed": True}),
    lambda message: message["info"].update(time={"completed": float("inf")}),
    lambda message: message["info"].update(sessionID="ses_other"),
])
def test_invalid_completion_or_wrong_session_is_rejected(monkeypatch, mutate):
    response = reply()
    mutate(response[0])
    server = FakeServer(monkeypatch, [response])
    with pytest.raises(OpenCodeError):
        create()
    server.assert_cleanup(aborted=True)


@pytest.mark.parametrize("data", [None, {}, [None], [{"parts": []}]])
def test_malformed_message_poll_fails_without_resubmitting(monkeypatch, data):
    server = FakeServer(monkeypatch, [data])
    with pytest.raises(OpenCodeError):
        create()
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


@pytest.mark.parametrize("session", [None, {}, {"id": 7}, {"id": "../other"}, {"id": ""}])
def test_malformed_session_id_never_targets_another_session(monkeypatch, session):
    server = FakeServer(monkeypatch)
    server.overrides["POST", "/session"] = session
    with pytest.raises(OpenCodeError, match="session ID"):
        create()
    assert server.requests("POST", "/prompt_async") == []
    assert server.requests("POST", "/abort") == []
    assert server.requests("DELETE", "") == []


@pytest.mark.parametrize("finish", ["length", "truncated", "max_tokens", "max_output_tokens"])
def test_truncated_json_is_rejected_even_when_it_parses(monkeypatch, finish):
    FakeServer(monkeypatch, [reply(TOOL_ACTION, finish=finish)])
    with pytest.raises(InvalidProtocolError, match="did not finish normally"):
        create(OpenCodeLocalClient(protocol_retries=0), tools=TOOLS)


def test_step_finish_length_is_also_truncation(monkeypatch):
    FakeServer(monkeypatch, [reply(parts=[{"type": "text", "text": TOOL_ACTION},
                                         {"type": "step-finish", "reason": "length"}])])
    with pytest.raises(InvalidProtocolError):
        create(OpenCodeLocalClient(protocol_retries=0), tools=TOOLS)


@pytest.mark.parametrize("text", [
    "", " ", '{"type":"final","text":"missing brace"',
    "```json\n" + FINAL_ACTION + "\n```", "Here: " + FINAL_ACTION,
    FINAL_ACTION + FINAL_ACTION, "[]", "null", '{"type":"other"}',
    '{"type":"tool_use","name":"unknown","input":{}}',
    '{"type":"tool_use","name":["echo"],"input":{}}',
    '{"type":"tool_use","name":"echo"}',
    '{"type":"tool_use","name":"echo","input":[]}',
    '{"type":"tool_use","name":"echo","input":"{}"}',
    '{"type":"tool_use","name":"echo","input":null}',
    '{"type":"tool_use","name":"echo","input":{},"extra":true}',
    '{"type":"tool_use","name":"echo","name":"unknown","input":{}}',
    '{"type":"tool_use","name":"echo","input":{"value":"a","value":"b"}}',
    '{"type":"tool_use","name":"echo","input":{"value":NaN}}',
    '{"type":"tool_use","name":"echo","input":{"value":1e999}}',
    '{"type":"final","text":0}', '{"type":"final","text":""}',
    '{"type":"final","text":"ok","name":"echo"}',
])
def test_invalid_actions_never_become_a_tool_or_success(monkeypatch, text):
    server = FakeServer(monkeypatch, [reply(text)])
    with pytest.raises(InvalidProtocolError):
        create(OpenCodeLocalClient(protocol_retries=0), tools=TOOLS)
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=False)


@pytest.mark.parametrize("parts", [[], ["not a part"], [{"type": "text"}],
                                  [{"type": "text", "text": 1}], {"type": "text"}])
def test_empty_or_malformed_parts_fail(monkeypatch, parts):
    FakeServer(monkeypatch, [reply(parts=parts)])
    with pytest.raises(InvalidProtocolError):
        create(OpenCodeLocalClient(protocol_retries=0))


@pytest.mark.parametrize("tools", [None, []])
def test_text_only_calls_preserve_natural_output_and_exclude_reasoning(monkeypatch, tools):
    text = '{"retrieve":false,"query":"","reason":"no context needed"}'
    server = FakeServer(monkeypatch, [reply(parts=[
        {"type": "reasoning", "text": "private reasoning"},
        {"type": "text", "text": "synthetic instruction", "synthetic": True},
        {"type": "text", "text": "ignored text", "ignored": True},
        {"type": "text", "text": text},
    ])])
    result = create(tools=tools, system="Choose whether to retrieve.")
    assert result.content[0].text == text
    assert result.usage.input_tokens == 11 and result.usage.output_tokens == 7
    assert server.requests("POST", "/prompt_async")[0]["payload"]["system"] == (
        "Choose whether to retrieve."
    )


def test_reasoning_without_public_text_cannot_complete(monkeypatch):
    FakeServer(monkeypatch, [reply(parts=[{"type": "reasoning", "text": FINAL_ACTION}])])
    with pytest.raises(InvalidProtocolError, match="empty assistant text"):
        create(OpenCodeLocalClient(protocol_retries=0))


def test_protocol_repair_is_bounded_explicit_and_aggregates_usage(monkeypatch):
    server = FakeServer(monkeypatch, [reply(TOOL_ACTION[:-1]), reply(TOOL_ACTION)])
    result = create(tools=TOOLS)
    assert result.stop_reason == "tool_use" and result.content[0].input == {"value": "hello"}
    assert result.protocol_retries == 1
    assert result.usage.input_tokens == 22 and result.usage.output_tokens == 14
    prompts = server.requests("POST", "/prompt_async")
    assert [call["path"] for call in prompts] == [
        "/session/ses_1/prompt_async", "/session/ses_2/prompt_async",
    ]
    assert "No Waku tool was executed" in prompts[1]["payload"]["parts"][0]["text"]
    assert prompts[0]["payload"]["model"] == prompts[1]["payload"]["model"]
    server.assert_cleanup(aborted=False, sessions=("ses_1", "ses_2"))


def test_quota_during_repair_retains_previous_usage_and_stops(monkeypatch):
    server = FakeServer(monkeypatch, [reply("invalid"), reply(completed=None)])
    server.status = {"ses_2": {"type": "retry", "action": {"reason": "free_tier_limit"}}}
    with pytest.raises(OpenCodeQuotaExceeded) as caught:
        create(tools=TOOLS)
    assert caught.value.usage.input_tokens == 11 and caught.value.usage.output_tokens == 7
    assert len(server.requests("POST", "/prompt_async")) == 2
    assert [call["path"] for call in server.requests("POST", "/abort")] == ["/session/ses_2/abort"]
    assert len(server.requests("DELETE", "")) == 2


def test_exhausted_protocol_retries_report_all_usage(monkeypatch):
    server = FakeServer(monkeypatch, [reply("invalid")] * 3)
    with pytest.raises(InvalidProtocolError) as caught:
        create(tools=TOOLS)
    assert caught.value.usage.input_tokens == 33 and caught.value.usage.output_tokens == 21
    assert len(server.requests("POST", "/prompt_async")) == 3
    assert len(server.requests("GET", "/experimental/tool/ids")) == 3
    server.assert_cleanup(aborted=False, sessions=("ses_1", "ses_2", "ses_3"))


def test_tool_discovery_refreshes_between_attempts(monkeypatch):
    server = FakeServer(monkeypatch, [reply("invalid"), reply(FINAL_ACTION)])
    server.discovery = lambda: ["bash"] if not server.sessions else ["bash", "new_plugin_tool"]
    create(tools=TOOLS)
    assert server.requests("POST", "/prompt_async")[1]["payload"]["tools"] == {
        "bash": False, "new_plugin_tool": False,
    }


def test_tool_discovery_refreshes_between_calls_on_the_same_client(monkeypatch):
    server = FakeServer(monkeypatch, [reply(), reply()])
    client = OpenCodeLocalClient()
    create(client)
    server.discovery.append("new_plugin_tool")
    create(client)
    assert len(server.requests("GET", "/experimental/tool/ids")) == 2
    assert server.requests("POST", "/prompt_async")[1]["payload"]["tools"]["new_plugin_tool"] is False


def test_model_errors_and_unexpected_native_tools_are_not_repaired(monkeypatch):
    server = FakeServer(monkeypatch, [reply(parts=[{"type": "tool", "tool": "bash"}])])
    with pytest.raises(OpenCodeError, match="native tool"):
        create(tools=TOOLS)
    assert len(server.requests("POST", "/prompt_async")) == 1


@pytest.mark.parametrize("summary", [False, True])
def test_native_tool_in_earlier_assistant_cannot_hide_behind_final_reply(monkeypatch, summary):
    earlier = reply(parts=[{"type": "tool", "tool": "bash", "state": {"status": "completed"}}])
    earlier[0]["info"]["summary"] = summary
    server = FakeServer(monkeypatch, [earlier + reply(FINAL_ACTION)])
    with pytest.raises(OpenCodeError, match="native tool"):
        create(tools=TOOLS)
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_model_error_does_not_select_a_fallback(monkeypatch):
    response = reply()
    response[0]["info"]["error"] = {"name": "ProviderAuthError", "data": {"message": "No access"}}
    server = FakeServer(monkeypatch, [response])
    with pytest.raises(OpenCodeError, match="No access"):
        create(model="custom/model", tools=TOOLS)
    assert len(server.requests("POST", "/prompt_async")) == 1
    server.assert_cleanup(aborted=True)


def test_input_sdk_and_dictionary_blocks_preserve_tool_history_without_reasoning(monkeypatch):
    server = FakeServer(monkeypatch)
    messages = [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "Calling echo."},
            {"type": "tool_use", "id": "call_1", "name": "echo", "input": {"value": "hello"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "Failed", "is_error": True},
        ]},
    ]
    create(messages=messages, system=[{"type": "text", "text": "Answer naturally."}])
    payload = server.requests("POST", "/prompt_async")[0]["payload"]
    assert payload["system"] == "Answer naturally."
    text = payload["parts"][0]["text"]
    assert "Calling echo." in text and '"tool_use_id": "call_1"' in text
    assert '"is_error": true' in text and "private reasoning" not in text


@pytest.mark.parametrize("options", [
    {"timeout": True}, {"timeout": -1}, {"timeout": float("inf")}, {"timeout": float("nan")},
    {"poll_interval": True}, {"poll_interval": 0}, {"poll_interval": -1},
    {"poll_interval": float("inf")}, {"http_timeout": True}, {"http_timeout": 0},
    {"http_timeout": -1}, {"http_timeout": float("nan")}, {"http_timeout": "bad"},
    {"protocol_retries": True}, {"protocol_retries": -1}, {"protocol_retries": 1.5},
])
def test_invalid_deadlines_and_retry_limits_are_rejected(options):
    with pytest.raises(ValueError):
        OpenCodeLocalClient(**options)


@pytest.mark.parametrize("model", [None, "", " ", "/model", "provider/"])
def test_invalid_model_selection_is_rejected_before_http(monkeypatch, model):
    server = FakeServer(monkeypatch)
    with pytest.raises(ValueError):
        create(model=model)
    assert server.calls == []


def test_existing_loop_executes_one_tool_then_receives_final_text(monkeypatch):
    server = FakeServer(monkeypatch, [reply("invalid"), reply(TOOL_ACTION), reply(FINAL_ACTION)])
    executed = []
    tools = ToolRegistry()

    def echo(value):
        executed.append(value)
        return "Observed: " + value

    tools.register(Tool("echo", "Echo a value", TOOLS[0]["input_schema"], echo))
    messages = [{"role": "user", "content": "Echo hello, then finish."}]
    result = run_loop(OpenCodeLocalClient(), MODEL, "Be helpful.", messages, tools,
                      max_iterations=2, stream=True)
    assert result.reply == "Done." and result.iterations == 2
    assert executed == ["hello"]
    assert result.tool_calls == [{"tool": "echo", "args": {"value": "hello"},
                                  "output": "Observed: hello"}]
    assert messages[1]["content"][0].id == messages[2]["content"][0]["tool_use_id"]
    final_prompt = server.requests("POST", "/prompt_async")[2]["payload"]["parts"][0]["text"]
    assert "ASSISTANT TOOL CALL" in final_prompt and "TOOL RESULT" in final_prompt
    assert "Observed: hello" in final_prompt
    assert len(server.requests("POST", "/prompt_async")) == 3
    server.assert_cleanup(aborted=False, sessions=("ses_1", "ses_2", "ses_3"))
