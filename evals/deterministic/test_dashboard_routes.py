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


def _source() -> str:
    return inspect.getsource(dashboard)


def _is_self_path(node: ast.AST | None) -> bool:
    """True for the BARE `self.path` attribute access itself — not something
    built from it."""
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _is_str_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """id(child) -> parent, for every node in `tree`. Used only to describe a
    violation (show what self.path is sitting inside); nothing here decides
    pass/fail on its own."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _describe(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        return type(node).__name__
    return text if len(text) <= 80 else text[:77] + "..."


def _literal_str_container(node: ast.AST | None) -> list[ast.AST] | None:
    """If `node` is a tuple/list/set literal, its elements; else None. Covers
    `self.path in (...)` and the tuple form of `self.path.startswith((...))`
    — both accept a container of alternatives instead of one string."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return list(node.elts)
    return None


def _dispatch_dict_names(tree: ast.Module) -> dict[str, int]:
    """Every name self.path is DISPATCHED against — `self.path in NAME`,
    `self.path not in NAME`, or `NAME[self.path]` — mapped to the line where
    it is first used that way.

    CONTROLLER RULING (A5 review round 3): what makes a dict a routing table
    is what the code DOES with it, not what it is called. Round 3 trusted any
    name bound to a dict literal, but only validated and pinned the keys of
    the one literally named `routes`, so `if self.path in STATIC_TYPES:` — or
    in any new `_ADMIN_ROUTES = {...}` a contributor adds by analogy with the
    `routes` dict a few hundred lines away — dispatched live with every test
    green. Deciding by the use site closes that without hardcoding a name:

      - `STATIC_TYPES` is an extension -> MIME lookup. It is read once, as
        `STATIC_TYPES.get(target.suffix, ...)`, INSIDE an already-pinned
        route's handler. self.path never meets it, so it is not a dispatch
        surface and nothing here touches it — no false positive on a
        legitimate content-type table.
      - Write `if self.path in STATIC_TYPES:` tomorrow and that same table
        becomes a dispatch surface by that act alone: its keys are collected
        as routes and have to be pinned, which is exactly the loud failure
        that case deserves.
      - Rename `routes` to anything at all and the guard follows it.
    """
    names: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            elements = [node.left, *node.comparators]
            for left, op, right in zip(elements, node.ops, elements[1:]):
                # Only `self.path in NAME`, never `NAME in self.path` — the
                # latter is a substring test, not a lookup, and falls through
                # to the literal-container check in the guard below.
                if isinstance(op, (ast.In, ast.NotIn)) and _is_self_path(left) \
                        and isinstance(right, ast.Name):
                    names.setdefault(right.id, node.lineno)
        elif (isinstance(node, ast.Subscript) and _is_self_path(node.slice)
              and isinstance(node.value, ast.Name)):
            names.setdefault(node.value.id, node.lineno)
    return names


def _dispatch_dict_facts(tree: ast.Module) -> tuple[set[str], list[str]]:
    """The routes every dispatch dict registers, and a violation for each one
    the guard cannot read. A dispatch dict must resolve to a `name = {...}`
    literal in this module and all of its keys must be string literals —
    exactly what `routes` has always had to satisfy, now applied to whatever
    name self.path is actually matched against."""
    used = _dispatch_dict_names(tree)
    literals: dict[str, list[ast.Dict]] = {}
    for node in ast.walk(tree):
        # `name = {...}` and the annotated `name: dict[...] = {...}`. Both are
        # a dict literal bound to a name; a type annotation is not a reason to
        # stop reading the keys.
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not targets or not isinstance(node.value, ast.Dict):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                literals.setdefault(target.id, []).append(node.value)

    routes: set[str] = set()
    violations: list[str] = []
    for name, line in sorted(used.items(), key=lambda item: (item[1], item[0])):
        dicts = literals.get(name)
        if not dicts:
            violations.append(
                f"line {line}: self.path is matched against `{name}`, which is "
                f"not assigned a `{name} = {{...}}` dict literal in this "
                f"module — the guard cannot read its keys, so the routes it "
                f"registers would reach production unpinned; write the "
                f"dispatch table as a dict literal here")
            continue
        for literal in dicts:
            for key in literal.keys:
                if _is_str_literal(key):
                    routes.add(key.value)
                    continue
                key_line = key.lineno if key is not None else literal.lineno
                shown = _describe(key) if key is not None else "**unpacked entry**"
                violations.append(
                    f"line {key_line}: `{name}` dict key `{shown}`, not a "
                    f"string literal — self.path is matched against this "
                    f"dict, so every key of it is a route and has to be "
                    f"written as a literal string")
    return routes, violations


