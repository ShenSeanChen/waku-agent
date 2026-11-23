"""Conversation previews mark truncation without changing the stored reply."""

import sqlite3

import pytest

from waku.ops.dashboard import session_list


@pytest.mark.parametrize("role, prefix", [("assistant", "waku: "), ("user", "you: ")])
@pytest.mark.parametrize("length", [0, 12, 80, 81, 200])
def test_session_preview_marks_only_truncated_messages(role, prefix, length):
    content = "羽" * length
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE chat_log (id INTEGER, session_id TEXT, role TEXT, "
            "content TEXT, source TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO chat_log VALUES (1, 'conversation', ?, ?, 'dashboard', '2026-09-28')",
            (role, content),
        )

        preview = session_list(conn)[0]["last"]

        if length > 80:
            assert preview == prefix + "羽" * 80 + "..."
        else:
            assert preview == prefix + content
        assert conn.execute("SELECT content FROM chat_log").fetchone()["content"] == content
