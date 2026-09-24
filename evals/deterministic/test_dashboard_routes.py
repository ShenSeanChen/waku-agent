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

import ast
import inspect
import re

from waku.ops import dashboard

# Every path the POST router accepts. `/api/compare` (non-streaming) was removed
# on 2026-07-26: nothing called it, and its implementation had drifted behind the
# streaming one badly enough to return wrong scores. `/api/connections`,
# `/api/connections/test` and `/api/providers` are here too — they are `routes`
# dict keys (value `None`) exactly like the rest of this set, just added later.
POST_ROUTES = {
    "/api/chat",
    "/api/memory",
    "/api/settings",
    "/api/query",
    "/api/session",
    "/api/pin",
    "/api/connections",
    "/api/connections/test",
    "/api/providers",
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
# dict (POST_ROUTES), and the judgment/memory-arena exact checks that were
# added beside them without a pin.
PINNED_POST = POST_ROUTES | STREAM_ROUTES | {
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


def _is_self_path(node: ast.AST | None) -> bool:
    """True for the BARE `self.path` attribute access itself — not something
    built from it."""
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _is_str_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _wraps_self_path(node: ast.AST | None) -> bool:
    """True if `node` puts self.path through a RECEIVER-preserving
    transformation — `self.path.strip()`, `self.path[1:]`,
    `self.path + "x"` — as opposed to self.path merely appearing as an
    ARGUMENT somewhere inside an unrelated call, e.g.
    `parse_qs(urlparse(self.path).query).get(...)`. Only the receiver chain
    is a plausible route-dispatch trick (compare/call the transformed path
    directly); self.path as an argument several calls deep is ordinary,
    unrelated use of the path string and must not be flagged. Recurses
    through `.attr(...)` receivers, subscripts and binary ops only — never
    through a plain call's arguments — so it stays as bounded as `routes`
    dict-name recognition: it does not chase into helper functions."""
    if node is None or _is_self_path(node):
        return False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        recv = node.func.value
        return _is_self_path(recv) or _wraps_self_path(recv)
    if isinstance(node, ast.Subscript):
        return _is_self_path(node.value) or _wraps_self_path(node.value)
    if isinstance(node, ast.BinOp):
        return (_is_self_path(node.left) or _wraps_self_path(node.left)
                or _is_self_path(node.right) or _wraps_self_path(node.right))
    return False


def _literal_str_container(node: ast.AST | None) -> list[ast.AST] | None:
    """If `node` is a tuple/list/set literal, its elements; else None. Covers
    `self.path in (...)` and the tuple form of `self.path.startswith((...))`
    — both accept a container of alternatives instead of one string."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return list(node.elts)
    return None


def _routes_dict_names(tree: ast.Module) -> set[str]:
    """Names bound to a `name = {...}` dict literal used for dispatch — today
    just `routes`. `self.path not in routes` is fine on its own: the dict's
    own keys are checked for literal-ness separately, below."""
    return {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            for t in node.targets if isinstance(t, ast.Name)}


def _ast_literal_routes(tree: ast.Module) -> set[str]:
    """Routes visible only in AST-only forms the three regexes above cannot
    see: every string literal inside `self.path in (...)` /
    `self.path not in (...)`, and inside a literal tuple argument to
    `self.path.startswith((...))`. Both are genuine literal routes — just
    written in a shape the regexes don't match — so a route registered this
    way must still show up as "found", or it could sit unpinned forever."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            elements = [node.left, *node.comparators]
            for left, op, right in zip(elements, node.ops, elements[1:]):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                other = right if _is_self_path(left) else (left if _is_self_path(right) else None)
                elts = _literal_str_container(other) if other is not None else None
                if elts is not None:
                    found |= {e.value for e in elts if _is_str_literal(e)}
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "startswith" and _is_self_path(node.func.value)
              and node.args):
            elts = _literal_str_container(node.args[0])
            if elts is not None:
                found |= {e.value for e in elts if _is_str_literal(e)}
    return found


def routes_in_dashboard() -> set[str]:
    """Every path dashboard.py matches. Four literal styles: `self.path ==
    "/x"`, `self.path.startswith("/x")` (string or literal tuple),
    `self.path in (...)` / `not in (...)`, and the route dict. A prefix is
    normalised to its path without a trailing `?`."""
    src = _source()
    found = set(_EXACT_PATH.findall(src))
    found |= {p.rstrip("?") for p in _PREFIX_PATH.findall(src)}
    routes_block = _ROUTES_DICT.search(src)
    if routes_block:
        found |= set(_DICT_KEY.findall(routes_block.group(1)))
    found |= {p.rstrip("?") for p in _ast_literal_routes(ast.parse(src))}
    return found


def dashboard_route_literal_violations() -> list[str]:
    """The AST-level twin of routes_in_dashboard(). DEFAULT-DENY by
    construction: rather than pattern-matching a fixed list of bad shapes
    (which is how round 1's Eq-only guard let a variable, a loop, and an
    `in`-operator tuple all through undetected), this enumerates every form
    `self.path` legitimately takes in dashboard.py's dispatch code and fails
    on anything else.

    Recognised, and required to be literal-only:
      - `self.path == "..."` (either order)
      - `self.path in (...)` / `self.path not in (...)` — every element a
        string literal, OR the container is the already-checked `routes` dict
      - `self.path.startswith("...")` or `self.path.startswith((...))`
      - a `routes` dict key

    Anything else that touches `self.path` in a comparison or a method call is
    a violation, not a silent pass:
      - any other comparison operator (`!=`, `<`, `is`, a chained compare)
      - `self.path` wrapped in another expression first — `self.path.strip()
        == ...`, `self.path[1:] in (...)` — not recognised, so rejected
        rather than ignored, the same way the bare-literal cases are
      - any method call on `self.path` other than `.startswith(...)`
    """
    tree = ast.parse(_source())
    routes_dict_names = _routes_dict_names(tree)
    violations: list[str] = []

    def kind(node: ast.AST) -> str:
        if _is_self_path(node):
            return "bare"
        return "wrapped" if _wraps_self_path(node) else "other"

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            elements = [node.left, *node.comparators]
            for left, op, right in zip(elements, node.ops, elements[1:]):
                lk, rk = kind(left), kind(right)
                if lk == "other" and rk == "other":
                    continue  # nothing here mentions self.path
                if lk == "wrapped" or rk == "wrapped":
                    wrapped = left if lk == "wrapped" else right
                    violations.append(
                        f"line {node.lineno}: self.path wrapped inside "
                        f"`{ast.unparse(wrapped)}` before the comparison — not "
                        f"a recognised route form; compare bare self.path "
                        f"against a literal instead")
                    continue
                other = right if lk == "bare" else left
                if isinstance(op, ast.Eq):
                    if not _is_str_literal(other):
                        violations.append(
                            f"line {node.lineno}: self.path == "
                            f"`{ast.unparse(other)}`, not a string literal — "
                            f"write the route path as a literal string")
                elif isinstance(op, (ast.In, ast.NotIn)):
                    op_word = "in" if isinstance(op, ast.In) else "not in"
                    if isinstance(other, ast.Name) and other.id in routes_dict_names:
                        continue  # membership against the routes dict, checked below
                    elts = _literal_str_container(other)
                    if elts is None or any(not _is_str_literal(e) for e in elts):
                        violations.append(
                            f"line {node.lineno}: self.path {op_word} "
                            f"`{ast.unparse(other)}`, not a tuple/list/set of "
                            f"string literals — write every route path as a "
                            f"literal string")
                else:
                    violations.append(
                        f"line {node.lineno}: self.path compared with "
                        f"`{type(op).__name__}`, a form this guard does not "
                        f"accept as a route check — use `==` or `in` against "
                        f"string literals instead")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            recv = kind(node.func.value)
            if recv == "other":
                continue
            if recv == "wrapped":
                violations.append(
                    f"line {node.lineno}: self.path wrapped inside "
                    f"`{ast.unparse(node.func.value)}` before "
                    f".{node.func.attr}(...) — not a recognised route form; "
                    f"call .startswith(...) on bare self.path instead")
                continue
            if node.func.attr != "startswith":
                violations.append(
                    f"line {node.lineno}: self.path.{node.func.attr}(...) — "
                    f"only self.path.startswith(...) is recognised as a "
                    f"route check")
                continue
            arg = node.args[0] if node.args else None
            elts = _literal_str_container(arg)
            ok = _is_str_literal(arg) or (elts is not None and all(_is_str_literal(e) for e in elts))
            if not ok:
                shown = ast.unparse(arg) if arg is not None else "<no argument>"
                violations.append(
                    f"line {node.lineno}: self.path.startswith(`{shown}`), "
                    f"not a string literal (or tuple of them) — write every "
                    f"route path as a literal string")
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
              and any(isinstance(t, ast.Name) and t.id == "routes" for t in node.targets)):
            for key in node.value.keys:
                if not _is_str_literal(key):
                    line = key.lineno if key is not None else node.lineno
                    shown = ast.unparse(key) if key is not None else "**unpacked entry**"
                    violations.append(
                        f"line {line}: routes dict key `{shown}`, not a string "
                        f"literal — write the route path as a literal string")
    return violations


def test_every_dashboard_route_comparison_is_a_string_literal():
    """CONTROLLER RULING (A5 review round 1, C1; hardened round 2 after the
    `in`-operator bypass got through an Eq-only version of this guard).
    routes_in_dashboard() is fooled by indirection — a variable, a loop, an
    `in ("/x",)` tuple, self.path wrapped in another expression — all fully
    live and dispatchable, all invisible to a regex or a guard that only
    checks the one form it was written for. Rather than teach the extractor
    to chase variables (which leads to loops, then helpers, then getattr, and
    never ends), this constrains dashboard.py's dispatch code instead, and
    defaults to FAILING on any comparison against self.path it doesn't
    positively recognise as literal — see dashboard_route_literal_violations
    for the full list of recognised forms. That makes routes_in_dashboard()
    complete by construction — there is no longer a shape of route it could
    miss — instead of complete by effort."""
    violations = dashboard_route_literal_violations()
    assert not violations, (
        "dashboard.py compares self.path against something other than a "
        "string literal (or uses a route-check form this guard doesn't "
        "recognise), so routes_in_dashboard() cannot see it and it can reach "
        "production unpinned. Write the route path as a literal string at "
        "the comparison instead:\n" + "\n".join(violations))


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
