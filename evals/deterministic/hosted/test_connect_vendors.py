"""DETERMINISTIC EVAL -- the hosted connector list (spec 029).

hosted/core/connectors.py is a second copy of two entries of
waku/connectors.toml, because hosted/ and waku/ never import each other. This
eval may read both, and it fails when the copies differ: an address that
moved in one file, or a write verb added to one list and not the other.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from hosted.core.connectors import VENDORS, is_connector_name

ROOT = Path(__file__).resolve().parents[3]


def _starter() -> dict:
    with (ROOT / "waku" / "connectors.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_v1_offers_notion_and_linear_and_nothing_else():
    # GitHub takes a personal access token typed into a page (open question 3).
    assert set(VENDORS) == {"notion", "linear"}


def test_each_vendor_is_the_starter_lists_entry():
    starter = _starter()
    for name, vendor in VENDORS.items():
        entry = starter[name]
        assert entry["auth"] == "oauth"
        assert (vendor.name, vendor.title, vendor.url) == (
            entry["name"], entry["title"], entry["url"])
        assert list(vendor.writes) == entry["writes"]


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
    assert is_connector_name("notion") and is_connector_name("linear")
    for other in ("github", "Notion", "notion/../x", "", None, 3, ["notion"]):
        assert not is_connector_name(other)
