"""DETERMINISTIC EVAL — delegate_task drives Claude Code and Codex, honestly.

Hermetic: claude/codex are NEVER spawned for real. The command builders are
asserted directly, and the stream parsers + full tool path run against fake CLIs
that emit the documented json schemas. What's pinned: the headless command
lines, the reply + usage + cost extraction, the honest per-agent failure
strings, and the per-agent transcript/ledger paper trail.
"""

from __future__ import annotations

import json
import sys

from waku.config import Settings
from waku.tools import experimental


def test_claude_cmd_is_headless_stream_json():
    cmd = experimental._claude_cmd("/fake/claude", Settings(home="."), "review this",
                                   "fable-5.1", "medium")
    assert cmd == ["/fake/claude", "-p", "review this",
                   "--output-format", "stream-json",
                   "--model", "fable-5.1", "--effort", "medium"]


def test_claude_cmd_omits_optional_model_and_effort():
    cmd = experimental._claude_cmd("/fake/claude", Settings(home="."), "t", "", "")
    assert "--model" not in cmd and "--effort" not in cmd


def test_codex_cmd_is_json_sandboxed():
    cmd = experimental._codex_cmd("/fake/codex", Settings(home="."), "fix tests",
                                  "astra", "")
    assert cmd[:3] == ["/fake/codex", "exec", "--json"]
    assert "workspace-write" in cmd and "--skip-git-repo-check" in cmd
    assert "-m" in cmd and "astra" in cmd and cmd[-1] == "fix tests"


def test_parse_claude_event_extracts_result_usage_cost():
    ev = {"type": "result", "result": "Built it.",
          "usage": {"input_tokens": 100, "output_tokens": 20},
          "total_cost_usd": 0.001}
    assert experimental._parse_claude_event(ev) == {
        "text": "Built it.", "usage": {"in": 100, "out": 20},
        "cost": 0.001, "turn_end": True}


def test_parse_claude_assistant_relays_delta_and_tool():
    ev = {"type": "assistant",
          "message": {"content": [{"type": "text", "text": "thinking"},
                                  {"type": "tool_use", "name": "Bash"}]}}
    upd = experimental._parse_claude_event(ev)
    assert upd["delta"] == "thinking" and upd["tools"] == ["Bash"]


def test_parse_codex_event_extracts_message_and_usage():
    msg = {"type": "item.completed",
           "item": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."},
                                {"type": "function_call", "name": "bash"}]}}
    upd = experimental._parse_codex_event(msg)
    assert upd["text"] == "Done." and upd["tools"] == ["bash"]
    usage = experimental._parse_codex_event(
        {"type": "turn_context", "usage": {"input_tokens": 30, "output_tokens": 5}})
    assert usage == {"usage": {"in": 30, "out": 5}}


def _install_fake(tmp_path, name, body):
    """Write `body` as an executable fake CLI (POSIX shebang / Windows .bat)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / f"{name}_impl.py"
    script.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        shim = bin_dir / f"{name}.bat"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return shim
    script.chmod(0o755)
    return script


FAKE_CLAUDE = '''#!/usr/bin/env python3
import json
for e in [
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "reviewing"},
        {"type": "tool_use", "name": "Bash"}]}},
    {"type": "result", "result": "Done. Built the thing.",
     "usage": {"input_tokens": 120, "output_tokens": 25},
     "total_cost_usd": 0.002},
]:
    print(json.dumps(e))
'''

FAKE_CODEX = '''#!/usr/bin/env python3
import json
for e in [
    {"type": "item.completed", "item": {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "Done. Fixed the tests."},
                 {"type": "function_call", "name": "bash"}]}},
    {"type": "turn_context", "usage": {"input_tokens": 90, "output_tokens": 15}},
]:
    print(json.dumps(e))
'''


def test_delegate_claude_streams_and_ledgers(tmp_path, monkeypatch):
    fake = _install_fake(tmp_path, "claude", FAKE_CLAUDE)
    monkeypatch.setattr(experimental.shutil, "which", lambda _: str(fake))
    home = tmp_path / "home"
    tool = experimental.make_delegate_tool(Settings(home=home))
    seen = []
    out = tool.fn(task="build it", agent="claude", model="fable-5.1", effort="medium",
                  _notify=lambda k, ev: seen.append((k, ev)))

    assert "Done. Built the thing." in out
    assert "sub-agent spend" in out
    kinds = [(k, ev.get("type")) for k, ev in seen]
    assert ("subagent", "tool") in kinds
    assert ("subagent", "turn_end") in kinds
    # the ledger is honest and names the sub-agent's own brain
    rec = json.loads((home / "usage.jsonl").read_text().strip().splitlines()[-1])
    assert rec["kind"] == "subagent" and rec["in"] == 120 and rec["out"] == 25
    assert rec["provider"] == "anthropic" and rec["model"] == "fable-5.1"


def test_delegate_codex_streams_and_ledgers(tmp_path, monkeypatch):
    fake = _install_fake(tmp_path, "codex", FAKE_CODEX)
    monkeypatch.setattr(experimental.shutil, "which", lambda _: str(fake))
    home = tmp_path / "home"
    tool = experimental.make_delegate_tool(Settings(home=home))
    seen = []
    out = tool.fn(task="fix the failing test", agent="codex", model="astra",
                  _notify=lambda k, ev: seen.append((k, ev)))

    assert "Done. Fixed the tests." in out
    kinds = [(k, ev.get("type")) for k, ev in seen]
    assert ("subagent", "tool") in kinds
    rec = json.loads((home / "usage.jsonl").read_text().strip().splitlines()[-1])
    assert rec["kind"] == "subagent" and rec["in"] == 90 and rec["out"] == 15
    assert rec["provider"] == "openai" and rec["model"] == "astra"


def test_delegate_unknown_agent(tmp_path, monkeypatch):
    tool = experimental.make_delegate_tool(Settings(home=tmp_path))
    out = tool.fn(task="anything", agent="gpt")
    assert "Unknown agent" in out and "claude" in out and "codex" in out


def test_delegate_claude_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(experimental.shutil, "which", lambda _: None)
    tool = experimental.make_delegate_tool(Settings(home=tmp_path))
    out = tool.fn(task="anything", agent="claude")
    assert experimental.CLAUDE_INSTALL_HINT in out and "isn't installed" in out


def test_delegate_pi_default_unchanged(tmp_path, monkeypatch):
    """`agent` defaults to pi, so the old call shape still means pi."""
    monkeypatch.setattr(experimental.shutil, "which", lambda _: "/fake/bin/pi")
    tool = experimental.make_delegate_tool(Settings(home=tmp_path))
    out = tool.fn(task="anything", cwd=str(tmp_path / "nope"))
    assert "doesn't exist" in out  # reached pi dispatch, not the unknown-agent path
