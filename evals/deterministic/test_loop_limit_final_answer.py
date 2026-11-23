"""DETERMINISTIC EVAL -- the loop's iteration limit no longer throws the turn away.

Sean's hosted turn, 2026-10-05: "research the top 5 videos' comments of Sean's
AI Stories YouTube Channel and give me a comprehensive analysis". Nine loops
guessed at the channel by name (a 500, two 400s, then a hit), the tenth pulled
comments for five videos, and run_loop hit max_iterations. The reply was the
fixed "(I hit my iteration limit ...)" string: 255k tokens in, $0.54, and every
comment it had gathered thrown away.

Three fixes, one test group each:
  1. at the limit the loop makes ONE more call with tools off, and its text is
     the reply (waku/loop/agent.py); if that call fails, the old message;
  2. a hosted tenant gets fifteen steps (hosted/spawner/template.py), and so
     does a laptop by default (waku/config.py);
  3. the prompt tells the model to stop guessing after two failed lookups of
     the same target and use or ask for the exact URL (waku/runtime/session.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evals.helpers import ScriptedClient, response, text_block, tool_block
from waku.loop.agent import LIMIT_NOTE, LIMIT_REPLY, run_loop
from waku.loop.models import OpenAICompatClient
from waku.ops import observability as obs
from waku.tools.registry import Tool, ToolRegistry

ASK = ("research the top 5 videos' comments of Sean's AI Stories YouTube Channel and give "
       "me a comprehensive analysis on what works well for my audience")


class RecordingClient(ScriptedClient):
    """The scripted model, which also keeps every call's arguments."""

    def __init__(self, script):
        super().__init__(script)
        self.calls: list[dict] = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(name="treg_call", description="treg",
                           input_schema={"type": "object", "properties": {}},
                           fn=lambda **_: json.dumps({"comments": ["love the whiteboards"]})))
    return registry


def _runaway(n: int) -> list:
    return [response([tool_block("treg_call", {}, f"tu_{i}")], "tool_use") for i in range(n)]


def _run(final, max_iterations: int = 3, trim=None):
    client = RecordingClient(_runaway(max_iterations) + [final])
    events: list[tuple[str, dict]] = []
    result = run_loop(client, "m", "Soul.", [{"role": "user", "content": ASK}], _registry(),
                      max_iterations=max_iterations, observer=lambda k, e: events.append((k, e)),
                      trim=trim)
    return client, events, result


# --- 1. one tools-off call at the limit ----------------------------------------------


def test_the_limit_makes_exactly_one_more_call_with_tools_off_and_its_text_is_the_reply():
    """before: the reply was LIMIT_REPLY and the gathered comments were lost."""
    answer = "What works: the whiteboards. Missing: one video's comments failed (400)."
    client, _, result = _run(response([text_block(answer)]))

    assert len(client.calls) == 3 + 1, "exactly one call after the three loops"
    final = client.calls[-1]
    assert final["tool_choice"] == {"type": "none"}
    assert LIMIT_NOTE in final["system"] and final["system"].startswith("Soul.")
    for call in client.calls[:-1]:
        assert "tool_choice" not in call, "the loop's own calls are unchanged"
    # the final call reads everything the turn gathered
    assert sum(1 for m in final["messages"] if m["role"] == "user"
               and isinstance(m["content"], list)) == 3

    assert result.reply == answer
    assert result.limit_reached is True and result.iterations == 3


def test_the_final_call_is_flagged_and_counted_in_the_trace():
    _, events, _ = _run(response([text_block("Answer.")]))
    llm = [e for k, e in events if k == "llm"]
    assert len(llm) == 4, "token accounting sees the final call too"
    assert llm[-1]["final_answer"] is True and llm[-1]["kind"] == "final"
    assert not any(e.get("final_answer") for e in llm[:-1])


def test_a_natural_end_makes_no_extra_call_and_sets_no_flag():
    client = RecordingClient([response([text_block("Paris.")])])
    result = run_loop(client, "m", "Soul.", [{"role": "user", "content": "hi"}], _registry())
    assert len(client.calls) == 1 and result.reply == "Paris." and result.limit_reached is False


