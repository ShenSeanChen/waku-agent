"""DETERMINISTIC EVAL: what the owner asked for after looking at the spec 030
pages (group D).

  1. Slack has a card on a person's own machine, set up like Telegram's.
  2. Every card whose vendor's mark is in the repo shows it.
  3. Tools starts with the connections that are connected right now.
  4. One verb: "Connect" until a card is connected, "Manage" after.
  5. OpenTelemetry is under "For developers", with adding a server by address.
  6. The search card is called "Web search", and names Tavily in its line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from waku import integrations
from waku.ops import browser_agent, dashboard
from waku.ops import connections_page as cp
from waku.tools import connectors

STATIC = Path(cp.__file__).resolve().parent / "static"
LINEAR = "https://mcp.linear.app/mcp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    for item in integrations.INTEGRATIONS:
        for field in item.env:
            monkeypatch.delenv(field.name, raising=False)
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    monkeypatch.setattr(browser_agent, "current", lambda: None)
    # The registry keeps each card's last test in memory. A test here records
    # one, and it must not reach the next test.
    monkeypatch.setattr(integrations, "_HEALTH", None)
    connectors.forget_sign_ins()
    return tmp_path


def _all(page):
    out = []
    for group in page["groups"]:
        out += group["cards"]
        if group["more"]:
            out += group["more"]["cards"]
    return out + page["developers"]["cards"]


def _card(page, card_id):
    return next(card for card in _all(page) if card["id"] == card_id)


# ---- 1. Slack on a person's own machine --------------------------------------

def test_slack_leads_where_you_talk_to_waku_on_a_persons_own_machine(home):
    page = dashboard.connections_page_data()
    talk = next(group for group in page["groups"] if group["id"] == "talk")

    assert [card["id"] for card in talk["cards"]] == ["slack", "telegram", "discord", "whatsapp"]
    card = talk["cards"][0]
    assert card["name"] == "Slack"
    assert (card["state"], card["label"]) == ("not-configured", "not connected")
    assert card["action"] == {"kind": "dialog", "label": "Connect", "target": "slack"}


def test_slack_is_set_up_with_the_persons_own_app_like_telegram(home, monkeypatch):
    row = next(item for item in integrations.INTEGRATIONS if item.key == "slack")

    assert row.group == "Channels"
    fields = {field.name: field for field in row.env}
    assert list(fields) == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_CHANNELS"]
    assert fields["SLACK_BOT_TOKEN"].secret and fields["SLACK_BOT_TOKEN"].required
    assert fields["SLACK_APP_TOKEN"].secret and fields["SLACK_APP_TOKEN"].required
    assert not fields["SLACK_ALLOWED_CHANNELS"].secret
    assert row.extra == "slack"
    assert row.setup_url.endswith("docs/integrations.md#slack-from-a-manifest")
    # `waku serve` hears Slack, not the dashboard: the dashboard must not try
    # to start it the way it starts Telegram.
    assert row.reload is integrations.ReloadMode.LIVE

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-a-real-token")
    view = next(v for v in integrations.list_connections() if v.key == "slack")
    assert view.status.state == "installed_but_unconfigured"
    assert "SLACK_APP_TOKEN" in view.status.message
    token = next(f for f in view.fields if f.name == "SLACK_BOT_TOKEN")
    assert token.value == "" and token.last4 == "oken", "a token is never in the payload"


def test_the_manifest_doc_the_slack_card_links_to_exists():
    doc = (Path(integrations.__file__).resolve().parents[1] / "docs" / "integrations.md")
    assert "### Slack from a manifest" in doc.read_text(encoding="utf-8")


def test_testing_the_slack_card_asks_slack_and_says_what_is_wrong(home, monkeypatch):
    """The card's Test button uses the check `waku connections` already makes."""
    seen = {}

    def status(env=None, fetch=None, extra_installed=None):
        seen.update(env)
        return False, "the bot token is missing the scope chat:write"

    monkeypatch.setattr(integrations, "slack_status", status)
    with pytest.raises(ValueError, match="missing the scope chat:write"):
        integrations._slack_probe({"SLACK_BOT_TOKEN": "xoxb-a", "SLACK_APP_TOKEN": "xapp-b"})
    assert seen["SLACK_BOT_TOKEN"] == "xoxb-a"

    monkeypatch.setattr(integrations, "slack_status",
                        lambda env=None, fetch=None, extra_installed=None: (True, "connected"))
    integrations._slack_probe({"SLACK_BOT_TOKEN": "xoxb-a", "SLACK_APP_TOKEN": "xapp-b"})
    assert integrations._probed(
        next(i for i in integrations.INTEGRATIONS if i.key == "slack")).probe is not None


def test_hosted_draws_no_second_slack_card(home, monkeypatch):
    """On hosted Waku the gateway knows about Slack, and the page draws its
    card from the gateway's answer. The registry's card would be a second
    card with the same name."""
    monkeypatch.setenv("WAKU_HOSTED", "1")

    page = dashboard.connections_page_data()

    assert "slack" not in [card["id"] for card in _all(page)]
    assert "Slack" not in page["hidden"]["why"]


def test_env_example_names_each_slack_value_once():
    text = (Path(integrations.__file__).resolve().parents[1] / ".env.example").read_text()
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_CHANNELS"):
        assert text.count(f"# {name}=") == 1, name


