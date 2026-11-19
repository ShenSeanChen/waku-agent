"""DETERMINISTIC EVAL -- Slack's events endpoint (spec 026 task C2,
acceptance 2 and 6).

Slack posts every workspace's mentions to one address, POST
/v1/triggers/slack. The gateway checks Slack's signature over the raw body,
finds the tenant by the workspace's team id, keeps one row in tenant_events
under Slack's event id, answers 200, and asks the scheduler for a tick.

The tests drive the real Gateway, ControlDb and Scheduler. The requests are
signed here with the fake signing secret, the way Slack signs them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3

import aiohttp
import pytest
from gatewaylib import (
    APEX,
    SLACK_SIGNING_SECRET,
    AlwaysOnContainer,
    FakeSlack,
    FakeSpawner,
    Harness,
    ops,
)

from hosted.core import idle
from hosted.gateway.config import SLACK_ENV_NAMES
from hosted.gateway.container_api import ContainerApi
from hosted.gateway.scheduler import Scheduler
from hosted.gateway.slack import REPLAY_WINDOW_SECONDS
from hosted.gateway.slack_thread import THREAD_HEAD
from hosted.gateway.store import ControlDb

PATH = "/v1/triggers/slack"


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


def mention(team: str = "T0ANA", event_id: str = "Ev0001", **event) -> dict:
    """An Events API envelope for one @mention, in Slack's shape."""
    return {"token": "legacy-verification-token", "team_id": team, "api_app_id": "A0WAKU",
            "type": "event_callback", "event_id": event_id, "event_time": 1_700_000_000,
            "event": {"type": "app_mention", "user": "U0ANA", "channel": "C0DEMO",
                      "text": "<@U0BOT> remind the channel on Friday at 09:00",
                      "ts": "1700000000.000100", **event}}


def sign(body: bytes, timestamp: int, secret: str = SLACK_SIGNING_SECRET) -> str:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


async def post(harness: Harness, envelope: dict | bytes, *, age: float = 0,
               signature: str | None = None, timestamp: str | None = None,
               headers: dict | None = None):
    """POST one envelope, signed `age` seconds ago unless a test says otherwise."""
    body = envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode()
    at = int(harness.clock.t - age)
    head = {"Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp if timestamp is not None else str(at),
            "X-Slack-Signature": signature if signature is not None else sign(body, at)}
    head.update(headers or {})
    head = {name: value for name, value in head.items() if value != ""}
    status, _, raw = await harness.send("POST", PATH, host=APEX, body=body, headers=head)
    return status, raw


def connect(harness: Harness, sub: str, team: str):
    """A tenant whose workspace is `team`. Written straight into the store:
    the install route has its own eval, and a sealed value this file never
    opens can be any bytes."""
    tenant = harness.store.create_tenant(sub=sub, email=f"{sub}@example.com",
                                         timezone="UTC")
    assert harness.store.set_slack_install(
        tenant.id, team_id=team, team_name=team, bot_user_id="U0BOT",
        token_sealed=b"sealed")
    return tenant


def rows(harness: Harness) -> list[tuple]:
    """(tenant, source, external id) of every kept event, read from the file."""
    conn = sqlite3.connect(harness.config.control_db)
    try:
        return conn.execute("SELECT tenant_id, source, external_id FROM tenant_events "
                            "ORDER BY id").fetchall()
    finally:
        conn.close()


def reopened(harness: Harness) -> ControlDb:
    return ControlDb(harness.config.control_db)


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


# --- route ---------------------------------------------------------------

def test_slack_is_routed_to_the_tenant_of_its_team(harness):
    """Acceptance 6: two tenants, two team ids, and each mention reaches its
    own tenant."""
    async def body():
        ana = connect(harness, "sub-ana", "T0ANA")
        bob = connect(harness, "sub-bob", "T0BOB")
        first = await post(harness, mention("T0ANA", "Ev0001"))
        second = await post(harness, mention("T0BOB", "Ev0002", thread_ts="1699999999.5"))
        return ana, bob, first, second, harness.store.pending_events(ana.id), \
            harness.store.pending_events(bob.id)

    ana, bob, first, second, for_ana, for_bob = _run(harness, body)
    assert first[0] == second[0] == 200
    assert rows(harness) == [(ana.id, "slack", "Ev0001"), (bob.id, "slack", "Ev0002")]
    [event] = for_ana
    assert event.session_id == "slack:C0DEMO:1700000000.000100"
    assert event.payload == {
        "text": "<@U0BOT> remind the channel on Friday at 09:00",
        "sender": {"id": "U0ANA"},
        "reply_to": {"channel": "C0DEMO", "thread_ts": "1700000000.000100"}}
    # A mention inside a thread answers in that thread.
    assert for_bob[0].session_id == "slack:C0DEMO:1699999999.5"
    assert for_bob[0].payload["reply_to"]["thread_ts"] == "1699999999.5"


