"""DETERMINISTIC EVAL — can memory be searched in the language it was stored in?

`_fts_query` built the FTS5 query from `[a-zA-Z0-9]`, but the index behind
`facts_fts` and `episodes_fts` is unicode61 (FTS5's default — neither table
declares a tokenizer), which keeps every Unicode alphanumeric and folds
diacritics. The two disagreed, and the index was not the side that was wrong:
"München" is stored as `munchen`, "Сергей" as `сергей`.

Everything outside ASCII was dropped on the way in, two different ways:

    "Müller"  -> `ller`   a fragment that matches nothing
    "Сергей"  -> ``       the empty string

The empty string is the worse half, because it isn't a no-op —
`SqliteEpisodeStore.search()` reads it as "just give me the recent ones". So a
Russian user asking about Сергей got an unrelated English episode handed to the
model under the heading "Relevant memory". Not *no* memory: someone else's.
That's the over-interpretation bias hero 1 exists to prevent, arriving through
the back door.

The five cases at the bottom pass both before and after. They're the ones to
read first in review: they pin that this widening didn't cost anything —
"car" still must not match "carpet", and English results stay byte-identical.

Offline, no model, no new dependency: real SQLite in tmp_path.
"""

from __future__ import annotations

import pytest

from waku.db import connect
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore, _fts_query


@pytest.fixture
def facts(tmp_path):
    return SqliteFactStore(connect(tmp_path))


@pytest.fixture
def episodes(tmp_path):
    return SqliteEpisodeStore(connect(tmp_path))


# ---------- the bug: the query builder threw away everything but ASCII


@pytest.mark.parametrize(
    "query,expected",
    [
        # accented Latin — truncated to a fragment, so it matched nothing
        ("Müller", "müller"),
        ("Wann treffe ich Müller?", "wann OR treffe OR ich OR müller"),
        # non-Latin scripts — reduced to "", which the episode store reads as
        # "give me the recent ones"
        ("Сергей", "сергей"),
        ("Γιώργος", "γιώργος"),
        ("محمد", "محمد"),
        ("שרה", "שרה"),
    ],
)
def test_a_query_keeps_the_alphanumerics_the_index_keeps(query, expected):
    """`[^\\W_]` is the same set unicode61 keeps, so the query and the index
    finally agree on what a word is."""
    assert _fts_query(query) == expected


@pytest.mark.parametrize("query,expected", [("阿历克斯", "阿历克斯*"), ("東京", "東京*")])
def test_unsegmented_scripts_are_searched_as_prefixes(query, expected):
    """unicode61 puts no boundary inside CJK, so 「阿历克斯喜欢游泳」 is indexed as
    ONE term and an exact match on the name can never hit it."""
    assert _fts_query(query) == expected


def test_an_accented_name_finds_the_fact_that_mentions_it(facts):
    facts.add("müller", "Müller prefers meetings in München before noon")
    assert facts.search("Müller"), "the fact is there; the query couldn't reach it"


def test_a_diacritic_is_folded_the_same_way_on_both_sides(facts):
    """unicode61 stores "München" as `munchen`. The query is folded by the same
    tokenizer, so it has to survive as a whole word to be folded at all."""
    facts.add("müller", "Müller prefers meetings in München before noon")
    assert facts.search("München")


def test_a_cyrillic_name_finds_its_fact(facts):
    facts.add("сергей", "Сергей любит плавание по утрам")
    assert facts.search("Сергей")


def test_a_cjk_name_finds_its_fact(facts):
    facts.add("阿历克斯", "阿历克斯喜欢游泳")
    assert facts.search("阿历克斯"), "an exact match can't hit an unsegmented run"


def test_a_non_ascii_query_no_longer_returns_an_unrelated_episode(episodes):
    """The worst symptom, stated directly. An empty FTS query fell through to
    recent(), so a question about Сергей was answered with whatever happened
    most recently — under the heading "Relevant memory"."""
    episodes.add("Planned the Acme demo with Alex", "2026-08-02")

    assert episodes.search("Сергей") == [], "silence is correct; someone else's memory is not"


# ---------- regression guards: these pass before AND after


def test_english_search_is_unchanged(facts):
    facts.add("alex", "Alex prefers morning meetings")
    assert facts.search("Alex") == ["[alex] Alex prefers morning meetings"]
    assert _fts_query("when am I meeting Alex?") == "when OR am OR meeting OR alex"


def test_car_must_not_match_carpet(facts):
    """The reason only unsegmented scripts get a `*`. Prefixing everything would
    look like it worked while quietly widening every English search."""
    facts.add("shopping", "the carpet is red")
    assert facts.search("car") == []


def test_punctuation_and_single_letters_are_still_dropped():
    assert _fts_query("!!! ??? ...") == ""
    assert _fts_query("a b c") == ""


def test_an_unsearchable_query_still_means_no_facts_not_all_facts(facts):
    """Facts fail closed on an empty query, and must keep doing so."""
    facts.add("alex", "Alex prefers morning meetings")
    assert facts.search("!!!") == []


def test_underscore_is_a_separator_here_because_it_is_one_in_the_index():
    """unicode61 splits on `_`, so `waku_agent` is two terms in the index and
    has to be two terms here too, or it matches nothing."""
    assert _fts_query("waku_agent") == "waku OR agent"
