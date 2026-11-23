"""DETERMINISTIC EVAL -- a direct message to the bot is its owner's private
chat, and nobody else's (spec 032, version A, decided on 2026-11-20).

Ana added Waku to the workspace T0ANA. Slack named her U0ANA when she pressed
Allow, and the gateway kept that. She writes to the bot directly, with no
mention: the message becomes one event whose `reply_to` says `private`, and
the run it starts is not told that several people read the place. Ben, in the
same workspace, writes to the bot directly too: he gets one fixed line, once
a day, and nothing is stored, so no container starts and no model is called.

A workspace added before the installer was stored has nobody the gateway can
call its owner, so nobody is treated as one until Ana presses "Update
permissions", which goes through the callback again.

The tests drive the real Gateway and ControlDb in front of a fake Slack, and
the container's side with the real hand-over, worker and schedule tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET,
    SLACK_TOKEN_KEY,
    FakeSlack,
    FakeSpawner,
    Harness,
    sign_in,
)

from hosted.gateway.secretbox import SecretBox
from hosted.gateway.slack import (
    OWNER_ONLY,
    OWNER_ONLY_EVERY_SECONDS,
    SCOPES,
    SlackRelay,
    seal_binding,
)
from hosted.gateway.slack_thread import DIRECT_HEAD, THREAD_HEAD
from hosted.gateway.store import ControlDb

PATH = "/v1/triggers/slack"
COOKIE = "__Host-waku_session"
ANA, BEN, BOT = "U0ANA", "U0BEN", "U0BOT"
CHAT = "D0ANA"          # the conversation between Ana and the bot
BENS_CHAT = "D0BEN"


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, slack):
    built = Harness(tmp_path, FakeSpawner())
    built.use_slack(slack)
    return built


def direct(event_id: str = "Ev0001", *, user: str = ANA, channel: str = CHAT,
           team: str = "T0ANA", **event) -> dict:
    """An Events API envelope for one direct message, in Slack's shape."""
    return {"team_id": team, "api_app_id": "A0WAKU", "type": "event_callback",
            "event_id": event_id, "event_time": 1_700_000_000,
            "event": {"type": "message", "channel_type": "im", "user": user,
                      "channel": channel, "text": "what are my latest memories?",
                      "ts": "1700000000.000100", **event}}


async def post(harness: Harness, envelope: dict) -> int:
    body = json.dumps(envelope).encode()
    at = int(harness.clock.t)
    base = b"v0:" + str(at).encode() + b":" + body
    signature = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base,
                                 hashlib.sha256).hexdigest()
    status, _, _ = await harness.send(
        "POST", PATH, host=APEX, body=body,
        headers={"Content-Type": "application/json", "X-Slack-Signature": signature,
                 "X-Slack-Request-Timestamp": str(at)})
    return status


def connect(harness: Harness, *, installer: str = ANA, team: str = "T0ANA"):
    """Ana's tenant, holding the workspace, with a token that opens."""
    tenant = harness.store.create_tenant(sub="sub-ana", email="ana@example.com",
                                         timezone="UTC")
    sealed = SecretBox.from_hex(SLACK_TOKEN_KEY).seal(SLACK_BOT_TOKEN,
                                                      bound_to=seal_binding(team))
    assert harness.store.set_slack_install(
        tenant.id, team_id=team, team_name="Ana & Co", bot_user_id=BOT,
        token_sealed=sealed, installer_user_id=installer)
    return tenant


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


def _stored(harness: Harness) -> int:
    conn = sqlite3.connect(harness.config.control_db)
    try:
        return conn.execute("SELECT COUNT(*) FROM tenant_events").fetchone()[0]
    finally:
        conn.close()


# --- the owner ------------------------------------------------------------

def test_the_owners_direct_message_becomes_an_event_marked_private(harness, slack):
    slack.users = {ANA: "Ana"}

    async def body():
        ana = connect(harness)
        status = await post(harness, direct())
        return status, harness.store.pending_events(ana.id)

    status, [event] = _run(harness, body)
    assert status == 200
    assert (event.source, event.external_id) == ("slack", "Ev0001")
    # The whole chat is one conversation, and the answer goes into the chat
    # itself: no thread.
    assert event.session_id == f"slack:{CHAT}"
    assert event.payload["reply_to"] == {"channel": CHAT, "private": True}
    assert event.payload["sender"] == {"id": ANA}
    # No mention was needed, and the agent is told whose chat this is.
    assert event.payload["text"] == (DIRECT_HEAD.format(who="Ana")
                                     + "\nwhat are my latest memories?")
    assert harness.woken == 1
    marked = [(call["body"]["channel"], call["body"]["name"])
              for call in slack.calls("/reactions.add")]
    assert marked == [(CHAT, "eyes")]
    assert slack.calls("/chat.postMessage") == [], "the gateway itself says nothing"


