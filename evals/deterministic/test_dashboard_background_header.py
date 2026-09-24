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

WHAT THIS GUARD IS, AND WHAT IT IS NOT. Read this before trusting it.

The tokens it walks — `fetch`, `postJSON`, `setInterval`, `setTimeout` and
`paused` — it walks exhaustively. Every occurrence of each, in every file
under js/, must resolve to a site declared in this file, and an undeclared
one fails whatever shape it is written in. That is what catches the poller
written with two levels of indirection, the one bound to a const as an
arrow, and the one that reschedules itself with setTimeout — the three
shapes that walked through the first version of this file, which tried to
enumerate what a poller looks like instead.

It is NOT a proof that the dashboard cannot make an unmarked request. "What
makes a request in a browser" is not a closed set, and there is no AST
here: this is text matching over JavaScript, deliberately, because
static/README.md rules out a build step and a JS test runner and the core
takes no new dependency. test_dashboard_routes.py can walk Python's `ast`
and genuinely close its set; this file cannot, and an earlier version of
this paragraph claimed it did while four live pollers went through.

So the other transports are denied BY NAME — XMLHttpRequest, EventSource,
WebSocket, navigator.sendBeacon, and `fetch` used as a value instead of
called. Reaching for one fails here and forces the same deliberate
decision a new fetch does. A named list is a blocklist, and a blocklist is
never complete: an indirect construction (`window["Event" + "Source"]`,
`eval`, a transport the platform grows after this was written) goes
through. That limit is real and stated rather than papered over, because
the next person maintaining this file will trust what it says about
itself.

EventSource is the one worth naming twice. A server-sent-events stream is
a permanent keepalive and carries no per-request header at all, so it
cannot be marked background under any policy — it would hold a tenant's
container awake for as long as the tab is open.

The names are matched in the raw source, comments included. A comment that
merely mentions one of them fails too; that is the cheap half of the
trade, and the fix is to reword the comment.

Checks:
  1. every `fetch(`/`postJSON(` occurrence in js/ sits in a declared
     function, classified as background-aware (its request is conditioned on
     its own `background` parameter) or user-action (it must never carry the
     header). Anything undeclared fails.
  2. each background-aware function's own call attaches BG conditionally,
     and no user-action call site references the header at all — a click, a
     send or a tab's first open is real engagement and must count.
  3. every `setInterval(`/`setTimeout(` occurrence in js/ is a declared
     site, and a declared callback that reaches a network function passes
     literal `true`. setTimeout is in the walk because a function that
     reschedules itself is a poller containing no `setInterval` at all.
  3b. no other transport appears by name, and `fetch` is never taken as a
     value — see the limits stated above.
  4. hiding the tab stops the timer-driven polls, and showing it again calls
     refresh(true) — not a plain refresh(), which would count as a user
     action and could wake a stopped container just by switching tabs.
  5. the pause/resume state machine: every write to `paused` — plain
     assignment only, so `paused &&= false` is refused as a form — and
     every startTimers()/stopTimers() call in js/ is a declared site, and
     the one action the paused status line names, sending a message,
     actually resumes. See the second half of this file.
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


NETWORK_TOKENS = ("fetch", "postJSON")

