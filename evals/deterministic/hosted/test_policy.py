"""Route policy and path hygiene -- design section 8, acceptance 22.

The gateway decides per route whether a request is the tenant's own business
or the platform's. These are the decisions, with no server anywhere near them.
"""

from __future__ import annotations

import pytest

from hosted.core import policy


@pytest.mark.parametrize("raw", [
    "//api/chat/stream",
    "/./api/voice",
    "/api/../api/compare/stream",
    "/api%2fsettings",
    "/api/ch%61t/stream",
    "/api/chat%",
    "/api\\chat",
    "api/chat",
    "",
    "/api/.",
    "/api/..",
])
def test_a_non_canonical_path_is_refused_before_matching(raw):
    """Every rewriting layer between the browser and the container could turn
    a path that matches no entry into one that does: CPython's parse_request
    rewrites a leading // to /, and aiohttp decodes /api/ch%61t/stream to
    /api/chat/stream. No dashboard route needs a percent sign, so none is
    allowed at all."""
    assert policy.path_refusal(raw) == policy.BAD_PATH
    outcome = policy.decide("GET", raw)
    assert outcome.verdict == "refuse" and outcome.status == 400


@pytest.mark.parametrize("raw", [
    "/", "/api/data", "/api/events?cursor=42", "/static/js/main.js",
    "/api/models?provider=anthropic",
    "/api/query?q=100%25",          # a percent in the QUERY is fine
])
def test_a_canonical_path_passes_the_hygiene_check(raw):
    assert policy.path_refusal(raw) == ""


def test_the_query_is_split_off_and_never_matched():
    assert policy.split_path("/api/events?cursor=42") == ("/api/events", "cursor=42")
    assert policy.split_path("/api/data") == ("/api/data", "")
    assert policy.split_path("/api/models?provider=") == ("/api/models", "provider=")


class FakeContainer:
    """Acceptance 22's second half, without a server: it records the request
    target it was handed, and nothing else."""

    def __init__(self):
        self.seen: list[str] = []

    def receive(self, raw_path: str) -> None:
        self.seen.append(policy.forward_line(raw_path))


@pytest.mark.parametrize("raw", [
    "/api/events?cursor=42",
    "/api/models?provider=",
    "/api/data",
    "/api/query?q=100%25",
    "/static/js/main.js",
])
def test_a_fake_container_receives_the_matched_raw_path_plus_the_raw_query(raw):
    """Acceptance 22: "a fake container receives exactly the matched raw path
    plus the raw query, so /api/events?cursor=42 arrives with its cursor."
    The wire half -- aiohttp's encoded=True, so the target is never
    re-encoded -- is E3's; this is the string E3 must send."""
    container = FakeContainer()
    container.receive(raw)
    assert container.seen == [raw], "the target was rewritten on the way in"


def test_matching_is_a_plain_string_prefix_and_the_longest_wins():
    assert policy.match("/api/memory") == "/api/memory"
    assert policy.match("/api/memory-arena/stream") == "/api/memory-arena/stream"
    assert policy.match("/api/memory-arena/anything-new") == "/api/memory-arena"
    assert policy.match("/api/compare/history") == "/api/compare/history"
    assert policy.match("/api/compare/stream") == "/api/compare/stream"
    assert policy.match("/api/compare/whatever") == "/api/compare"
    assert policy.match("/api/revealX") == "/api/reveal"     # dashboard.py:991 does this too
    assert policy.match("/static/design/tokens.css") == "/static/"


def test_the_arena_routes_are_blocked_and_the_memory_route_is_not():
    assert policy.decide("POST", "/api/memory").verdict == "pass"
    assert policy.decide("GET", "/api/memory-arena").verdict == "block"
    assert policy.decide("GET", "/api/memory-arena/stores").verdict == "block"
    assert policy.decide("GET", "/api/judgment-arena").verdict == "block"
    assert policy.decide("POST", "/api/judgment-arena/stream").verdict == "block"
    assert policy.decide("POST", "/api/compare/stream").verdict == "block"
    assert policy.decide("GET", "/api/compare/history").verdict == "pass"
    assert policy.decide("POST", "/api/voice").verdict == "block"
    assert policy.decide("GET", "/api/reveal").verdict == "block"


def test_every_blocked_route_has_a_sentence_and_every_filtered_route_a_filter():
    """A decision with no message renders an empty error in the stock UI, and
    a route classified `filter` with nothing behind it would raise a KeyError
    on a live request. Both sets are closed here rather than at call time."""
    blocked = {r for r, d in policy.DECISIONS.items() if d == policy.BLOCK}
    filtered = {r for r, d in policy.DECISIONS.items() if d == policy.FILTER}
    assert blocked == set(policy.BLOCK_MESSAGES)
    assert filtered == set(policy.FILTERS)
    assert all(policy.BLOCK_MESSAGES[r] for r in blocked)


def test_every_decision_is_one_of_the_three():
    assert set(policy.DECISIONS.values()) <= {policy.PASS, policy.BLOCK, policy.FILTER}


def test_a_refusal_on_a_streaming_route_is_marked_as_one():
    """Spec, "Error shape": on a streaming route the error is one terminal
    `done` event, not a JSON body. Four blocked routes are streaming, and E3
    must not have to re-derive which."""
    assert policy.STREAMING_ROUTES == {
        "/api/chat/stream", "/api/graph/stream", "/api/compare/stream",
        "/api/memory-arena/stream", "/api/judgment-arena/stream", "/api/voice"}
    for route in ("/api/compare/stream", "/api/memory-arena/stream",
                  "/api/judgment-arena/stream", "/api/voice"):
        outcome = policy.decide("POST", route)
        assert outcome.verdict == "block" and outcome.streaming is True, route
    assert policy.decide("POST", "/api/chat/stream").streaming is True
    assert policy.decide("POST", "/api/memory").streaming is False


