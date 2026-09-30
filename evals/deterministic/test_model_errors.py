"""One refused call, one clean sentence.

Acceptance 3's last sentences. A 401 or 403 must not be retried by waku's
own non-streaming fallback: each call site reaches the provider once per
turn. A 429 is left to the SDK's own retry policy.

And the sentence reaches a reader by BOTH routes it can take: the
gateway's "done" event, and a graph node's entry in a run's `errors` map.
The second one was specified and not built — a node error was `repr(exc)`,
so a tenant who hit the free-tier cap inside a workflow read
`Refused('Free tier used up. Add your own key in Models.')` in the Graph
card instead of the sentence written for them.
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
    from waku.loop.agent import error_text

    assert error_text(Refused(403, "Free tier used up. Add your own key in Models.")) \
        == "Free tier used up. Add your own key in Models."


def test_a_non_provider_error_keeps_todays_text():
    from waku.loop.agent import error_text

    assert error_text(ValueError("bad input")) == "ValueError: bad input"


def test_a_status_code_without_a_message_keeps_todays_text():
    """The guard needs BOTH a message and a status_code. The test above
    exercises the neither-half; this is the status_code-alone half, which an
    implementation that checked only `status_code` would pass while turning
    every bare HTTP error into an empty string on the screen."""
    from waku.loop.agent import error_text

    class NoMessage(Exception):
        def __init__(self):
            super().__init__("upstream said no")
            self.status_code = 503
            self.message = ""

    assert error_text(NoMessage()) == "NoMessage: upstream said no"


def test_a_refused_call_inside_a_graph_node_reads_as_the_message():
    """Acceptance 3's other half. A node error is recorded, never raised, so
    this string IS what the Graph card shows whoever ran the workflow."""
    from waku.graph import END, START, Graph, Node, run_graph

    def refuse(state: dict) -> dict:
        raise Refused(403, "Free tier used up. Add your own key in Models.")

    graph = Graph("gated")
    graph.add_node(Node(name="gate", fn=refuse, kind="tool"))
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)

    state = run_graph(graph, {})
    assert state["errors"]["gate"] == "Free tier used up. Add your own key in Models.", (
        f"the graph node's error is {state['errors']['gate']!r} — a tenant reads "
        f"this in the Graph card, so it has to be the provider's sentence, not "
        f"a Python repr"
    )


def test_an_ordinary_graph_node_bug_still_reads_as_a_bug():
    """The other direction: a plain exception inside a node is a bug, not a
    decision, and keeps the type name that says so."""
    from waku.graph import END, START, Graph, Node, run_graph

    def explode(state: dict) -> dict:
        raise ValueError("bad input")

    graph = Graph("buggy")
    graph.add_node(Node(name="step", fn=explode, kind="tool"))
    graph.add_edge(START, "step")
    graph.add_edge("step", END)

    assert run_graph(graph, {})["errors"]["step"] == "ValueError: bad input"