def test_a_direct_message_the_owner_wrote_in_a_thread_is_answered_in_that_thread(
        harness, slack):
    slack.replies_answer = {"ok": True, "messages": [
        {"user": ANA, "ts": "1699999999.5", "text": "draft a note to Emily"},
        {"user": BOT, "ts": "1699999999.6", "text": "Here is a draft."},
        {"user": ANA, "ts": "1700000000.000100", "text": "shorter please"}]}

    async def body():
        ana = connect(harness)
        await post(harness, direct(thread_ts="1699999999.5", text="shorter please"))
        return harness.store.pending_events(ana.id)

    [event] = _run(harness, body)
    assert event.session_id == f"slack:{CHAT}:1699999999.5"
    assert event.payload["reply_to"] == {"channel": CHAT, "thread_ts": "1699999999.5",
                                         "private": True}
    assert event.payload["text"].splitlines() == [
        DIRECT_HEAD.format(who="Your person"), THREAD_HEAD,
        "someone: draft a note to Emily", "Waku (you): Here is a draft.", "",
        "shorter please"]


def test_a_redelivered_direct_message_is_one_event_and_one_wake(harness):
    async def body():
        ana = connect(harness)
        first = await post(harness, direct("Ev0001"))
        second = await post(harness, direct("Ev0001"))
        return first, second, harness.store.pending_events(ana.id)

    first, second, kept = _run(harness, body)
    assert first == second == 200
    assert len(kept) == 1 and harness.woken == 1


@pytest.mark.parametrize("change", [
    {"subtype": "message_changed"},                       # an edit
    {"subtype": "message_deleted"},                       # a deletion
    {"subtype": "file_share"},                            # any other subtype
    {"subtype": "bot_message", "bot_id": "B0OTHER"},      # another app
    {"bot_id": "B0WAKU"},                                 # the bot's own answer
    {"user": BOT},                                        # the same, named as a user
    {"user_team": "T0ELSE"},                              # a person of another workspace
    {"ts": ""},                                           # not a message Slack would send
    {"channel": "U0ANA"},                                 # not a direct-message id
], ids=lambda change: next(iter(change.items()))[1] or "empty")
def test_what_is_not_a_person_writing_is_dropped_without_a_word(harness, slack, change):
    async def body():
        connect(harness)
        return await post(harness, direct(**change))

    assert _run(harness, body) == 200
    assert _stored(harness) == 0 and harness.woken == 0
    assert slack.calls("/chat.postMessage") == [], "not even the fixed line"
    assert slack.calls("/reactions.add") == []


# --- anybody else ---------------------------------------------------------

def test_another_persons_direct_message_gets_the_fixed_line_and_starts_nothing(
        harness, slack):
    async def body():
        connect(harness)
        return await post(harness, direct(user=BEN, channel=BENS_CHAT))

    assert _run(harness, body) == 200
    assert _stored(harness) == 0, "no event, so no container and no model call"
    assert harness.woken == 0
    assert harness.spawner.requests == [], "no container was asked for"
    [said] = slack.calls("/chat.postMessage")
    assert said["body"] == {"channel": BENS_CHAT, "text": OWNER_ONLY}
    assert said["authorization"] == f"Bearer {SLACK_BOT_TOKEN}"
    assert slack.calls("/reactions.add") == [], "nothing says it was heard"
    assert "channel" in OWNER_ONLY and "added me" in OWNER_ONLY


def test_the_fixed_line_is_said_once_a_day_to_each_person(harness, slack):
    async def body():
        connect(harness)
        await post(harness, direct("Ev0001", user=BEN, channel=BENS_CHAT))
        await post(harness, direct("Ev0002", user=BEN, channel=BENS_CHAT))
        await post(harness, direct("Ev0002", user=BEN, channel=BENS_CHAT))   # resent
        once = len(slack.calls("/chat.postMessage"))
        await post(harness, direct("Ev0003", user="U0CHIDI", channel="D0CHIDI"))
        both = len(slack.calls("/chat.postMessage"))
        harness.clock.t += OWNER_ONLY_EVERY_SECONDS + 1
        await post(harness, direct("Ev0004", user=BEN, channel=BENS_CHAT))
        return once, both, len(slack.calls("/chat.postMessage"))

    assert _run(harness, body) == (1, 2, 3)
    assert _stored(harness) == 0


