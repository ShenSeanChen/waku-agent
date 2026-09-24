"""Route policy, path hygiene and payload filters -- design section 8.

Pure logic. The gateway's outermost layer calls these and does the I/O.

WHAT MAKES THIS TABLE TRUSTWORTHY. Design section 8 says a route with no entry
passes, and that is right: isolation comes from the container, not from here,
so a new upstream route can at worst spend its own tenant's quota. What would
NOT be right is nobody noticing that a route arrived. So the table is closed
against waku's own pinned route list: evals/deterministic/hosted/
test_route_contract.py asserts that the keys here are exactly the routes
evals/deterministic/test_dashboard_routes.py pins, plus two prefixes that are
not routes of their own ("/" and "/api/compare").  Add a route to the dashboard
and that test names it until somebody classifies it.

Two entries are prefixes rather than routes, and both exist to make the
default DENY rather than PASS for a family that is blocked as a whole:
  "/api/compare"       so a compare route added later is blocked, not passed
  "/api/memory-arena"  same, and it is longer than "/api/memory" so it wins
"/api/compare/history" is longer than "/api/compare" and passes, which is the
one compare route the hosted dashboard needs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

PASS = "pass"
BLOCK = "block"
FILTER = "filter"

PLATFORM_PROVIDER = "waku-platform"
BACKGROUND_HEADER = "X-Waku-Background"

PAUSED_STATUS = 503
PAUSED_CODE = "paused"
PAUSED_BODY = {"error": "Paused. Send a message to wake it.", "code": PAUSED_CODE}

# Every `code` a hosted error body may carry. B4's test_paused_contract.py
# pins this against every `.code` comparison in the dashboard's JavaScript, in
# both directions: a code here that no page branch reads fails, and a page
# branch reading a code that is not here fails. Add to this set and to the
# page in the same PR, or do neither.
CODES = frozenset({PAUSED_CODE})

BAD_PATH = "That is not a path this dashboard serves."
WRONG_METHOD = "That route only takes a POST."
ARENA_BLOCKED = "The arenas are not available on hosted waku."
VOICE_BLOCKED = "Voice is not available on hosted waku."
REVEAL_BLOCKED = "Opening a file in an editor only works on your own machine."
PLATFORM_FIELDS_REFUSED = ("The hosted free tier's key and endpoint are set by the "
                           "platform. Pick another provider to use your own key.")
CONNECTION_REFUSED = "Only Tavily and Notion can be connected on hosted waku."

# Design section 9: a turn is one request to one of these three. The gateway
# sees turns; the proxy only sees individual model calls.
TURN_ROUTES = frozenset({"/api/chat", "/api/chat/stream", "/api/graph/stream"})

# Routes the browser reads as a stream. Spec, "Error shape": on one of these
# an error is a single terminal `done` event carrying `error`, not a JSON
# body. Four of the six are blocked, and E3 must not re-derive which by hand.
STREAMING_ROUTES = frozenset({
    "/api/chat/stream", "/api/graph/stream", "/api/compare/stream",
    "/api/memory-arena/stream", "/api/judgment-arena/stream", "/api/voice",
})

# Design section 8: everything else on /api/connections is a channel (deferred),
# a host-bound integration (cannot work in a container), a hosted memory
# backend or telemetry (platform decisions). An allowlist, so a connection
# added upstream is refused until somebody decides.
ALLOWED_CONNECTIONS = frozenset({"notion", "tavily"})

DROPPED_SETTINGS_FIELDS = ("experimental",)
PLATFORM_REFUSED_FIELDS = ("key", "base_url", "custom_key")

DECISIONS: dict[str, str] = {
    # per-tenant by construction: own process, own home
    "/": PASS,
    "/static/": PASS,
    "/api/data": PASS,
    "/api/events": PASS,
    "/api/session": PASS,
    "/api/memory": PASS,
    "/api/chat": PASS,
    "/api/chat/stream": PASS,
    "/api/graph/stream": PASS,
    "/api/pin": PASS,
    "/api/models": PASS,
    "/api/query": PASS,
    "/api/compare/history": PASS,
    # filtered: the payload decides
    "/api/providers": FILTER,
    "/api/settings": FILTER,
    "/api/connections": FILTER,
    "/api/connections/test": FILTER,
    # blocked in the MVP
    "/api/compare": BLOCK,
    "/api/compare/clear": BLOCK,
    "/api/compare/regrade": BLOCK,
    "/api/compare/delete_run": BLOCK,
    "/api/compare/stream": BLOCK,
    "/api/memory-arena": BLOCK,
    "/api/memory-arena/stores": BLOCK,
    "/api/memory-arena/clean": BLOCK,
    "/api/memory-arena/stream": BLOCK,
    "/api/judgment-arena": BLOCK,
    "/api/judgment-arena/key": BLOCK,
    "/api/judgment-arena/stream": BLOCK,
    "/api/voice": BLOCK,
    "/api/reveal": BLOCK,
}

BLOCK_MESSAGES: dict[str, str] = {
    "/api/compare": ARENA_BLOCKED,
    "/api/compare/clear": ARENA_BLOCKED,
    "/api/compare/regrade": ARENA_BLOCKED,
    "/api/compare/delete_run": ARENA_BLOCKED,
    "/api/compare/stream": ARENA_BLOCKED,
    "/api/memory-arena": ARENA_BLOCKED,
    "/api/memory-arena/stores": ARENA_BLOCKED,
    "/api/memory-arena/clean": ARENA_BLOCKED,
    "/api/memory-arena/stream": ARENA_BLOCKED,
    "/api/judgment-arena": ARENA_BLOCKED,
    "/api/judgment-arena/key": ARENA_BLOCKED,
    "/api/judgment-arena/stream": ARENA_BLOCKED,
    "/api/voice": VOICE_BLOCKED,
    "/api/reveal": REVEAL_BLOCKED,
}

# Substrings no path may contain. The query is split off first, so a percent
# sign in a query string is untouched.
_BAD_IN_PATH = ("//", "/./", "/../", "\\", "%")


def _has_control_character(text: str) -> bool:
    """Any C0 control, DEL, or C1 control. Checked across the WHOLE request
    target, query included, because forward_line forwards the query as
    received and a request line splits wherever the newline sits.

    The spec's list of forbidden path shapes (spec.md, "Route policy") names
    five: no leading //, /./, /../, backslash, percent. Control characters are
    a sixth, added here because a raw newline in the target E3 sends with
    encoded=True is a second request line on the wire. The spec sentence wants
    the same clause; it lives in the other repository, so it is called out in
    this task's report rather than edited here.
    """
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in text)


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    payload: dict | None = None
    message: str = ""


@dataclass(frozen=True)
class Outcome:
    verdict: str            # "pass" | "block" | "refuse" | "rewrite"
    status: int = 200
    message: str = ""
    payload: dict | None = None
    route: str = ""
    # True when the answer has to be one terminal `done` event carrying
    # `error` instead of a JSON body, because the page reads this route as a
    # stream. Set from STREAMING_ROUTES so E3 reads it rather than deciding.
    streaming: bool = False


def split_path(raw_path: str) -> tuple[str, str]:
    """(path, raw query). The query is forwarded as received and never
    matched, so /api/events?cursor=42 keeps its cursor."""
    path, _, query = raw_path.partition("?")
    return path, query


def forward_line(raw_path: str) -> str:
    """Exactly what the gateway sends the container as the request target.

    The raw path it matched plus the raw query, never a re-encoded form. The
    wire half -- aiohttp's encoded=True, so nothing re-encodes it on the way
    out -- is E3's; this is the string E3 must send. Acceptance 22.
    """
    path, query = split_path(raw_path)
    return f"{path}?{query}" if query else path


def path_refusal(raw_path: object) -> str:
    """Empty when the path is fine, the 400 sentence when it is not."""
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return BAD_PATH
    if _has_control_character(raw_path):
        return BAD_PATH
    path, _ = split_path(raw_path)
    if any(bad in path for bad in _BAD_IN_PATH):
        return BAD_PATH
    if path.endswith(("/.", "/..")):
        return BAD_PATH
    return ""


def match(path: str) -> str | None:
    """The longest entry that is a plain string prefix of `path`."""
    best: str | None = None
    for route in DECISIONS:
        if path.startswith(route) and (best is None or len(route) > len(best)):
            best = route
    return best


def is_turn(path: str) -> bool:
    return path in TURN_ROUTES


def is_background(headers: Mapping[str, str]) -> bool:
    """A background request never starts a container and never counts as
    activity. A tenant who strips the header only keeps their own container
    awake, which the running cap already bounds."""
    for name, value in headers.items():
        if name.lower() == BACKGROUND_HEADER.lower():
            return isinstance(value, str) and value.strip() == "1"
    return False


def _filter_settings(payload: dict) -> FilterResult:
    return FilterResult(True, {k: v for k, v in payload.items()
                               if k not in DROPPED_SETTINGS_FIELDS})


def _filter_providers(payload: dict) -> FilterResult:
    if payload.get("provider") != PLATFORM_PROVIDER:
        return FilterResult(True, dict(payload))
    if any(payload.get(field) not in (None, "") for field in PLATFORM_REFUSED_FIELDS):
        return FilterResult(False, None, PLATFORM_FIELDS_REFUSED)
    return FilterResult(True, dict(payload))


def _filter_connections(payload: dict) -> FilterResult:
    if payload.get("key") in ALLOWED_CONNECTIONS:
        return FilterResult(True, dict(payload))
    return FilterResult(False, None, CONNECTION_REFUSED)


FILTERS: dict[str, Callable[[dict], FilterResult]] = {
    "/api/settings": _filter_settings,
    "/api/providers": _filter_providers,
    "/api/connections": _filter_connections,
    "/api/connections/test": _filter_connections,
}


def decide(method: str, raw_path: str, payload: dict | None = None) -> Outcome:
    """The per-request decision, with no HTTP anywhere near it.

    `method` matters for exactly one thing: a filter rewrites a JSON body, and
    a request with no body has none to rewrite. All four filtered routes are
    POST-only in dashboard.py today, so anything else on one of them is
    refused rather than forwarded with a synthesised {} body.
    """
    refusal = path_refusal(raw_path)
    if refusal:
        return Outcome("refuse", 400, refusal)
    path, _query = split_path(raw_path)
    streaming = path in STREAMING_ROUTES
    route = match(path)
    # match() cannot answer None here, and the branch that pretended it could
    # was unreachable: path_refusal has already required a leading "/", and
    # "/" is itself an entry, so every path that gets this far matches at
    # least that one. test_every_path_that_survives_hygiene_matches_an_entry
    # pins the invariant -- delete "/" from DECISIONS and it fails there,
    # rather than here as a KeyError on somebody's live request.
    decision = DECISIONS[route]
    if decision == BLOCK:
        return Outcome("block", 403, BLOCK_MESSAGES[route], route=route, streaming=streaming)
    if decision == FILTER:
        if method.upper() != "POST":
            return Outcome("refuse", 405, WRONG_METHOD, route=route, streaming=streaming)
        result = FILTERS[route](payload or {})
        if not result.allowed:
            return Outcome("refuse", 403, result.message, route=route, streaming=streaming)
        return Outcome("rewrite", payload=result.payload, route=route, streaming=streaming)
    return Outcome(PASS, route=route, streaming=streaming)
