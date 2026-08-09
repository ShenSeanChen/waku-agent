"""DETERMINISTIC EVAL — every semantic-memory backend, held to the same contract.

This suite exists because the contract was never written down, so the second
backend silently didn't meet it. `SqliteFactStore` had six methods,
`SupabaseFactStore` had two, and `WAKU_SEMANTIC_STORE=supabase` therefore
broke the dashboard's memory page, the manage_memory tool's update and delete
— and worst of all made `search_with_ids` return `[]` through a
`hasattr(...) else []` guard, so the agent told users a fact didn't exist while
it sat in the database.

So the shape of this file matters more than any single assertion: it is
parametrized over BACKENDS, and every backend runs every test. Adding a store
is not "write a class"; it is "write a class that passes this". That is the
whole point of waku/memory/semantic/base.py.

The first test is the one that would have caught the original bug on the day it
shipped, and it is one line: `isinstance(store, FactStore)`. A runtime
checkable Protocol verifies the methods are PRESENT — which is exactly the
failure we had. It cannot check signatures or behaviour, so the rest of the
file does that by exercising them.

Offline by default: SQLite in tmp_path, no keys, no network. Supabase joins the
parametrization only when you opt in explicitly (see `_supabase_store`), because
its `add()` calls OpenAI for embeddings and costs real money — `make gate` must
stay green for a contributor with no accounts.
"""

from __future__ import annotations

import inspect
import os

import pytest

from waku.db import connect
from waku.memory.semantic.base import FactStore
from waku.memory.semantic.store import SqliteFactStore


def _sqlite_store(tmp_path):
    return SqliteFactStore(connect(tmp_path))


def _supabase_store(tmp_path):
    """Opt-in only. Needs a Supabase project AND an OpenAI key (embeddings are
    billed per call), so it is gated on an explicit env var rather than on the
    credentials happening to be present — a maintainer with a populated .env
    should not start paying for embeddings by running the test suite."""
    if os.getenv("WAKU_TEST_SUPABASE") != "1":
        pytest.skip("set WAKU_TEST_SUPABASE=1 (plus SUPABASE_* and OPENAI_API_KEY) to include it")
    from waku.config import Settings
    from waku.memory.semantic.supabase_store import SupabaseFactStore

    return SupabaseFactStore(Settings(home=tmp_path))


def _mem0_store(tmp_path):
    """Opt-in only, same reasoning as Supabase: this one talks to a paid hosted
    service and writes real memories into a real account. A maintainer running
    the suite must not start filling somebody's Mem0 workspace with test facts
    about Priya's meeting preferences."""
    if os.getenv("WAKU_TEST_MEM0") != "1":
        pytest.skip("set WAKU_TEST_MEM0=1 (plus MEM0_API_KEY) to include it")
    from waku.config import Settings
    from waku.memory.semantic.mem0_store import Mem0FactStore

    return Mem0FactStore(Settings(home=tmp_path))


def _zep_store(tmp_path):
    if os.getenv("WAKU_TEST_ZEP") != "1":
        pytest.skip("set WAKU_TEST_ZEP=1 (plus ZEP_API_KEY) to include it")
    from waku.config import Settings
    from waku.memory.semantic.zep_store import ZepFactStore

    return ZepFactStore(Settings(home=tmp_path))


def _langmem_store(tmp_path):
    """Needs no account — LangMem is a toolkit, not a service — but semantic
    search still bills OpenAI for embeddings, so it stays opt-in like the rest.
    Without the index the store is a plain dict and the tests would pass while
    measuring nothing."""
    if os.getenv("WAKU_TEST_LANGMEM") != "1":
        pytest.skip("set WAKU_TEST_LANGMEM=1 (plus OPENAI_API_KEY) to include it")
    from waku.config import Settings
    from waku.memory.semantic.langmem_store import LangMemFactStore

    return LangMemFactStore(Settings(home=tmp_path))


BACKENDS = {"sqlite": _sqlite_store, "supabase": _supabase_store, "mem0": _mem0_store,
            "zep": _zep_store, "langmem": _langmem_store}


@pytest.fixture(params=list(BACKENDS), ids=list(BACKENDS))
def store(request, tmp_path):
    return BACKENDS[request.param](tmp_path)


def test_the_backend_declares_every_method_the_contract_requires(store):
    """THE regression. `SupabaseFactStore` shipped missing four of the six and
    nothing anywhere noticed until a user switched backends."""
    assert isinstance(store, FactStore), (
        f"{type(store).__name__} does not satisfy the FactStore protocol — missing: "
        + ", ".join(m for m in _protocol_methods() if not hasattr(store, m))
    )


def test_add_then_search_round_trips(store):
    store.add("jensen", "Jensen always wears a leather jacket")
    assert any("leather jacket" in hit for hit in store.search("jensen leather jacket"))


def test_a_miss_returns_empty_rather_than_raising(store):
    """A memory backend that throws takes the whole turn down with it."""
    assert store.search("nothing here matches this at all") == []


