"""OFFLINE search_web backend selection: Tavily, Firecrawl, DuckDuckGo.

No network. urlopen is stubbed so the picker and payload shape are pinned
without depending on a live key.
"""

from __future__ import annotations

import io
import json

from waku.tools import search


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _install_urlopen(monkeypatch, captured: dict, payload: bytes, url_contains: str | None = None):
    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["auth"] = req.headers.get("Authorization") or req.get_header("Authorization")
        if url_contains:
            assert url_contains in req.full_url, req.full_url
        return _Response(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _clear_search_env(monkeypatch):
    for name in ("TAVILY_API_KEY", "WAKU_SEARCH_API_KEY", "FIRECRAWL_API_KEY",
                 "WAKU_SEARCH_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def test_firecrawl_key_wins_over_tavily(monkeypatch):
    """Pasting FIRECRAWL_API_KEY is the signal to use Firecrawl, even if Tavily is also set."""
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    captured = {}
    payload = json.dumps({
        "data": {"web": [{"title": "Fire", "markdown": "md body", "url": "https://fc.example"}]},
    }).encode()
    _install_urlopen(monkeypatch, captured, payload, url_contains="api.firecrawl.dev/v2/search")
    out = search.make_tool().fn("best tools", max_results=2)
    assert "via Firecrawl" in out
    assert "https://fc.example" in out
    assert "md body" in out
    body = json.loads(captured["body"])
    assert body == {"query": "best tools", "limit": 2}
    assert captured["auth"] == "Bearer fc-test"


def test_env_override_can_force_tavily(monkeypatch):
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("WAKU_SEARCH_BACKEND", "tavily")
    captured = {}
    _install_urlopen(
        monkeypatch, captured,
        json.dumps({"results": [{"title": "A", "content": "snip", "url": "https://a.example"}]}).encode(),
        url_contains="api.tavily.com",
    )
    out = search.make_tool().fn("world cup", max_results=3)
    assert "via Tavily" in out
    assert "api.firecrawl.dev" not in captured["url"]


def test_firecrawl_only_auto_selects_firecrawl(monkeypatch):
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    captured = {}
    _install_urlopen(
        monkeypatch, captured,
        json.dumps([{"title": "List", "description": "desc", "url": "https://list.example"}]).encode(),
        url_contains="api.firecrawl.dev",
    )
    out = search.make_tool().fn("query")
    assert "via Firecrawl" in out
    assert "desc" in out


def test_explicit_firecrawl_without_key_is_honest(monkeypatch):
    _clear_search_env(monkeypatch)
    monkeypatch.setenv("WAKU_SEARCH_BACKEND", "firecrawl")
    out = search.make_tool().fn("query")
    assert "FIRECRAWL_API_KEY is empty" in out


def test_no_keys_uses_duckduckgo(monkeypatch):
    _clear_search_env(monkeypatch)
    captured = {}
    html = (
        b'<a class="result__a" href="https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fddg.example">'
        b'Title</a><a class="result__snippet">Snippet</a>'
    )
    _install_urlopen(monkeypatch, captured, html, url_contains="duckduckgo.com")
    out = search.make_tool().fn("query")
    assert "via DuckDuckGo" in out
    assert "https://ddg.example" in out
