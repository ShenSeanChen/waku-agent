"""The first-run Waku Memory question (spec 020 A2): S1, S3, and the guards
around them. Offline: the sign-in is replaced, so no browser opens and nothing
reaches Waku Memory.
"""

from __future__ import annotations

import json
import os
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
    # spec 021: yes installs the Waku plugin; offline, the install is replaced
    monkeypatch.setattr(memory_choice, "install_plugin", lambda h: (True, "Installed the Waku plugin."))
    yield tmp_path
    # save() writes os.environ itself. A monkeypatch.delenv here would record
    # the saved value and put it back at teardown, leaking the choice into
    # every later test (a leaked "plugin" stops consolidation's sends).
    os.environ.pop("WAKU_MEMORY_MODE", None)


def _env(home) -> str:
    path = home / ".env"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_s1_a_fresh_home_is_asked_and_enter_means_yes(home):
    """S1: no saved choice shows the question, and an empty answer is yes."""
    said = []
    mode = memory_choice.ask(Settings(home=home, memory_mode=""), input_fn=lambda _: "", out=said.append)
    assert mode == "plugin"
    # spec 021: the sentence says what the plugin sends, and what it does not
    assert "sends each message you type and each reply" in said[0]
    assert "Your files, the commands Waku runs and your model keys are not sent." in said[0]
    assert "WAKU_MEMORY_MODE=" in _env(home) and "plugin" in _env(home)
    assert said[-1] == "Installed the Waku plugin."


def test_a_plugin_that_cannot_be_installed_falls_back_to_a_direct_connection(home, monkeypatch):
    monkeypatch.setattr(memory_choice, "install_plugin", lambda h: (False, "offline"))
    said = []
    memory_choice.ask(Settings(home=home, memory_mode=""), input_fn=lambda _: "", out=said.append)
    assert "agent" in _env(home) and "plugin" not in _env(home)
    assert said[-1].startswith("The Waku plugin could not be installed (offline)") and said[-1].endswith("Connected.")


def test_install_plugin_installs_from_the_marketplace_and_signs_in(tmp_path, monkeypatch):
    from evals.deterministic.plugin_fixtures import marketplace, official
    from waku.plugins import store
    from waku.tools import mcp_cli

    home = tmp_path / "home"
    official(monkeypatch, marketplace(tmp_path / "market"))
    signed = []

    def fake_sign_in(h, name):
        signed.append(name)
        (h / "mcp-auth").mkdir(parents=True, exist_ok=True)
        (h / "mcp-auth" / f"{name}.json").write_text("{}", encoding="utf-8")
        return True, "signed in"

    monkeypatch.setattr(mcp_cli, "sign_in", fake_sign_in)
    installed, message = memory_choice.install_plugin(home)
    assert installed and message.startswith("Installed the Waku plugin and connected")
    assert signed == ["waku_memory"] and "waku@waku-memory" in store.read(home)
    assert memory_choice.already_connected(home)


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


def test_hosted_provisioning_sets_hook_so_a_tenant_is_never_asked(home, monkeypatch):
    # spec 021: `hook` now reads as `agent`, which a tenant is never asked about either
    monkeypatch.setenv("WAKU_MEMORY_MODE", "hook")
    assert Settings(home=home).memory_mode == "agent"
    assert memory_choice.pending(Settings(home=home)) is False


def test_an_unrecognised_mode_cannot_be_saved(home):
    with pytest.raises(ValueError):
        memory_choice.save("always")
    assert _env(home) == ""


def test_the_dashboard_saves_the_answer_through_the_settings_route(home, monkeypatch):
    monkeypatch.setattr(memory_choice, "connect", lambda h: "Connected.")
    out = settings_api.apply_settings({"memory_mode": "plugin"})
    assert out["ok"] and out["memory_message"] == "Installed the Waku plugin." and out["memory_mode"] == "plugin"
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


def test_a_plugin_that_cannot_send_turns_is_not_recorded_as_plugin(tmp_path, monkeypatch):
    """Review fix 6: a plugin with no Stop hook on Waku Memory sends no turn,
    so a yes must fall back to agent. (Today's marketplace plugin, with no
    .waku-plugin manifest, is refused earlier by the first-run check.)"""
    from evals.deterministic.plugin_fixtures import marketplace, official
    from waku.plugins import store

    market = marketplace(tmp_path / "market")
    hooks = market / "plugins" / "waku" / "hooks" / "waku-agent.json"
    data = json.loads(hooks.read_text())
    del data["hooks"]["Stop"]                                        # no hook sends the turn
    hooks.write_text(json.dumps(data))
    official(monkeypatch, market)
    home = tmp_path / "home"
    monkeypatch.setattr(memory_choice, "connect", lambda h: "Connected.")
    installed, message = memory_choice.install_plugin(home)
    assert installed is False and "does not send Waku Agent's turns" in message
    assert store.read(home) == {}
    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    reply = memory_choice.apply("plugin", home)
    os.environ.pop("WAKU_MEMORY_MODE", None)
    assert reply.startswith("The Waku plugin could not be installed (the Waku plugin in the marketplace")
    saved = (home / ".env").read_text()
    assert "WAKU_MEMORY_MODE" in saved and "agent" in saved and "plugin" not in saved


@pytest.mark.parametrize("change, reason", [
    (lambda m: m["mcpServers"].update({"extra": {"url": "https://extra.example/mcp"}}), "more than Waku Memory"),
    (lambda m: m.update({"hooks": {"hooks": {"PreToolUse": [{"hooks": [
        {"type": "mcp_tool", "server": "waku_memory", "tool": "session.hook"}]}]}}}), "does not describe"),
])
def test_the_first_run_installs_only_a_plugin_its_sentence_describes(tmp_path, monkeypatch, change, reason):
    """Review fix 12: the first-run yes confirmed whatever the marketplace held."""
    from evals.deterministic.plugin_fixtures import marketplace, official
    from waku.plugins import store

    market = marketplace(tmp_path / "market")
    path = market / "plugins" / "waku" / ".waku-plugin" / "plugin.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
    official(monkeypatch, market)
    from waku.tools import mcp_cli

    monkeypatch.setattr(mcp_cli, "sign_in", lambda h, name: (True, ""))   # never a browser
    home = tmp_path / "home"
    installed, message = memory_choice.install_plugin(home)
    assert installed is False and reason in message and store.read(home) == {}


def test_todays_marketplace_plugin_falls_back_to_agent(tmp_path, monkeypatch):
    """The plugin as waku-one/waku-memory-plugins@main holds it today: Claude
    Code files only, its server named waku."""
    import shutil

    from evals.deterministic.plugin_fixtures import marketplace, official
    from waku.plugins import store

    market = marketplace(tmp_path / "market")
    shutil.rmtree(market / "plugins" / "waku" / ".waku-plugin")
    official(monkeypatch, market)
    installed, _ = memory_choice.install_plugin(tmp_path / "home")
    assert installed is False and store.read(tmp_path / "home") == {}
