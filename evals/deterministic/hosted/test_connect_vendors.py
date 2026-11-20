"""DETERMINISTIC EVAL -- the hosted connector list (spec 029).

hosted/core/connectors.py is a second copy of three entries of
waku/connectors.toml, because hosted/ and waku/ never import each other. This
eval may read both, and it fails when the copies differ: an address that
moved in one file, or a write verb added to one list and not the other.

GitHub's entry differs in two places, on purpose (spec 029 F). On a person's
own machine it takes a personal access token; on hosted Waku a person signs
in through a GitHub App. And hosted Waku calls the read-only address of the
same server.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from hosted.core.connectors import VENDORS, is_connector_name

ROOT = Path(__file__).resolve().parents[3]


def _starter() -> dict:
    with (ROOT / "waku" / "connectors.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_the_list_is_notion_linear_github_and_gmail_and_nothing_else():
    assert set(VENDORS) == {"notion", "linear", "github", "gmail"}
    # Gmail is the one that is not an MCP server (spec 031), so it has no
    # entry in the starter list and is not compared with one below.
    assert [name for name, vendor in VENDORS.items() if not vendor.mcp] == ["gmail"]


def test_each_vendor_is_the_starter_lists_entry():
    starter = _starter()
    for name, vendor in VENDORS.items():
        if not vendor.mcp:
            assert name not in starter
            continue
        entry = starter[name]
        assert (vendor.name, vendor.title) == (entry["name"], entry["title"])
        assert list(vendor.writes) == entry["writes"]
        if name == "github":
            assert entry["auth"] == "GITHUB_PERSONAL_ACCESS_TOKEN"
            assert entry["url"] == "https://api.githubcopilot.com/mcp/"
            assert vendor.url == entry["url"] + "readonly"
        else:
            assert entry["auth"] == "oauth"
            assert vendor.url == entry["url"]


def test_a_tool_that_writes_is_told_from_one_that_reads():
    notion, linear = VENDORS["notion"], VENDORS["linear"]
    assert notion.is_write("notion-create-pages")
    assert notion.is_write("notion-update-page")
    assert not notion.is_write("notion-search")
    assert not notion.is_write("notion-fetch")
    assert linear.is_write("create_issue")
    assert linear.is_write("save_comment")
    assert not linear.is_write("list_issues")
    assert not linear.is_write("get_issue")


def test_only_a_listed_name_is_a_connector_name():
    assert all(is_connector_name(name) for name in ("notion", "linear", "github"))
    for other in ("slack", "Notion", "notion/../x", "", None, 3, ["notion"]):
        assert not is_connector_name(other)
