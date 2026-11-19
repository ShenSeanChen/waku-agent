"""The Connections page, composed (spec 030).

Connections is the one place a person connects anything. This module decides
which card is in which group, what each card is called, what state it shows
and what its one button does. The dashboard's script draws what it is handed
and decides nothing, because the script has no test runner and this file has
evals (`evals/deterministic/test_connections_page.py`).

A card is: a name, a state, one line of what it gives, and at most one button.

Pure functions over what `/api/data` already holds. Nothing here reads a
file, a key or the network.
"""

from __future__ import annotations

# The three groups, in reading order, named by what a person does.
GROUPS = (
    ("talk", "Where you talk to Waku"),
    ("apps", "Apps Waku can use"),
    ("memory", "Where memory is kept"),
)
# The memory arena's stores, under their own heading inside the third group.
COMPARE_TITLE = "Memory stores to compare"
COMPARE_STORES = ("mem0", "zep", "langmem", "supabase")

# Where a registry group's cards go (waku/integrations.py names the groups).
REGISTRY_GROUP = {
    "Channels": "talk",
    "Calendar & Productivity": "apps",
    "Search & Observability": "apps",
    "Memory & Storage": "memory",
}

# A state's class in style.css, and the words a person reads. One label per
# state: "not connected" is the same on every card.
STATES = {
    "connected": "connected",
    "needs-setup": "needs setup",
    "configured": "configured · not tested",
    "not-configured": "not connected",
    "error": "error",
}
NEEDS_SIGN_IN = "needs sign-in"
# Every (class, words) pair a card may show. "needs sign-in" shares the class
# of "needs setup": both wait for the person.
PAIRS = frozenset(STATES.items()) | {("needs-setup", NEEDS_SIGN_IN)}

# What a card's one button does:
#   dialog   opens the registry row `target` in the connection dialog
#   chat     sends `/connect <target>` through the chat dock
#   add      adds the starter connector `target`
#   manage   opens the server `target`: its account, its tools, Remove
#   href     opens the address `target` in a new tab
ACTIONS = ("dialog", "chat", "add", "manage", "href")

WAKU_MEMORY_ID = "waku-memory"
WAKU_MEMORY_PAGE = "https://www.waku.one/memories"
WAKU_MEMORY_WHAT = "One memory that every agent of yours reads and writes."

# One line per starter connector. An entry without a line here gets the
# sentence in `_starter_what`.
STARTER_WHAT = {
    "notion": "Lets Waku read and write your Notion pages.",
    "linear": "Lets Waku read and update your Linear issues.",
    "github": "Lets Waku read your GitHub repositories, issues and pull requests.",
}

_REGISTRY_STATE = {
    "connected": "connected",
    "error": "error",
    "configured": "configured",
    "installed_but_unconfigured": "needs-setup",
    "not_configured": "not-configured",
}
_TREG_STATE = {
    "connected": ("connected", STATES["connected"]),
    "not_signed_in": ("needs-setup", NEEDS_SIGN_IN),
    "not_added": ("not-configured", STATES["not-configured"]),
}


# The cards that have a drawing under waku/ops/static/. A card without one
# shows the first letter of its name.
_REGISTRY_LOGO = "logos/connections/{key}.svg"
_STARTER_LOGOS = {"notion": "logos/connections/notion.svg"}
_WAKU_LOGO = "waku-mark.svg"


def _card(card_id: str, name: str, state: str, what: str, *, label: str = "", logo: str = "",
          why: str = "", action: dict | None = None) -> dict:
    return {"id": card_id, "name": name, "logo": logo, "state": state,
            "label": label or STATES[state], "what": what, "why": why, "action": action}


def _action(kind: str, label: str, target: str) -> dict:
    return {"kind": kind, "label": label, "target": target}


def starter_id(name: str) -> str:
    return f"app-{name}"


def server_id(name: str) -> str:
    return f"server-{name}"


def registry_card(view: dict) -> dict:
    """A card for one row of the integrations registry. Its button opens the
    row's own dialog."""
    status = view.get("status") or {}
    state = _REGISTRY_STATE.get(str(status.get("state") or ""), "not-configured")
    # The reason is on the card where it adds something: a missing value, a
    # missing package or a failed test. For the other states it repeats the label.
    why = str(status.get("message") or "") if state in ("needs-setup", "error") else ""
    return _card(view["key"], view["name"], state, view["what"],
                 logo=_REGISTRY_LOGO.format(key=view["key"]), why=why,
                 action=_action("dialog", "Configure" if state == "not-configured" else "Edit",
                                view["key"]))


def treg_card(card: dict) -> dict:
    """treg signs in on its own page, so its button sends `/connect treg`
    through the chat. Where a key of the person's own can be set, the button
    opens that key's dialog instead (spec 014)."""
    state, label = _TREG_STATE.get(card.get("state"), _TREG_STATE["not_added"])
    if card.get("configurable"):
        action = _action("dialog", "Configure", card["key"])
    elif state == "connected":
        action = None
    else:
        action = _action("chat", "Connect", card["key"])
    return _card(card["key"], card["name"], state, card["what"], label=label,
                 logo=_REGISTRY_LOGO.format(key=card["key"]),
                 why=str(card.get("detail") or ""), action=action)