# ---- 2. a logo on every card ---------------------------------------------------

def test_slack_and_github_cards_show_their_mark(home):
    page = dashboard.connections_page_data()

    for card_id in ("slack", "app-github", "app-notion"):
        logo = _card(page, card_id)["logo"]
        assert logo and (STATIC / logo).is_file(), card_id
        assert (STATIC / logo).read_text(encoding="utf-8").startswith("<svg ")


def test_the_marks_this_change_adds_say_where_they_came_from():
    """A third-party mark is not a Waku brand file. The README beside the
    logos names each one's source and licence."""
    readme = (STATIC / "logos" / "connections" / "README.md").read_text(encoding="utf-8")
    for name in ("slack.svg", "github.svg"):
        assert (STATIC / "logos" / "connections" / name).is_file()
        assert f"`{name}`" in readme, f"{name} has no source in the README"


# ---- 3. Tools starts with what is connected -----------------------------------

def test_tools_lists_nothing_as_connected_in_an_empty_home(home):
    assert dashboard.tools_info()["connected"] == []


def test_tools_lists_each_connection_that_is_set_up_with_its_logo(home, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-not-a-real-key")
    monkeypatch.setenv("WAKU_SLOT_GATE", "jev")
    integrations.record_health("typesafe", integrations.IntegrationStatus(
        integrations.IntegrationState.CONNECTED))
    connectors.add(home, "linear", url=LINEAR)        # added, and nobody signed in

    page = dashboard.connections_page_data()
    connected = dashboard.tools_info()["connected"]

    assert (_card(page, "tavily")["label"], _card(page, "typesafe")["label"]) == (
        "configured · not tested", "connected")
    assert [c["id"] for c in connected] == ["tavily", "typesafe"]
    assert connected[0] == {"id": "tavily", "name": "Web search",
                            "logo": "logos/connections/tavily.svg"}
    assert connected[1]["name"] == "TypeSafe Jev"


def test_a_connection_that_failed_its_test_is_not_listed_as_connected(home, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    integrations.record_health("tavily", integrations.IntegrationStatus(
        integrations.IntegrationState.ERROR, "Tavily returned HTTP 401"))

    assert dashboard.tools_info()["connected"] == []


# ---- 4. one verb ------------------------------------------------------------

def test_a_card_says_connect_until_it_is_connected_and_manage_after(home, monkeypatch):
    page = dashboard.connections_page_data()
    for card in _all(page):
        if card["action"]:
            assert card["action"]["label"] == "Connect", card["id"]

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-not-a-real-key")
    monkeypatch.setenv("WAKU_SLOT_GATE", "jev")
    integrations.record_health("typesafe", integrations.IntegrationStatus(
        integrations.IntegrationState.CONNECTED))
    page = dashboard.connections_page_data()
    assert _card(page, "tavily")["action"]["label"] == "Manage"
    assert _card(page, "typesafe")["action"]["label"] == "Manage"
    assert _card(page, "discord")["action"]["label"] == "Connect"


def test_no_card_says_configure_or_edit(home, monkeypatch):
    from waku.tools import treg

    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")      # a card that needs setup
    connectors.add(home, "linear", url=LINEAR)               # a server that waits for sign-in
    monkeypatch.setattr(treg, "card", lambda home: {
        "key": "treg", "name": "treg", "group": "Search & Observability", "what": "Live data.",
        "state": "connected", "detail": "", "configurable": True})

    labels = {card["id"]: card["action"]["label"]
              for card in _all(dashboard.connections_page_data()) if card["action"]}

    assert set(labels.values()) <= {"Connect", "Manage"}, labels
    assert labels["notion"] == "Connect", "half set up is not connected"
    assert labels["treg"] == "Manage", "connected, and its key can be changed"
    assert labels["app-linear"] == "Manage", "the server exists, so the button opens it"


# ---- 5. For developers ----------------------------------------------------------

def test_opentelemetry_is_under_for_developers_and_not_an_app(home):
    page = dashboard.connections_page_data()
    apps = next(group for group in page["groups"] if group["id"] == "apps")

    assert "otel" not in [card["id"] for card in apps["cards"]]
    assert page["developers"]["title"] == "For developers"
    assert [card["id"] for card in page["developers"]["cards"]] == ["otel"]
    assert page["developers"]["add_by_address"] is True
    assert "add_by_address" not in page, "one place says whether a server can be added"


def test_hosted_keeps_the_section_and_offers_nothing_in_it(home, monkeypatch):
    monkeypatch.setenv("WAKU_HOSTED", "1")

    developers = dashboard.connections_page_data()["developers"]

    assert developers["add_by_address"] is False
    assert [(c["id"], c["label"], c["action"]) for c in developers["cards"]] == [
        ("otel", "not available here", None)]


# ---- 6. Web search ----------------------------------------------------------------

def test_the_search_card_is_called_web_search_and_names_tavily_in_its_line(home):
    card = _card(dashboard.connections_page_data(), "tavily")

    assert card["name"] == "Web search"
    assert card["what"] == "Search the web, through Tavily."
    assert "Tavily" not in [c["name"] for c in _all(dashboard.connections_page_data())]
