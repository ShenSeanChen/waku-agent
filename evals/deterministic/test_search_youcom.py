"""DETERMINISTIC EVAL — the youcom search backend, proven offline.

CI has no You.com key, so nothing here asserts "searching really works" —
that is verified by hand with a live key (see the PR description). What CAN
be pinned in CI is the part that would actually hurt if it broke:

  - which backend fires for which combination of env keys (the precedence
    chain is You.com → Tavily → DuckDuckGo, and it must be stable);
  - that a You.com response renders through the same three columns
    (title / snippet / url) every other backend uses;
  - that a failing keyed backend comes back as a sentence the model can
    act on, not an exception that kills the turn.

Lesson carried over from test_gh_tool.py: assert on the request that WOULD
go out, not on prose describing it. The network is faked below, so the
recorded URL/body are proof of which backend was selected.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from waku import integrations
from waku.tools import search


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def seen(monkeypatch):
    """Record every urlopen call and return canned payloads per endpoint."""
    calls: list[dict] = []

    def fake(req: urllib.request.Request, timeout: float = 0):  # noqa: ARG001
        raw = req.data.decode() if isinstance(req.data, (bytes, bytearray)) else ""
        calls.append({
            "url": req.full_url,
            "body": json.loads(raw) if raw else None,
            "headers": dict(req.headers),
        })
        if "ydc-index.io" in req.full_url:
            return FakeResponse({"results": {"web": [
                {"title": "You.com hit", "description": "a clean summary",
                 "url": "https://example.com/you"},
                {"title": "Snippets-only hit", "snippets": ["first snippet"],
                 "url": "https://example.com/snippy"},
            ]}})
        if "tavily.com" in req.full_url:
            return FakeResponse({"results": [
                {"title": "Tavily hit", "content": "tavily summary",
                 "url": "https://example.com/tavily"}]})
        raise AssertionError(f"unexpected endpoint: {req.full_url}")

    monkeypatch.setattr(search.urllib.request, "urlopen", fake)
    return calls


def _tool_fn():
    return search.make_tool().fn


# --- the precedence chain ---------------------------------------------------


def test_youcom_key_beats_tavily_key(monkeypatch, seen):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    out = _tool_fn()("query", 5)
    assert len(seen) == 1 and "ydc-index.io" in seen[0]["url"], "YDC_API_KEY must win"
    assert "(via You.com):" in out


def test_youcom_key_alone_selects_youcom(monkeypatch, seen):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = _tool_fn()("query", 5)
    assert len(seen) == 1 and "ydc-index.io" in seen[0]["url"]
    assert "(via You.com):" in out


def test_tavily_key_still_used_when_no_youcom_key(monkeypatch, seen):
    monkeypatch.delenv("YDC_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    out = _tool_fn()("query", 5)
    assert len(seen) == 1 and "tavily.com" in seen[0]["url"], "regression: plain Tavily setups must not change"
    assert "(via Tavily):" in out


def test_youcom_request_shape(monkeypatch, seen):
    """THE request test: fixed endpoint, X-API-Key header, query + count body."""
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")
    _tool_fn()("world cup fixtures", 3)
    call = seen[0]
    assert call["url"] == "https://ydc-index.io/v1/search"
    assert call["headers"]["X-api-key"] == "ydc-key"  # urllib title-cases header names
    assert call["body"] == {"query": "world cup fixtures", "count": 3}


# --- rendering through the shared three columns -----------------------------


def test_youcom_results_render_like_every_backend(monkeypatch, seen):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")
    out = _tool_fn()("query", 5)
    assert "1. You.com hit\n   a clean summary\n   https://example.com/you" in out
    assert "2. Snippets-only hit\n   first snippet\n   https://example.com/snippy" in out


def test_youcom_hits_capped_at_max_results(monkeypatch, seen):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")
    out = _tool_fn()("query", 1)
    assert "https://example.com/you" in out
    assert "https://example.com/snippy" not in out


# --- failure is a sentence, never an exception -------------------------------


def test_youcom_failure_is_honest_text(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")

    def boom(req, timeout=0):  # noqa: ARG001
        raise OSError("connection reset")

    monkeypatch.setattr(search.urllib.request, "urlopen", boom)
    out = _tool_fn()("query", 5)
    assert out.startswith("Web search failed")
    assert "connection reset" in out
    assert "ydc-key" not in out, "the key must never leak into tool output"


def test_youcom_empty_results_ask_for_a_better_query(monkeypatch, seen):
    monkeypatch.setenv("YDC_API_KEY", "ydc-key")

    def empty(req, timeout=0):  # noqa: ARG001
        return FakeResponse({"results": {"web": []}})

    monkeypatch.setattr(search.urllib.request, "urlopen", empty)
    out = _tool_fn()("query", 5)
    assert out == "No results found. Try a more specific query."


# --- the Connections registry entry ------------------------------------------

def test_youcom_is_a_registered_integration_with_a_probe():
    item = integrations._find_integration("youcom")
    assert item is not None, "youcom must appear in `waku connections` and the dashboard"
    assert item.group == "Search & Observability"
    assert [field.name for field in item.env] == ["YDC_API_KEY"]
    assert item.env[0].secret is True
    assert integrations._probed(item).probe is integrations._youcom_probe
