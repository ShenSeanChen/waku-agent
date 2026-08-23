"""get_stock_price — latest quote for a well-known public company.

Same shape as search.py: stdlib urllib only, zero new dependencies. Uses
Finnhub's quote endpoint (a real, documented REST API) instead of scraping an
undocumented endpoint the way yfinance does.

get_portfolio_performance — today's gain/loss on a small, fixed demo
portfolio (waku/db.py's portfolio_positions table). Read-only, distinct tool
from get_stock_price so the model can tell "quote me a ticker" apart from
"how's my portfolio doing" — but it shares this file and _fetch_quote because
it's the same Finnhub client, just a second use of it.

Both tools are registered only when FINNHUB_API_KEY is set (see __init__.py)
— unlike search_web there is no keyless fallback, so a tool the model could
call and always fail is worse than no tool at all.

v1 scope, on purpose: this only takes a ticker (NVDA, AAPL, META, ...), not a
company name. The model is expected to already know the ticker for well-known
companies from its own training; there is no name→ticker lookup yet.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request

from waku.tools.registry import Tool

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_TIMEOUT = 10
_DISCLAIMER = "may be delayed or reflect the latest market close — check an official broker for a live price"


def _fetch_quote(ticker: str, api_key: str) -> dict:
    params = urllib.parse.urlencode({"symbol": ticker, "token": api_key})
    req = urllib.request.Request(f"{_QUOTE_URL}?{params}")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


def make_tool(api_key: str) -> Tool:
    def get_stock_price(ticker: str) -> str:
        ticker = ticker.strip().upper()
        try:
            quote = _fetch_quote(ticker, api_key)
        except Exception as exc:
            return (f"Couldn't get a price for '{ticker}': {exc}. The Finnhub API may be "
                     "rate-limited or down — try again shortly, or check FINNHUB_API_KEY.")
        price = quote.get("c")
        prev_close = quote.get("pc")
        if not price and not prev_close:
            return (f"No price data found for '{ticker}'. Check the ticker symbol is "
                     "correct (e.g. NVDA for Nvidia).")
        return f"{ticker} is ${price:.2f} ({_DISCLAIMER})."

    return Tool(
        name="get_stock_price",
        description=(
            "Look up the latest known price for a well-known public company's stock, by "
            "ticker symbol (e.g. NVDA, AAPL, META). Only reliable for major, widely-known "
            "companies — you must already know the ticker; this tool does not resolve "
            "company names to symbols. The price returned may be delayed or from the last "
            "market close, not a live feed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "stock ticker symbol, e.g. 'NVDA'"},
            },
            "required": ["ticker"],
        },
        fn=get_stock_price,
    )


def make_portfolio_tool(conn: sqlite3.Connection, api_key: str) -> Tool:
    def get_portfolio_performance() -> str:
        rows = conn.execute(
            "SELECT ticker, shares, price_yesterday, average_price FROM portfolio_positions"
        ).fetchall()
        if not rows:
            return "No positions in the portfolio."

        lines = []
        total_gain = 0.0
        total_baseline_value = 0.0
        for row in rows:
            ticker = row["ticker"]
            try:
                quote = _fetch_quote(ticker, api_key)
            except Exception as exc:
                return (f"Couldn't get today's price for '{ticker}': {exc}. Portfolio "
                         "performance needs a price for every position, so no gain is reported.")
            price = quote.get("c")
            if not price:
                return f"No current price returned for '{ticker}' — can't compute portfolio gain."

            shares = row["shares"]
            baseline = row["price_yesterday"]
            gain = (price - baseline) * shares
            pct = (price - baseline) / baseline * 100
            total_gain += gain
            total_baseline_value += baseline * shares
            sign = "+" if gain >= 0 else "-"
            lines.append(f"{ticker}: {sign}${abs(gain):.2f} ({sign}{abs(pct):.2f}%) today "
                         f"({shares:g} shares at ${price:.2f})")

        total_pct = (total_gain / total_baseline_value * 100) if total_baseline_value else 0.0
        sign = "+" if total_gain >= 0 else "-"
        lines.append(f"Total: {sign}${abs(total_gain):.2f} ({sign}{abs(total_pct):.2f}%) today ({_DISCLAIMER})")
        return "\n".join(lines)

    return Tool(
        name="get_portfolio_performance",
        description=(
            "Report today's dollar and percent gain or loss for each position in the user's "
            "portfolio, plus a total. This is a small, fixed demo portfolio — not a real, "
            "live brokerage account — and it's read-only; there is no tool to add, remove, "
            "or edit positions."
        ),
        input_schema={"type": "object", "properties": {}},
        fn=get_portfolio_performance,
    )
