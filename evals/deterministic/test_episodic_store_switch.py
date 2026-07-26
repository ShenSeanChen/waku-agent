"""Deterministic tests for the sqlite|notion episodic-store switch."""

from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest

from waku.config import Settings
from waku.memory import Memory
from waku.memory.episodic.store import SqliteEpisodeStore


class _FakeNotionClient:
    """In-memory fake for notion_client.Client (>= 2.5, data-sources API).

    Pages are class-level so a store created inside dashboard code sees the
    same rows as one created in the test body."""

    _pages: ClassVar[list[dict]] = []
    _init_count = 0     # Client constructions — the dashboard must build ONE
    _query_count = 0    # data-source queries — the result cache throttles these
    _query_fails = False   # simulate a Notion outage after a successful fetch

    def __init__(self, auth: str | None = None) -> None:
        type(self)._init_count += 1
        self.auth = auth
        self.databases = types.SimpleNamespace(retrieve=self._retrieve)
        self.data_sources = types.SimpleNamespace(query=self._query)
        self.pages = types.SimpleNamespace(create=self._create, update=self._update)

    def _retrieve(self, *, database_id: str) -> dict:
        assert database_id == "test-db-id"
        return {"data_sources": [{"id": "test-ds-id"}]}

    def _create(self, *, parent: dict, properties: dict) -> dict:
        cls = type(self)
        page = {
            "id": f"page-{len(cls._pages) + 1}",
            "parent": parent,
            "properties": properties,
            "created_time": "2026-07-18T00:00:00.000Z",
        }
        cls._pages.append(page)
        return page

    def _update(self, *, page_id: str, archived: bool) -> dict:
        for page in type(self)._pages:
            if page["id"] == page_id:
                page["archived"] = archived
                return page
        return {}

    def _query(self, *, data_source_id: str, start_cursor: str | None = None) -> dict:
        if type(self)._query_fails:
            raise RuntimeError("notion is down")
        type(self)._query_count += 1
        assert data_source_id == "test-ds-id"
        # the real API excludes archived pages from query results
        return {"results": [p for p in type(self)._pages if not p.get("archived")],
                "has_more": False}


@pytest.fixture
def fake_notion(monkeypatch):
    _FakeNotionClient._pages = []
    _FakeNotionClient._init_count = 0
    _FakeNotionClient._query_count = 0
    _FakeNotionClient._query_fails = False
    fake_module = types.ModuleType("notion_client")
    fake_module.Client = _FakeNotionClient
    monkeypatch.setitem(sys.modules, "notion_client", fake_module)
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_EPISODES_DATABASE_ID", "test-db-id")
    # the dashboard caches the store + result module-wide — reset between tests
    from waku.ops import dashboard

    monkeypatch.setattr(dashboard, "_notion_store", None)
    monkeypatch.setattr(dashboard, "_notion_episodes", None)
    return _FakeNotionClient


def test_settings_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("WAKU_EPISODIC_STORE", raising=False)
    assert Settings().episodic_store == "sqlite"


def test_settings_reads_episodic_store_env(monkeypatch):
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")
    assert Settings().episodic_store == "notion"


def test_factory_returns_sqlite_store_by_default(monkeypatch):
    monkeypatch.delenv("WAKU_EPISODIC_STORE", raising=False)
    store = Memory._make_episode_store(conn=None, settings=Settings())
    assert isinstance(store, SqliteEpisodeStore)


def test_factory_returns_notion_store_when_configured(monkeypatch, fake_notion):
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")
    store = Memory._make_episode_store(conn=None, settings=Settings())
    from waku.memory.episodic.notion_store import NotionEpisodeStore

    assert isinstance(store, NotionEpisodeStore)


def test_apply_settings_rejects_unknown_episodic_store(monkeypatch, tmp_path):
    # chdir so a regression of the guard can't write into the real project .env
    monkeypatch.chdir(tmp_path)
    from waku.ops.dashboard import apply_settings

    result = apply_settings({"provider": "anthropic", "episodic_store": "bogus"})
    assert "error" in result
    assert "episodic_store" in result["error"]


