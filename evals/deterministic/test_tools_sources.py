"""DETERMINISTIC EVAL: the Tools page says where every ability comes from
(spec 030, decision 2).

Each tool in `/api/data` carries `from`: None for a tool that is built in, or
the card on the Connections page that controls it. Starter connections nobody
connected are listed once, so a person looking for a missing ability finds
the way.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from waku.ops import browser_agent, dashboard
from waku.tools import connectors

LINEAR = "https://mcp.linear.app/mcp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    from waku import integrations

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    for item in integrations.INTEGRATIONS:
        for field in item.env:
            monkeypatch.delenv(field.name, raising=False)
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    monkeypatch.setattr(browser_agent, "current", lambda: None)
    connectors.forget_sign_ins()
    return tmp_path


def _tool(name):
    return SimpleNamespace(name=name, description=f"what {name} does", mcp=None)


def _live(monkeypatch, names, connected=()):
    """A running agent whose registry holds these tools."""
    bridge = SimpleNamespace(connected=lambda name: name in connected)
    live = SimpleNamespace(mcp_bridge=bridge,
                           tools=SimpleNamespace(_tools={n: _tool(n) for n in names}))
    monkeypatch.setattr(browser_agent, "current", lambda: live)


def _from(tools):
    return {row["name"]: row["from"] for row in tools["catalog"]}


def test_a_tool_waku_ships_with_is_built_in(home):
    tools = dashboard.tools_info()

    sources = _from(tools)
    assert sources["create_event"] is None and sources["manage_memory"] is None
    assert sources["search_web"] is None, "without a Tavily key the search is Waku's own"
    json.dumps(tools)


def test_web_search_names_tavily_once_its_key_is_set(home, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")

    assert _from(dashboard.tools_info())["search_web"] == {"id": "tavily", "name": "Tavily"}


def test_a_connected_servers_tool_names_its_card(home, monkeypatch):
    connectors.add(home, "linear", url=LINEAR)
    connectors.add(home, "example", url="https://mcp.example.com/mcp")
    _live(monkeypatch, ["create_event", "linear_list_issues", "example_lookup"],
          connected=("linear", "example"))

    sources = _from(dashboard.tools_info())

    assert sources["linear_list_issues"] == {"id": "app-linear", "name": "Linear"}
    assert sources["example_lookup"] == {"id": "server-example", "name": "example"}
    assert sources["create_event"] is None


def test_a_tool_goes_to_the_server_with_the_longest_matching_name(home, monkeypatch):
    connectors.add(home, "git", url="https://git.example.com/mcp")
    connectors.add(home, "git_hub", url="https://hub.example.com/mcp")
    _live(monkeypatch, ["git_hub_list", "git_log"], connected=("git", "git_hub"))

    sources = _from(dashboard.tools_info())

    assert sources["git_hub_list"]["id"] == "server-git_hub"
    assert sources["git_log"]["id"] == "server-git"


def test_waku_memory_and_treg_tools_name_their_own_cards(home, monkeypatch):
    from waku.plugins import store

    monkeypatch.setattr(store, "servers", lambda home: [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True,
         "plugin": "waku"}])
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "treg", "url": "https://treg.to/mcp", "oauth": True}]}), encoding="utf-8")
    _live(monkeypatch, ["waku_memory_memory_search", "treg_search"],
          connected=("waku_memory", "treg"))

    sources = _from(dashboard.tools_info())

    assert sources["waku_memory_memory_search"] == {"id": "waku-memory", "name": "Waku Memory"}
    assert sources["treg_search"] == {"id": "treg", "name": "treg"}


def test_an_apple_tool_names_the_apple_tools_card(home, monkeypatch):
    _live(monkeypatch, ["read_apple_mail", "save_note"])

    sources = _from(dashboard.tools_info())

    assert sources["read_apple_mail"] == {"id": "apple_tools", "name": "Apple Tools"}
    assert sources["save_note"] is None


def test_every_source_is_a_card_that_is_on_the_connections_page(home, monkeypatch):
    """Acceptance 3: one click from Tools reaches the card."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    connectors.add(home, "linear", url=LINEAR)
    _live(monkeypatch, ["search_web", "linear_list_issues", "read_apple_mail"],
          connected=("linear",))

    page = dashboard.connections_page_data()
    on_page = {card["id"]: card["name"] for group in page["groups"]
               for card in group["cards"] + (group["more"] or {"cards": []})["cards"]}
    sources = [s for s in _from(dashboard.tools_info()).values() if s]

    assert len(sources) == 3
    for source in sources:
        assert on_page[source["id"]] == source["name"]


def test_starter_connections_nobody_connected_are_listed_once(home):
    tools = dashboard.tools_info()

    assert tools["not_connected"] == [{"id": "app-linear", "name": "Linear"},
                                      {"id": "app-notion", "name": "Notion"},
                                      {"id": "app-github", "name": "GitHub"}]

    connectors.add(home, "linear", url=LINEAR)

    assert [c["name"] for c in dashboard.tools_info()["not_connected"]] == ["Notion", "GitHub"]
