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
  5. the pause/resume state machine: every `paused =` write and every
     startTimers()/stopTimers() call in js/ is a declared site, and the one
     action the paused status line names — sending a message — actually
     resumes. See the second half of this file.
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


# ---------------------------------------------------------------------------
# The pause/resume state machine.
#
# The header half above is only half of acceptance 9. The other half is
# "stops its timers, and resumes them after the next user action" — and it
# had a hole that every per-task review missed, because this file used to
# contain no assertion of any kind about `paused`, `stopTimers` or resume:
# `paused` was cleared only inside refresh(), sendChat() never called it, so
# a tenant who did exactly what the banner told them ("Paused. Send a message
# to wake it.") got their reply on a page that stayed frozen on pre-pause
# data for the life of the page, under a banner still saying "paused".
#
# Guarded the way A5's route guard finally had to be: not by listing the ways
# resume could go wrong, but by walking the closed set of literal tokens that
# can touch this state — every `paused =` assignment, every startTimers() and
# stopTimers() call — and DENYING any site not on a whitelist. A new route
# out of the paused state has to be declared here, which is the moment
# someone asks whether it actually resumes.
# ---------------------------------------------------------------------------

# (file, enclosing function) -> the values that site may write to `paused`.
# `<top level>` means module scope (the bootstrap, an event listener).
PAUSED_WRITERS = {
    ("main.js", "handleNotOk"): {"true"},
    ("main.js", "resumeLive"): {"false"},
}

# (file, enclosing function) -> which timer calls that site may make.
TIMER_CALLERS = {
    ("main.js", "handleNotOk"): {"stopTimers"},
    ("main.js", "resumeLive"): {"startTimers"},
    # the visibilitychange listener and the bootstrap, both module scope
    ("main.js", "<top level>"): {"startTimers", "stopTimers"},
}


def _function_ranges(src: str) -> list[tuple[int, int, str]]:
    """(start, end, name) for every `function name(...){...}` in `src`, by the
    same depth-count `_body_from` uses."""
    out = []
    for m in FUNC_DEF_RE.finditer(src):
        body = _body_from(src, m.end())
        out.append((m.end(), m.end() + len(body), m.group(1)))
    return out


def _enclosing(ranges: list[tuple[int, int, str]], pos: int) -> str:
    """Innermost named function containing `pos`, or `<top level>`."""
    best, width = "<top level>", None
    for start, end, name in ranges:
        if start <= pos < end and (width is None or end - start < width):
            best, width = name, end - start
    return best


def test_only_declared_sites_write_the_paused_flag():
    """Every assignment to `paused` in js/, whatever file it is in, must be a
    site this eval knows about. C1 was a MISSING write (no route out of the
    paused state from sendChat), so the guard that matters is the one that
    forces the set of writers to stay small and named: with exactly one
    clearer, "does sending a message resume?" is a question about one
    function, not about every call site in the dashboard."""
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        ranges = _function_ranges(src)
        for m in re.finditer(r"(?:(let|var|const)\s+)?\bpaused\s*=\s*([A-Za-z0-9_\"']+)", src):
            if src[m.end(2):m.end(2) + 1] == "=":   # `paused ==` / `===`
                continue
            if m.group(1):                          # the declaration itself
                continue
            found.setdefault((path.name, _enclosing(ranges, m.start())), set()).add(m.group(2))
    assert found == PAUSED_WRITERS, (
        "the set of places that write `paused` changed.\n"
        f"  found:    {found}\n"
        f"  declared: {PAUSED_WRITERS}\n"
        "Every route into and out of the paused state has to be declared here. "
        "If this is a new route OUT, check it also restarts the timers — a "
        "cleared flag with stopped timers is the frozen page C1 shipped."
    )


def test_only_declared_sites_start_or_stop_the_timers():
    """Same walk over the other half of the state: the two timers. A site that
    clears `paused` without restarting them, or restarts them while paused,
    leaves the dashboard in a state its own status line lies about."""
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        ranges = _function_ranges(src)
        for m in re.finditer(r"\b(startTimers|stopTimers)\(\)", src):
            enclosing = _enclosing(ranges, m.start())
            if (path.name, enclosing) in (("main.js", "startTimers"), ("main.js", "stopTimers")):
                continue   # the definitions themselves
            found.setdefault((path.name, enclosing), set()).add(m.group(1))
    assert found == TIMER_CALLERS, (
        "the set of places that start or stop the timer-driven polls changed.\n"
        f"  found:    {found}\n"
        f"  declared: {TIMER_CALLERS}"
    )


def test_resume_is_one_function_that_also_restarts_the_polls():
    """`resumeLive()` is the single way back. Clearing the flag without
    starting the timers again is exactly the frozen page: the banner goes
    away and nothing ever polls /api/data again."""
    body = _function_body(_read("main.js"), "resumeLive")
    assert "paused = false" in body, "resumeLive() must clear `paused`"
    assert "startTimers()" in body, (
        "resumeLive() must restart the timer-driven polls — clearing `paused` "
        "on its own leaves both intervals null and the whole page frozen on "
        "pre-pause data."
    )


def test_sending_a_message_resumes_a_paused_dashboard():
    """C1, pinned. The paused status line says "Send a message to wake it."
    sendChat() is that message. It must route back through the resume path —
    and as a USER action, not a background one: a plain `refresh()`, never
    `refresh(true)`, because a person typing is exactly the engagement the
    hosted gateway is counting."""
    body = _function_body(_read("render.js"), "sendChat")
    assert "paused" in body, (
        "sendChat() no longer looks at `paused`. It is the one action the "
        "paused status line names; if it does not resume, the tenant reads "
        '"Paused. Send a message to wake it.", sends a message, gets a reply, '
        "and watches the banner stay up while every card on the page holds "
        "pre-pause data for the life of the page. That is C1."
    )
    assert re.search(r"\brefresh\(\s*\)", body), (
        "sendChat() must reach refresh() to resume — no other call clears "
        "`paused`."
    )
    assert not re.search(r"\brefresh\(\s*true\s*\)", body), (
        "sendChat()'s refresh must be user-driven (`refresh()`), not "
        "`refresh(true)` — a person typing a message is real engagement and "
        "must reach the gateway without X-Waku-Background."
    )


def test_showing_a_hidden_tab_does_not_restart_a_paused_dashboards_polls():
    """The show branch used to call startTimers() unconditionally, so
    switching back to a hidden tab fired background polls at a container the
    pause had deliberately stopped talking to. Resume is refresh()'s job, on
    a 2xx, through resumeLive()."""
    src = _read("main.js")
    m = re.search(r'document\.addEventListener\("visibilitychange".*?\n\}\);', src, re.DOTALL)
    assert m, "expected a visibilitychange listener in main.js"
    block = m.group(0)
    assert re.search(r"if\s*\(\s*!\s*paused\s*\)\s*startTimers\(\)", block), (
        "showing a hidden tab must only restart the polls when we are NOT "
        "paused — while paused the timers stay stopped, and a 2xx from the "
        "one background refresh below resumes them through resumeLive()."
    )