def _isolated_home(monkeypatch, tmp_path):
    """Point collect()/memory_action() at a throwaway WAKU_HOME with no network
    warm-up (provider anthropic, no base_url)."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    monkeypatch.delenv("WAKU_BASE_URL", raising=False)


def test_collect_reads_episodes_from_notion_when_active(monkeypatch, fake_notion, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore().add("episode from notion", "2026-07-18")

    from waku.ops.dashboard import collect

    data = collect()
    assert data["episodes_source"] == "notion"
    assert data["episodes_error"] == ""
    assert [e["summary"] for e in data["episodes"]] == ["episode from notion"]


def test_collect_episodes_default_to_sqlite(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WAKU_EPISODIC_STORE", raising=False)

    from waku.ops.dashboard import collect

    data = collect()
    assert data["episodes_source"] == "sqlite"
    assert data["episodes_error"] == ""
    assert data["episodes"] == []


def test_memory_action_delete_episode_routes_to_notion(monkeypatch, fake_notion, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore().add("to delete", "2026-07-18")
    page_id = _FakeNotionClient._pages[0]["id"]

    from waku.ops.dashboard import memory_action

    assert memory_action({"action": "delete_episode", "id": page_id}) == {"ok": True}
    assert _FakeNotionClient._pages[0]["archived"] is True


def test_collect_episodes_notion_outage_degrades_gracefully(monkeypatch, fake_notion, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # constructor raises ValueError

    from waku.ops.dashboard import collect

    data = collect()
    assert data["episodes"] == []
    assert data["episodes_source"] == "notion"
    assert "NOTION_TOKEN" in data["episodes_error"]
    assert "facts" in data  # the rest of the payload still rendered


def test_manage_memory_delete_episode_accepts_notion_string_id(fake_notion):
    from waku.memory.episodic.notion_store import NotionEpisodeStore
    from waku.tools.memory_admin import make_manage_memory_tool

    store = NotionEpisodeStore()
    store.add("via tool", "2026-07-18")
    page_id = _FakeNotionClient._pages[0]["id"]

    memory = types.SimpleNamespace(episodes=store, facts=None)
    tool = make_manage_memory_tool(memory)
    assert tool.fn("delete", kind="episode", id=page_id) == f"Deleted episode #{page_id}."
    assert _FakeNotionClient._pages[0]["archived"] is True


def test_manage_memory_delete_episode_sqlite_accepts_string_id(tmp_path):
    from waku.db import connect
    from waku.memory.episodic.store import SqliteEpisodeStore
    from waku.tools.memory_admin import make_manage_memory_tool

    conn = connect(tmp_path)
    store = SqliteEpisodeStore(conn)
    store.add("sqlite ep", "2026-07-18")

    memory = types.SimpleNamespace(episodes=store, facts=None)
    tool = make_manage_memory_tool(memory)
    assert tool.fn("delete", kind="episode", id="1") == "Deleted episode #1."
    assert store.recent(top_k=1) == []


def test_collect_builds_notion_client_once_across_refreshes(monkeypatch, fake_notion, tmp_path):
    """Issue #20: repeated collect() calls (dashboard auto-refresh) must reuse
    ONE Notion client and serve the cached result within the TTL — not rebuild
    the client and re-query Notion on every poll."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore().add("episode from notion", "2026-07-18")

    from waku.ops import dashboard

    fake_notion._init_count = 0   # ignore the setup construction above
    fake_notion._query_count = 0

    payloads = [dashboard.collect() for _ in range(3)]

    assert all(p["episodes_source"] == "notion" and p["episodes_error"] == "" for p in payloads)
    assert [p["episodes"] for p in payloads] == [payloads[0]["episodes"]] * 3
    assert fake_notion._init_count == 1   # client built once, then cached
    assert fake_notion._query_count == 1  # result cached for the TTL, no re-query


def test_collect_refetches_after_ttl_but_reuses_client(monkeypatch, fake_notion, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore().add("ep", "2026-07-18")

    from waku.ops import dashboard

    monkeypatch.setattr(dashboard, "_NOTION_EPISODES_TTL", 0)   # every poll is stale
    fake_notion._init_count = 0
    fake_notion._query_count = 0

    dashboard.collect()
    dashboard.collect()

    assert fake_notion._query_count == 2   # TTL expired → refetch each time
    assert fake_notion._init_count == 1    # …but the client is still built once


def test_collect_serves_stale_episodes_during_notion_outage(monkeypatch, fake_notion, tmp_path):
    """House rule: a Notion outage must degrade gracefully — and with a cache
    on hand the tab keeps its last good data instead of going blank."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore().add("cached episode", "2026-07-18")

    from waku.ops import dashboard

    assert dashboard.collect()["episodes_error"] == ""

    monkeypatch.setattr(dashboard, "_NOTION_EPISODES_TTL", 0)
    fake_notion._query_fails = True
    data = dashboard.collect()
    assert data["episodes_source"] == "notion"
    assert "notion is down" in data["episodes_error"]
    assert [e["summary"] for e in data["episodes"]] == ["cached episode"]
    assert "facts" in data   # the rest of the payload still rendered


def test_delete_episode_busts_the_episodes_cache(monkeypatch, fake_notion, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_EPISODIC_STORE", "notion")

    from waku.memory.episodic.notion_store import NotionEpisodeStore

    store = NotionEpisodeStore()
    store.add("first", "2026-07-18")
    store.add("second", "2026-07-19")

    from waku.ops import dashboard

    assert len(dashboard.collect()["episodes"]) == 2   # populates the cache

    page_id = _FakeNotionClient._pages[0]["id"]
    assert dashboard.memory_action({"action": "delete_episode", "id": page_id}) == {"ok": True}

    after = dashboard.collect()   # cache busted → refetch, not the stale pair
    assert [e["summary"] for e in after["episodes"]] == ["second"]