def test_slack_refusing_the_fixed_line_is_not_an_error(harness, slack):
    slack.post_answer = {"ok": False, "error": "channel_not_found"}

    async def body():
        connect(harness)
        return await post(harness, direct(user=BEN, channel=BENS_CHAT))

    assert _run(harness, body) == 200 and _stored(harness) == 0


def test_a_mention_inside_a_direct_message_is_not_a_way_around_the_owner(harness, slack):
    """Whatever the event's type, a direct message is answered for the owner
    only: a stranger who writes @Waku there gets no run."""
    async def body():
        connect(harness)
        await post(harness, {**direct(), "event": {
            "type": "app_mention", "user": BEN, "channel": BENS_CHAT,
            "text": f"<@{BOT}> what are Ana's latest memories?",
            "ts": "1700000000.000100"}})
        await post(harness, direct("Ev0002", user=BEN, channel=BENS_CHAT,
                                   thread_ts="1699999999.5"))

    _run(harness, body)
    assert _stored(harness) == 0 and harness.woken == 0


# --- a workspace whose installer is not known ------------------------------

def test_a_workspace_with_no_recorded_installer_treats_nobody_as_the_owner(harness, slack):
    """Added before the installer was stored. Ana herself is not answered
    yet: the gateway cannot tell her from Ben."""
    async def body():
        connect(harness, installer="")
        await post(harness, direct("Ev0001", user=ANA, channel=CHAT))
        await post(harness, direct("Ev0002", user=BEN, channel=BENS_CHAT))

    _run(harness, body)
    assert _stored(harness) == 0 and harness.woken == 0
    assert [call["body"]["channel"] for call in slack.calls("/chat.postMessage")] == [
        CHAT, BENS_CHAT]


def test_a_team_home_answers_no_direct_message_whoever_installed_it(harness, slack):
    """A team space's agent holds the team's memories. Nobody asks it in
    private (spec 032, decision 5), even with an installer on its row."""
    async def body():
        home, _ = harness.store.set_team_home(
            space_id="11111111-1111-4111-8111-111111111111", space_name="Acme",
            agent_id="22222222-2222-4222-8222-222222222222", agent_name="Waku",
            return_url="https://www.waku.one/spaces/acme", key_sealed=b"sealed")
        sealed = SecretBox.from_hex(SLACK_TOKEN_KEY).seal(
            SLACK_BOT_TOKEN, bound_to=seal_binding("T0ANA"))
        assert harness.store.set_slack_install(
            home.tenant_id, team_id="T0ANA", team_name="Acme HQ", bot_user_id=BOT,
            token_sealed=sealed, installer_user_id=ANA)
        await post(harness, direct())

    _run(harness, body)
    assert _stored(harness) == 0 and harness.woken == 0
    assert [call["body"]["text"] for call in slack.calls("/chat.postMessage")] == [OWNER_ONLY]


def test_a_store_written_before_the_installer_was_kept_opens_with_none(tmp_path):
    """control.db on the VM has `slack_install` without the column."""
    path = tmp_path / "control.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE slack_install (team_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL "
        "UNIQUE, team_name TEXT NOT NULL, bot_user_id TEXT NOT NULL, token_sealed BLOB "
        "NOT NULL, installed_at REAL NOT NULL, needs_reconnect INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO slack_install VALUES ('T0ANA', 'tn_old', 'Ana & Co', 'U0BOT', "
        "x'00', 1.0, 0);")
    old.commit()
    old.close()

    store = ControlDb(path)
    try:
        [install] = store.slack_installs()
    finally:
        store.close()
    assert (install.team_id, install.installer_user_id) == ("T0ANA", "")


async def _approve(harness: Harness, cookie: str) -> str:
    """Press the button on /setup and Allow on Slack's page."""
    _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                       cookie=f"{COOKIE}={cookie}")
    state = dict(urllib.parse.parse_qsl(
        urllib.parse.urlsplit(headers["location"]).query))["state"]
    target = "/slack/oauth/callback?" + urllib.parse.urlencode(
        {"code": "code-1", "state": state})
    _, headers, _ = await harness.send("GET", target, host=APEX,
                                       cookie=f"{COOKIE}={cookie}")
    return headers["location"]