def _server_state(row: dict, waiting: dict | None) -> tuple[str, str]:
    account = str(row.get("account") or "")
    if (waiting or {}).get("name") == row["name"] or account.startswith(("not signed in",
                                                                         "unreadable")):
        return "needs-setup", NEEDS_SIGN_IN
    if row.get("connected") is False:
        return "not-configured", STATES["not-configured"]
    if row.get("connected") or account:
        return "connected", STATES["connected"]
    # Added, and no agent has started it yet: nothing has tried the server.
    return "configured", STATES["configured"]


def _server_why(row: dict, state: str) -> str:
    parts = []
    account = str(row.get("account") or "")
    if account and state == "connected":
        parts.append(account)
    if row.get("tools") is not None:
        count = f"{row['tools']} tool{'' if row['tools'] == 1 else 's'}"
        if row.get("asks"):
            count += f", {row['asks']} ask first"
        parts.append(count)
    if state == "not-configured":
        parts.append("It did not connect when the agent started.")
    return " · ".join(parts)


def _starter_what(entry: dict) -> str:
    return STARTER_WHAT.get(entry["name"], f"Lets Waku use {entry['title']}.")


def starter_card(entry: dict, row: dict | None, waiting: dict | None) -> dict:
    """A card for one connector of the starter list (spec 025). `row` is the
    server in mcp.json at the connector's address, or None when nobody added
    it."""
    logo = _STARTER_LOGOS.get(entry["name"], "")
    if row is None:
        return _card(starter_id(entry["name"]), entry["title"], "not-configured",
                     _starter_what(entry), logo=logo,
                     action=_action("add", "Connect", entry["name"]))
    state, label = _server_state(row, waiting)
    return _card(starter_id(entry["name"]), entry["title"], state, _starter_what(entry),
                 label=label, logo=logo, why=_server_why(row, state),
                 action=_action("manage", "Manage", row["name"]))


def server_card(row: dict, waiting: dict | None) -> dict:
    """A card for a server a person added by its address, or one a plugin
    brought. It is called what the person called it."""
    state, label = _server_state(row, waiting)
    what = f"From the {row['plugin']} plugin." if row.get("plugin") else row["address"]
    return _card(server_id(row["name"]), row["name"], state, what, label=label,
                 why=_server_why(row, state), action=_action("manage", "Manage", row["name"]))


def waku_memory_card(info: dict, *, connectable: bool = True) -> dict:
    """Waku Memory's card. `connectable` is False where the platform sets it
    up, so the card offers nothing to press."""
    if info.get("connected"):
        return _card(WAKU_MEMORY_ID, "Waku Memory", "connected", WAKU_MEMORY_WHAT,
                     logo=_WAKU_LOGO, why=str(info.get("account") or ""),
                     action=_action("href", "Open waku.one", WAKU_MEMORY_PAGE))
    return _card(WAKU_MEMORY_ID, "Waku Memory", "not-configured", WAKU_MEMORY_WHAT,
                 logo=_WAKU_LOGO,
                 action=_action("chat", "Connect", WAKU_MEMORY_ID) if connectable else None)


def _unique_names(cards: list[dict], fixed: set[str]) -> None:
    """No two cards share a name (acceptance 1). Only a server's name is
    chosen by a person, so only a server's card is renamed."""
    taken = set(fixed)
    for card in cards:
        if card["name"].casefold() in taken:
            card["name"] = f"{card['name']} (added by address)"
        taken.add(card["name"].casefold())


def page(connections: list[dict], mcp_cards: list[dict], connectors: dict,
         waku_memory: dict) -> dict:
    """The whole page: three groups of cards, the sign-in link while one
    waits, and whether a server can be added by its address here."""
    grouped: dict[str, list[dict]] = {group_id: [] for group_id, _ in GROUPS}
    compare: list[dict] = []
    # A registry row that a sign-in card stands for (treg's key) is drawn
    # once, as that card. Its dialog is still the registry row's.
    drawn = {card["key"] for card in mcp_cards}
    for view in connections:
        if view["key"] in drawn:
            continue
        card = registry_card(view)
        if view["key"] in COMPARE_STORES:
            compare.append(card)
        else:
            grouped[REGISTRY_GROUP.get(view["group"], "apps")].append(card)
    for card in mcp_cards:
        grouped[REGISTRY_GROUP.get(card.get("group"), "apps")].append(treg_card(card))

    waiting = connectors.get("sign_in")
    rows = {row["name"]: row for row in connectors.get("servers", [])}
    apps_first: list[dict] = []
    own: list[dict] = []
    if connectors.get("available"):
        claimed = set()
        for entry in connectors.get("starter", []):
            row = rows.get(entry.get("server") or "")
            if row is not None:
                claimed.add(row["name"])
            apps_first.append(starter_card(entry, row, waiting))
        # Waku Memory and treg have a card of their own, so their server
        # is not drawn a second time.
        own = [server_card(row, waiting) for row in rows.values()
               if row["name"] not in claimed and not row.get("own_card")]
    fixed = {card["name"].casefold()
             for card in apps_first + compare + [c for cards in grouped.values() for c in cards]}
    fixed.add("waku memory")
    _unique_names(own, fixed)
    grouped["apps"] = apps_first + own + grouped["apps"]
    grouped["memory"].insert(0, waku_memory_card(waku_memory))

    return {
        "groups": [{"id": group_id, "title": title, "cards": grouped[group_id],
                    "more": ({"title": COMPARE_TITLE, "cards": compare}
                             if group_id == "memory" and compare else None)}
                   for group_id, title in GROUPS],
        "sign_in": waiting,
        "add_by_address": bool(connectors.get("available")),
    }