def test_a_route_answers_without_starting_a_container(harness):
    """The handler stores and answers. Starting the container is the
    scheduler's, which the handler only asks for a tick."""
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        status, raw = await post(harness, mention())
        return status, raw

    status, raw = _run(harness, body)
    assert (status, json.loads(raw)) == (200, {"ok": True})
    assert ops(harness.spawner, "start") == []
    assert harness.woken == 1


def test_a_redelivered_route_event_is_one_row_and_one_tick(harness):
    """Acceptance 2: Slack resends an event whose answer was slow, with the
    same event id."""
    async def body():
        ana = connect(harness, "sub-ana", "T0ANA")
        first = await post(harness, mention())
        again = await post(harness, mention(), headers={"X-Slack-Retry-Num": "1"})
        return ana, first[0], again[0]

    ana, first, again = _run(harness, body)
    assert (first, again) == (200, 200)
    assert rows(harness) == [(ana.id, "slack", "Ev0001")]
    assert harness.woken == 1


def test_the_route_answers_slacks_url_verification(harness):
    async def body():
        challenge = {"type": "url_verification", "challenge": "3eZbrw1aBm2rZgRNFdxV",
                     "token": "legacy"}
        signed = await post(harness, challenge)
        unsigned = await post(harness, challenge, signature="v0=" + "0" * 64)
        return signed, unsigned

    signed, unsigned = _run(harness, body)
    assert signed[0] == 200 and json.loads(signed[1]) == {"challenge": "3eZbrw1aBm2rZgRNFdxV"}
    assert unsigned[0] == 403 and b"3eZbrw1aBm2rZgRNFdxV" not in unsigned[1]


@pytest.mark.parametrize("change", [
    {"type": "message"},                 # not a mention
    {"bot_id": "B0OTHER"},               # another bot's message
    {"user_team": "T0ELSE"},             # a person from another workspace
    {"ts": ""},                          # nothing to answer in
])
def test_a_route_event_that_is_not_a_persons_mention_is_dropped(harness, change):
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        return await post(harness, mention(**change))

    status, _ = _run(harness, body)
    assert status == 200
    assert rows(harness) == []
    assert harness.woken == 0


def test_a_route_event_with_no_event_id_is_dropped(harness):
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        return await post(harness, mention(event_id=""))

    assert _run(harness, body)[0] == 200
    assert rows(harness) == []


def test_a_route_event_for_a_disabled_tenant_is_dropped(harness):
    async def body():
        ana = connect(harness, "sub-ana", "T0ANA")
        harness.store.set_status(ana.id, "disabled")
        return await post(harness, mention())

    assert _run(harness, body)[0] == 200
    assert rows(harness) == []
    assert harness.woken == 0


def test_the_route_forgets_a_workspace_that_removed_the_app(harness):
    async def body():
        ana = connect(harness, "sub-ana", "T0ANA")
        connect(harness, "sub-bob", "T0BOB")
        removed = {"type": "event_callback", "team_id": "T0ANA", "event_id": "Ev9",
                   "event": {"type": "app_uninstalled"}}
        status, _ = await post(harness, removed)
        return ana, status

    ana, status = _run(harness, body)
    store = reopened(harness)
    assert status == 200
    assert store.slack_tenant("T0ANA") is None and store.slack_install(ana.id) is None
    assert store.slack_tenant("T0BOB") is not None


def test_the_route_takes_a_post_and_a_json_object(harness):
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        got = await harness.send("GET", PATH, host=APEX)
        return got[0], (await post(harness, b"not json"))[0], (await post(harness, b"[1]"))[0]

    assert _run(harness, body) == (405, 400, 400)
    assert rows(harness) == []


