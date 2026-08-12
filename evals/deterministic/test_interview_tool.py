"""DETERMINISTIC EVAL — log_interview creates a row, then updates the SAME
row as an interview progresses through rounds, instead of fragmenting."""

from __future__ import annotations

from waku.db import connect
from waku.tools.interviews import make_tool


def test_log_interview_creates_row(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    result = tool.fn(company="ByteDance", role="Backend Engineer", round="一面",
                      status="进行中", notes="asked about system design")

    assert "ByteDance" in result
    row = conn.execute("SELECT company, role, round, status, notes FROM interview_entries").fetchone()
    assert row["company"] == "ByteDance"
    assert row["round"] == "一面"
    assert row["status"] == "进行中"


def test_log_interview_updates_open_row_on_next_round(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="ByteDance", role="Backend Engineer", round="一面", status="进行中")
    tool.fn(company="ByteDance", role="Backend Engineer", round="二面", status="待跟进",
             notes="waiting to hear back")

    rows = conn.execute("SELECT round, status, notes FROM interview_entries").fetchall()
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0]["round"] == "二面"
    assert rows[0]["status"] == "待跟进"
    assert rows[0]["notes"] == "waiting to hear back"


def test_log_interview_starts_fresh_row_after_closed(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="ByteDance", role="Backend Engineer", round="HR面", status="失败")
    tool.fn(company="ByteDance", role="Backend Engineer", round="一面", status="进行中")

    rows = conn.execute("SELECT status FROM interview_entries ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["失败", "进行中"]
