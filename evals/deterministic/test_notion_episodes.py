"""Deterministic eval for NotionEpisodeStore.

Stubs `notion_client.Client` so no real network requests are made.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest


class _FakeNotionClient:
    """In-memory fake for notion_client.Client (>= 2.5, data-sources API)."""

    def __init__(self, auth: str | None = None) -> None:
        self.auth = auth
        self.databases = types.SimpleNamespace(retrieve=self._retrieve)
        self.data_sources = types.SimpleNamespace(query=self._query)
        self.pages = types.SimpleNamespace(create=self._create, update=self._update)
        self._pages: list[dict] = []
        self._created_count = 0

    def _retrieve(self, *, database_id: str) -> dict:
        assert database_id in ("test-db-id", "3a14b87b9c3280afac7ae76e389e4ba7")
        return {"data_sources": [{"id": "test-ds-id"}]}

    def _create(self, *, parent: dict, properties: dict) -> dict:
        self._created_count += 1
        page = {
            "id": f"page-{self._created_count}",
            "parent": parent,
            "properties": properties,
            "created_time": f"2026-07-{self._created_count:02d}T00:00:00.000Z",
        }
        self._pages.append(page)
        return page

    def _update(self, *, page_id: str, archived: bool) -> dict:
        for page in self._pages:
            if page["id"] == page_id:
                page["archived"] = archived
                return page
        return {}

    def _query(self, *, data_source_id: str, start_cursor: str | None = None) -> dict:
        assert data_source_id == "test-ds-id"
        return {"results": list(self._pages), "has_more": False}


from waku.memory.episodic.notion_store import (
    NotionEpisodeStore,
    normalize_database_id,
)


@pytest.fixture
def store(monkeypatch):
    fake_module = types.ModuleType("notion_client")
    fake_module.Client = _FakeNotionClient
    monkeypatch.setitem(sys.modules, "notion_client", fake_module)
    return NotionEpisodeStore(token="test-token", database_id="test-db-id")


def test_add_creates_page_with_correct_properties(store):
    store.add("planned the demo with Alex", "2026-07-10")

    created = store.client._pages[0]
    assert created["parent"]["data_source_id"] == "test-ds-id"
    title_parts = created["properties"]["Name"]["title"]
    assert title_parts[0]["text"]["content"] == "2026-07-10"
    summary_parts = created["properties"]["Summary"]["rich_text"]
    assert summary_parts[0]["text"]["content"] == "planned the demo with Alex"


def test_recent_returns_latest_episodes_sorted_by_happened_at(store):
    store.add("first episode", "2026-07-08")
    store.add("second episode", "2026-07-10")
    store.add("third episode", "2026-07-09")

    recent = store.recent(top_k=2)
    assert recent == [
        "(2026-07-10) second episode",
        "(2026-07-09) third episode",
    ]


def test_search_filters_by_keyword_and_returns_matching_episodes(store):
    store.add("planned the demo with Alex", "2026-07-10")
    store.add("wrote the quarterly report", "2026-07-09")
    store.add("demo follow-up with team", "2026-07-08")

    results = store.search("demo", top_k=2)
    assert results == [
        "(2026-07-10) planned the demo with Alex",
        "(2026-07-08) demo follow-up with team",
    ]


def test_search_falls_back_to_recent_when_query_has_no_keywords(store):
    store.add("only episode", "2026-07-10")

    results = store.search("!!!", top_k=1)
    assert results == ["(2026-07-10) only episode"]


def test_search_does_not_match_substrings(store):
    store.add("demolition planning", "2026-07-10")
    store.add("demo preparation", "2026-07-09")

    results = store.search("demo", top_k=10)
    assert results == ["(2026-07-09) demo preparation"]


def test_list_returns_correct_dict_shape_and_limit(store):
    store.add("first episode", "2026-07-08")
    store.add("second episode", "2026-07-10")
    store.add("third episode", "2026-07-09")

    episodes = store.list(limit=2)
    assert len(episodes) == 2
    assert episodes == [
        {
            "id": "page-2",
            "happened_at": "2026-07-10",
            "summary": "second episode",
            "created_at": "2026-07-02T00:00:00.000Z",
        },
        {
            "id": "page-3",
            "happened_at": "2026-07-09",
            "summary": "third episode",
            "created_at": "2026-07-03T00:00:00.000Z",
        },
    ]


def test_delete_archives_page(store):
    store.add("episode to delete", "2026-07-10")
    page_id = store.client._pages[0]["id"]

    assert store.delete(page_id) is True
    assert store.client._pages[0]["archived"] is True


def test_constructor_requires_token_and_database_id(monkeypatch):
    fake_module = types.ModuleType("notion_client")
    fake_module.Client = _FakeNotionClient
    monkeypatch.setitem(sys.modules, "notion_client", fake_module)
    # Isolate from a developer's real .env, which may set these.
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_EPISODES_DATABASE_ID", raising=False)

    with pytest.raises(ValueError, match="NOTION_TOKEN"):
        NotionEpisodeStore(token=None, database_id="test-db-id")

    with pytest.raises(ValueError, match="NOTION_EPISODES_DATABASE_ID"):
        NotionEpisodeStore(token="test-token", database_id=None)


def test_constructor_accepts_a_copied_database_link(monkeypatch):
    fake_module = types.ModuleType("notion_client")
    fake_module.Client = _FakeNotionClient
    monkeypatch.setitem(sys.modules, "notion_client", fake_module)

    store = NotionEpisodeStore(
        token="test-token",
        database_id=(
            "https://app.notion.com/p/3a14b87b9c3280afac7ae76e389e4ba7"
            "?v=3a14b87b9c32802abf0f000caaf78263"
        ),
    )
    assert store.database_id == "3a14b87b9c3280afac7ae76e389e4ba7"


def test_normalize_database_id_uses_path_not_view_id():
    assert normalize_database_id(
        "https://app.notion.com/p/3a14b87b9c3280afac7ae76e389e4ba7"
        "?v=3a14b87b9c32802abf0f000caaf78263"
    ) == "3a14b87b9c3280afac7ae76e389e4ba7"


def test_module_imports_without_notion_client_installed():
    code = """
import sys
assert "notion_client" not in sys.modules
from waku.memory.episodic.notion_store import NotionEpisodeStore
assert "notion_client" not in sys.modules
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