def test_without_its_configuration_the_route_refuses_and_says_what_is_missing(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())       # no use_slack: Slack is off

    async def body():
        return await post(harness, mention())

    status, raw = _run(harness, body)
    assert status == 503
    assert all(name in json.loads(raw)["error"] for name in SLACK_ENV_NAMES)


# --- signature -----------------------------------------------------------

@pytest.mark.parametrize("forge", [
    lambda body, at: "v0=" + "0" * 64,                          # a made-up signature
    lambda body, at: sign(body, at, secret="another-secret"),   # another app's secret
    lambda body, at: sign(body + b" ", at),                     # a body changed after signing
    lambda body, at: sign(body, at - 1),                        # another timestamp's signature
    lambda body, at: sign(body, at)[3:],                        # no v0= version
    lambda body, at: sign(body, at).upper(),
    lambda body, at: "",                                        # no header at all
])
def test_a_bad_signature_is_refused_and_writes_nothing(harness, forge):
    """Acceptance 6: 403, and no row."""
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        raw = json.dumps(mention()).encode()
        return await post(harness, raw, signature=forge(raw, int(harness.clock.t)))

    status, _ = _run(harness, body)
    assert status == 403
    assert rows(harness) == []
    assert harness.woken == 0


def test_a_signature_older_than_five_minutes_is_refused_as_a_replay(harness):
    """A request Slack signed is good for five minutes. After that a copy of
    it, with its own valid signature, writes nothing."""
    assert REPLAY_WINDOW_SECONDS == 300

    async def body():
        connect(harness, "sub-ana", "T0ANA")
        return [(await post(harness, mention(event_id=f"Ev{age}"), age=age))[0]
                for age in (301, 86_400, 300, -300, -301)]

    assert _run(harness, body) == [403, 403, 200, 200, 403]
    assert [row[2] for row in rows(harness)] == ["Ev300", "Ev-300"]


@pytest.mark.parametrize("timestamp", ["", "soon", "1700000000.5", "-1700000000",
                                       " 1700000000", "1" * 40])
def test_a_signature_with_no_usable_timestamp_is_refused(harness, timestamp):
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        raw = json.dumps(mention()).encode()
        # Signed over exactly the timestamp text that is sent, so only the
        # timestamp's own shape can be what refuses it.
        base = b"v0:" + timestamp.encode() + b":" + raw
        signature = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base,
                                     hashlib.sha256).hexdigest()
        return await post(harness, raw, timestamp=timestamp, signature=signature)

    assert _run(harness, body)[0] == 403
    assert rows(harness) == []


def test_the_signature_is_checked_over_the_bytes_slack_sent(harness):
    """The same JSON with different spacing has a different signature. A
    handler that parsed the body and signed what it wrote back out would
    refuse this one."""
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        raw = json.dumps(mention(), indent=3, ensure_ascii=False).encode() + b"\n"
        return await post(harness, raw)

    assert _run(harness, body)[0] == 200
    assert len(rows(harness)) == 1


# --- unknown -------------------------------------------------------------

def test_an_unknown_team_is_answered_200_and_writes_nothing(harness):
    """Acceptance 6. Slack retries anything but a 200, and a workspace this
    gateway does not know will not become known by being retried. It is
    counted, so an operator can see it happening."""
    async def body():
        connect(harness, "sub-ana", "T0ANA")
        answers = [await post(harness, mention("T0NOBODY", "Ev1")),
                   await post(harness, mention("T0NOBODY", "Ev2")),
                   await post(harness, mention("", "Ev3"))]
        return [status for status, _ in answers], harness.slack.unknown_teams

    statuses, counted = _run(harness, body)
    assert statuses == [200, 200, 200]
    assert counted == 3
    assert rows(harness) == []
    assert harness.woken == 0


# --- the tick a mention asks for -----------------------------------------

