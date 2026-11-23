"""DETERMINISTIC EVAL -- the Connections page and the gateway agree on what a
hosted person can connect (waku-agent spec 030).

`waku/` and `hosted/` never import each other, so the list exists twice: the
gateway's allowlist (hosted/core/policy.py) and the page's
(waku/ops/connections_page.py). A card the page offers and the gateway
refuses is a button that answers 403, and a card the gateway takes and the
page greys out is a connection nobody can make.
"""

from __future__ import annotations

from hosted.core import policy
from waku.ops import connections_page


def test_the_page_offers_exactly_the_connections_the_gateway_takes():
    assert connections_page.HOSTED_CONNECTIONS == policy.ALLOWED_CONNECTIONS


def test_a_card_the_page_offers_on_hosted_is_a_save_the_gateway_passes():
    for key in connections_page.HOSTED_CONNECTIONS:
        outcome = policy.decide("POST", "/api/connections", {"key": key, "values": {}})
        assert outcome.verdict == "rewrite", key
    refused = policy.decide("POST", "/api/connections", {"key": "telegram", "values": {}})
    assert (refused.verdict, refused.status) == ("refuse", 403)
