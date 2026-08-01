"""DETERMINISTIC EVAL — hero 1: the gate that decides whether to search memory.

CLAUDE.md calls `retrieval_gate.py` hero 1, and until now it had no test. It is
the answer to the most-asked question about this repo ("why not just search
memory every turn?"), so it is the one piece that most needs to keep working.

Everything here is offline: a ScriptedClient plays back whatever the small model
would have said, so these cases pin the PARSING and the FAILURE POSTURE — the
parts that are ours. Whether a given model says true or false for a given
sentence is a judgment call and belongs in evals/judge/, not here.

The contract in one line: **the gate fails OPEN.** Anything it cannot understand
must produce "retrieve", because a stale memory beats a lost one. Every case
below either proves a clean decision is read correctly, or proves a broken one
still lets memory through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, response, text_block
from milli.memory.retrieval_gate import GATE_PROMPT, should_retrieve


def gate(reply_text: str):
    """Run the gate against a model that says exactly `reply_text`."""
    client = ScriptedClient([response([text_block(reply_text)])])
    return should_retrieve(client, "small-model", "when am I meeting Alex?")


# ---------- the happy path: a clean decision is read correctly


def test_a_yes_carries_the_search_query_through():
    retrieve, query, reason = gate(
        '{"retrieve": true, "query": "Alex meeting", "reason": "asks about a person"}'
    )
    assert retrieve is True
    assert query == "Alex meeting", "the gate's query is what actually gets searched"
    assert reason == "asks about a person"


def test_a_no_skips_retrieval():
    retrieve, _query, reason = gate(
        '{"retrieve": false, "query": "", "reason": "general knowledge"}'
    )
    assert retrieve is False
    assert reason == "general knowledge"


def test_prose_around_the_json_is_tolerated():
    """Small models like to explain themselves. Slicing from the first { to the
    last } is deliberate — a chatty model must not cost the user their memory."""
    retrieve, query, _ = gate(
        'Sure! Here is my decision:\n'
        '{"retrieve": true, "query": "Alex", "reason": "personal"}\n'
        'Let me know if you need anything else.'
    )
    assert retrieve is True
    assert query == "Alex"


def test_a_reasoning_block_before_the_json_is_tolerated():
    """The live bug this guards: reasoning models (Kimi K3 and friends) emit a
    thinking block first. max_tokens was 100, which truncated the JSON away
    entirely; it is 600 now. The parser has to cope with the preamble too."""
    retrieve, query, _ = gate(
        "<thinking>The user names a person and asks about a plan, so this needs "
        "their stored memory.</thinking>"
        '{"retrieve": true, "query": "Alex meeting", "reason": "names a person"}'
    )
    assert retrieve is True
    assert query == "Alex meeting"


# ---------- the contract that actually matters: fail OPEN


def test_a_reply_with_no_json_at_all_fails_open():
    """Truncated or reasoning-only output. Retrieve anyway, and SAY why — the
    reason string surfaces in the dashboard and the trace, so a gate that is
    quietly failing every turn is visible rather than mysterious."""
    retrieve, query, reason = gate("I think you should probably look that up.")
    assert retrieve is True
    assert query == "when am I meeting Alex?", "falls back to the raw message as the query"
    assert "failing open" in reason


def test_malformed_json_fails_open():
    retrieve, _query, reason = gate('{"retrieve": true, "query": ')
    assert retrieve is True
    assert "gate failed open" in reason


def test_an_api_error_fails_open_and_names_the_error_type():
    """A dead key, a 429, a network blip — none of them may silently turn memory
    off. Naming the exception type is what made a real outage debuggable."""

    class Boom:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_kw):
            raise TimeoutError("upstream took too long")

    retrieve, query, reason = should_retrieve(Boom(), "small-model", "who is Raj?")
    assert retrieve is True
    assert query == "who is Raj?"
    assert "gate failed open" in reason and "TimeoutError" in reason


@pytest.mark.parametrize("missing", [
    '{"reason": "no retrieve key"}',
    '{}',
])
def test_a_json_reply_missing_the_decision_reads_as_no(missing):
    """Distinct from failing open on purpose: this IS valid JSON, so the gate
    trusts it. bool(None) is False. Pinned so the difference between 'the model
    answered' and 'the model broke' stays deliberate rather than accidental."""
    retrieve, _query, _reason = gate(missing)
    assert retrieve is False


def test_a_missing_query_falls_back_to_the_whole_message():
    """A yes with no keywords must still search something useful."""
    retrieve, query, _ = gate('{"retrieve": true, "reason": "personal"}')
    assert retrieve is True
    assert query == "when am I meeting Alex?"


# ---------- the prompt itself


def test_the_gate_asks_for_json_only_and_formats_the_message_in():
    """The prompt is load-bearing: it is why the reply is parseable at all. If
    someone loosens it, the parser above starts failing open on every turn and
    the whole point of hero 1 (skip retrieval when it doesn't help) is lost."""
    filled = GATE_PROMPT.format(message="who is Raj?")
    assert "who is Raj?" in filled
    assert "ONLY this JSON" in filled
    assert '"retrieve"' in filled and '"query"' in filled and '"reason"' in filled


def test_the_gate_is_exactly_one_model_call():
    """Hero 1's whole pitch is 'one cheap call decides whether to pay for a
    search'. If it ever costs two calls, the economics stop making sense."""
    calls = []

    class Counting:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kw):
            calls.append(kw)
            return response([text_block('{"retrieve": false, "query": "", "reason": "math"}')])

    should_retrieve(Counting(), "small-model", "what is 2+2?")
    assert len(calls) == 1
    assert calls[0]["model"] == "small-model", "the gate must use the SMALL model, not the big one"
    assert calls[0]["max_tokens"] >= 600, "reasoning models need room before the JSON"
