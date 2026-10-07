"""DETERMINISTIC EVAL -- save_note tells the model the truth about where a fact went
(spec 018).

`save_note` wrote to the agent's own facts table only, and answered "Saved to
memory". With Waku Memory connected the model then told the person "saved" while
Waku Memory had nothing. Now the tool sends the fact through the same `remember`
callable consolidation uses, and its answer names the outcome. A real sqlite home
stands in for the store; a plain function stands in for Waku Memory.
"""

from __future__ import annotations

from types import SimpleNamespace

from waku.db import connect
from waku.memory.semantic.store import SqliteFactStore
from waku.tools.notes import make_tool


def _tool(tmp_path, remember):
    conn = connect(tmp_path)
    memory = SimpleNamespace(remember=remember)
    return conn, make_tool(conn, memory)


def _row(conn):
    return conn.execute("SELECT subject, content, source, synced, scope FROM facts").fetchone()


def test_a_connected_save_goes_to_waku_memory_and_the_answer_gives_the_id(tmp_path):
    sent = []

    def remember(body, scope):
        sent.append((body, scope))
        return "mem-1"

    conn, tool = _tool(tmp_path, remember)
    answer = tool.fn("Alex", "Alex prefers morning meetings.")
    assert answer == "Saved here and in Waku Memory (id mem-1)."
    assert sent == [("Alex prefers morning meetings.", "global")]
    assert tuple(_row(conn)) == ("alex", "Alex prefers morning meetings.", "user", 1, "global")


def test_a_failed_send_says_so_and_leaves_the_fact_waiting_for_a_retry(tmp_path):
    def remember(body, scope):
        raise RuntimeError("Waku Memory answered 503")

    conn, tool = _tool(tmp_path, remember)
    answer = tool.fn("Alex", "Alex prefers morning meetings.")
    assert answer == ("Saved here. Waku Memory did not take it "
                      "(Waku Memory answered 503); it is sent again later.")
    row = _row(conn)
    assert (row["synced"], row["scope"]) == (0, "global")
    assert [f["content"] for f in SqliteFactStore(conn).unsynced()] == ["Alex prefers morning meetings."]


def test_a_long_failure_reason_is_cut_and_kept_on_one_line(tmp_path):
    def remember(body, scope):
        raise RuntimeError("first line\n" + "x" * 300)

    _, tool = _tool(tmp_path, remember)
    answer = tool.fn("Alex", "A fact.")
    assert "\n" not in answer
    assert len(answer) < 200


def test_with_no_waku_memory_the_fact_is_saved_here_and_the_answer_says_only_that(tmp_path):
    conn, tool = _tool(tmp_path, None)
    assert tool.fn("Alex", "A fact.") == "Saved here. Waku Memory is not connected."
    assert _row(conn)["synced"] == 1


def test_with_no_memory_object_at_all_the_answer_is_the_same(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    assert tool.fn("Alex", "A fact.") == "Saved here. Waku Memory is not connected."


def test_a_server_that_names_no_id_still_counts_as_taken(tmp_path):
    _, tool = _tool(tmp_path, lambda body, scope: None)
    assert tool.fn("Alex", "A fact.") == "Saved here and in Waku Memory."


from waku.app import Waku


def test_a_fact_save_note_saved_counts_as_read_so_consolidation_leaves_it_out():
    result = SimpleNamespace(
        recalled="",
        tool_calls=[{"tool": "save_note",
                     "args": {"subject": "Alex", "content": "Alex prefers morning meetings."},
                     "output": "Saved here and in Waku Memory (id mem-1)."}],
    )
    text = Waku._recalled(SimpleNamespace(mcp_bridge=None), result)
    assert "Alex prefers morning meetings." in text
