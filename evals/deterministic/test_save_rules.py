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
    assert "This replaces any older instruction to save everything durable." in system
