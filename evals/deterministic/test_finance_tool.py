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
