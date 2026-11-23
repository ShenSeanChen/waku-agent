"""DETERMINISTIC EVAL -- a gateway whose key does not open its Slack tokens
(spec 026 group C).

The key that seals each workspace's bot token is in config/gateway.env, which
no backup holds. After a restore onto a new VM, or an edit of that line, the
gateway holds tokens it cannot open. It must start anyway, say so in its log,
and show each affected person "Reconnect Slack" on the setup page.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_BOT_TOKEN,
    SLACK_TOKEN_KEY,
    FakeSlack,
    FakeSpawner,
    Harness,
    sign_in,
    slack_settings,
)

from hosted.gateway.secretbox import SecretBox
from hosted.gateway.slack import seal_binding
from hosted.gateway.store import ControlDb

OLD_KEY = "a1" * 32          # the key of the VM the backup came from
COOKIE = "__Host-waku_session"


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, slack):
    built = Harness(tmp_path, FakeSpawner())
    built.use_slack(slack)          # this gateway's key is SLACK_TOKEN_KEY
    return built


def _connect(harness: Harness, tenant_id: str, team: str, key: str) -> None:
    """A workspace connected under `key`, as a restored control.db holds it."""
    assert harness.store.set_slack_install(
        tenant_id, team_id=team, team_name=f"{team} Inc", bot_user_id="U0BOT",
        token_sealed=SecretBox.from_hex(key).seal(SLACK_BOT_TOKEN,
                                                  bound_to=seal_binding(team)))


async def _steps(harness: Harness, cookie: str) -> tuple[dict, str]:
    _, _, body = await harness.send("GET", "/setup", host=APEX,
                                    cookie=f"{COOKIE}={cookie}")
    page = body.decode("utf-8")
    return dict(re.findall(r'<li data-step="([a-z-]+)" data-state="([a-z]+)"', page)), page


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


def test_a_start_with_another_key_marks_the_workspaces_it_cannot_open(harness, caplog):
    async def body():
        ana, ana_cookie, _ = await sign_in(harness)
        bob, bob_cookie, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        _connect(harness, ana, "T0ANA", OLD_KEY)             # sealed on the old VM
        _connect(harness, bob, "T0BOB", SLACK_TOKEN_KEY)     # sealed with this key
        before = await _steps(harness, ana_cookie)
        with caplog.at_level(logging.INFO):
            unreadable = harness.slack.check_tokens()        # what main() calls at start
        return (ana, bob, before, unreadable, await _steps(harness, ana_cookie),
                await _steps(harness, bob_cookie))

    ana, bob, before, unreadable, (ana_steps, ana_page), (bob_steps, _) = _run(harness, body)
    assert unreadable == ["T0ANA"]
    assert before[0]["slack"] == "done"      # until the start looked, nobody knew
    # Ana is told, and has one thing to press. Bob sees no change.
    assert ana_steps == {"sign-in": "done", "slack": "todo", "mention": "todo"}
    assert "Reconnect Slack" in ana_page and "T0ANA Inc" in ana_page
    assert re.findall(r'<a [^>]*href="([^"]+)"', ana_page) == ["/slack/install"]
    assert bob_steps == {"sign-in": "done", "slack": "done", "mention": "done"}
    store = ControlDb(harness.config.control_db)
    assert store.slack_install(ana).needs_reconnect is True
    assert store.slack_install(bob).needs_reconnect is False
    # The operator's line: which key, which file, how many, and no secret.
    [line] = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert "WAKU_SLACK_TOKEN_KEY" in line and "config/gateway.env" in line
    assert "1 of 2" in line and "T0ANA" in line
    assert SLACK_BOT_TOKEN not in caplog.text
    assert SLACK_TOKEN_KEY not in caplog.text and OLD_KEY not in caplog.text


def test_a_start_with_the_right_key_marks_nothing_and_logs_no_error(harness, caplog):
    async def body():
        ana, cookie, _ = await sign_in(harness)
        _connect(harness, ana, "T0ANA", SLACK_TOKEN_KEY)
        with caplog.at_level(logging.INFO):
            return harness.slack.check_tokens(), await _steps(harness, cookie)

    unreadable, (steps, page) = _run(harness, body)
    assert unreadable == []
    assert steps["slack"] == "done" and "Reconnect Slack" not in page
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_the_check_never_stops_the_gateway(harness, caplog, monkeypatch):
    """A stored value that is not a sealed value at all is marked like any
    other, and a database that cannot be read is logged. Neither raises: the
    gateway serves every tenant, and Slack is one feature of it."""
    async def body():
        ana, _, _ = await sign_in(harness)
        for blob in (b"", b"\x00", b"not sealed", b"\x01" + b"x" * 40):
            harness.store.set_slack_install(ana, team_id="T0ANA", team_name="x",
                                            bot_user_id="U", token_sealed=blob)
            assert harness.slack.check_tokens() == ["T0ANA"], blob

        def broken():
            raise OSError("disk I/O error")

        monkeypatch.setattr(harness.store, "slack_installs", broken)
        with caplog.at_level(logging.INFO):
            return harness.slack.check_tokens()

    assert _run(harness, body) == []
    assert any("could not check" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.ERROR)


def test_putting_the_old_key_back_clears_the_mark(tmp_path, slack):
    """The operator copies the key from the old VM and recreates the gateway.
    Nobody has to reconnect."""
    first = Harness(tmp_path, FakeSpawner())
    first.use_slack(slack)

    async def marked():
        ana, _, _ = await sign_in(first)
        _connect(first, ana, "T0ANA", OLD_KEY)
        return ana, first.slack.check_tokens()

    ana, unreadable = _run(first, marked)
    assert unreadable == ["T0ANA"]

    second = Harness(tmp_path, FakeSpawner())
    second.use_slack(slack, slack_settings(token_key=bytes.fromhex(OLD_KEY)))

    async def restarted():
        return second.slack.check_tokens(), second.store.slack_install(ana).needs_reconnect

    assert _run(second, restarted) == ([], False)


def test_reconnecting_replaces_the_token_and_clears_the_mark(harness, slack):
    async def body():
        ana, cookie, _ = await sign_in(harness)
        _connect(harness, ana, "T0ANA", OLD_KEY)
        harness.slack.check_tokens()
        signed = f"{COOKIE}={cookie}"
        _, headers, _ = await harness.send("GET", "/slack/install", host=APEX, cookie=signed)
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["location"]).query)["state"][0]
        await harness.send("GET", f"/slack/oauth/callback?code=c&state={state}",
                           host=APEX, cookie=signed)
        return ana, await _steps(harness, cookie), harness.slack.check_tokens()

    ana, (steps, page), unreadable = _run(harness, body)
    assert steps == {"sign-in": "done", "slack": "done", "mention": "done"}
    assert "Reconnect Slack" not in page and unreadable == []
    install = ControlDb(harness.config.control_db).slack_install(ana)
    assert install.needs_reconnect is False
    assert SecretBox.from_hex(SLACK_TOKEN_KEY).open(
        install.token_sealed, bound_to=seal_binding("T0ANA")) == SLACK_BOT_TOKEN


def test_a_send_that_finds_an_unreadable_token_marks_the_workspace(harness):
    """A key changed while the gateway kept running is found at the first
    message instead of at the next start."""
    async def body():
        ana, cookie, _ = await sign_in(harness)
        _connect(harness, ana, "T0ANA", OLD_KEY)
        answer = await harness.slack.relay.send(ana, "C0DEMO", "hello")
        return answer, await _steps(harness, cookie)

    answer, (steps, page) = _run(harness, body)
    assert answer == {"ok": False, "error": "token_unreadable"}
    assert steps["slack"] == "todo" and "Reconnect Slack" in page
