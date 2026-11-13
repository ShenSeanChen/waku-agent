"""WAKU_MEMORY_MODE (spec 020 A1): which path Waku Memory takes, and the two
behaviours that depend on it. Offline: no Waku Memory server and no `mcp`
package is needed, because the bridge is a stand-in that records how it was built.
"""

from __future__ import annotations

import json
import sys
import types
from typing import ClassVar

import pytest

from waku.config import Settings
from waku.db import connect
from waku.tools import build_registry
from waku.tools.waku_memory import consolidation_every, is_waku_memory


@pytest.mark.parametrize("raw, expected", [
    ("", ""), ("agent", "agent"), ("HOOK", "agent"), (" off ", "off"), ("plugin", "plugin"),
    ("sometimes", ""),   # a typo is "not chosen", never a silent "off"
])
def test_memory_mode_reads_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("WAKU_MEMORY_MODE", raw)
    assert Settings().memory_mode == expected


def test_is_waku_memory_matches_by_name_or_url():
    assert is_waku_memory({"name": "waku_memory", "url": "https://elsewhere/mcp"})
    assert is_waku_memory({"name": "mine", "url": "https://api.waku.one/mcp/"})
    assert not is_waku_memory({"name": "fs", "command": "npx"})
    assert not is_waku_memory({"name": "treg", "url": "https://treg.to/mcp/"})


class _FakeBridge:
    built: ClassVar[list] = []

    def __init__(self, config_path, skip=None, **kwargs):
        self.config_path, self.skip = config_path, skip
        type(self).built.append(self)

    def start(self):
        return []


def _registry(tmp_path, monkeypatch, mode):
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True},
        {"name": "fs", "command": "npx"}]}), encoding="utf-8")
    fake = types.ModuleType("waku.tools.mcp_client")
    fake.MCPBridge = _FakeBridge
    monkeypatch.setitem(sys.modules, "waku.tools.mcp_client", fake)
    _FakeBridge.built = []
    build_registry(connect(tmp_path), Settings(home=tmp_path, memory_mode=mode))
    return _FakeBridge.built[0]


def test_off_leaves_only_waku_memory_unconnected(tmp_path, monkeypatch):
    bridge = _registry(tmp_path, monkeypatch, "off")
    assert bridge.skip is not None
    assert bridge.skip({"name": "waku_memory"}) is True
    assert bridge.skip({"name": "fs", "command": "npx"}) is False


@pytest.mark.parametrize("mode", ["", "agent", "plugin"])
def test_every_other_mode_connects_everything_in_mcp_json(tmp_path, monkeypatch, mode):
    assert _registry(tmp_path, monkeypatch, mode).skip is None


def test_a_connected_choice_consolidates_every_turn(monkeypatch):
    monkeypatch.delenv("WAKU_CONSOLIDATE_EVERY", raising=False)
    chose = Settings(memory_mode="agent")
    assert consolidation_every(chose, connected=True) == 1
    # spec 021: under "plugin" the server extracts each turn, so this agent
    # consolidates locally at its ordinary cadence; a batch with a paused turn
    # is due at once (amendment E2, test_plugin_capture_state.py)
    assert consolidation_every(Settings(memory_mode="plugin"), connected=True) == 6
    assert consolidation_every(chose, connected=False) == 6          # sign-in failed: stay local
    assert consolidation_every(Settings(memory_mode="off"), connected=True) == 6
    assert consolidation_every(Settings(memory_mode=""), connected=True) == 6   # no choice yet


def test_an_explicit_cadence_always_wins(monkeypatch):
    monkeypatch.setenv("WAKU_CONSOLIDATE_EVERY", "4")
    assert consolidation_every(Settings(memory_mode="agent"), connected=True) == 4


def test_mcp_is_a_default_dependency_and_its_extra_still_works():
    """AGENTS.md rule 3's one exception (spec 020): a fresh install can sign in
    to Waku Memory with no second install command. The extra stays, because
    `pip install 'waku-agent[mcp]'` and the tenant image's `--extra mcp` name it."""
    import tomllib
    from pathlib import Path

    project = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert any(d.split(">=")[0] == "mcp" for d in project["dependencies"])
    assert "mcp" in project["optional-dependencies"]
    defaults = sorted(d.split(">=")[0] for d in project["dependencies"])
    assert defaults == ["anthropic", "mcp", "openai", "python-dotenv", "rich"], defaults
