"""DETERMINISTIC EVAL -- the hosted agent's prompt is cacheable (spec 039 B3).

Every model call of a turn sent the same tools and system prompt again at
full price (F4). The current time was the second line of the system prompt,
so every byte after it changed once a minute (F6), and no prefix could be
cached. Now:

  1. The system prompt puts what does not change first (SOUL.md, the model
     line, the harness rules) and the time, the retrieved memory and the
     matched skills after it. `Session.stable_prefix` is that first part.
  2. On the hosted free tier (`waku-platform`), the loop sends the system
     prompt as two text blocks with one `cache_control` breakpoint at the end
     of the stable part. Anthropic renders the tools before the system
     prompt, so the tool schemas are in the cached prefix too.
  3. Every other provider gets the system prompt as one string, as before.
  4. Each `llm` event reports the call's cache read and cache write tokens.
"""

from __future__ import annotations

from types import SimpleNamespace

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.loop.agent import LIMIT_NOTE, run_loop
from waku.runtime.session import Session
from waku.tools.registry import Tool, ToolRegistry

BREAKPOINT = {"type": "ephemeral"}


class Recording(ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.calls: list[dict] = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return super()._create(**kwargs)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(name="search", description="search",
                           input_schema={"type": "object", "properties": {}},
                           fn=lambda **_: "found"))
    return registry


# --- 1. the order of the system prompt ------------------------------------------------


class _Memory:
    def gated_retrieve(self, message, notify=None):
        return "- Emily is Sean's designer."

    def matching_skills(self, message):
        return "Skill: weekly brief."


def test_the_time_and_the_memory_come_after_the_stable_prefix(tmp_path):
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    session = Session(app.settings, memory=_Memory())
    system = session.build_system("Who is Emily?")
    prefix = session.stable_prefix
    assert prefix and system.startswith(prefix)
    assert "You are Waku" in prefix
    assert "Your model:" in prefix
    for volatile in ("Right now it is", "Emily is Sean's designer.", "Skill: weekly brief."):
        assert volatile not in prefix, volatile
        assert volatile in system[len(prefix):], volatile


def test_the_stable_prefix_is_the_same_for_two_different_questions(tmp_path):
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    session = Session(app.settings, memory=_Memory())
    session.build_system("Who is Emily?")
    first = session.stable_prefix
    session.build_system("What is on my calendar tomorrow?")
    assert session.stable_prefix == first


# --- 2 and 3. the breakpoint ------------------------------------------------------------


def test_with_a_cache_prefix_the_loop_sends_two_blocks_and_one_breakpoint():
    client = Recording([response([tool_block("search", {}, "tu_1")], "tool_use"),
                        response([text_block("done")])])
    run_loop(client, "m", "Soul.\nRules.\nRight now it is 09:14.",
             [{"role": "user", "content": "x"}], _registry(), cache_prefix="Soul.\nRules.")
    assert len(client.calls) == 2
    for call in client.calls:
        assert call["system"] == [
            {"type": "text", "text": "Soul.\nRules.", "cache_control": BREAKPOINT},
            {"type": "text", "text": "\nRight now it is 09:14."}]
    assert client.calls[0]["tools"] == client.calls[1]["tools"]


def test_without_a_cache_prefix_the_system_prompt_stays_one_string():
    client = Recording([response([text_block("done")])])
    run_loop(client, "m", "Soul.", [{"role": "user", "content": "x"}], _registry())
    assert client.calls[0]["system"] == "Soul."


def test_a_prefix_the_prompt_does_not_start_with_is_not_cached():
    client = Recording([response([text_block("done")])])
    run_loop(client, "m", "Soul.", [{"role": "user", "content": "x"}], _registry(),
             cache_prefix="Another soul.")
    assert client.calls[0]["system"] == "Soul."


def test_the_tools_off_answer_after_the_limit_keeps_the_breakpoint():
    client = Recording([response([tool_block("search", {}, "tu_1")], "tool_use"),
                        response([text_block("from what I have")])])
    result = run_loop(client, "m", "Soul.\nnow", [{"role": "user", "content": "x"}],
                      _registry(), max_iterations=1, cache_prefix="Soul.")
    assert result.reply == "from what I have"
    final = client.calls[-1]["system"]
    assert final[0] == {"type": "text", "text": "Soul.", "cache_control": BREAKPOINT}
    assert final[-1] == {"type": "text", "text": f"\n\n{LIMIT_NOTE}"}


def _turn_calls(tmp_path, provider: str) -> list[dict]:
    client = Recording([
        response([text_block('{"retrieve": false, "query": "", "reason": "t"}')]),
        response([tool_block("save_note", {"subject": "Emily", "content": "designer"}, "tu_1")],
                 "tool_use"),
        response([text_block("Saved.")]),
    ])
    app = make_waku(tmp_path / "home", client=client, provider=provider)
    app.respond("remember Emily is a designer")
    return [c for c in client.calls if c.get("tools")]


def test_a_hosted_turn_caches_the_same_prefix_on_every_loop_call(tmp_path):
    calls = _turn_calls(tmp_path, "waku-platform")
    assert len(calls) == 2
    first = [c["system"][0] for c in calls]
    assert first[0] == first[1]
    assert first[0]["cache_control"] == BREAKPOINT
    assert "Right now it is" not in first[0]["text"]
    assert "Right now it is" in calls[0]["system"][1]["text"]


def test_a_laptop_turn_sends_one_string_as_before(tmp_path):
    calls = _turn_calls(tmp_path, "anthropic")
    assert all(isinstance(c["system"], str) for c in calls)


# --- 4. the llm event reports cache tokens ------------------------------------------------


def test_each_llm_event_reports_cache_read_and_cache_write_tokens():
    reply = response([text_block("done")])
    reply.usage = SimpleNamespace(input_tokens=500, output_tokens=40,
                                  cache_read_input_tokens=12000,
                                  cache_creation_input_tokens=300)
    events: list = []
    run_loop(ScriptedClient([reply]), "m", "Soul.", [{"role": "user", "content": "x"}],
             _registry(), observer=lambda kind, ev: events.append((kind, ev)))
    usage = next(ev["usage"] for kind, ev in events if kind == "llm")
    assert usage == {"in": 500, "out": 40, "cache_read": 12000, "cache_write": 300}


def test_a_provider_that_reports_no_cache_fields_reports_zero():
    events: list = []
    run_loop(ScriptedClient([response([text_block("done")])]), "m", "Soul.",
             [{"role": "user", "content": "x"}], _registry(),
             observer=lambda kind, ev: events.append((kind, ev)))
    usage = next(ev["usage"] for kind, ev in events if kind == "llm")
    assert usage == {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
