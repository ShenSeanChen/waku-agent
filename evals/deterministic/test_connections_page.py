"""DETERMINISTIC EVAL: the Connections page is the one place a person
connects anything (spec 030).

The dashboard's JavaScript has no test runner, so the page is composed in
Python (`waku/ops/connections_page.py`) and the script only draws what it is
handed. These tests read the composed page: which group a card is in, what it
is called, what state it shows and what its one button does.
"""

from __future__ import annotations

import json

import pytest

from waku.ops import connections_page as cp
from waku.tools import connectors

LINEAR = "https://mcp.linear.app/mcp"
NOTION = "https://mcp.notion.com/mcp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home on a person's own machine, with no key of any kind."""
    from waku import integrations

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    for item in integrations.INTEGRATIONS:
        for field in item.env:
            monkeypatch.delenv(field.name, raising=False)
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    connectors.forget_sign_ins()
    return tmp_path


def _page(**changes):
    from waku.ops import dashboard

    return dashboard.connections_page_data(**changes)


def _cards(page):
    out = []
    for group in page["groups"]:
        out += group["cards"]
        if group["more"]:
            out += group["more"]["cards"]
    return out + page["developers"]["cards"]


def _group(page, group_id):
    return next(group for group in page["groups"] if group["id"] == group_id)


def _card(page, card_id):
    return next(card for card in _cards(page) if card["id"] == card_id)


def test_the_page_has_three_groups_named_by_what_a_person_does(home):
    page = _page()

    assert [(g["id"], g["title"]) for g in page["groups"]] == [
        ("talk", "Where you talk to Waku"),
        ("apps", "Apps Waku can use"),
        ("memory", "Where memory is kept"),
    ]
    json.dumps(page)


def test_each_group_holds_the_cards_the_spec_names(home):
    page = _page()

    assert [c["id"] for c in _group(page, "talk")["cards"]] == ["slack", "telegram", "discord",
                                                                 "whatsapp"]
    apps = [c["id"] for c in _group(page, "apps")["cards"]]
    assert apps[:3] == ["app-linear", "app-notion", "app-github"], "the starter list comes first"
    assert set(apps) == {"app-linear", "app-notion", "app-github", "google_calendar",
                         "apple_calendar", "apple_tools", "tavily", "treg"}
    assert [c["id"] for c in page["developers"]["cards"]] == ["otel"]
    memory = _group(page, "memory")
    assert [c["id"] for c in memory["cards"]] == ["waku-memory", "notion", "typesafe"]
    assert memory["more"]["title"] == "Memory stores to compare"
    assert [c["id"] for c in memory["more"]["cards"]] == ["mem0", "zep", "langmem", "supabase"]
    assert _group(page, "talk")["more"] is None and _group(page, "apps")["more"] is None


def test_no_two_cards_share_a_name_and_notion_is_there_twice(home):
    """Acceptance 1."""
    page = _page()
    names = [card["name"] for card in _cards(page)]

    assert len(names) == len({name.casefold() for name in names}), names
    assert _card(page, "app-notion")["name"] == "Notion"
    assert _card(page, "notion")["name"] == "Notion database (memory store)"


def test_a_server_named_like_another_card_still_gets_a_name_of_its_own(home):
    connectors.add(home, "telegram", url="https://mcp.example.com/mcp")

    page = _page()
    names = [card["name"] for card in _cards(page)]

    assert len(names) == len({name.casefold() for name in names}), names
    assert _card(page, "server-telegram")["name"] == "telegram (added by address)"


def test_every_card_has_a_name_a_state_one_line_and_at_most_one_button(home):
    """Decision 3."""
    for card in _cards(_page()):
        assert set(card) == {"id", "name", "logo", "state", "label", "what", "why", "action"}
        assert card["name"] and card["what"], card["id"]
        assert (card["state"], card["label"]) in cp.PAIRS, card["id"]
        action = card["action"]
        assert action is None or (set(action) == {"kind", "label", "target"}
                                  and action["kind"] in cp.ACTIONS and action["label"]), card


def test_a_cards_drawing_is_a_file_the_dashboard_serves(home):
    from pathlib import Path

    static = Path(cp.__file__).resolve().parent / "static"
    cards = _cards(_page())

    for card in cards:
        assert card["logo"] == "" or (static / card["logo"]).is_file(), card
    assert _card(_page(), "app-notion")["logo"] == _card(_page(), "notion")["logo"] != ""