# EVERY network call site in js/, classified. The walk below finds every
# `fetch(` and `postJSON(` occurrence in the directory and denies any that is
# not in this table, so adding a request to the dashboard means adding a line
# here — which is the moment the question gets asked.
#
#   "background"  the function takes a `background` parameter and conditions
#                 its own request on it. Exactly the keys of BACKGROUND_AWARE
#                 above; the two are cross-checked so they cannot drift.
#   "user"        a click, a send, a tab's first open. Real engagement: the
#                 request must reach the gateway with NO header at all.
#   "primitive"   postJSON itself, which forwards whatever headers its caller
#                 hands it and decides nothing.
NETWORK_CALLERS = {
    ("compare.js", "loadCompareHistory"): "background",
    ("compare.js", "clearCompareHistory"): "user",
    ("compare.js", "regradeCompare"): "user",
    ("compare.js", "gradeCard"): "user",
    ("compare.js", "deleteCompareRun"): "user",
    ("compare.js", "runCompare"): "user",
    ("compare.js", "loadMemoryArena"): "background",
    ("compare.js", "runMemoryArena"): "user",
    ("compare.js", "maSeeAll"): "user",
    ("compare.js", "cleanMemoryStores"): "user",
    ("compare.js", "loadMemoryStores"): "user",
    ("diagram.js", "pollEvents"): "background",
    ("dock.js", "newChat"): "user",
    ("dock.js", "loadThreadInto"): "background",
    ("dock.js", "switchTo"): "user",
    ("graph.js", "runGraph"): "user",
    ("judgment.js", "loadJudgmentArena"): "background",
    ("judgment.js", "runJudgmentArena"): "user",
    ("main.js", "refresh"): "background",
    ("main.js", "stopMic"): "user",
    ("memory.js", "saveFact"): "user",
    ("memory.js", "delMem"): "user",
    ("memory.js", "saveSoul"): "user",
    ("memory.js", "saveSkill"): "user",
    ("models.js", "saveSettings"): "user",
    ("models.js", "loadModelList"): "user",
    ("models.js", "switchModel"): "user",
    ("models.js", "loadAddModels"): "background",
    ("models.js", "pinModel"): "user",
    ("models.js", "saveJevKey"): "user",
    ("models.js", "toggleProvider"): "user",
    ("models.js", "loadModalModels"): "user",
    ("models.js", "submitProviderModal"): "user",
    ("render.js", "sendChat"): "user",
    ("util.js", "revealFile"): "user",
    ("util.js", "postJSON"): "primitive",
    ("views.js", "runQuery"): "user",
    ("views.js", "saveConnection"): "user",
    ("views.js", "testConnection"): "user",
    ("views.js", "saveProvider"): "user",
}

# The other ways a browser can put bytes on the wire, denied by name. This is
# a blocklist, which is never complete — see the limits in the module
# docstring. It exists so that reaching for one of these fails here and forces
# the same deliberate decision a new fetch does.
#
# Matched in the raw source, comments included, so a comment that names one
# fails too. That is deliberate: stripping comments correctly needs a
# JavaScript lexer (nested template literals defeat anything less), and the
# cost of the cheap version is one reworded comment.
FORBIDDEN_TRANSPORTS = {
    "XMLHttpRequest":
        "the pre-fetch transport; it takes headers, so route it through "
        "fetch()/postJSON() and declare it in NETWORK_CALLERS instead",
    "EventSource":
        "a server-sent-events stream is a PERMANENT keepalive and carries no "
        "per-request header at all, so it can never be marked background — an "
        "open tab would hold a hosted tenant's container awake indefinitely",
    "WebSocket":
        "same as EventSource: one long-lived connection, no per-message "
        "header the gateway reads, and no way to call it idle",
    "sendBeacon":
        "fire-and-forget POST with no header control and no response to check",
}

# `fetch` taken as a VALUE rather than called. `const _f = fetch; _f(url)` is
# a request this file's fetch-call-site walk cannot see, because the call site
# is spelled `_f(`.
FETCH_ALIAS_SHAPES = {
    r"(?:[=:(,\[]|=>)\s*fetch\b(?!\s*\()": "fetch bound to a name or passed as a value",
    r"\bfetch\s*\.\s*(?:bind|call|apply)\b": "fetch re-bound",
    r"\b(?:window|globalThis|self)\s*\.\s*fetch\b": "fetch reached through the global object",
}


