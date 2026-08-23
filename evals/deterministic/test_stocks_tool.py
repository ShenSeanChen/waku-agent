"""DETERMINISTIC EVAL — get_stock_price, offline.

No live Finnhub call: urlopen is monkeypatched to a canned response, matching
search.py's contract of "no network in evals/deterministic/". What's pinned:
the exact model-facing sentence (ticker, price, delay disclaimer), the honest
message for an unknown ticker, the honest message for a network/API failure,
and that the tool only registers when FINNHUB_API_KEY is set.
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


def _stub_urlopen(monkeypatch, payload: dict | None = None, raises: Exception | None = None):
    def fake(req, timeout=None):
        if raises is not None:
            raise raises
        return _FakeResponse(payload)

    monkeypatch.setattr("waku.tools.stocks.urllib.request.urlopen", fake)


def test_returns_the_exact_sentence_the_model_sees(monkeypatch):
    _stub_urlopen(monkeypatch, {"c": 187.42, "pc": 185.0})
    tool = stocks.make_tool(api_key="fake-key")
    out = tool.fn(ticker="nvda")
    assert out == (
        "NVDA is $187.42 (may be delayed or reflect the latest market close — "
        "check an official broker for a live price)."
    )


def test_unknown_ticker_is_an_honest_sentence_not_a_price(monkeypatch):
    _stub_urlopen(monkeypatch, {"c": 0, "pc": 0})
    tool = stocks.make_tool(api_key="fake-key")
    out = tool.fn(ticker="BOGUS")
    assert "No price data found for 'BOGUS'" in out
    assert "$" not in out


def test_api_failure_returns_a_sentence_not_a_traceback(monkeypatch):
    _stub_urlopen(monkeypatch, raises=TimeoutError("timed out"))
    tool = stocks.make_tool(api_key="fake-key")
    out = tool.fn(ticker="NVDA")
    assert out.startswith("Couldn't get a price for 'NVDA'")
    assert "timed out" in out


def test_not_registered_without_a_key(tmp_path):
    settings = Settings(home=tmp_path / "nokey", finnhub_api_key="")
    settings.ensure_home()
    conn = connect(settings.home)
    assert "get_stock_price" not in build_registry(conn, settings, None)._tools


def test_registered_when_a_key_is_present(tmp_path):
    settings = Settings(home=tmp_path / "haskey", finnhub_api_key="fake-key")
    settings.ensure_home()
    conn = connect(settings.home)
    assert "get_stock_price" in build_registry(conn, settings, None)._tools


def test_dashboard_catalog_matches_the_real_registry(tmp_path, monkeypatch):
    """Regression: the Tools tab has a SECOND, hand-built catalog (tools_info in
    dashboard.py) used before a live agent exists, which drifted from
    build_registry once already (the module's own comment warns about this).
    get_stock_price shipped without updating it — this pins both branches so
    the tab can't silently omit or falsely advertise the tool again."""
    from waku.ops import browser_agent, dashboard

    monkeypatch.setattr(browser_agent, "current", lambda: None)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    names = {c["name"] for c in dashboard.tools_info()["catalog"]}
    assert "get_stock_price" not in names

    monkeypatch.setenv("FINNHUB_API_KEY", "fake-key")
    catalog = dashboard.tools_info()["catalog"]
    entry = next((c for c in catalog if c["name"] == "get_stock_price"), None)
    assert entry is not None
    assert entry["source"] == "finance"