def _ast_literal_routes(tree: ast.Module) -> set[str]:
    """Routes visible only in AST-only forms the three regexes above cannot
    see: every string literal inside `self.path in (...)` /
    `self.path not in (...)`, and inside a literal tuple argument to
    `self.path.startswith((...))`. Both are genuine literal routes — just
    written in a shape the regexes don't match — so a route registered this
    way must still show up as "found", or it could sit unpinned forever. This
    also covers reversed-order `"/literal" == self.path`: the regex above
    requires `self.path ==` textually, so a route written the other way
    round is invisible to it even though it is fully literal — the AST
    check below looks at both sides, same as the guard does."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            elements = [node.left, *node.comparators]
            for left, op, right in zip(elements, node.ops, elements[1:]):
                for side, other in ((left, right), (right, left)):
                    if not _is_self_path(side):
                        continue
                    if isinstance(op, ast.Eq) and _is_str_literal(other):
                        found.add(other.value)
                    elif isinstance(op, (ast.In, ast.NotIn)):
                        elts = _literal_str_container(other)
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
    `self.path in (...)` / `not in (...)`, and the keys of every dispatch
    dict — any dict self.path is matched against, not just the one named
    `routes`. A prefix is normalised to its path without a trailing `?`."""
    src = _source()
    tree = ast.parse(src)
    found = set(_EXACT_PATH.findall(src))
    found |= {p.rstrip("?") for p in _PREFIX_PATH.findall(src)}
    found |= {p.rstrip("?") for p in _ast_literal_routes(tree)}
    found |= _dispatch_dict_facts(tree)[0]
    return found


def dashboard_route_literal_violations() -> list[str]:
    """The AST-level twin of routes_in_dashboard(). CONTROLLER RULING (A5
    review round 2): rounds 1 and 2 both enumerated COMPARISON FORMS
    (`self.path == ...`, then also `in`/`startswith`/wrapped-expressions) —
    an open set. Each round closed what the last review found and the next
    reviewer found six more (reversed `==`, an f-string, an `IfExp`, a
    walrus, `urlparse(self.path)`, `re.match`, a `match` statement). Adding
    shape #7 would just invite #8.

    So this walks the closed set instead: every OCCURRENCE of `self.path` —
    it is one specific attribute access, and ast.walk finds all of them,
    full stop. For each occurrence, this asks only "is this sitting in one
    of a few whitelisted, literal-only positions", and fails if not. How the
    surrounding expression is written no longer matters: an f-string, an
    IfExp, a walrus, or any other wrapper simply never lands self.path in a
    whitelisted position, so it fails automatically without this guard
    having to have been written with that shape in mind.

    Whitelisted positions for a self.path occurrence (all require the OTHER
    side to be a string literal, or a literal tuple/list/set of them):
      - either side of `self.path == "..."` or `self.path != "..."`
      - either side of `self.path in (...)` / `self.path not in (...)` — or
        the container is a dispatch dict, whose keys are then validated and
        collected as routes by `_dispatch_dict_facts`, whatever it is named
      - the receiver of `self.path.startswith(...)`
      - the index of `NAME[self.path]` (a lookup into a dispatch dict, whose
        keys are validated and pinned the same way)
      - the argument to `urlparse(self.path).query` — reading the QUERY
        STRING is not a route match the way reading `.path` back out would
        be, and today's code only ever reads `.query`; `urlparse(self.path)
        .path == "..."` (one of the six shapes this round closes) is a
        DIFFERENT position — the parent is `.path`, not `.query` — so it is
        deliberately NOT covered by this one and still fails
      - the argument to `self._serve_static(self.path)` — a call to the
        handler's own method, not a hand-off outside the class (that is
        `_self_passed_to_helper`'s job), already reached only after
        `self.path.startswith("/static/")` passed

    Every other occurrence is a violation: an f-string, an `IfExp`, a
    walrus, an argument to any other call (`re.match(pattern, self.path)`),
    a subscript, a `match` subject, an unrecognised comparison operator —
    anything at all. There is deliberately no "match statement with literal
    cases" carve-out: dashboard.py does not use one today, and adding that
    exception would be exactly the kind of shape-chasing this rewrite exists
    to stop.
    """
    tree = ast.parse(_source())
    dispatch_dicts = _dispatch_dict_names(tree)
    parents = _parent_map(tree)
    violations: list[str] = _dispatch_dict_facts(tree)[1]
    handled: set[int] = set()  # id() of every self.path node matched to a whitelisted position

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            elements = [node.left, *node.comparators]
            for left, op, right in zip(elements, node.ops, elements[1:]):
                for side, other in ((left, right), (right, left)):
                    if not _is_self_path(side):
                        continue
                    handled.add(id(side))
                    if isinstance(op, (ast.Eq, ast.NotEq)):
                        if not _is_str_literal(other):
                            violations.append(
                                f"line {node.lineno}: self.path compared "
                                f"(`{type(op).__name__}`) against "
                                f"`{_describe(other)}`, not a string literal "
                                f"— write the route path as a literal string")
                    elif isinstance(op, (ast.In, ast.NotIn)):
                        op_word = "in" if isinstance(op, ast.In) else "not in"
                        if isinstance(other, ast.Name) and other.id in dispatch_dicts:
                            # Membership against a dispatch dict. Its keys are
                            # validated and collected as routes by
                            # _dispatch_dict_facts, whatever it is called.
                            continue
                        elts = _literal_str_container(other)
                        if elts is None or any(not _is_str_literal(e) for e in elts):
                            violations.append(
                                f"line {node.lineno}: self.path {op_word} "
                                f"`{_describe(other)}`, not a tuple/list/set "
                                f"of string literals — write every route "
                                f"path as a literal string")
                    else:
                        violations.append(
                            f"line {node.lineno}: self.path compared with "
                            f"`{type(op).__name__}`, a form this guard does "
                            f"not accept — use ==, !=, in, or not in against "
                            f"string literals")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and _is_self_path(node.func.value)):
            handled.add(id(node.func.value))
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
                shown = _describe(arg) if arg is not None else "<no argument>"
                violations.append(
                    f"line {node.lineno}: self.path.startswith(`{shown}`), "
                    f"not a string literal (or tuple of them) — write every "
                    f"route path as a literal string")
        elif (isinstance(node, ast.Subscript) and _is_self_path(node.slice)
              and isinstance(node.value, ast.Name)):
            # NAME[self.path] — a dispatch dict lookup. _dispatch_dict_facts
            # resolves NAME, checks its keys and pins them.
            handled.add(id(node.slice))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "urlparse" and node.args and _is_self_path(node.args[0])
              and isinstance(parents.get(id(node)), ast.Attribute)
              and parents[id(node)].attr == "query"):
            # urlparse(self.path).query — reading the QUERY STRING, never the
            # path. Whitelisted only in this exact position: `.query` cannot
            # be compared as a route match the way `.path` could, which is
            # why urlparse(self.path).path == "..." (one of the six shapes
            # this round closes) is deliberately NOT covered by this branch
            # and falls through to the catch-all below.
            handled.add(id(node.args[0]))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
              and node.func.attr == "_serve_static" and node.args and _is_self_path(node.args[0])):
            # self._serve_static(self.path) — a call to the handler's OWN
            # method (not a hand-off outside the class; see
            # _self_passed_to_helper for that check), already reached only
            # after self.path.startswith("/static/") passed.
            handled.add(id(node.args[0]))

    # Anything left over is a self.path occurrence in a context this guard
    # doesn't recognise at all — an f-string, an IfExp, a walrus, an argument
    # to an unrelated call, a match subject, anything.
    for node in ast.walk(tree):
        if _is_self_path(node) and id(node) not in handled:
            parent = parents.get(id(node))
            where = _describe(parent) if parent is not None else "self.path"
            violations.append(
                f"line {node.lineno}: self.path appears inside `{where}` "
                f"({type(parent).__name__ if parent is not None else '?'}), "
                f"not a recognised route-check position — compare self.path "
                f"directly (`self.path == \"...\"`, `self.path in (...)`, "
                f"`self.path.startswith(...)`) against literals instead")
    return violations


