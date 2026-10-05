"""DETERMINISTIC EVAL — `waku connect treg` puts treg in every local Waku Agent.

Spec 007 E. Before this, a laptop had treg only after a hand edit of
mcp.json and `waku mcp login treg`. These pin what the command does to that
file (add, keep what is there, add nothing twice, never overwrite a server
named treg that points somewhere else, which is what a hosted container's
relay entry is), that both doors reach it, and the three states the
Connections card shows. No browser opens: the sign-in is replaced by a stub.
"""

from __future__ import annotations

import json
import sys

import pytest

from waku.tools import mcp_cli, treg

TREG = {"name": "treg", "url": "https://treg.to/mcp/", "oauth": True}
RELAY = {"name": "treg", "url": "http://10.88.0.1:8788/treg/mcp/", "auth_env": "WAKU_PLATFORM_TOKEN"}


def _config(home):
    return json.loads((home / "mcp.json").read_text(encoding="utf-8"))


def _write(home, servers):
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")


def test_adds_treg_to_a_fresh_config(tmp_path):
    status, spec = treg.add_server(tmp_path)
    assert status == "added" and spec == TREG
    assert _config(tmp_path)["servers"] == [TREG]


def test_keeps_the_servers_already_there(tmp_path):
    memory = {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True}
    _write(tmp_path, [{"name": "fs", "command": "npx", "args": ["x"]}, memory])
    treg.add_server(tmp_path)
    servers = _config(tmp_path)["servers"]
    assert servers[:2] == [{"name": "fs", "command": "npx", "args": ["x"]}, memory]
    assert servers[2] == TREG


def test_running_it_twice_adds_nothing(tmp_path):
    treg.add_server(tmp_path)
    before = (tmp_path / "mcp.json").read_bytes()
    status, _ = treg.add_server(tmp_path)
    assert status == "present"
    assert (tmp_path / "mcp.json").read_bytes() == before


def test_a_hand_added_entry_under_another_name_counts(tmp_path):
    _write(tmp_path, [{"name": "data", "url": "https://treg.to/mcp", "oauth": True}])
    status, spec = treg.add_server(tmp_path)
    assert status == "present" and spec["name"] == "data"
    assert len(_config(tmp_path)["servers"]) == 1


def test_a_conflicting_treg_is_left_alone(tmp_path, monkeypatch):
    """A hosted container's relay entry is a server named treg pointing at the
    metering proxy. `/connect treg` there must not replace it."""
    monkeypatch.setattr(treg, "_has_mcp", lambda: True)
    _write(tmp_path, [RELAY])
    before = (tmp_path / "mcp.json").read_bytes()
    assert treg.add_server(tmp_path)[0] == "conflict"
    reply = treg.connect(tmp_path)
    assert "already points at" in reply
    assert (tmp_path / "mcp.json").read_bytes() == before


def test_without_the_mcp_extra_it_says_how_to_install_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(treg, "_has_mcp", lambda: False)
    reply = treg.connect(tmp_path)
    assert "waku-agent[mcp]" in reply
    assert not (tmp_path / "mcp.json").exists()


@pytest.fixture
def signing(monkeypatch, tmp_path):
    """The mcp extra present, the token file in tmp_path, sign-in stubbed."""
    monkeypatch.setattr(treg, "_has_mcp", lambda: True)
    token = tmp_path / "token.json"
    monkeypatch.setattr(mcp_cli, "_auth_file", lambda home, name: token)
    calls = []

    def stub(home, name):
        calls.append(name)
        token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
        return True, f"\n  {name} — someone"

    monkeypatch.setattr(mcp_cli, "sign_in", stub)
    return calls, token


def test_connect_adds_then_signs_in_once(signing, tmp_path):
    calls, _ = signing
    reply = treg.connect(tmp_path)
    assert calls == ["treg"]
    assert reply.startswith("Added treg") and "Connected to treg" in reply
    assert _config(tmp_path)["servers"] == [TREG]


def test_already_signed_in_opens_no_browser(signing, tmp_path):
    calls, token = signing
    treg.add_server(tmp_path)
    token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
    reply = treg.connect(tmp_path)
    assert calls == [], "a signed-in server must not send the user to the browser again"
    assert "already connected" in reply


def test_a_failed_sign_in_is_reported_not_hidden(monkeypatch, tmp_path):
    monkeypatch.setattr(treg, "_has_mcp", lambda: True)
    monkeypatch.setattr(mcp_cli, "_auth_file", lambda home, name: tmp_path / "none.json")
    monkeypatch.setattr(mcp_cli, "sign_in",
                        lambda home, name: (False, "\n  Sign-in did not complete — 'treg' has no token."))
    reply = treg.connect(tmp_path)
    assert "Sign-in did not complete" in reply and "Connected" not in reply


def test_the_card_shows_each_state(signing, tmp_path):
    _, token = signing
    assert treg.card(tmp_path)["state"] == "not_added"
    treg.add_server(tmp_path)
    assert treg.card(tmp_path)["state"] == "not_signed_in"
    token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
    assert treg.card(tmp_path)["state"] == "connected"


def test_a_hosted_relay_reads_as_connected(tmp_path):
    _write(tmp_path, [RELAY])
    assert treg.state(tmp_path)[0] == "connected"


def test_a_broken_config_does_not_break_the_page(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")
    assert treg.card(tmp_path)["state"] == "not_added"


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(treg, "connect", lambda home: calls.append(home) or "stub: treg connected")
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    return calls


def test_cli_connect_treg_reaches_the_sign_in(stubbed, monkeypatch, capsys):
    from waku.__main__ import main

    monkeypatch.setattr(sys, "argv", ["waku", "connect", "treg"])
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 0
    assert stubbed, "`waku connect treg` never reached treg.connect"
    assert "stub: treg connected" in capsys.readouterr().out


def test_chat_connect_treg_runs_in_the_dashboard(stubbed):
    from waku.ops import commands, dashboard

    events = []
    dashboard._run_command(commands.parse("/connect treg"), lambda kind, ev: events.append((kind, ev)))
    assert stubbed, "`/connect treg` in the chat never reached treg.connect"
    kind, ev = events[-1]
    assert kind == "done" and "stub: treg connected" in ev["reply"]


def test_the_connections_page_and_first_run_offer_treg():
    """The page reads `mcp_connections` from /api/data, and its Connect button
    sends `/connect <key>` through the chat. The first-run screen offers treg
    next to Waku Memory."""
    import inspect
    from pathlib import Path

    from waku.ops import dashboard

    assert '"mcp_connections": [treg.card(home)]' in inspect.getsource(dashboard.collect)
    static = Path(dashboard.__file__).parent / "static"
    views = (static / "js" / "views.js").read_text(encoding="utf-8")
    assert "d.mcp_connections" in views and "`/connect ${key}`" in views
    setup = (static / "js" / "setup.js").read_text(encoding="utf-8")
    assert "/connect waku-memory" in setup and "/connect treg" in setup