def test_an_install_records_the_person_who_pressed_allow(harness, slack):
    slack.oauth_answer = {**slack.oauth_answer, "authed_user": {"id": ANA}}

    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        landed = await _approve(harness, cookie)
        return landed, harness.store.slack_install(tenant_id)

    landed, install = _run(harness, body)
    assert landed == "/setup?slack=connected"
    assert install.installer_user_id == ANA


def test_update_permissions_teaches_an_existing_install_its_owner(harness, slack):
    """The path for a workspace added before this change: the same person
    approves again, and from then on her direct message is hers."""
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        await _approve(harness, cookie)                  # Slack named nobody
        before = harness.store.slack_install(tenant_id).installer_user_id
        await post(harness, direct("Ev0001"))
        unanswered = len(harness.store.pending_events(tenant_id))

        slack.oauth_answer = {**slack.oauth_answer, "authed_user": {"id": ANA}}
        await _approve(harness, cookie)                  # "Update permissions"
        after = harness.store.slack_install(tenant_id).installer_user_id
        await post(harness, direct("Ev0002"))
        return before, unanswered, after, harness.store.pending_events(tenant_id)

    before, unanswered, after, [event] = _run(harness, body)
    assert (before, unanswered, after) == ("", 0, ANA)
    assert event.external_id == "Ev0002" and event.payload["reply_to"]["private"] is True


def test_an_approval_that_names_nobody_keeps_the_owner_it_had(harness, slack):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        slack.oauth_answer = {**slack.oauth_answer, "authed_user": {"id": ANA}}
        await _approve(harness, cookie)
        slack.oauth_answer = {**slack.oauth_answer, "authed_user": {"id": "not an id"}}
        await _approve(harness, cookie)
        return harness.store.slack_install(tenant_id)

    assert _run(harness, body).installer_user_id == ANA


def test_the_install_asks_slack_for_direct_messages(harness):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                           cookie=f"{COOKIE}={cookie}")
        return dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(headers["location"]).query))["scope"]

    scope = _run(harness, body).split(",")
    assert "im:history" in scope and scope == list(SCOPES)
    assert "im:write" not in scope, "the bot opens no direct message of its own"


def test_a_workspace_that_did_not_approve_again_hears_its_channels_as_before(harness, slack):
    """No new scope means Slack sends no direct message. A mention is the
    row it always was, with no mark."""
    async def body():
        ana = connect(harness, installer="")
        await post(harness, {**direct(), "event": {
            "type": "app_mention", "user": BEN, "channel": "C0DEMO",
            "text": f"<@{BOT}> hello", "ts": "1700000000.000100"}})
        return harness.store.pending_events(ana.id)

    [event] = _run(harness, body)
    assert event.session_id == "slack:C0DEMO:1700000000.000100"
    assert event.payload["reply_to"] == {"channel": "C0DEMO",
                                         "thread_ts": "1700000000.000100"}


# --- the mark, inside the container ----------------------------------------