def _self_passed_to_helper(tree: ast.Module) -> list[str]:
    """CONTROLLER RULING (A5 review round 2, cross-module case): a route
    dispatched by a helper module never mentions `self.path` in
    dashboard.py at all, so the walk above can't see it — a route matched
    inside `some_helper.dispatch(self)` is invisible to it by construction.
    This is the narrow, separate invariant that closes that last shape:
    do_GET and do_POST may not hand bare `self` to anything outside the
    handler class. A normal `self._send(...)` / `self.wfile.write(...)`
    call is unaffected — there, `self` is the RECEIVER of one of the
    handler's own methods/attributes, not a plain argument."""
    violations: list[str] = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in ("do_GET", "do_POST")):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            bare_self_args = [a for a in (*node.args, *(kw.value for kw in node.keywords))
                               if isinstance(a, ast.Name) and a.id == "self"]
            if bare_self_args:
                violations.append(
                    f"line {node.lineno}: {fn.name} passes bare `self` to "
                    f"`{_describe(node.func)}(...)` — route dispatch has to "
                    f"stay inline in dashboard.py; a helper that receives "
                    f"self could match self.path somewhere this guard "
                    f"cannot see")
    return violations


def test_every_dashboard_route_comparison_is_a_string_literal():
    """CONTROLLER RULING (A5 review round 1, C1; rewritten round 3 — see
    dashboard_route_literal_violations for the full reasoning). Two rounds
    of enumerating comparison forms both got walked around, because that
    set is open. This enumerates self.path OCCURRENCES instead, which is
    closed, and fails on any occurrence that doesn't sit in one of a small
    set of literal-only positions."""
    violations = dashboard_route_literal_violations()
    assert not violations, (
        "dashboard.py uses self.path somewhere this guard doesn't recognise "
        "as a literal route check, so routes_in_dashboard() cannot see it "
        "and it can reach production unpinned:\n" + "\n".join(violations))


def test_dashboard_handlers_never_hand_off_self():
    """CONTROLLER RULING (A5 review round 2, cross-module case): see
    _self_passed_to_helper. A route dispatched from another module never
    mentions self.path in dashboard.py, so no walk over this file's AST can
    ever see it — this is the one shape that needs its own assertion
    instead of being caught by the self.path walk above."""
    violations = _self_passed_to_helper(ast.parse(_source()))
    assert not violations, (
        "do_GET/do_POST hands bare `self` to something outside the handler "
        "class, which could dispatch a route this guard cannot see:\n"
        + "\n".join(violations))


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
