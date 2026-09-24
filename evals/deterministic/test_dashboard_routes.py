"""DETERMINISTIC EVAL — the dashboard's public surface, pinned.

This is a characterization net, not a feature test. It exists so that moving or
deleting code inside dashboard.py cannot silently remove a URL the browser calls
or a key the page reads. Written BEFORE the dead-code deletion and the 1,695 ->
~880 line split, and it stayed green through both without a single edit to the
route lists — which is the whole point: the browser's contract never moved, only
the code behind it.

The handler list below is checked with getattr(dashboard, ...), so it also pins
the re-exports. After the split, most handlers LIVE in arena / catalog /
settings_api / browser_agent and are imported here. If one stops being reachable
from `dashboard`, the router breaks and this fails.

If you add a route or a payload key on purpose, update the list below in the
same commit — that edit is the review signal that the public surface changed.
"""

from __future__ import annotations

import inspect
import re

from waku.ops import dashboard

# Every path the POST router accepts. `/api/compare` (non-streaming) was removed
# on 2026-07-26: nothing called it, and its implementation had drifted behind the
# streaming one badly enough to return wrong scores.
POST_ROUTES = {
    "/api/chat",
    "/api/memory",
    "/api/settings",
    "/api/query",
    "/api/session",
    "/api/pin",
    "/api/compare/clear",
    "/api/compare/regrade",
    "/api/compare/delete_run",
}

# Paths served on GET, either exactly or as a prefix.
GET_PATHS = {
    "/api/data",
    "/api/models",
    "/api/events",
    "/api/reveal",
    "/api/compare/history",
    "/static/",
}

# Streaming endpoints. These are what the dashboard actually uses for chat and
# racing; the browser reads them as SSE, so they are handled before the router.
STREAM_ROUTES = {"/api/chat/stream", "/api/compare/stream", "/api/voice",
                 "/api/graph/stream"}

# Every path do_GET matches, exactly or as a prefix. Group A's Judgment Arena
# and Memory Arena added routes here without ever landing in GET_PATHS above —
# this is the set that closes that gap and that group E's gateway policy table
# reads from.
PINNED_GET = GET_PATHS | {
    "/api/judgment-arena",
    "/api/memory-arena",
    "/api/memory-arena/stores",
}

# Every path do_POST matches: the streaming `if self.path == ...` checks handled
# before the router (STREAM_ROUTES, which live inside do_POST too), the route
# dict (POST_ROUTES), and the handful of exact checks that were added beside
# them without a pin — providers, connections, and the arena routes.
PINNED_POST = POST_ROUTES | STREAM_ROUTES | {
    "/api/connections",
    "/api/connections/test",
    "/api/providers",
    "/api/judgment-arena/key",
    "/api/judgment-arena/stream",
    "/api/memory-arena/clean",
    "/api/memory-arena/stream",
}

_EXACT_PATH = re.compile(r'self\.path\s*==\s*"([^"]+)"')
_PREFIX_PATH = re.compile(r'self\.path\.startswith\("([^"]+)"\)')
_ROUTES_DICT = re.compile(r"routes\s*=\s*\{([^}]*)\}", re.DOTALL)
_DICT_KEY = re.compile(r'"([^"]+)"\s*:')


def _source() -> str:
    return inspect.getsource(dashboard)


def routes_in_dashboard() -> set[str]:
    """Every path dashboard.py matches, in all three styles it uses:
    `self.path == "/x"`, `self.path.startswith("/x")`, and the route dict.
    A prefix is normalised to its path without a trailing `?`."""
    src = _source()
    found = set(_EXACT_PATH.findall(src))
    found |= {p.rstrip("?") for p in _PREFIX_PATH.findall(src)}
    routes_block = _ROUTES_DICT.search(src)
    if routes_block:
        found |= set(_DICT_KEY.findall(routes_block.group(1)))
    return found


def test_every_post_route_is_still_registered():
    src = _source()
    for path in POST_ROUTES:
        assert f'"{path}"' in src, f"POST route disappeared: {path}"


def test_every_get_path_is_still_served():
    src = _source()
    for path in GET_PATHS:
        assert f'"{path}"' in src, f"GET path disappeared: {path}"


def test_streaming_routes_survive():
    """The dashboard's chat dock and the Arena both depend on these. Losing one
    breaks the UI silently — the fetch just 404s into a dead column."""
    src = _source()
    for path in STREAM_ROUTES:
        assert f'"{path}"' in src, f"streaming route disappeared: {path}"


def test_every_route_in_the_handler_is_pinned():
    """Both directions. A route added without a pin fails here, and so does
    a pin whose route was deleted — the gateway's policy table in group E
    reads these sets and a stale entry is as bad as a missing one."""
    found = routes_in_dashboard()          # all three styles, see below
    assert found == PINNED_GET | PINNED_POST, (
        f"unpinned: {sorted(found - (PINNED_GET | PINNED_POST))}\n"
        f"stale pins: {sorted((PINNED_GET | PINNED_POST) - found)}")


def test_the_handlers_behind_the_routes_exist_and_are_callable():
    for name in ("collect", "chat", "chat_stream", "compare_stream", "graph_stream", "memory_action",
                 "apply_settings", "run_query", "session_action", "pin_action",
                 "list_models", "events_since", "reveal_path", "settings_info",
                 "tools_info", "compare_clear", "compare_regrade", "compare_delete_run"):
        fn = getattr(dashboard, name, None)
        assert callable(fn), f"handler missing or not callable: {name}"


def test_api_models_returns_picker_contract(monkeypatch):
    """The model picker depends on /api/models returning models and listed."""
    import io
    import json
    import urllib.request

    from waku.ops import catalog

    monkeypatch.setenv("WAKU_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("WAKU_MODEL", raising=False)
    monkeypatch.delenv("WAKU_SMALL_MODEL", raising=False)

    def fake_urlopen(req, timeout=10):
        return io.BytesIO(json.dumps(
            {"data": [{"id": "vendor/model:free"}]}
        ).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    catalog._models_cache.clear()

    result = catalog.list_models("openrouter")
    assert result["listed"] is True
    assert isinstance(result["models"], list)
    assert result["models"][0]["id"] == "vendor/model:free"

    catalog._models_cache.clear()


def test_collect_returns_the_keys_the_page_reads():
    """`/api/data` is read by every view in static/js/. These are the keys the
    frontend indexes into; dropping one blanks a tab with no error."""
    expected = {
        "settings", "tools", "facts", "episodes", "soul", "chat_log", "sessions",
        "turns", "stats", "db", "skills", "trace_file", "chat_pending", "graph",
    }
    src = inspect.getsource(dashboard.collect)
    for key in expected:
        assert f'"{key}"' in src, f"collect() no longer returns: {key}"


def test_the_removed_arena_duplicate_stays_removed():
    """_compare_one and compare_models were a stale copy of the arena, missing
    completion scoring, quality grading, sub-agent relay, history recording and
    the scoring module. Anyone reaching them got a race that looked right and
    was quietly wrong. Re-adding a second racing path should be deliberate."""
    src = _source()
    assert "def _compare_one" not in src
    assert "def compare_models" not in src
