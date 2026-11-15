"""Each page carries the raw rows of its own table, and no page exists only
to show them again.

Until 2026-11-14 a Database page listed four tables, and every one of them
was already listed on another page: facts on Memory > Semantic, episodes on
Memory > Episodic, calendar_events on Tools > Results and chat_log on
Gateway. The page is gone. Its read-only SQL console is a page of its own
under Setup.
"""

from __future__ import annotations

from pathlib import Path

from waku.ops import dashboard

STATIC = Path(dashboard.__file__).parent / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_sidebar_has_a_sql_console_and_no_database_page():
    shell = _read("index.html")
    assert 'href="#sql"' in shell and 'data-v="sql"' in shell
    assert "#database" not in shell and 'id="n-db"' not in shell
    assert "n-db" not in _read("js/main.js")


def test_the_sql_console_is_a_view_and_the_database_view_is_gone():
    views = _read("js/views.js")
    assert "\n  sql(d){" in views and "\n  database(d" not in views
    assert "#database" not in views
    # the 5-second refresh must not wipe a query someone is typing
    assert 'view === "sql"' in _read("js/main.js")


def test_each_table_has_its_raw_rows_on_the_page_that_lists_it():
    views = _read("js/views.js")
    for table in ("facts", "episodes", "calendar_events", "chat_log"):
        assert f'rawRows(d, "{table}"' in views, table
        # and the SQL console says where each table is shown
        assert f"\n  {table}: [" in views, table
    # the handler the toggle names is a top-level function
    assert "function rawToggle(name, el)" in views


def test_the_tables_the_pages_read_are_still_in_the_payload(tmp_path, monkeypatch):
    """The raw rows come from `db.tables`, which the Database page used to
    read. Removing the page must not remove the data under it."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    names = [t["name"] for t in dashboard.collect()["db"]["tables"]]
    assert names == ["calendar_events", "facts", "episodes", "chat_log"]
