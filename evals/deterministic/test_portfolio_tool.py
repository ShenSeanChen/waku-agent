"""DETERMINISTIC EVAL — get_portfolio_performance, offline.

No live Finnhub call: urlopen is monkeypatched to a canned per-ticker
response, matching stocks.py's own eval contract of "no network in
evals/deterministic/". What's pinned: the per-position gain math, that the
tool fails the whole call (not a partial result) when one ticker's fetch
errors, and that it only registers when FINNHUB_API_KEY is set.
"""

from __future__ import annotations

import json

from waku.config import Settings
from waku.db import connect
from waku.tools import build_registry, stocks


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_quotes(monkeypatch, quotes: dict[str, dict] | None = None, raises: Exception | None = None):
    """quotes maps ticker -> {"c": price}; the ticker is read off the request URL."""
    def fake(req, timeout=None):
        if raises is not None:
            raise raises
        ticker = dict(pair.split("=") for pair in req.full_url.split("?", 1)[1].split("&"))["symbol"]
        return _FakeResponse(quotes[ticker])

    monkeypatch.setattr("waku.tools.stocks.urllib.request.urlopen", fake)


def _seeded_conn(tmp_path):
    settings = Settings(home=tmp_path / "portfolio", finnhub_api_key="fake-key")
    settings.ensure_home()
    return connect(settings.home)


def test_reports_per_position_gain_and_a_total(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path)
    # demo seed: NVDA 10sh @170 baseline, AAPL 15sh @225, MSFT 5sh @410
    _stub_quotes(monkeypatch, {"NVDA": {"c": 180.0}, "AAPL": {"c": 220.0}, "MSFT": {"c": 410.0}})
    tool = stocks.make_portfolio_tool(conn, api_key="fake-key")
    out = tool.fn()

    assert "NVDA: +$100.00 (+5.88%)" in out
    assert "AAPL: -$75.00 (-2.22%)" in out
    assert "MSFT: +$0.00 (+0.00%)" in out
    assert "Total: +$25.00" in out


def test_one_failed_ticker_fails_the_whole_call(tmp_path, monkeypatch):
    conn = _seeded_conn(tmp_path)
    _stub_quotes(monkeypatch, raises=TimeoutError("timed out"))
    tool = stocks.make_portfolio_tool(conn, api_key="fake-key")
    out = tool.fn()

    assert out.startswith("Couldn't get today's price for")
    assert "timed out" in out
    assert "Total" not in out


def test_not_registered_without_a_key(tmp_path):
    settings = Settings(home=tmp_path / "nokey", finnhub_api_key="")
    settings.ensure_home()
    conn = connect(settings.home)
    assert "get_portfolio_performance" not in build_registry(conn, settings, None)._tools


def test_registered_when_a_key_is_present(tmp_path):
    settings = Settings(home=tmp_path / "haskey", finnhub_api_key="fake-key")
    settings.ensure_home()
    conn = connect(settings.home)
    assert "get_portfolio_performance" in build_registry(conn, settings, None)._tools