@pytest.fixture
def hosted_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOSTED", "1")
    monkeypatch.delenv("WAKU_OWNER", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    return home


def _handed(harness: Harness, home, envelopes: list[dict]):
    """Post the envelopes to the gateway, hand what it kept to a container's
    inbox the way the scheduler does, and return the container's events."""
    from waku.alwayson import events, handover
    from waku.db import connect as open_state

    async def body():
        ana = connect(harness)
        for envelope in envelopes:
            await post(harness, envelope)
        return harness.store.pending_events(ana.id)

    kept = _run(harness, body)
    conn = open_state(home)
    status, _ = handover.accept(conn, {"events": [
        {"source": e.source, "external_id": e.external_id, "session_id": e.session_id,
         "payload": e.payload} for e in kept]})
    assert status == 200
    return conn, [events.claim_next(conn) for _ in kept]


def test_the_run_of_the_owners_private_chat_is_not_told_it_is_a_shared_place(
        harness, hosted_home):
    from waku.alwayson import approvals, worker

    mention = {**direct("Ev0002"), "event": {
        "type": "app_mention", "user": ANA, "channel": "C0DEMO",
        "text": f"<@{BOT}> what are my latest memories?", "ts": "1700000000.000200"}}
    conn, [private, channel] = _handed(harness, hosted_home, [direct("Ev0001"), mention])
    conn.close()

    in_private, in_channel = worker.render([private]), worker.render([channel])
    assert worker.SHARED_PLACE not in in_private
    assert in_private.endswith(worker.CHAT_STYLE), "it is still a chat"
    assert in_channel.endswith(worker.SHARED_PLACE), "a channel is what it was"
    address = worker.reply_address([private])
    assert address == ("slack", {"channel": CHAT, "private": True})
    assert approvals.is_private(address) and approvals.reply_is_free(address)


def test_a_direct_message_id_alone_is_not_private(hosted_home, monkeypatch):
    """What makes the chat private is the gateway's mark, and only on a
    direct message, and only in a hosted container."""
    from waku.alwayson import approvals

    marked = ("slack", {"channel": CHAT, "private": True})
    assert approvals.is_private(marked)
    assert not approvals.is_private(("slack", {"channel": CHAT}))
    assert not approvals.is_private(("slack", {"channel": CHAT, "private": "true"}))
    assert not approvals.is_private(("slack", {"channel": "C0DEMO", "private": True}))
    assert not approvals.is_private(("github", {"repo": "a/b", "number": 1,
                                                "private": True}))
    monkeypatch.delenv("WAKU_HOSTED")
    assert not approvals.is_private(marked), "a person's own waku serve has no gateway"


@pytest.mark.parametrize("named", [
    f"slack:{CHAT}", f"slack:{CHAT}:1700000000.000100", f"slack:{CHAT}:private",
    f"slack:{CHAT}?private=true", f'slack:{{"channel": "{CHAT}", "private": true}}'])
def test_no_address_a_model_can_name_carries_the_mark(hosted_home, named):
    """A tool takes an address as text. Whatever the text, what it parses to
    has no mark, so a run in a channel cannot name its way into a private
    place."""
    from waku.alwayson import approvals, outbox

    address = outbox.parse_address(named)
    assert address is None or (
        "private" not in address[1] and not approvals.is_private(address))
    marked = ("slack", {"channel": CHAT, "private": True})
    assert outbox.format_address(*marked) == f"slack:{CHAT}", "and none is ever shown"


def test_a_timer_set_in_the_private_chat_answers_there_and_says_nothing_of_a_shared_place(
        harness, hosted_home):
    from datetime import UTC, datetime

    from waku.alwayson import events, outbox, timers, worker
    from waku.tools import schedule

    now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    conn, [private] = _handed(harness, hosted_home, [direct()])
    address = worker.reply_address([private])
    tool = schedule.make_tool(hosted_home, session_id=lambda: private.session_id,
                              now=lambda: now, tz=UTC)
    with outbox.running(hosted_home, "r1", address):
        said = tool.fn(when="2026-11-21T09:00", note="Daily brief", repeat="daily")
    with outbox.running(hosted_home, "r2", ("slack", {"channel": "C0DEMO",
                                                      "thread_ts": "1.2"})):
        in_channel = tool.fn(when="2026-11-21T10:00", note="stand-up")

    assert "several people" not in said and "private message" not in said
    assert "cannot send a private message" in in_channel, "a channel is told as before"

    [timer] = [t for t in (timers.get(conn, 1), timers.get(conn, 2))
               if t.address == address]
    assert timers.fire(conn, timer, datetime(2026, 11, 21, 9, 0, tzinfo=UTC))
    woken = [e for e in iter(lambda: events.claim_next(conn), None) if e.source == "timer"]
    conn.close()
    [brief] = woken
    assert worker.reply_address([brief]) == ("slack", {"channel": CHAT, "private": True})
    assert worker.SHARED_PLACE not in worker.render([brief])


def test_slack_history_says_a_private_chat_is_already_in_the_conversation(hosted_home):
    from waku.alwayson import outbox
    from waku.tools import slack_history

    asked = []
    tool = slack_history.make_tool(hosted_home, "http://proxy", "token",
                                   post=lambda *sent: asked.append(sent) or {"ok": True})
    with outbox.running(hosted_home, "r1", ("slack", {"channel": CHAT, "private": True})):
        said = tool.fn()

    assert said == slack_history.PRIVATE_CHAT and asked == []


# --- the relay --------------------------------------------------------------

@pytest.mark.parametrize("to", ["U0BEN", "W0BEN", "@ben", "ben", "#general"])
def test_the_relay_still_refuses_a_person_as_a_destination(to):
    """Slack opens a new direct message for a user id. The relay posts to a
    conversation's id and to nothing else, so an agent cannot start a chat
    with a person who never wrote to it."""
    relay = SlackRelay(store=None, box=None, api=None)

    answer = asyncio.run(relay.send("tn_" + "a" * 16, to, "hello"))
    assert answer == {"ok": False, "error": "bad_request"}
