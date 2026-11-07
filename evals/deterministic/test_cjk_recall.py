"""CJK keywords inside an unsegmented token must reach stored memories."""

import pytest

from waku.db import connect
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore


@pytest.mark.parametrize("query", ["上海", "自行车", "Tokyo 上海", "上海 上海"])
def test_facts_recall_interior_cjk_keywords(tmp_path, query):
    conn = connect(tmp_path)
    store = SqliteFactStore(conn)
    store.add("个人资料", "我现在住在上海，喜欢骑自行车")
    assert store.search(query) == ["[个人资料] 我现在住在上海，喜欢骑自行车"]
    assert [r["content"] for r in store.search_with_ids(query)] == ["我现在住在上海，喜欢骑自行车"]
    conn.close()


def test_episodes_recall_interior_keyword_with_recency_and_limit(tmp_path):
    conn = connect(tmp_path)
    store = SqliteEpisodeStore(conn)
    store.add("与小王讨论了上海的项目", "2026-11-01")
    store.add("与小李讨论了上海的项目", "2026-11-02")
    store.add("北京项目", "2026-11-03")
    assert store.search("上海", top_k=1) == ["(2026-11-02) 与小李讨论了上海的项目"]
    assert store.search("上海", top_k=2) == [
        "(2026-11-02) 与小李讨论了上海的项目", "(2026-11-01) 与小王讨论了上海的项目",
    ]
    conn.close()


def test_fts_matches_rank_before_contains_only_matches_without_duplicates(tmp_path):
    conn = connect(tmp_path)
    store = SqliteFactStore(conn)
    store.add("city", "上海")
    store.add("newer", "我现在住在上海")
    assert store.search("上海", top_k=1) == ["[city] 上海"]
    assert store.search("上海", top_k=2) == ["[city] 上海", "[newer] 我现在住在上海"]
    assert len(store.search_with_ids("上海", top_k=2)) == 2
    conn.close()


def test_english_and_nonmatching_cjk_do_not_fall_back_to_unrelated_memories(tmp_path):
    conn = connect(tmp_path)
    facts, episodes = SqliteFactStore(conn), SqliteEpisodeStore(conn)
    facts.add("object", "carpet 上海")
    episodes.add("carpet 上海", "2026-11-01")
    for query in ["car", "北京", "北京' OR 1=1 --", "北京_%"]:
        assert facts.search(query) == []
        assert facts.search_with_ids(query) == []
        assert episodes.search(query) == []
    conn.close()


def test_updates_and_deletes_are_immediately_visible_without_reindexing(tmp_path):
    conn = connect(tmp_path)
    store = SqliteFactStore(conn)
    store.add("profile", "我住在上海")
    [row] = store.search_with_ids("上海")
    store.update(row["id"], "我住在北京")
    assert store.search("上海") == []
    assert store.search("北京") == ["[profile] 我住在北京"]
    store.delete(row["id"])
    assert store.search("北京") == []
    conn.close()
