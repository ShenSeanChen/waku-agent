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


def _source() -> str:
    return inspect.getsource(dashboard)


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


def test_the_handlers_behind_the_routes_exist_and_are_callable():
    for name in ("collect", "chat", "chat_stream", "compare_stream", "graph_stream", "memory_action",
                 "apply_settings", "run_query", "session_action", "pin_action",
                 "list_models", "events_since", "reveal_path", "settings_info",
                 "tools_info", "compare_clear", "compare_regrade", "compare_delete_run"):
        fn = getattr(dashboard, name, None)
        assert callable(fn), f"handler missing or not callable: {name}"


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