def test_search_with_ids_returns_ids_that_update_accepts(store):
    """The lookup step of every correction. When this returns [] on failure
    instead of raising, the agent reports the memory does not exist — which is
    precisely what the `hasattr` guard in manage_memory used to cause."""
    store.add("elon", "The launch is in March")
    rows = store.search_with_ids("elon launch")
    assert rows, "search_with_ids found nothing it just stored"
    assert "id" in rows[0] and "content" in rows[0]
    assert store.update(rows[0]["id"], "The launch is in June") is True


def test_update_changes_what_search_returns(store):
    store.add("elon", "The launch is in March")
    rid = store.search_with_ids("elon launch")[0]["id"]
    store.update(rid, "The launch is in June")
    assert any("June" in hit for hit in store.search("elon launch"))


def test_delete_removes_the_fact(store):
    store.add("tom", "Tom Holland cannot keep a secret")
    rid = store.search_with_ids("tom holland secret")[0]["id"]
    assert store.delete(rid) is True
    assert not any("secret" in hit for hit in store.search("tom holland secret"))


@pytest.mark.parametrize("method", ["update", "delete"])
def test_an_unknown_id_is_false_not_an_exception(store, method):
    """False means "no such row". Raising here would turn a stale id in the
    dashboard into a 500."""
    assert getattr(store, method)(*( (999_999, "x") if method == "update" else (999_999,) )) is False


def test_list_returns_what_was_added(store):
    store.add("pikachu", "Pikachu is coming to the dinner party")
    rows = store.list(50)
    assert any("Pikachu" in r["content"] for r in rows)
    assert all({"id", "subject", "content"} <= set(r) for r in rows)


def test_non_ascii_round_trips_on_every_backend(store):
    """The 2026-08-05 bug (see test_memory_search.py) was one backend's query
    builder dropping non-ASCII. Pinning it HERE means a new backend cannot
    reintroduce it — the failure mode was users silently receiving somebody
    else's memories, which no amount of English test coverage would catch."""
    store.add("сергей", "Сергей любит плавать")
    assert store.search("Сергей") != [], "a Cyrillic fact stored but not findable"


# --- the suite keeps itself honest -------------------------------------------

def _protocol_methods() -> list[str]:
    """The contract's own method names, read off the class.

    NOT `FactStore.__protocol_attrs__` — that is a CPython implementation
    detail of typing.Protocol, absent on 3.11 and free to change. Reading
    `vars()` is stable, and it also stays in declaration order, which makes the
    failure message below read in the same order as base.py."""
    return [n for n, v in vars(FactStore).items() if callable(v) and not n.startswith("_")]


def test_every_method_on_the_contract_is_actually_exercised_here():
    """A Protocol only helps if the suite grows with it. Add a method to
    FactStore without testing it and this fails — otherwise the contract
    silently widens and we are back to trusting that backends implement it,
    which is the exact assumption that broke."""
    source = inspect.getsource(inspect.getmodule(test_add_then_search_round_trips))
    body = source.split("# --- the suite keeps itself honest", 1)[0]
    untested = [m for m in _protocol_methods() if f"store.{m}(" not in body
                and f'"{m}"' not in body]
    assert untested == [], f"FactStore methods with no test in this file: {untested}"


# --- the blank-optional-field trap -------------------------------------------

def test_a_blank_optional_field_falls_back_to_its_default(monkeypatch):
    """The Connections form writes EVERY optional field it shows, so leaving one
    blank stores NAME='' rather than omitting it — and os.getenv only applies a
    default when the variable is MISSING.

    Found the first time a real key was saved through the dashboard, which is the
    only way to hit it: a hand-edited .env just leaves the line out. A blank
    "Settle seconds" box made float("") raise on every Zep call, and a blank
    "User id" would have silently scoped the graph to "" instead of "waku" —
    which is the worse half, because nothing would have errored.
    """
    from waku.memory.semantic.base import env_or

    monkeypatch.setenv("WAKU_TEST_BLANK", "")
    assert env_or("WAKU_TEST_BLANK", "fallback") == "fallback"
    assert float(env_or("WAKU_TEST_BLANK", "2.0")) == 2.0

    monkeypatch.setenv("WAKU_TEST_BLANK", "   ")
    assert env_or("WAKU_TEST_BLANK", "fallback") == "fallback", "whitespace is blank too"

    monkeypatch.setenv("WAKU_TEST_BLANK", "real")
    assert env_or("WAKU_TEST_BLANK", "fallback") == "real"

    monkeypatch.delenv("WAKU_TEST_BLANK")
    assert env_or("WAKU_TEST_BLANK", "fallback") == "fallback"


def test_no_backend_reads_an_optional_setting_with_bare_os_getenv():
    """os.getenv(name, default) is the wrong tool for anything the Connections
    form can write blank. Required fields are fine — those use os.environ[...]
    and should raise loudly when absent."""
    import re
    from pathlib import Path

    semantic = Path(__file__).resolve().parents[2] / "waku" / "memory" / "semantic"
    offenders = []
    for path in semantic.glob("*_store.py"):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'os\.getenv\(\s*["\'][A-Z_]+["\']\s*,\s*["\'][^"\']+["\']', line):
                offenders.append(f"{path.name}:{num}")
    assert offenders == [], f"use env_or() — a blank form field defeats these: {offenders}"
