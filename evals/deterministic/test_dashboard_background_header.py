"""DETERMINISTIC EVAL — the dashboard tells a timer's poll from a person.

A hosted tenant's container stops when the tenant is idle, and that only
works if "idle" is honest. The dashboard used to poll /api/data every 5s and
/api/events every 450ms unconditionally, tab hidden or not — an open
background tab kept a container alive forever. `X-Waku-Background: 1` is how
the frontend now marks a request as a timer's poll rather than a person doing
something (group E's hosted gateway reads it to decide whether the request
counts as activity).

There is no browser in CI and no JS test runner (static/README.md — on
purpose), so this is a text-level guard, not a behavioural one. It cannot
watch the Network tab; only Sean's browser check (task-A3-brief.md step 6)
does that. What it CAN catch, offline and every run, is the regression that
actually matters months from now: someone adds a new `setInterval(...)` poll,
or a new fetch inside one of the functions that a poll already reaches, and
forgets the header — silently making a tenant's container immortal again.

Checks:
  1. each background-aware wrapper function's own fetch/postJSON call is
     conditioned on its `background` parameter and attaches BG.
  2. every `setInterval(...)` callback in js/ is safe: it either calls no
     function that fetches, or passes a literal `true` to the ones that do —
     found by walking each callback's own function bodies, not a hardcoded
     list, so a genuinely new poller is checked the same way as an old one.
  3. hiding the tab stops the timer-driven polls, and showing it again calls
     refresh(true) — not a plain refresh(), which would count as a user
     action and could wake a stopped container just by switching tabs.
  4. no OTHER fetch/postJSON call site in js/ references the header at all —
     a user-action call site (a click, a send, a tab's first open) must never
     send it, because that traffic is real engagement and must count as
     activity.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "js"

# --- the wrapper functions a poll can reach, and how each attaches the header.
# `pattern` is the exact conditional-header expression its fetch/postJSON call
# must contain. Adding a new one here is exactly the fix for the regression
# this eval exists to catch: a new timer-driven fetch that doesn't carry it.
BACKGROUND_AWARE = {
    "main.js": {
        "refresh": r'fetch\("/api/data",\s*background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined\)',
    },
    "diagram.js": {
        "pollEvents": r'background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined',
    },
    "dock.js": {
        "loadThreadInto": r'postJSON\("/api/session",\s*\{action:\s*mode,\s*id\},\s*background\s*\?\s*BG\s*:\s*\{\}\)',
    },
    "compare.js": {
        "loadCompareHistory": r'fetch\("/api/compare/history",\s*background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined\)',
        "loadMemoryArena": r'background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined',
    },
    "models.js": {
        "loadAddModels": r'background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined',
    },
    "judgment.js": {
        "loadJudgmentArena": r'fetch\("/api/judgment-arena",\s*background\s*\?\s*\{headers:\s*BG\}\s*:\s*undefined\)',
    },
}

def _read(name: str) -> str:
    return (JS_DIR / name).read_text()


def _body_from(src: str, start: int) -> str:
    """Text from just after an opening `{` (index `start`, pointing at the
    character after it) to its matching close brace, by depth-counting. The
    dashboard's function bodies have no template-literal braces deep enough
    to fool this in practice — every use below is checked against a real
    parse (test_static_js_parses.py) and its own regex match."""
    depth = 1
    i = start
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _function_body(src: str, name: str) -> str:
    """Text of `function <name>(...){ ... }` (async or not)."""
    m = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*{{", src)
    assert m, f"expected to find `function {name}(...)` in the source"
    return _body_from(src, m.end())


FUNC_DEF_RE = re.compile(r"function\s+(\w+)\s*\([^)]*\)\s*\{")


def _all_function_bodies() -> dict[str, str]:
    """name -> body text, for every top-level `function name(...){...}` across
    js/. Names are unique across the shared global scope by the codebase's own
    rule (static/README.md: "a function/let/const in one file is visible to
    all the others"), so first-definition-wins is safe here."""
    bodies: dict[str, str] = {}
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        for m in FUNC_DEF_RE.finditer(src):
            bodies.setdefault(m.group(1), _body_from(src, m.end()))
    return bodies


def _fetches_directly(text: str) -> bool:
    return "fetch(" in text or "postJSON(" in text


def test_background_header_value_is_defined_once():
    """The interface group E's gateway reads — pinned so nobody quietly
    renames it or drops the "1"."""
    src = _read("main.js")
    hits = re.findall(r'const BG = \{"X-Waku-Background":\s*"1"\}', src)
    assert len(hits) == 1, (
        'expected exactly one `const BG = {"X-Waku-Background": "1"};` in main.js, '
        f"found {len(hits)}"
    )


@pytest.mark.parametrize("filename,functions", BACKGROUND_AWARE.items())
def test_background_aware_functions_attach_the_header(filename, functions):
    """Every function a timer's poll can reach must condition its own fetch
    on the `background` flag it was called with — not hardcode either way."""
    src = _read(filename)
    for fn_name, pattern in functions.items():
        body = _function_body(src, fn_name)
        assert re.search(pattern, body), (
            f"{filename}: {fn_name}() no longer attaches X-Waku-Background "
            f"conditionally on its `background` flag — a timer-driven call "
            f"through here would go untagged. Expected to find a pattern "
            f"matching {pattern!r} in its body."
        )


def test_setinterval_calls_pass_the_background_flag():
    """Every `setInterval(...)` in js/ must be safe against the regression
    this eval exists for: a poller that fetches without ever marking itself
    as background.

    setInterval doesn't pass its callback any argument on its own, so a bare
    `setInterval(fn, ms)` always runs `fn()` with every parameter at its
    default — background=false for any function this task gave one. That's
    only a problem if `fn` (or a function the callback calls) actually
    fetches: `setInterval(tickLive, 1000)` is fine (tickLive touches no
    network), `setInterval(refresh, 5000)` would not be (caught below,
    because refresh()'s own body contains a fetch).

    So: for every setInterval callback, find every named function it calls
    (one level — this codebase's tickers call a single helper, not a chain)
    whose OWN body fetches directly, and require that call to pass literal
    `true`. A direct inline `fetch(`/`postJSON(` in the callback itself must
    show the same `background ? …` pattern the wrappers use."""
    bodies = _all_function_bodies()
    failures = []
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            for m in re.finditer(r"setInterval\(([^,]+),\s*\d+\)", line):
                callee = m.group(1).strip()
                if _fetches_directly(callee) and "background" not in callee:
                    failures.append(
                        f"{path.name}:{lineno}: setInterval callback fetches directly "
                        f"with no `background` check in sight — a timer-driven poll "
                        f"here would go untagged."
                    )
                    continue
                bare = re.fullmatch(r"\w+", callee)
                if bare:
                    body = bodies.get(callee)
                    if body is not None and _fetches_directly(body):
                        failures.append(
                            f"{path.name}:{lineno}: setInterval({callee}, ...) passes no "
                            f"arguments, so {callee}() always runs with background=false "
                            f"— but {callee}() itself fetches, so this poll would go "
                            f"untagged. Wrap it as `() => {callee}(true)`."
                        )
                    continue
                for call in re.finditer(r"\b([A-Za-z_]\w*)\(([^()]*)\)", callee):
                    name, arg = call.group(1), call.group(2).strip()
                    body = bodies.get(name)
                    if body is not None and _fetches_directly(body) and arg != "true":
                        failures.append(
                            f"{path.name}:{lineno}: setInterval callback calls "
                            f"{name}({arg}) — {name}() fetches, so a timer-driven poll "
                            f"through it must pass `true`, not {arg!r}."
                        )
    assert not failures, "\n".join(failures)


def test_visibilitychange_show_branch_is_timer_driven():
    """Showing a hidden tab must not itself count as the user action that
    wakes a stopped container (task-A3-brief.md step 4) — its refresh() call
    is background, same as the interval it restarts."""
    src = _read("main.js")
    m = re.search(r'document\.addEventListener\("visibilitychange".*?\}\);', src, re.DOTALL)
    assert m, "expected a visibilitychange listener in main.js"
    block = m.group(0)
    assert "stopTimers()" in block, "hiding the tab must stop the timer-driven polls"
    assert re.search(r"refresh\(true\)", block), (
        "showing the tab must call refresh(true) — a plain refresh() here would "
        "count as a user action and could wake a stopped container just by "
        "switching back to the tab"
    )
    assert "startTimers()" in block, "showing the tab must restart the timer-driven polls"


def test_no_user_action_call_site_carries_the_header():
    """The other half of the guarantee: nothing outside a recognised
    background-aware function (test above) ever references the header — a
    click, a send, or a tab's first open is real engagement and must reach
    the gateway with no X-Waku-Background header at all."""
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        stripped = src
        for fn_name in BACKGROUND_AWARE.get(path.name, {}):
            body = _function_body(src, fn_name)
            stripped = stripped.replace(body, "")
        # the header's own definition, not a call site.
        stripped = re.sub(r'const BG = \{"X-Waku-Background":\s*"1"\};', "", stripped)
        leaks = re.findall(r"X-Waku-Background|\bBG\b", stripped)
        assert not leaks, (
            f"{path.name}: found {leaks!r} outside a recognised background-aware "
            f"function — a user-action fetch/postJSON call site must never "
            f"attach X-Waku-Background, and a new background-aware function must "
            f"be added to BACKGROUND_AWARE in this eval, not left for this check "
            f"to flag as a leak."
        )