def test_a_mention_reaches_a_sleeping_tenants_container_without_waiting_for_a_tick(
        tmp_path, slack, monkeypatch):
    """Acceptance 1 through the Slack route. Nothing here advances the
    scheduler: the only tick is the one the insert asked for."""
    monkeypatch.setattr(idle, "READY_TIMEOUT_SECONDS", 0)
    container = AlwaysOnContainer()
    container.start()
    harness = Harness(tmp_path, FakeSpawner())
    harness.use_slack(slack)
    harness.spawner.address, harness.spawner.port = "127.0.0.1", container.port

    async def body():
        async with aiohttp.ClientSession() as session:
            scheduler = Scheduler(store=harness.store, launcher=harness.launcher,
                                  containers=ContainerApi(session), now=harness.clock)
            harness.on_wake = scheduler.kick
            ana = connect(harness, "sub-ana", "T0ANA")
            status, _ = await post(harness, mention())
            await scheduler.settled()
            return ana, status, harness.store.pending_events(ana.id)

    try:
        ana, status, pending = _run(harness, body)
    finally:
        container.stop()
    assert status == 200
    assert [r["tenant_id"] for r in ops(harness.spawner, "start")] == [ana.id]
    assert [[e["external_id"] for e in batch] for batch in container.handed] == [["Ev0001"]]
    assert pending == []


# --- the thread a mention is in (2026-11-18) ------------------------------

def connect_readable(harness: Harness, sub: str, team: str):
    """A tenant whose workspace token opens, so the gateway can ask Slack."""
    from gatewaylib import SLACK_BOT_TOKEN, SLACK_TOKEN_KEY

    from hosted.gateway.secretbox import SecretBox
    from hosted.gateway.slack import seal_binding

    tenant = harness.store.create_tenant(sub=sub, email=f"{sub}@example.com",
                                         timezone="UTC")
    sealed = SecretBox.from_hex(SLACK_TOKEN_KEY).seal(SLACK_BOT_TOKEN,
                                                      bound_to=seal_binding(team))
    assert harness.store.set_slack_install(
        tenant.id, team_id=team, team_name=team, bot_user_id="U0BOT", token_sealed=sealed)
    return tenant


THREAD = [
    {"user": "U0SEAN", "ts": "1699999999.5",
     "text": "<@U0EMILY> the board messages feel deep, four clicks to see one"},
    {"user": "U0EMILY", "ts": "1699999999.6",
     "text": "Board messages are not in the memory system yet &amp; that is why"},
    {"user": "U0BOT", "ts": "1699999999.7", "text": "Noted."},
    {"user": "U0SEAN", "ts": "1700000000.000100",
     "text": "<@U0BOT> what do you think of Emily's suggestion?"},
    {"user": "U0EMILY", "ts": "1700000000.000200", "text": "written after the mention"},
]


def test_a_mention_in_a_thread_reaches_the_agent_with_the_thread_above_it(harness, slack):
    """The first mention on hosted Waku: "what do you think of Emily's
    suggestion?", and the agent could see nothing from Emily."""
    slack.replies_answer = {"ok": True, "messages": THREAD}
    slack.users = {"U0SEAN": "Sean Chen", "U0EMILY": "Emily Tsai"}

    async def body():
        ana = connect_readable(harness, "sub-ana", "T0ANA")
        await post(harness, mention(
            "T0ANA", "Ev0001", user="U0SEAN", thread_ts="1699999999.5",
            text="<@U0BOT> what do you think of Emily's suggestion?"))
        return harness.store.pending_events(ana.id)

    [event] = _run(harness, body)
    assert event.payload["text"].splitlines() == [
        THREAD_HEAD,
        "Sean Chen: @Emily Tsai the board messages feel deep, four clicks to see one",
        "Emily Tsai: Board messages are not in the memory system yet & that is why",
        "Waku (you): Noted.",
        "",
        "[Sean Chen mentions you in this thread:]",
        "what do you think of Emily's suggestion?",
    ]
    asked = slack.calls("/conversations.replies")
    assert [call["body"]["ts"] for call in asked] == ["1699999999.5"], "that thread only"
    assert event.payload["reply_to"] == {"channel": "C0DEMO", "thread_ts": "1699999999.5"}


def test_a_mention_outside_a_thread_reads_no_thread(harness, slack):
    slack.users = {"U0ANA": "Ana"}

    async def body():
        ana = connect_readable(harness, "sub-ana", "T0ANA")
        await post(harness, mention("T0ANA", "Ev0001"))
        return harness.store.pending_events(ana.id)

    [event] = _run(harness, body)
    assert event.payload["text"] == ("[Ana mentions you in Slack:]\n"
                                     "remind the channel on Friday at 09:00")
    assert slack.calls("/conversations.replies") == []