# Every `setInterval(`/`setTimeout(` in js/, by the function it sits in, with
# the exact callback text. Same default-deny, and setTimeout is in here for a
# specific reason: a function that calls itself back on a timer is a poller
# with no `setInterval` anywhere in it — diagram.js's playNext is exactly that
# shape, already in the codebase and harmless because it repaints. Walking
# only setInterval would let the next one fetch.
DECLARED_TIMERS = {
    # the demo animation: a self-rescheduling repaint, no request
    ("diagram.js", "hot", "setTimeout"): {"()=>el.classList.remove(cls)"},
    ("diagram.js", "playNext", "setTimeout"): {"playNext"},
    # the graph card's repaint while a run is streaming: render() only
    ("graph.js", "runGraph", "setInterval"): {"() => { if (graphRun.running) render(); }"},
    # carries a render's provenance to a load it defers; the load itself is in
    # NETWORK_CALLERS and receives the captured flag
    ("main.js", "deferBg", "setTimeout"): {"() => load(bg)"},
    # the voice hint restoring its placeholder
    ("main.js", "<top level>", "setTimeout"): {'()=>{ i.placeholder = "Message Waku…"; }'},
    # the two timer-driven polls, the reason this file exists
    ("main.js", "startTimers", "setInterval"): {"() => refresh(true)", "() => pollEvents(true)"},
    # the status line's "updated Ns ago" tick: text only, no request
    ("main.js", "<top level>", "setInterval"): {"tickLive"},
    # the dock's elapsed counter while waiting for the first token: repaint only
    ("render.js", "sendChat", "setInterval"):
        {"() => { if (pending.pending && !pending.stream) syncChatLogs(); }"},
    # menu/copy-button chrome
    ("ui.js", "openMenu", "setTimeout"): {'() => document.addEventListener("click", _menuOutside)'},
    ("util.js", "copyCode", "setTimeout"):
        {'() => { btn.textContent = orig; btn.classList.remove("copied"); }'},
    ("util.js", "copyMsg", "setTimeout"):
        {'() => { btn.textContent = orig; btn.classList.remove("copied"); }'},
}


def _network_sites() -> dict[tuple[str, str], set[str]]:
    """(file, enclosing function) -> which network tokens appear there, for
    every `fetch(`/`postJSON(` occurrence under js/. A token that is the name
    in its own `function name(...)` definition is the definition, not a call
    site, and is skipped."""
    sites: dict[tuple[str, str], set[str]] = {}
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        ranges = _function_ranges(src)
        defs = {m.start(1) for m in FUNC_DEF_RE.finditer(src)}
        for m in re.finditer(rf"\b({'|'.join(NETWORK_TOKENS)})\(", src):
            if m.start(1) in defs:
                continue
            sites.setdefault((path.name, _enclosing(ranges, m.start())), set()).add(m.group(1))
    return sites


def test_every_network_call_site_in_the_dashboard_is_declared():
    """Default-deny over the one closed set in the problem.

    Not "is this poller shaped like a poller we recognise" — that set is
    open and unwinnable. `fetch(` and `postJSON(` are literal tokens and
    there are forty of them; every one has to resolve to a named function
    this table classifies. A request added anywhere, reached by any number
    of levels of indirection, from a timer written in any style, fails here
    until someone says which kind it is."""
    found = _network_sites()
    missing = {k: sorted(v) for k, v in found.items() if k not in NETWORK_CALLERS}
    gone = sorted(k for k in NETWORK_CALLERS if k not in found)
    assert not missing, (
        f"undeclared network call site(s): {missing}\n"
        "Every fetch/postJSON in js/ must be declared in NETWORK_CALLERS as "
        '"background" (conditioned on its own `background` parameter, so a '
        'timer-driven call carries X-Waku-Background) or "user" (a click, a '
        "send, a tab's first open — real engagement, never tagged). A call "
        "site at <top level> is neither and has to move into a function."
    )
    assert not gone, (
        f"declared network call site(s) that no longer exist: {gone} — "
        "remove them from NETWORK_CALLERS."
    )


