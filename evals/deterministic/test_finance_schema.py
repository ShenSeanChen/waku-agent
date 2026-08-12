"""DETERMINISTIC EVAL — the finance/interview tables exist with the right shape."""

from __future__ import annotations

from waku.db import connect


def test_finance_entries_table_has_expected_columns(tmp_path):
    conn = connect(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(finance_entries)").fetchall()}
    assert cols == {"id", "date", "account", "currency", "pnl_amount", "note", "created_at"}


def test_interview_entries_table_has_expected_columns(tmp_path):
    conn = connect(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(interview_entries)").fetchall()}
    assert cols == {
        "id", "company", "role", "round", "date", "status", "notes", "created_at", "updated_at",
    }