def test_the_settings_filter_drops_experimental_and_keeps_the_rest():
    """Otherwise a tenant turns on the delegation tools. pi is absent from the
    image too, so this is the second of two layers."""
    outcome = policy.decide("POST", "/api/settings",
                            {"experimental": True, "graph_workflows": True})
    assert outcome.verdict == "rewrite"
    assert outcome.payload == {"graph_workflows": True}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "HEAD", "get", "patch"])
def test_a_filtered_route_reached_with_anything_but_post_is_refused(method):
    """All four filtered routes are POST-only in dashboard.py today. Without
    the method, decide() would answer `rewrite` with a synthesised {} body and
    the gateway would forward it with a Content-Length."""
    outcome = policy.decide(method, "/api/settings", {})
    assert outcome.verdict == "refuse" and outcome.status == 405
    assert outcome.message == policy.WRONG_METHOD


@pytest.mark.parametrize("method", ["POST", "post", "Post"])
def test_the_method_check_is_case_insensitive(method):
    """aiohttp gives the method upper-cased, but nothing in core/ should
    depend on that: a filter that refused a lower-case post would refuse
    every save."""
    assert policy.decide(method, "/api/settings", {"experimental": True}).verdict == "rewrite"


def test_the_providers_filter_refuses_a_key_for_the_platform_row():
    for field in ("key", "base_url", "custom_key"):
        outcome = policy.decide("POST", "/api/providers",
                                {"provider": "waku-platform", field: "anything"})
        assert outcome.verdict == "refuse", field
        assert outcome.message == policy.PLATFORM_FIELDS_REFUSED


def test_the_providers_filter_lets_byok_through_untouched():
    """Adding your own key is the ordinary provider switch, and it has to be."""
    payload = {"provider": "anthropic", "key": "sk-ant-x", "model": "claude-sonnet-5"}
    outcome = policy.decide("POST", "/api/providers", dict(payload))
    assert outcome.verdict == "rewrite" and outcome.payload == payload


def test_the_providers_filter_lets_an_empty_field_through():
    """The Models modal always submits its Base URL field. An empty string is
    not a value the tenant set, and refusing it would break the free tier's
    own save."""
    outcome = policy.decide("POST", "/api/providers",
                            {"provider": "waku-platform", "base_url": "", "custom_key": None})
    assert outcome.verdict == "rewrite"


@pytest.mark.parametrize("route", ["/api/connections", "/api/connections/test"])
@pytest.mark.parametrize("key", ["tavily", "notion"])
def test_the_two_allowed_connections_pass(route, key):
    assert policy.decide("POST", route, {"key": key}).verdict == "rewrite"


@pytest.mark.parametrize("route", ["/api/connections", "/api/connections/test"])
@pytest.mark.parametrize("key", ["telegram", "discord", "whatsapp", "google_calendar",
                                 "apple_calendar", "apple_tools", "mem0", "zep",
                                 "langmem", "supabase", "otel", "", "something-new"])
def test_every_other_connection_is_refused(route, key):
    """An allowlist, not a blocklist: a connection added to waku upstream is
    refused here until somebody decides about it. Channels are deferred,
    host-bound integrations cannot work in a container, and memory backends
    and telemetry are platform decisions."""
    outcome = policy.decide("POST", route, {"key": key})
    assert outcome.verdict == "refuse"
    assert outcome.message == policy.CONNECTION_REFUSED


def test_the_three_turn_routes_are_the_ones_the_gateway_counts():
    assert policy.TURN_ROUTES == {"/api/chat", "/api/chat/stream", "/api/graph/stream"}
    assert policy.is_turn("/api/chat/stream") is True
    assert policy.is_turn("/api/data") is False
    assert policy.is_turn("/api/compare/stream") is False


def test_background_is_the_header_and_nothing_else():
    """A3 marks every timer-driven request. The gateway classifies by the
    header, not by path, because refresh() runs on a timer AND after a user
    action and only the timer's call carries it."""
    assert policy.is_background({"X-Waku-Background": "1"}) is True
    assert policy.is_background({"x-waku-background": "1"}) is True
    assert policy.is_background({"X-Waku-Background": " 1 "}) is True
    assert policy.is_background({"X-Waku-Background": "0"}) is False
    assert policy.is_background({"X-Waku-Background": ""}) is False
    assert policy.is_background({}) is False


def test_the_paused_reply_is_the_one_the_page_reads():
    """B4's test_paused_contract.py pins this against main.js. Here it is
    pinned against the spec's own words."""
    assert policy.PAUSED_STATUS == 503
    assert policy.PAUSED_CODE == "paused"
    assert policy.PAUSED_BODY == {"error": "Paused. Send a message to wake it.",
                                  "code": "paused"}


def test_every_code_a_hosted_body_can_carry_is_declared():
    """CODES is the set B4 pins against the dashboard's JavaScript. A body
    built with a code that is not in here is a branch no page reads."""
    assert policy.CODES == {policy.PAUSED_CODE}
    assert policy.PAUSED_BODY["code"] in policy.CODES


def test_a_path_with_no_entry_passes():
    """Design section 8: isolation does not depend on this table, because the
    container provides it. At worst a new upstream route spends that tenant's
    own quota."""
    assert policy.decide("GET", "/something-waku-grows-later").verdict == "pass"