def test_a_workspace_without_the_new_scopes_still_gets_its_mention(harness, slack):
    """It added the app before the gateway asked to read threads. Slack
    answers missing_scope, and the mention is not lost."""
    async def body():
        ana = connect_readable(harness, "sub-ana", "T0ANA")
        status, _ = await post(harness, mention("T0ANA", "Ev0001", thread_ts="1699999999.5"))
        return status, harness.store.pending_events(ana.id)

    status, [event] = _run(harness, body)
    assert status == 200
    assert event.payload["text"] == ("[someone mentions you in Slack:]\n"
                                     "remind the channel on Friday at 09:00")


# --- a follow-up in the agent's thread, with no mention (2026-11-19) -------

def follow_up(event_id: str, text: str = "aren't you connected to waku memories?",
              **event) -> dict:
    """A plain message in a thread, as `message.channels` delivers it."""
    return {"team_id": "T0ANA", "type": "event_callback", "event_id": event_id,
            "event": {"type": "message", "user": "U0ANA", "channel": "C0DEMO",
                      "text": text, "ts": "1700000000.000300",
                      "thread_ts": "1700000000.000100", **event}}


def test_a_follow_up_in_a_thread_the_agent_is_in_reaches_the_agent(harness, slack):
    """A person answered the agent in its thread without @Waku, and nothing
    came back."""
    from hosted.gateway.slack_thread import FOLLOW_UP_HEAD

    slack.users = {"U0ANA": "Ana"}

    async def body():
        ana = connect_readable(harness, "sub-ana", "T0ANA")
        await post(harness, mention("T0ANA", "Ev0001"))
        await post(harness, follow_up("Ev0002"))
        return harness.store.pending_events(ana.id)

    first, second = _run(harness, body)
    assert second.session_id == first.session_id == "slack:C0DEMO:1700000000.000100"
    assert second.payload["text"] == (FOLLOW_UP_HEAD.format(who="Ana")
                                      + "\naren't you connected to waku memories?")
    assert "SILENT" in second.payload["text"], "people also talk to each other there"
    assert second.payload["reply_to"] == {"channel": "C0DEMO",
                                          "thread_ts": "1700000000.000100"}


@pytest.mark.parametrize("envelope", [
    follow_up("Ev0002", thread_ts="1699990000.000001"),          # a thread it was never in
    follow_up("Ev0002", text="<@U0BOT> again"),                  # arrives as app_mention too
    follow_up("Ev0002", bot_id="B0WAKU"),                        # the agent's own reply
    follow_up("Ev0002", subtype="message_changed"),              # an edit
    follow_up("Ev0002", thread_ts="1700000000.000300"),          # not a reply: a new message
], ids=["another-thread", "a-mention", "a-bot", "an-edit", "top-level"])
def test_a_message_that_is_not_a_follow_up_is_dropped(harness, envelope):
    async def body():
        ana = connect_readable(harness, "sub-ana", "T0ANA")
        await post(harness, mention("T0ANA", "Ev0001"))
        status, _ = await post(harness, envelope)
        return status, harness.store.pending_events(ana.id)

    status, events = _run(harness, body)
    assert status == 200
    assert [event.external_id for event in events] == ["Ev0001"]


# --- the message is marked as heard (2026-11-19) ----------------------------

def test_a_kept_message_is_marked_at_once_and_a_dropped_one_is_not(harness, slack):
    """A follow-up got no answer for minutes and nothing in Slack said
    whether the agent had heard it."""
    async def body():
        connect_readable(harness, "sub-ana", "T0ANA")
        await post(harness, mention("T0ANA", "Ev0001"))
        await post(harness, follow_up("Ev0002"))
        await post(harness, follow_up("Ev0003", thread_ts="1699990000.000001"))

    _run(harness, body)
    marked = [(call["body"]["channel"], call["body"]["timestamp"], call["body"]["name"])
              for call in slack.calls("/reactions.add")]
    assert marked == [("C0DEMO", "1700000000.000100", "eyes"),
                      ("C0DEMO", "1700000000.000300", "eyes")]
