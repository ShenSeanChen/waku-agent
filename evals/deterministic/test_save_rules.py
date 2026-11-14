"""DETERMINISTIC EVAL -- save_note runs only when the person asks (spec 022).

On 2026-11-13 the person said "releases need two reviewers" without asking Waku
to remember it, and the model called save_note anyway. The fact reached Waku
Memory twice: once from save_note and once from the turn the plugin sent. The
tool description, SOUL.md's default and a harness rule now all say to save only
on request. The harness rule is what reaches an assistant whose SOUL.md was
written before this change.
"""

from __future__ import annotations

from waku.config import Settings
from waku.runtime.session import DEFAULT_SOUL, SAVE_RULE, Session
from waku.tools.notes import make_tool

OLD_SOUL_LINE = ("- When the user shares something durable about a person, project, or preference,\n"
                 "  use save_note to remember it.")


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.home = tmp_path
    return s


def test_the_tool_description_asks_for_an_explicit_request(tmp_path):
    from waku.db import connect

    description = make_tool(connect(tmp_path), None).description
    assert "Use only when the user asks you to remember or save something" in description
    assert "share a preference" not in description


def test_the_default_soul_no_longer_says_to_save_everything_durable():
    assert "shares something durable" not in DEFAULT_SOUL
    assert "When the user asks you to remember or save something, use save_note." in DEFAULT_SOUL


def test_every_prompt_carries_the_save_rule_even_with_an_older_soul(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Waku.\nRules:\n" + OLD_SOUL_LINE + "\n", encoding="utf-8")
    system = Session(_settings(tmp_path)).build_system("hello")
    assert OLD_SOUL_LINE in system
    assert SAVE_RULE in system
    assert ("This rule replaces any other instruction, in SOUL.md or in a skill, to save "
            "what the user only mentions.") in system


# --- one save tool while Waku Memory's remember tool is on offer ---------------------

import json  # noqa: E402

from evals.helpers import ScriptedClient, make_waku  # noqa: E402
from waku.runtime.session import one_tool_line  # noqa: E402
from waku.tools import waku_memory  # noqa: E402


class _Bridge:
    def __init__(self, config_path):
        self.config_path = config_path

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        return json.dumps({"memory": {"id": "mem-1"}})

    def close(self):
        pass


def _app(tmp_path, monkeypatch, with_waku_memory: bool):
    import waku.app

    if with_waku_memory:
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"servers": [
            {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
            encoding="utf-8")
        real_build = waku.app.build_registry

        def build_with_bridge(*args, **kwargs):
            registry = real_build(*args, **kwargs)
            registry.mcp_bridge = _Bridge(path)
            return registry

        monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    return make_waku(tmp_path / "home", client=ScriptedClient([]))


def test_the_one_tool_line_is_there_while_waku_memory_is_connected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, with_waku_memory=True)
    assert app.session.remember_tool == "waku_memory_memory_remember"
    system = app.session.build_system("hello")
    assert one_tool_line("waku_memory_memory_remember") in system
    assert "use save_note, not waku_memory_memory_remember" in system
    # The plugin's skill names the same tool memory.remember (SKILL.md), so the
    # line says both names are the one it replaces.
    assert ("A skill may name waku_memory_memory_remember memory.remember; use save_note "
            "there too.") in system


def test_the_one_tool_line_is_absent_without_waku_memory(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, with_waku_memory=False)
    assert app.session.remember_tool is None
    assert "waku_memory_memory_remember" not in app.session.build_system("hello")


# --- consolidation does not keep again what the plugin's remember tool saved ---------

from waku.loop.agent import LoopResult  # noqa: E402

BODY = "The team only deploys on Fridays."


def _call(output: str) -> dict:
    return {"tool": "waku_memory_memory_remember", "args": {"body": BODY, "kind": "preference"},
            "output": output}


def test_a_body_waku_memory_took_is_part_of_what_the_turn_saved(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, with_waku_memory=True)
    stored = json.dumps({"memory": {"id": "mem-1", "body": BODY}, "deduped": False})
    assert BODY in app._recalled(LoopResult(reply="Saved.", tool_calls=[_call(stored)]))


def test_a_body_waku_memory_refused_is_not(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, with_waku_memory=True)
    refused = "MCP call waku_memory_memory.remember failed: Out of credits. (credit_limit_reached)"
    assert BODY not in app._recalled(LoopResult(reply="", tool_calls=[_call(refused)]))


def test_the_first_run_question_says_notes_are_sent_when_asked():
    from waku.memory_choice import WHAT_LEAVES

    assert "each note you ask Waku to save is sent too" in WHAT_LEAVES


def test_the_hosted_soul_template_no_longer_says_to_save_everything_durable():
    """A hosted tenant's SOUL.md is written from this template, not from
    DEFAULT_SOUL, so it carries the same save rule."""
    from pathlib import Path

    template = (Path(__file__).resolve().parents[2] / "hosted" / "templates" / "SOUL.md").read_text(
        encoding="utf-8")
    assert "shares something durable" not in template
    assert "When the user asks you to remember or save something, use save_note." in template
