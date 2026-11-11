"""The first-run Waku Memory question (spec 020 A2): S1, S3, and the guards
around them. Offline: the sign-in is replaced, so no browser opens and nothing
reaches Waku Memory.
"""

from __future__ import annotations

import json
import sys

import pytest

from waku import memory_choice
from waku.config import Settings
from waku.ops import settings_api
from waku.tools import waku_memory


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway Waku home whose .env is the only place a choice is saved."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.delenv("WAKU_MEMORY_MODE", raising=False)
    monkeypatch.delenv("WAKU_MEMORY_PROMPT", raising=False)
    monkeypatch.chdir(tmp_path)
    signed_in = []
    monkeypatch.setattr(memory_choice, "connect", lambda h: signed_in.append(h) or "Connected.")
    yield tmp_path
    monkeypatch.delenv("WAKU_MEMORY_MODE", raising=False)


def _env(home) -> str:
    path = home / ".env"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_s1_a_fresh_home_is_asked_and_enter_means_yes(home):
    """S1: no saved choice shows the question, and an empty answer is yes."""
    said = []
    mode = memory_choice.ask(Settings(home=home, memory_mode=""), input_fn=lambda _: "", out=said.append)
    assert mode == "agent"
    assert "Your chat history and your model keys are not sent." in said[0]
    assert "WAKU_MEMORY_MODE=" in _env(home) and "agent" in _env(home)
    assert said[-1] == "Connected."


def test_s3_keep_local_only_opens_no_connection_and_is_not_asked_again(home, monkeypatch):
    """S3: the answer is saved as off, nothing signs in, and the next run is silent."""
    connected = []
    monkeypatch.setattr(memory_choice, "connect", lambda h: connected.append(h))
    said = []
    assert memory_choice.ask(Settings(home=home, memory_mode=""), input_fn=lambda _: "n", out=said.append) == "off"
    assert connected == []
    assert "off" in _env(home)
    assert "Your memory stays on this machine" in said[-1]
    assert memory_choice.ask(Settings(home=home, memory_mode="off"), input_fn=lambda _: "", out=said.append) is None


def test_a_person_who_already_connected_is_not_asked(home):
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True}]}), encoding="utf-8")
    assert memory_choice.pending(Settings(home=home, memory_mode="")) is False
    assert memory_choice.ask(Settings(home=home, memory_mode=""), input_fn=lambda _: "n", out=print) is None
    assert _env(home) == ""


def test_a_prompt_is_never_shown_without_a_person_to_answer(home, monkeypatch):
    """A pipe, a gateway, CI and a hosted container have no terminal."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert memory_choice.maybe_ask(Settings(home=home, memory_mode="")) is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("WAKU_MEMORY_PROMPT", "0")
    assert memory_choice.maybe_ask(Settings(home=home, memory_mode="")) is None
    assert _env(home) == ""


def test_hosted_provisioning_sets_hook_so_a_tenant_is_never_asked(home):
    assert memory_choice.pending(Settings(home=home, memory_mode="hook")) is False


def test_an_unrecognised_mode_cannot_be_saved(home):
    with pytest.raises(ValueError):
        memory_choice.save("always")
    assert _env(home) == ""


def test_the_dashboard_saves_the_answer_through_the_settings_route(home, monkeypatch):
    monkeypatch.setattr(memory_choice, "connect", lambda h: "Connected.")
    out = settings_api.apply_settings({"memory_mode": "agent"})
    assert out["ok"] and out["memory_message"] == "Connected." and out["memory_mode"] == "agent"
    assert settings_api.apply_settings({"memory_mode": "nonsense"})["ok"] is True   # not a choice: ignored
    assert memory_choice.card(Settings(home=home, memory_mode=""))["pending"] is True


def test_a_direct_connect_records_the_choice_and_leaves_a_chosen_mode_alone(home, monkeypatch):
    monkeypatch.setattr(waku_memory, "_connect", lambda h: "Connected.")
    monkeypatch.setattr(waku_memory, "status", lambda h: "connected · sean@example.com")
    assert waku_memory.connect(home) == "Connected."
    assert "agent" in _env(home)
    monkeypatch.setenv("WAKU_MEMORY_MODE", "hook")
    (home / ".env").write_text("", encoding="utf-8")
    waku_memory.connect(home)
    assert _env(home) == ""
