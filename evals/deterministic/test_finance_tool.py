"""DETERMINISTIC EVAL — log_pnl writes the right row, and rejects unknown accounts."""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block


def test_log_pnl_writes_row(tmp_path):
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    turn = [
        response(
            [tool_block("log_pnl", {"account": "IBKR", "pnl_amount": 200, "note": "tech rally"})],
            "tool_use",
        ),
        response([text_block("Logged.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient([gate] + turn))
    app.respond("log today's IBKR pnl: +200, tech rally")

    row = app.conn.execute(
        "SELECT account, currency, pnl_amount, note FROM finance_entries"
    ).fetchone()
    assert row["account"] == "IBKR"
    assert row["currency"] == "USD"
    assert row["pnl_amount"] == 200
    assert row["note"] == "tech rally"


def test_log_pnl_rejects_unknown_account(tmp_path):
    from waku.db import connect
    from waku.tools.finance import make_tool

    conn = connect(tmp_path)
    tool = make_tool(conn)
    result = tool.fn(account="Robinhood", pnl_amount=50)

    assert "unknown account" in result.lower()
    assert conn.execute("SELECT COUNT(*) FROM finance_entries").fetchone()[0] == 0


def test_log_pnl_refuses_duplicate_same_day_same_account(tmp_path):
    from waku.db import connect
    from waku.tools.finance import make_tool

    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(account="IBKR", pnl_amount=200, date="2026-08-12")
    result = tool.fn(account="IBKR", pnl_amount=999, date="2026-08-12")

    assert "already logged" in result.lower()
    rows = conn.execute(
        "SELECT pnl_amount FROM finance_entries WHERE account='IBKR' AND date='2026-08-12'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pnl_amount"] == 200  # original value untouched


def test_log_pnl_overwrite_corrects_the_existing_entry(tmp_path):
    from waku.db import connect
    from waku.tools.finance import make_tool

    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(account="A股", pnl_amount=-10, date="2026-08-12")
    result = tool.fn(account="A股", pnl_amount=-39, date="2026-08-12", overwrite=True)

    assert "-39" in result
    rows = conn.execute(
        "SELECT pnl_amount FROM finance_entries WHERE account='A股' AND date='2026-08-12'"
    ).fetchall()
    assert len(rows) == 1  # corrected in place, not a second row
    assert rows[0]["pnl_amount"] == -39


def test_log_pnl_overwrite_with_no_existing_entry_just_logs_it(tmp_path):
    from waku.db import connect
    from waku.tools.finance import make_tool

    conn = connect(tmp_path)
    tool = make_tool(conn)
    result = tool.fn(account="A股", pnl_amount=-39, date="2026-08-12", overwrite=True)

    assert "logged" in result.lower()
    rows = conn.execute("SELECT pnl_amount FROM finance_entries WHERE account='A股'").fetchall()
    assert len(rows) == 1
    assert rows[0]["pnl_amount"] == -39
