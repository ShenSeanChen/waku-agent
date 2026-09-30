"""DETERMINISTIC EVAL — `waku connect waku-memory` sets up Waku Memory safely.

Before this command, connecting meant hand-writing .waku/mcp.json from the
README, and the README showed an address that Waku Memory had retired: anyone
who followed it got a server that refused them. These tests pin what the
command does to that file (add, keep what is there, repair the retired
address, never overwrite a deliberate one) and that both doors reach it. No
browser opens: the sign-in is replaced by a stub.
"""

from __future__ import annotations

import json
import sys

import pytest

from waku.tools import mcp_cli, waku_memory


def _config(home):
    return json.loads((home / "mcp.json").read_text(encoding="utf-8"))


def _write(home, servers):
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")


def test_adds_waku_memory_to_a_fresh_config(tmp_path):
    status, spec = waku_memory.add_server(tmp_path)
    assert status == "added"
    assert _config(tmp_path)["servers"] == [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True}]
    assert spec["name"] == "waku_memory"


def test_keeps_the_servers_already_there(tmp_path):
    _write(tmp_path, [{"name": "fs", "command": "npx", "args": ["x"]}])
    waku_memory.add_server(tmp_path)
    servers = _config(tmp_path)["servers"]
    assert servers[0] == {"name": "fs", "command": "npx", "args": ["x"]}
    assert [s["name"] for s in servers] == ["fs", "waku_memory"]


def test_running_it_twice_adds_nothing(tmp_path):
    waku_memory.add_server(tmp_path)
    status, _ = waku_memory.add_server(tmp_path)
    assert status == "present"
    assert len(_config(tmp_path)["servers"]) == 1


def test_a_config_copied_from_the_old_readme_is_moved_to_the_current_address(tmp_path):
    _write(tmp_path, [{"name": "waku_memory", "url": "https://d1o2fv4416yi84.cloudfront.net/mcp",
                       "oauth": True}])
    status, _ = waku_memory.add_server(tmp_path)
    assert status == "moved"
    assert _config(tmp_path)["servers"][0]["url"] == "https://api.waku.one/mcp"


def test_a_deliberate_other_address_is_left_alone(tmp_path):
    _write(tmp_path, [{"name": "waku_memory", "url": "http://127.0.0.1:9000/mcp", "oauth": True}])
    status, _ = waku_memory.add_server(tmp_path)
    assert status == "conflict"
    assert _config(tmp_path)["servers"][0]["url"] == "http://127.0.0.1:9000/mcp"


def test_without_the_mcp_extra_it_says_how_to_install_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(waku_memory, "_has_mcp", lambda: False)
    reply = waku_memory.connect(tmp_path)
    assert "waku-agent[mcp]" in reply
    assert not (tmp_path / "mcp.json").exists()


@pytest.fixture
def signing(monkeypatch, tmp_path):
    """The mcp extra present, the token file in tmp_path, sign-in stubbed."""
    monkeypatch.setattr(waku_memory, "_has_mcp", lambda: True)
    token = tmp_path / "token.json"
    monkeypatch.setattr(mcp_cli, "_auth_file", lambda home, name: token)
    calls = []

    def stub(home, name, ok=True):
        calls.append(name)
        token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
        return True, f"\n  {name} — someone"

    monkeypatch.setattr(mcp_cli, "sign_in", stub)
    return calls, token


def test_connect_signs_in_once_and_says_what_happened(signing, tmp_path):
    calls, _ = signing
    reply = waku_memory.connect(tmp_path)
    assert calls == ["waku_memory"]
    assert reply.startswith("Added Waku Memory") and "Connected to Waku Memory" in reply
    assert "https://www.waku.one/docs" in reply


def test_already_signed_in_opens_no_browser(signing, tmp_path):
    calls, token = signing
    waku_memory.add_server(tmp_path)
    token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
    reply = waku_memory.connect(tmp_path)
    assert calls == [], "a signed-in server must not send the user to the browser again"
    assert "already connected" in reply


def test_a_failed_sign_in_is_reported_not_hidden(monkeypatch, tmp_path):
    monkeypatch.setattr(waku_memory, "_has_mcp", lambda: True)
    monkeypatch.setattr(mcp_cli, "_auth_file", lambda home, name: tmp_path / "none.json")
    monkeypatch.setattr(mcp_cli, "sign_in",
                        lambda home, name: (False, "\n  Sign-in did not complete — 'waku_memory' has no token."))
    reply = waku_memory.connect(tmp_path)
    assert "Sign-in did not complete" in reply and "Connected" not in reply


def test_an_api_key_setup_needs_no_browser(signing, tmp_path):
    calls, _ = signing
    _write(tmp_path, [{"name": "waku_memory", "url": "https://api.waku.one/mcp",
                       "auth_env": "WAKU_MEMORY_API_KEY"}])
    reply = waku_memory.connect(tmp_path)
    assert calls == []
    assert "$WAKU_MEMORY_API_KEY" in reply


def test_status_says_how_to_connect_when_nothing_is_set_up(tmp_path):
    assert waku_memory.status(tmp_path) == "not connected · run: waku connect waku-memory"


def test_status_names_the_account_once_signed_in(signing, tmp_path):
    _, token = signing
    waku_memory.add_server(tmp_path)
    assert waku_memory.status(tmp_path).startswith("configured · not signed in")
    token.write_text(json.dumps({"tokens": {"access_token": "x"}}), encoding="utf-8")
    assert waku_memory.status(tmp_path).startswith("connected · ")


def test_waku_connections_lists_waku_memory(monkeypatch, tmp_path, capsys):
    from waku import integrations

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    integrations._HEALTH = None
    integrations.cli_main()
    out = capsys.readouterr().out
    assert "Waku Memory" in out and "waku connect waku-memory" in out


@pytest.fixture
def connector(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(waku_memory, "connect", lambda home: calls.append(home) or "stub: connected")
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    return calls


def test_cli_door_reaches_the_connector(connector, monkeypatch, capsys):
    from waku.__main__ import main

    monkeypatch.setattr(sys, "argv", ["waku", "connect", "waku-memory"])
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 0 and connector
    assert "stub: connected" in capsys.readouterr().out


def test_chat_door_reaches_the_connector(connector):
    from waku.ops import commands, dashboard

    events = []
    dashboard._run_command(commands.parse("/connect waku-memory"), lambda kind, ev: events.append((kind, ev)))
    assert connector, "`/connect waku-memory` in the chat never reached the connector"
    assert events[-1][1]["reply"] == "stub: connected"
