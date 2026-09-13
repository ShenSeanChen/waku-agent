"""DETERMINISTIC EVAL — a model can't fake a "[tools used: ...]" line.

Live bug, 2026-08-12: qwen-max (openai wire format) replied to "支付宝基金
赚了173" with plain text claiming "[tools used: log_pnl(...) -> Logged
支付宝基金 2026-08-12: +173 CNY ...]" — but never emitted a tool_use block, so
log_pnl never ran. finance_entries had no row for it. The same thing happened
seconds later correcting a calendar event's title (EAPM -> EPAM): the model
claimed create_event ran and synced to Apple Calendar; state.db, calendar.ics,
and Apple Calendar all still said EAPM. The fabricated tag reached the user
and was written to chat_log as if it were the real harness-appended trace
(waku/runtime/session.py only appends that tag from actual tool_calls).

The loop must never let text-only output carry that tag through as fact.
"""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_waku, response, text_block


def test_text_only_reply_cannot_carry_a_tools_used_tag(tmp_path):
    fabricated = (
        "已经为您记录下支付宝基金今天的收益为+173 CNY.\n"
        "[tools used: log_pnl({'account': '支付宝基金', 'pnl_amount': 173}) -> "
        "Logged 支付宝基金 2026-08-12: +173 CNY (state.db, finance_entries)]"
    )
    script = [
        response([text_block('{"retrieve": false, "query": "", "reason": "t"}')]),
        response([text_block(fabricated)]),  # end_turn, NO tool_use block
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    result = app.respond("支付宝基金赚了173")

    assert "[tools used:" not in result.reply
    assert result.tool_calls == []  # nothing actually ran this turn

    # the lie must not survive into the chat log either
    row = app.conn.execute("SELECT content FROM chat_log ORDER BY id DESC LIMIT 1").fetchone()
    assert "[tools used:" not in row["content"]

    # and the tool itself was never invoked, so nothing landed
    entry = app.conn.execute(
        "SELECT id FROM finance_entries WHERE account=?", ("支付宝基金",)
    ).fetchone()
    assert entry is None


def test_real_tool_calls_still_get_their_trace_appended(tmp_path):
    """The fix must only strip FABRICATED tags — a real tool call still gets
    its honest [tools used: ...] line from session.add_exchange."""
    from evals.helpers import tool_block

    script = [
        response([text_block('{"retrieve": false, "query": "", "reason": "t"}')]),
        response(
            [tool_block("log_pnl", {"account": "雪球基金", "pnl_amount": 95})],
            stop_reason="tool_use",
        ),
        response([text_block("记下了，雪球基金今天 +95。")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    result = app.respond("雪球基金赚了95")

    assert result.tool_calls and result.tool_calls[0]["tool"] == "log_pnl"
    row = app.conn.execute("SELECT content FROM chat_log ORDER BY id DESC LIMIT 1").fetchone()
    assert "[tools used: log_pnl" in row["content"]

    entry = app.conn.execute(
        "SELECT pnl_amount FROM finance_entries WHERE account=?", ("雪球基金",)
    ).fetchone()
    assert entry["pnl_amount"] == 95
