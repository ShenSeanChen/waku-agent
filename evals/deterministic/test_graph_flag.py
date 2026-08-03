"""The WAKU_GRAPH_WORKFLOWS flag — same toggle contract as WAKU_EXPERIMENTAL.

Clone of test_experimental_toggle.py's shape: the UI can render it, turning it
OFF is not swallowed, an explicit Settings value beats the env, and
load_settings reads the env var.
"""

from __future__ import annotations

from waku.config import Settings, load_settings


def test_settings_exposes_the_flag_so_the_ui_can_render_a_toggle(monkeypatch):
    from waku.ops import settings_api

    monkeypatch.delenv("WAKU_GRAPH_WORKFLOWS", raising=False)
    assert settings_api.settings_info()["graph_workflows"] is False
    monkeypatch.setenv("WAKU_GRAPH_WORKFLOWS", "1")
    assert settings_api.settings_info()["graph_workflows"] is True


def test_turning_it_off_is_not_swallowed():
    """settings_save must use `is not None` — "" means OFF, absent means
    don't touch. `if graph_workflows:` would make the toggle one-way."""
    def rule(payload: dict):
        graph_workflows = payload.get("graph_workflows")
        if graph_workflows is None:
            return None
        return "1" if str(graph_workflows).strip() else ""

    assert rule({"graph_workflows": "1"}) == "1"
    assert rule({"graph_workflows": ""}) == ""
    assert rule({}) is None


def test_an_explicit_setting_beats_the_global_env_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_GRAPH_WORKFLOWS", "1")
    assert Settings(home=tmp_path, graph_workflows=False).graph_workflows is False
    monkeypatch.delenv("WAKU_GRAPH_WORKFLOWS", raising=False)
    assert Settings(home=tmp_path, graph_workflows=True).graph_workflows is True


def test_env_var_is_what_load_settings_reads(monkeypatch):
    monkeypatch.delenv("WAKU_GRAPH_WORKFLOWS", raising=False)
    assert load_settings().graph_workflows is False
    monkeypatch.setenv("WAKU_GRAPH_WORKFLOWS", "1")
    assert load_settings().graph_workflows is True