def test_a_starter_connector_nobody_added_offers_connect(home):
    card = _card(_page(), "app-linear")

    assert (card["state"], card["label"]) == ("not-configured", "not connected")
    assert card["action"] == {"kind": "add", "label": "Connect", "target": "linear"}


def test_a_starter_connector_that_waits_for_sign_in_says_so_and_can_be_managed(home):
    connectors.add(home, "linear", url=LINEAR)

    page = _page()
    card = _card(page, "app-linear")

    assert (card["state"], card["label"]) == ("needs-setup", "needs sign-in")
    assert card["action"] == {"kind": "manage", "label": "Manage", "target": "linear"}
    assert [c["id"] for c in _cards(page)].count("app-linear") == 1
    assert "server-linear" not in [c["id"] for c in _cards(page)], "one card for one server"


def test_a_signed_in_connector_is_connected_and_says_as_whom(home, monkeypatch):
    from waku.ops import dashboard

    connectors.add(home, "notion", url=NOTION)
    real = dashboard._mcp_row

    def signed_in(*args):
        row = real(*args)
        row["account"] = "as sean@example.com"
        return row

    monkeypatch.setattr(dashboard, "_mcp_row", signed_in)

    card = _card(_page(), "app-notion")

    assert (card["state"], card["label"]) == ("connected", "connected")
    assert "as sean@example.com" in card["why"]


def test_a_server_added_by_address_gets_a_card_of_its_own(home):
    connectors.add(home, "example", url="https://mcp.example.com/mcp")

    card = _card(_page(), "server-example")

    assert card["name"] == "example"
    assert card["what"] == "https://mcp.example.com/mcp"
    assert card["action"] == {"kind": "manage", "label": "Manage", "target": "example"}
    assert _group(_page(), "apps")["cards"][3]["id"] == "server-example", "after the starter list"


def test_a_program_on_this_machine_is_set_up_and_not_tested(home):
    connectors.add(home, "fs", command="npx", args=["x"])

    card = _card(_page(), "server-fs")

    assert (card["state"], card["label"]) == ("configured", "configured · not tested")


def test_treg_and_waku_memory_are_drawn_once_each(home, monkeypatch):
    from waku.plugins import store

    monkeypatch.setattr(store, "servers", lambda home: [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True,
         "plugin": "waku"}])
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "treg", "url": "https://treg.to/mcp", "oauth": True}]}), encoding="utf-8")

    ids = [card["id"] for card in _cards(_page())]

    assert ids.count("treg") == 1 and ids.count("waku-memory") == 1
    assert "server-treg" not in ids and "server-waku_memory" not in ids


def test_waku_memory_card_offers_connect_until_it_is_connected(home, monkeypatch):
    from waku.ops import dashboard

    card = _card(_page(), "waku-memory")
    assert card["name"] == "Waku Memory"
    assert (card["state"], card["label"]) == ("not-configured", "not connected")
    assert card["action"] == {"kind": "chat", "label": "Connect", "target": "waku-memory"}

    monkeypatch.setattr(dashboard, "waku_memory_info", lambda settings: {
        "connected": True, "account": "as sean@example.com", "by_plugin": True, "mode": "plugin"})
    card = _card(_page(), "waku-memory")
    assert (card["state"], card["label"]) == ("connected", "connected")
    assert card["why"] == "as sean@example.com"
    assert card["action"] == {"kind": "href", "label": "Open waku.one",
                              "target": "https://www.waku.one/memories"}


def test_a_registry_card_opens_its_own_dialog(home, monkeypatch):
    card = _card(_page(), "telegram")
    assert (card["state"], card["label"]) == ("not-configured", "not connected")
    assert card["action"] == {"kind": "dialog", "label": "Connect", "target": "telegram"}

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    card = _card(_page(), "tavily")
    assert (card["state"], card["label"]) == ("configured", "configured · not tested")
    assert card["action"] == {"kind": "dialog", "label": "Manage", "target": "tavily"}


def test_a_card_that_needs_setup_says_what_is_missing(home, monkeypatch):
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    card = _card(_page(), "notion")

    assert (card["state"], card["label"]) == ("needs-setup", "needs setup")
    assert "NOTION_TOKEN" in card["why"]


