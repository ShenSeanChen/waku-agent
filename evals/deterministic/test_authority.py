"""DETERMINISTIC EVAL — the authority ladder, enforced at the tool gate.

Hermetic, no model, no network. What's pinned: the ladder's ordering, the
per-tool levels, the gate's allow/deny decisions (including the owner-only
`irreversible` set), the ToolRegistry integration, and the project.toml bridge.
"""

from __future__ import annotations

from waku.tools.authority import LEVELS, AuthorityGate, gate_from_project
from waku.tools.registry import Tool, ToolRegistry


def test_levels_are_ordered_least_to_most_authority():
    assert [LEVELS.index(l) for l in LEVELS] == list(range(len(LEVELS)))
    assert LEVELS.index("observe") < LEVELS.index("irreversible")


def test_observe_tool_is_allowed_at_any_level():
    assert AuthorityGate(level="observe").allow("search_web")[0] is True
    assert AuthorityGate(level="irreversible").allow("search_web")[0] is True


def test_sandbox_tool_denied_below_sandbox_and_allowed_at_sandbox():
    gate = AuthorityGate(level="recommend")
    ok, why = gate.allow("delegate_task")
    assert ok is False and "sandbox" in why
    assert AuthorityGate(level="sandbox").allow("delegate_task")[0] is True


def test_irreversible_tool_is_denied_even_at_top_level():
    gate = AuthorityGate(level="irreversible", irreversible={"settle"})
    ok, why = gate.allow("settle")
    assert ok is False and "owner approval" in why


def test_unlisted_tool_defaults_to_recommend():
    assert AuthorityGate(level="plan").allow("some_new_tool")[0] is True
    assert AuthorityGate(level="observe").allow("some_new_tool")[0] is False


def test_registry_gate_refuses_before_running():
    called = []

    def fn(**kwargs):
        called.append(True)
        return "ran"

    reg = ToolRegistry(gate=AuthorityGate(level="observe"))
    reg.register(Tool(name="delegate_task", description="", input_schema={}, fn=fn))
    out = reg.execute("delegate_task", {})
    assert out.startswith("Refused")
    assert called == []  # the tool never ran


def test_registry_without_gate_is_ungated():
    reg = ToolRegistry()
    reg.register(Tool(name="x", description="", input_schema={}, fn=lambda **k: "ran"))
    assert reg.execute("x", {}) == "ran"


def test_gate_from_project_reads_the_authority_table(tmp_path):
    (tmp_path / "project.toml").write_text(
        '[authority]\ndefault = "sandbox"\nirreversible = ["settle"]\n', encoding="utf-8")
    gate = gate_from_project(tmp_path)
    assert gate is not None
    assert gate.level == "sandbox" and "settle" in gate.irreversible


def test_gate_from_project_is_none_without_a_harness(tmp_path):
    assert gate_from_project(tmp_path) is None


def test_waku_harness_binds_the_gate(tmp_path):
    from evals.helpers import ScriptedClient, make_waku

    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "project.toml").write_text(
        '[authority]\ndefault = "observe"\n', encoding="utf-8")
    app = make_waku(tmp_path / "home", client=ScriptedClient([]), harness=str(harness))
    assert app.tools.gate is not None
    assert app.tools.gate.allow("delegate_task")[0] is False   # observe < sandbox


def test_no_harness_means_ungated(tmp_path):
    from evals.helpers import ScriptedClient, make_waku

    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    assert app.tools.gate is None


def test_authority_to_sandbox_maps_thinking_rungs_to_read_only():
    from waku.tools.authority import authority_to_sandbox

    assert authority_to_sandbox("observe") == "read-only"
    assert authority_to_sandbox("recommend") == "read-only"
    assert authority_to_sandbox("plan") == "read-only"


def test_authority_to_sandbox_gates_the_top_rungs():
    from waku.tools.authority import authority_to_sandbox

    assert authority_to_sandbox("sandbox") == "workspace-write"
    assert authority_to_sandbox("reversible") is None
    assert authority_to_sandbox("irreversible") is None