def test_no_other_transport_appears_in_the_dashboards_js():
    """The blocklist half. The walk above sees `fetch(` and `postJSON(`; a
    request made any other way is invisible to it, and an XHR poller driven by
    requestAnimationFrame or an EventSource opened on page load is a perfectly
    ordinary thing for someone to reach for."""
    failures = []
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        for name, why in FORBIDDEN_TRANSPORTS.items():
            for m in re.finditer(rf"\b{re.escape(name)}\b", src):
                failures.append(
                    f"{path.name}:{src[:m.start()].count(chr(10)) + 1}: {name} — {why}")
    assert not failures, (
        "\n".join(failures) + "\n\nIf this is a deliberate new transport, it "
        "needs a decision about X-Waku-Background before it ships, not a line "
        "removed from FORBIDDEN_TRANSPORTS. If it is only a mention in a "
        "comment, reword the comment."
    )


def test_fetch_is_always_called_never_taken_as_a_value():
    """`const _f = fetch; _f("/api/data")` is a request whose call site is
    spelled `_f(`, so the fetch-call-site walk never sees it and the site is
    never classified. Requiring `fetch` to be immediately called keeps that
    walk's view of the file honest."""
    failures = []
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        for pattern, why in FETCH_ALIAS_SHAPES.items():
            for m in re.finditer(pattern, src):
                failures.append(
                    f"{path.name}:{src[:m.start()].count(chr(10)) + 1}: {why} "
                    f"(`{src[m.start():m.end()].strip()}`) — call fetch() at the "
                    f"site that needs it, so NETWORK_CALLERS can classify it")
    assert not failures, "\n".join(failures)


def test_the_two_tables_agree_on_which_functions_are_background_aware():
    """BACKGROUND_AWARE pins the exact conditional-header expression; the
    table above pins the set. Drift between them is how a function stops
    being checked while still looking checked."""
    from_patterns = {(f, fn) for f, fns in BACKGROUND_AWARE.items() for fn in fns}
    from_table = {k for k, kind in NETWORK_CALLERS.items() if kind == "background"}
    assert from_patterns == from_table, (
        "BACKGROUND_AWARE and NETWORK_CALLERS disagree about which functions "
        f"are background-aware.\n  only in BACKGROUND_AWARE: {sorted(from_patterns - from_table)}"
        f"\n  only in NETWORK_CALLERS:  {sorted(from_table - from_patterns)}"
    )


def test_user_action_call_sites_never_name_the_header():
    """The other half of the guarantee, checked per site rather than per
    file: a user-action function must not mention BG at all."""
    failures = []
    for (filename, fn_name), kind in sorted(NETWORK_CALLERS.items()):
        if kind != "user":
            continue
        body = _function_body(_read(filename), fn_name)
        if re.search(r"\bBG\b|X-Waku-Background", body):
            failures.append(f"{filename}: {fn_name}()")
    assert not failures, (
        f"user-action call site(s) attaching the background header: {failures} "
        "— a click, a send or a tab's first open is real engagement and must "
        "reach the gateway untagged, or a hosted tenant's container stops "
        "while they are using it."
    )


def test_every_timer_in_the_dashboard_is_declared():
    """`setInterval(` and `setTimeout(` are the other closed tokens. Declaring
    the callback text means a timer that changes what it calls fails here and
    gets read again — and it catches the shapes the enumerating version let
    through, because it never asks what shape a poller has. Two-level
    indirection, an arrow bound to a const, a function that reschedules
    itself: all of them need one of these two tokens, and all of them fail
    here until declared."""
    found: dict[tuple[str, str, str], set[str]] = {}
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        ranges = _function_ranges(src)
        for m in re.finditer(r"\b(setInterval|setTimeout)\(", src):
            # the callback is everything up to the LAST top-level comma of
            # the argument list; find the closing paren by depth first.
            depth, i = 1, m.end()
            while depth and i < len(src):
                if src[i] in "({[":
                    depth += 1
                elif src[i] in ")}]":
                    depth -= 1
                i += 1
            callback = src[m.end():i - 1].rsplit(",", 1)[0].strip()
            key = (path.name, _enclosing(ranges, m.start()), m.group(1))
            found.setdefault(key, set()).add(callback)
    assert found == DECLARED_TIMERS, (
        "the set of timers in js/ changed.\n"
        f"  found:    {found}\n"
        f"  declared: {DECLARED_TIMERS}\n"
        "A new or changed timer has to be declared here. If its callback "
        "reaches anything in NETWORK_CALLERS, it must pass literal `true` — "
        "a timer hands its callback no arguments, so a bare "
        "`setInterval(refresh, 5000)` polls with background=false forever and "
        "holds a hosted tenant's container awake."
    )


