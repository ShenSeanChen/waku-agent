"""waku-agent spec 021 review fix 4: the hosted gateway refuses /plugin.

The container also refuses it, but only while its provider is the
platform's, and a tenant can switch the provider. The gateway reads the chat
body as text, because hosted/ never imports waku/."""

from __future__ import annotations

import pytest

from hosted.core import policy


@pytest.mark.parametrize("route", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("message", ["/plugin install waku@waku-memory --yes", "  /plugin list", "/plugins"])
def test_a_plugin_command_in_a_chat_body_is_refused(route, message):
    outcome = policy.decide("POST", route, {"message": message})
    assert (outcome.verdict, outcome.status, outcome.message) == ("refuse", 403, policy.PLUGIN_REFUSED)
    assert outcome.streaming is (route in policy.STREAMING_ROUTES)


@pytest.mark.parametrize("message", ["what plugins do I have?", "/connect treg", "", None])
def test_other_chat_messages_pass(message):
    assert policy.decide("POST", "/api/chat", {"message": message}).verdict == policy.PASS


def test_a_plugin_command_on_another_route_is_not_read_as_chat():
    assert policy.decide("POST", "/api/memory", {"message": "/plugin list"}).verdict == policy.PASS
