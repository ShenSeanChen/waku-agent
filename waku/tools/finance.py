"""log_pnl — records a day's investment profit/loss against a fixed account.

Append-only, currency-aware, no FX conversion (CNY and USD accounts are never
summed together). See docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls

from waku.tools.registry import Tool

# Fixed account → currency map. Adding an account means editing this dict and
# nothing else the tool touches (the dashboard reads it too, Task 6).
ACCOUNTS: dict[str, str] = {
    "A股": "CNY",
    "支付宝基金": "CNY",
    "雪球基金": "CNY",
    "IBKR": "USD",
    "BIT": "USD",
}


def make_tool(conn: sqlite3.Connection) -> Tool:
    def log_pnl(
        account: str, pnl_amount: float, date: str = "", note: str = "", overwrite: bool = False
    ) -> str:
        if account not in ACCOUNTS:
            valid = ", ".join(ACCOUNTS)
            return f"Error: unknown account '{account}'. Valid accounts: {valid}"
        currency = ACCOUNTS[account]
        entry_date = date or date_cls.today().isoformat()
        existing = conn.execute(
            "SELECT id, pnl_amount, currency FROM finance_entries WHERE account=? AND date=?",
            (account, entry_date),
        ).fetchone()
        if existing and not overwrite:
            sign = "+" if existing["pnl_amount"] >= 0 else ""
            return (
                f"Already logged {account} for {entry_date}: {sign}{existing['pnl_amount']} {existing['currency']}. "
                f"If that's wrong, ask me to correct it and I'll overwrite it — just say the new amount again."
            )
        sign = "+" if pnl_amount >= 0 else ""
        if existing:
            conn.execute(
                "UPDATE finance_entries SET pnl_amount=?, note=? WHERE id=?",
                (pnl_amount, note, existing["id"]),
            )
            conn.commit()
            return (
                f"Corrected {account} {entry_date}: {sign}{pnl_amount} {currency} "
                f"(was {existing['pnl_amount']} {existing['currency']}) (state.db, finance_entries)"
            )
        conn.execute(
            "INSERT INTO finance_entries (date, account, currency, pnl_amount, note) VALUES (?,?,?,?,?)",
            (entry_date, account, currency, pnl_amount, note),
        )
        conn.commit()
        return f"Logged {account} {entry_date}: {sign}{pnl_amount} {currency} (state.db, finance_entries)"

    return Tool(
        name="log_pnl",
        description=(
            "Record a day's profit/loss for one of the user's investment accounts. "
            "Use when the user reports how much they made or lost today in a specific "
            f"account. Valid accounts: {', '.join(ACCOUNTS)}. Never guess an account name "
            "that isn't in that list — ask which one they mean instead. If this returns "
            "'Already logged ...', that account already has an entry for that day — ask the "
            "user to confirm before calling again with overwrite=true to correct it; never "
            "set overwrite=true without the user explicitly confirming the correction."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "enum": list(ACCOUNTS),
                    "description": "Which account this P&L belongs to",
                },
                "pnl_amount": {
                    "type": "number",
                    "description": "Signed profit/loss for the day, in the account's own currency",
                },
                "date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD); defaults to today if omitted",
                },
                "note": {"type": "string", "description": "Optional short note, e.g. why"},
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "Set true only after the user has confirmed they want to correct an "
                        "existing same-day entry for this account. Defaults to false."
                    ),
                },
            },
            "required": ["account", "pnl_amount"],
        },
        fn=log_pnl,
    )
