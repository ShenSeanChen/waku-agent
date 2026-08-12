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


def test_log_interview_matches_case_insensitively_and_preserves_omitted_fields(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="ByteDance", role="Backend Engineer", round="一面", status="进行中",
             notes="first round notes")
    # different capitalization on the second call, and notes omitted entirely
    tool.fn(company="bytedance", role="backend engineer", round="二面", status="待跟进")

    rows = conn.execute("SELECT company, round, status, notes FROM interview_entries").fetchall()
    assert len(rows) == 1  # matched despite the capitalization difference
    assert rows[0]["round"] == "二面"
    assert rows[0]["status"] == "待跟进"
    assert rows[0]["notes"] == "first round notes"  # preserved, not blanked, because omitted


def test_log_interview_treats_empty_string_as_omitted(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="Acme", role="PM", round="一面", status="进行中", notes="asked about roadmap")
    result = tool.fn(company="Acme", role="PM", round="", status="通过", notes="")

    row = conn.execute("SELECT round, notes FROM interview_entries").fetchone()
    assert row["round"] == "一面"  # NOT blanked, even though "" was explicitly passed
    assert row["notes"] == "asked about roadmap"
    assert "一面" in result  # the return string reports the round that's actually in the row, not "no round given"


def test_log_interview_correcting_role_updates_same_row(tmp_path):
    """Regression: matching used to require company AND role to match, so
    calling log_interview with a corrected role (fixing a typo/placeholder)
    silently created a second row instead of fixing the existing one — the
    old wrong-role row stayed in place, unaffected. Matching on company alone
    (still gated to open statuses) means a role correction lands on the same
    interview process."""
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="兴业证券", role="未指定职位", round="终面", status="进行中")
    result = tool.fn(company="兴业证券", role="AI 应用", round="终面", status="进行中")

    rows = conn.execute("SELECT company, role FROM interview_entries").fetchall()
    assert len(rows) == 1  # corrected in place, not a second row
    assert rows[0]["role"] == "AI 应用"
    assert "AI 应用" in result