def test_adding_a_server_by_address_is_offered_on_a_persons_own_machine(home):
    page = _page()

    assert page["developers"]["add_by_address"] is True
    assert page["sign_in"] is None


def test_the_page_carries_the_sign_in_link_while_one_waits(home, monkeypatch):
    from waku.ops import dashboard

    real = dashboard.connectors_data

    def waiting():
        data = real()
        data["sign_in"] = {"name": "linear", "link": "https://linear.app/oauth/authorize?x=1"}
        return data

    monkeypatch.setattr(dashboard, "connectors_data", waiting)

    assert _page()["sign_in"] == {"name": "linear",
                                  "link": "https://linear.app/oauth/authorize?x=1"}


def test_every_registry_group_has_a_place_on_the_page():
    """A registry group with no entry would leave its cards off the page."""
    from waku import integrations

    groups = {item.group for item in integrations.registry()} - {"AI Providers"}

    assert groups <= set(cp.REGISTRY_GROUP), groups - set(cp.REGISTRY_GROUP)
    assert set(cp.REGISTRY_GROUP.values()) <= {group_id for group_id, _ in cp.GROUPS}


def test_the_page_is_in_the_dashboards_payload(home):
    from waku.ops import dashboard

    data = dashboard.collect()

    assert [g["id"] for g in data["connections_page"]["groups"]] == ["talk", "apps", "memory"]


# ---- hosted Waku (decision 3) -------------------------------------------------

@pytest.fixture
def hosted(home, monkeypatch):
    """A hosted container: started with WAKU_HOSTED=1."""
    monkeypatch.setenv("WAKU_HOSTED", "1")
    return home


def test_the_payload_says_whether_the_dashboard_is_hosted(home, monkeypatch):
    from waku.ops import dashboard

    assert dashboard.collect()["hosted"] is False
    assert dashboard.connections_page_data()["hidden"] is None

    monkeypatch.setenv("WAKU_HOSTED", "1")
    assert dashboard.collect()["hosted"] is True


def test_hosted_shows_no_card_that_can_never_work_there_and_says_why_once(hosted):
    """Acceptance 5."""
    page = _page()
    ids = [card["id"] for card in _cards(page)]

    assert "apple_calendar" not in ids and "apple_tools" not in ids
    assert page["hidden"] == {
        "title": "2 cards are not shown on hosted Waku",
        "why": "Apple Calendar and Apple Tools need a Mac, and hosted Waku runs on a Linux server.",
    }


def test_hosted_offers_a_button_only_where_the_gateway_takes_the_save(hosted):
    """A card whose save the gateway refuses reads "not available here" and
    has no button, so the page does not offer what it will refuse."""
    page = _page()

    for card in _cards(page):
        if card["id"] in cp.HOSTED_CONNECTIONS:
            assert card["action"] is not None, card["id"]
            assert card["label"] != "not available here"
        elif card["id"] != "waku-memory":
            assert card["action"] is None, card["id"]
            assert (card["state"], card["label"]) == ("not-configured", "not available here")
    assert _card(page, "telegram")["label"] == "not available here"
    assert _card(page, "notion")["action"]["kind"] == "dialog"
    assert _card(page, "tavily")["action"]["kind"] == "dialog"


def test_hosted_lists_the_starter_apps_and_cannot_connect_them_yet(hosted):
    page = _page()

    for card_id in ("app-linear", "app-notion", "app-github"):
        card = _card(page, card_id)
        assert card["action"] is None
        assert card["label"] == "not available here"
    assert page["developers"]["add_by_address"] is False


def test_hosted_waku_memory_is_set_up_by_the_platform_so_it_has_no_button(hosted):
    card = _card(_page(), "waku-memory")

    assert card["action"] is None
    assert card["why"] == "Hosted Waku sets this up when you sign in."


def test_hosted_adds_no_card_names_twice(hosted):
    names = [card["name"] for card in _cards(_page())]

    assert len(names) == len({name.casefold() for name in names})


def test_hosted_tools_page_offers_nothing_to_connect(hosted):
    from waku.ops import dashboard

    assert dashboard.tools_info()["not_connected"] == []


def test_a_hosted_container_whose_person_changed_provider_is_still_hosted(hosted, monkeypatch):
    """The servers' routes are blocked by the gateway whatever the provider
    is, so the page must not offer them after a provider change."""
    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")

    assert dashboard.connectors_data()["available"] is False
