"""A connector whose sign-in has lapsed never blocks a turn (spec 038).

On 2026-11-25 a lapsed treg sign-in timed out a whole dashboard turn: Waku
opened a browser at treg's page and waited up to 15 minutes for it before the
first model call. Now a turn never opens a browser. The connector is left out,
the reply says so once a day, and the person signs in when they choose.
"""

from __future__ import annotations

import asyncio
import webbrowser
from datetime import UTC, datetime, timedelta

import pytest

from waku.loop.agent import LoopResult, text_with_sign_in
from waku.tools import connectors
from waku.tools.mcp_oauth import SignInNeeded, build_provider


def _redirect(provider):
    """The handler the SDK calls when no token can be refreshed."""
    return provider.context.redirect_handler


def test_a_turn_refuses_a_sign_in_and_opens_nothing(tmp_path, monkeypatch):
    opened, needed = [], []
    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)
    provider = build_provider("https://example.test/mcp", "treg", tmp_path,
                              on_sign_in_needed=needed.append)

    with pytest.raises(SignInNeeded):
        asyncio.run(_redirect(provider)("https://example.test/authorize?x=1"))

    assert needed == ["treg"] and opened == []


def test_a_sign_in_the_person_starts_still_opens_the_browser(tmp_path, monkeypatch):
    """`waku mcp login` and "connect treg" build no on_sign_in_needed."""
    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)
    provider = build_provider("https://example.test/mcp", "treg", tmp_path)

    asyncio.run(_redirect(provider)("https://example.test/authorize?x=1"))

    assert opened == ["https://example.test/authorize?x=1"]


def test_lapsed_sign_in_never_blocks_a_turn(tmp_path, monkeypatch):
    """A never-signed-in server is skipped by the agent's own bridge without
    a browser, and the build does not wait for it."""
    from evals.helpers import ScriptedClient, make_waku

    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)
    connectors.add(tmp_path, "treg", url="http://127.0.0.1:9/mcp")

    waku = make_waku(tmp_path, client=ScriptedClient([]), episodic_store="sqlite")
    try:
        bridge = waku.tools.mcp_bridge
        assert bridge.interactive is False
        assert bridge.needs_sign_in == ["treg"] and not bridge.connected("treg")
        assert bridge._deadline(bridge.servers) == bridge.timeout * 2
    finally:
        waku.close()
        waku.conn.close()
    assert opened == []


def test_needs_sign_in_notice_once_a_day(tmp_path):
    now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    first = connectors.sign_in_reminders(tmp_path, ["treg"], now=now)
    again = connectors.sign_in_reminders(tmp_path, ["treg"], now=now + timedelta(hours=3))
    next_day = connectors.sign_in_reminders(tmp_path, ["treg"], now=now + timedelta(hours=25))

    assert len(first) == 1 and "treg" in first[0] and 'Say "connect treg"' in first[0]
    assert again == []
    assert len(next_day) == 1


def test_a_text_gateway_sends_the_sign_in_line_with_the_reply():
    line = connectors.sign_in_line("treg")
    result = LoopResult(reply="Here is the digest.", tool_calls=[], iterations=1,
                        sign_in_notices=[line])

    assert text_with_sign_in(result) == f"Here is the digest.\n\n{line}"
    assert text_with_sign_in(LoopResult(reply="Hi", tool_calls=[], iterations=1)) == "Hi"


def test_the_connections_card_says_needs_sign_in():
    from waku.ops.connections_page import NEEDS_SIGN_IN, _server_state

    row = {"name": "treg", "connected": False, "account": "sean@example.test",
           "needs_sign_in": True}
    assert _server_state(row, None)[1] == NEEDS_SIGN_IN


# ---- amendment F and G: checked at start, a card with Sign in and Not now ----

@pytest.fixture
def clean_record():
    connectors._NEEDS.clear()
    yield
    connectors._NEEDS.clear()


def test_the_start_check_records_who_needs_a_sign_in(tmp_path, clean_record):
    class FakeBridge:
        closed = False

        def __init__(self):
            self.needs_sign_in = ["treg"]

        def start(self):
            return []

        def close(self):
            FakeBridge.closed = True

    assert connectors.check_sign_ins(tmp_path, bridge=FakeBridge) == ["treg"]
    assert FakeBridge.closed
    assert connectors.needing_sign_in(tmp_path) == ["treg"]


def test_a_finished_sign_in_clears_the_card(tmp_path, clean_record):
    import os
    import time

    connectors.record_needs_sign_in(["treg"], at=time.time() - 60)
    token = tmp_path / "mcp-auth" / "treg.json"
    token.parent.mkdir()
    token.write_text("{}", encoding="utf-8")
    os.utime(token, None)   # written after the finding, as a sign-in does

    assert connectors.needing_sign_in(tmp_path) == []


def test_not_now_quiets_the_card_for_a_day(tmp_path):
    now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    assert connectors.sign_in_due(tmp_path, ["treg", "linear"], now=now) == ["treg", "linear"]
    connectors.snooze_sign_in(tmp_path, "treg", now=now)
    assert connectors.sign_in_due(tmp_path, ["treg", "linear"],
                                  now=now + timedelta(hours=2)) == ["linear"]
    assert connectors.sign_in_due(tmp_path, ["treg"],
                                  now=now + timedelta(hours=25)) == ["treg"]


def test_the_card_opens_no_browser_and_names_both_buttons():
    """The card only sends "connect <name>" or Not now; no link is in it."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "waku/ops/static/js/render.js").read_text(
        encoding="utf-8")
    card = js[js.index("function renderSignInCards"):js.index("async function snoozeSignIn")]
    assert '"Sign in"' in card and '"Not now"' in card
    assert "connect ${esc(name)}" in card
    assert "window.open" not in card and "http" not in card


def test_the_not_now_route_is_blocked_on_hosted():
    from hosted.core.policy import BLOCK_MESSAGES, CONNECTORS_BLOCKED

    assert BLOCK_MESSAGES["/api/connectors/snooze"] == CONNECTORS_BLOCKED