def test_declared_timers_that_fetch_pass_the_background_flag():
    """Cheap net over the closed declared set: if a declared callback names a
    function that puts a request on the wire, it passes literal `true`."""
    network_names = {fn for _, fn in NETWORK_CALLERS}
    failures = []
    for (filename, enclosing, token), callbacks in sorted(DECLARED_TIMERS.items()):
        for callback in sorted(callbacks):
            for call in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)", callback):
                name, arg = call.group(1), call.group(2).strip()
                if name in network_names and arg != "true":
                    failures.append(
                        f"{filename}: the {token} in {enclosing} calls {name}({arg}) "
                        f"— {name}() puts a request on the wire, so a timer-driven "
                        f"call through it must pass `true`, not {arg!r}.")
            if re.fullmatch(r"\w+", callback) and callback in network_names:
                failures.append(
                    f"{filename}: the {token} in {enclosing} is "
                    f"`{token}({callback}, ...)`, which passes no arguments, so "
                    f"{callback}() always runs with background=false — but it "
                    f"fetches. Wrap it as `() => {callback}(true)`.")
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


# `paused` written by anything other than a plain `=`. JavaScript has a dozen
# of these and `paused &&= false` clears the flag exactly as `paused = false`
# does, while matching nothing that looks for `paused =`. Rather than list the
# operators to accept, this matches an assignment of ANY form and lets the
# test below refuse every form but the plain one.
PAUSED_WRITE_RE = re.compile(
    r"(?:(?P<decl>let|var|const)\s+)?\bpaused\b\s*"
    r"(?P<op>(?:\*\*|<<|>>>?|[+\-*/%&|^]|&&|\|\||\?\?)?=(?!=)|\+\+|--)"
    r"\s*(?P<value>[A-Za-z0-9_\"']+)?")
PAUSED_PREFIX_RE = re.compile(r"(?:\+\+|--)\s*\bpaused\b")


def test_paused_is_only_ever_written_by_plain_assignment():
    """The declared-writer walk below reads `paused = <value>`. Every other
    assignment form in the language writes the same flag and would not be
    read by it: `paused &&= false` clears it, `paused ||= true` sets it,
    `paused--` coerces it to a number. One recognised form, refused
    otherwise, keeps the walk's answer complete."""
    failures = []
    for path in sorted(JS_DIR.glob("*.js")):
        src = path.read_text()
        for m in PAUSED_WRITE_RE.finditer(src):
            if m.group("op") == "=":
                continue
            failures.append(
                f"{path.name}:{src[:m.start()].count(chr(10)) + 1}: "
                f"`paused {m.group('op')}` — write it as `paused = true` or "
                f"`paused = false` at a site declared in PAUSED_WRITERS, so "
                f"the walk over routes into and out of the paused state stays "
                f"complete")
        for m in PAUSED_PREFIX_RE.finditer(src):
            failures.append(
                f"{path.name}:{src[:m.start()].count(chr(10)) + 1}: "
                f"`{src[m.start():m.end()]}` — same rule; plain assignment only")
    assert not failures, "\n".join(failures)


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
        for m in PAUSED_WRITE_RE.finditer(src):
            if m.group("decl"):                     # the declaration itself
                continue
            if m.group("op") != "=":                # refused by the test above
                continue
            found.setdefault((path.name, _enclosing(ranges, m.start())),
                             set()).add(m.group("value"))
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