def test_the_trim_hook_runs_before_the_final_call():
    seen: list[int] = []
    _run(response([text_block("Answer.")]), trim=lambda messages: seen.append(len(messages)))
    # before loops 2 and 3, and once more before the final call
    assert len(seen) == 3


@pytest.mark.parametrize("final", [
    RuntimeError("provider down"),
    response([tool_block("treg_call", {}, "tu_x")], "tool_use"),   # no text at all
], ids=["call-fails", "no-text"])
def test_when_the_final_call_fails_the_reply_is_the_old_message(final):
    client, _, result = _run(final)
    assert len(client.calls) == 4
    assert result.reply == LIMIT_REPLY and "iteration limit" in result.reply
    assert result.limit_reached is True


def test_the_openai_adapter_sends_tool_choice_none():
    """The loop speaks Anthropic's shape; the OpenAI-wire providers get "none"."""
    adapter = OpenAICompatClient.__new__(OpenAICompatClient)
    tools = _registry().schemas()
    kwargs = adapter._to_openai(model="m", messages=[{"role": "user", "content": "x"}],
                                max_tokens=10, tools=tools, tool_choice={"type": "none"})
    assert kwargs["tool_choice"] == "none" and kwargs["tools"]
    plain = adapter._to_openai(model="m", messages=[{"role": "user", "content": "x"}],
                               max_tokens=10, tools=tools)
    assert "tool_choice" not in plain


def test_the_waterfall_story_says_limit_reached_then_final_answer():
    t0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    at = lambda s: (t0 + timedelta(seconds=s)).isoformat()  # noqa: E731
    loop = lambda s: {"type": "llm", "kind": "loop", "stop_reason": "tool_use",  # noqa: E731
                      "usage": {"in": 100, "out": 10}, "ts": at(s)}
    events = [
        {"type": "turn_start", "turn_id": "t_yt", "user_message": ASK, "ts": at(0)},
        loop(1), loop(2),
        {"type": "llm", "kind": "final", "final_answer": True, "stop_reason": "end_turn",
         "usage": {"in": 100, "out": 500}, "ts": at(3)},
        {"type": "turn_end", "turn_id": "t_yt", "reply": "Answer.", "iterations": 2, "ts": at(3)},
    ]
    turn = obs.build_turn(obs.group_turns(events)[0], provider="anthropic", model="m")
    texts = [p["text"] for p in turn["story"]]
    assert texts == ["question", "2 loops", "limit reached → final answer"]
    part = turn["story"][-1]
    assert turn["steps"][part["step"]]["final_answer"] is True


# --- 2. fifteen steps -----------------------------------------------------------------


# The hosted half, WAKU_MAX_ITERATIONS=15 in every tenant container's
# environment, is pinned in hosted/test_container_template.py beside the rest
# of that environment.


def test_the_laptop_default_is_fifteen_too(monkeypatch):
    from waku.config import Settings

    monkeypatch.delenv("WAKU_MAX_ITERATIONS", raising=False)
    assert Settings().max_iterations == 15


# --- 3. stop guessing, ask for the URL --------------------------------------------------


def test_the_prompt_says_to_stop_guessing_after_two_failed_lookups(tmp_path):
    from waku.config import Settings
    from waku.runtime.session import Session

    system = Session(Settings(home=tmp_path)).build_system(ASK)
    rule = next(line for line in system.splitlines() if "fails twice" in line)
    assert "stop guessing" in rule and "URL" in rule and "ask" in rule


def test_the_rule_reaches_an_assistant_with_an_old_soul(tmp_path):
    """SOUL.md is written once; a rule there would never reach a tenant that
    already has one. This one is the harness's, so it does."""
    from waku.config import Settings
    from waku.runtime.session import Session

    (tmp_path / "SOUL.md").write_text("You are Waku. Old soul.", encoding="utf-8")
    assert "fails twice" in Session(Settings(home=tmp_path)).build_system("hi")
