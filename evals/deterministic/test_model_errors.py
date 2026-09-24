"""One refused call, one clean sentence.

Acceptance 3's last sentences. A 401 or 403 must not be retried by waku's
own non-streaming fallback: each call site reaches the provider once per
turn. A 429 is left to the SDK's own retry policy.
"""

from __future__ import annotations

import pytest

from evals.helpers import response, text_block


class Refused(Exception):
    """Shaped like the SDK's APIStatusError: a status and a message."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def test_a_401_is_not_retried_without_streaming():
    calls = []

    class Client:
        class messages:
            @staticmethod
            def stream(**kwargs):
                calls.append("stream")
                raise Refused(401, "Free tier used up. Add your own key in Models.")

            @staticmethod
            def create(**kwargs):
                calls.append("create")
                raise AssertionError("the fallback must not run on a 4xx")

    from waku.loop import agent
    from waku.tools.registry import ToolRegistry

    with pytest.raises(Refused):
        agent.run_loop(Client(), model="m", system="s", messages=[],
                        tools=ToolRegistry(), stream=True)
    assert calls == ["stream"], f"expected one call, got {calls}"


def test_a_500_still_falls_back():
    """The fallback exists for transport faults. Keep it for those."""
    calls = []

    class Client:
        class messages:
            @staticmethod
            def stream(**kwargs):
                calls.append("stream")
                raise Refused(500, "internal")

            @staticmethod
            def create(**kwargs):
                calls.append("create")
                return response([text_block("ok")])

    from waku.loop import agent
    from waku.tools.registry import ToolRegistry

    result = agent.run_loop(Client(), model="m", system="s", messages=[],
                             tools=ToolRegistry(), stream=True)
    assert calls == ["stream", "create"], f"expected fallback, got {calls}"
    assert result.reply == "ok"


def test_the_done_event_carries_only_the_message():
    from waku.ops.dashboard import error_text

    assert error_text(Refused(403, "Free tier used up. Add your own key in Models.")) \
        == "Free tier used up. Add your own key in Models."


def test_a_non_provider_error_keeps_todays_text():
    from waku.ops.dashboard import error_text

    assert error_text(ValueError("bad input")) == "ValueError: bad input"
