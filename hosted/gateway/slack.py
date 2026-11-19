"""The public Waku Slack app (spec 026 group C, decision 5).

One Slack app serves every workspace. Four things happen here:

  install   "Add to Slack": GET /slack/install sends a signed-in person to
            Slack, and GET /slack/oauth/callback exchanges the code Slack
            sends back for that workspace's bot token. The token is stored
            sealed (secretbox.py) beside the tenant, with the map from the
            workspace's team id to the tenant.
  events    POST /v1/triggers/slack: Slack posts every workspace's mentions
            here. A signed request becomes one row in tenant_events for the
            tenant its team id maps to.
  relay     SlackRelay.send: a tenant's container asks for a message to be
            posted, and the gateway posts it with that tenant's token. The
            token never enters a container.
  history   SlackHistory.read: a tenant's container asks for the recent
            messages of the channel its run is in, and the gateway reads
            them with that tenant's token (spec 029 D).

THE STATE IS WHAT TIES AN INSTALL TO A PERSON. Slack returns whatever `state`
it was given, so the gateway issues one per start: random, bound to the
signed-in tenant, good for ten minutes and for one use. The callback also
requires the browser's own session to name the same tenant. Without both, a
person could approve the app in their workspace and finish the install in
somebody else's browser, and that person's agent would answer a workspace
they never chose.

NOTHING HERE LOGS OR RETURNS A TOKEN, THE CLIENT SECRET OR THE SIGNING SECRET.
A log line carries a tenant id, a team id and Slack's own error code.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import gzip
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import zoneinfo
from collections.abc import Callable

import aiohttp
from aiohttp import web

from hosted import log
from hosted.core.tenant import is_tenant_id
from hosted.gateway import answers, sessions
from hosted.gateway.config import SlackSettings
from hosted.gateway.secretbox import SecretBox, Unreadable
from hosted.gateway.slack_thread import BUDGET_SECONDS, ThreadReader
from hosted.ports.control import ControlStore

_LOG = log.get(__name__)

SLACK_API = "https://slack.com/api"
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
INSTALL_PATH = "/slack/install"
CALLBACK_PATH = "/slack/oauth/callback"
EVENTS_PATH = "/v1/triggers/slack"
SETUP_PATH = "/setup"
# What the bot may do: hear a mention of itself, and post a message.
# The last three read the thread a mention is in, and the names in it
# (slack_thread.py). A workspace that added the app before they were asked for
# keeps working without them, and gets them when its person adds the app again.
SCOPES = ("app_mentions:read", "chat:write", "channels:history", "groups:history",
          "users:read", "reactions:write", "channels:read", "groups:read")
# What the bot puts on a message the moment it is kept for the agent, so the
# person sees it was heard before the answer is written. A container that was
# stopped takes some seconds to start, and until 2026-11-19 nothing in Slack
# said that anything was happening.
SEEN_REACTION = "eyes"
STATE_TTL_SECONDS = 600
# One request to Slack. The person's browser, or Slack's own three-second
# limit on an event, is waiting behind it.
TIMEOUT_SECONDS = 10
TEAM_ID = re.compile(r"T[A-Z0-9]{2,31}")
# How old, or how far ahead, a signed request's timestamp may be. Slack's own
# number: past it, a recorded request is a replay.
REPLAY_WINDOW_SECONDS = 300
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_VERSION = "v0"
# Unix seconds as Slack writes them: digits and nothing else.
_TIMESTAMP = re.compile(r"[0-9]{1,12}")

EVENTS_POST_ONLY = "Slack sends its events as a POST."
BAD_SIGNATURE = "That request was not signed by Slack."
NOT_AN_EVENT = "That is not a Slack event."

NOT_CONFIGURED = "Slack is not set up on this server"

# Where the callback sends the browser, as /setup?slack=<outcome>. A closed
# set: the setup page shows a sentence for each and ignores anything else.
CONNECTED, DENIED, EXPIRED, FAILED, TAKEN = "connected", "denied", "expired", "failed", "taken"


def not_configured(problem: str) -> web.Response:
    """What every Slack route answers when the four values are not all set.
    `problem` names the missing names and never a value (config.slack_from)."""
    return answers.json_error(503, f"{NOT_CONFIGURED}: {problem}.")


def seal_binding(team_id: str) -> str:
    """What a workspace's sealed token is bound to, so one row's value does
    not open in another row."""
    return f"slack:{team_id}"


def signed_by_slack(secret: str, *, timestamp: str, signature: str, body: bytes,
                    now: float) -> bool:
    """Whether Slack signed exactly these bytes, within the last five minutes.

    Slack signs `v0:<timestamp>:<raw body>` with the app's signing secret and
    sends the HMAC-SHA256 as `v0=<hex>`. The timestamp is inside what is
    signed, so it cannot be moved forward on a recorded request, and one
    older than REPLAY_WINDOW_SECONDS is refused however good its signature.
    The comparison takes the same time whatever the two strings share.
    """
    if not _TIMESTAMP.fullmatch(timestamp):
        return False
    if abs(now - int(timestamp)) > REPLAY_WINDOW_SECONDS:
        return False
    base = f"{SIGNATURE_VERSION}:{timestamp}:".encode("ascii") + body
    expected = f"{SIGNATURE_VERSION}=" + hmac.new(
        secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def event_row(envelope: dict) -> dict | None:
    """What one Events API envelope becomes in tenant_events, or None when it
    is not a person in this workspace mentioning the bot.

    The same row the container's own Slack source writes under `waku serve`
    (spec 023): Slack's event id is the external id, so a resent event is a
    no-op, and each thread is its own conversation.
    """
    event = envelope.get("event")
    if not isinstance(event, dict):
        return None
    event_id, team_id = envelope.get("event_id"), envelope.get("team_id")
    channel, ts = event.get("channel"), event.get("ts")
    if event.get("type") != "app_mention" or event.get("bot_id"):
        return None
    if not all(isinstance(value, str) and value for value in (event_id, channel, ts)):
        return None
    # A shared channel carries people from other workspaces, and `user_team`
    # names the sender's. A stranger's mention would spend this tenant's
    # credits, so it is dropped.
    if event.get("user_team", team_id) != team_id:
        return None
    thread = event.get("thread_ts") if isinstance(event.get("thread_ts"), str) else ts
    return {"source": "slack", "external_id": event_id,
            "session_id": f"slack:{channel}:{thread}",
            "payload": {"text": str(event.get("text") or ""),
                        "sender": {"id": str(event.get("user") or "")},
                        "reply_to": {"channel": channel, "thread_ts": thread}}}


def follow_up_row(envelope: dict, bot_user_id: str) -> dict | None:
    """What a plain message in a thread becomes, or None. `_route` keeps it
    only for a thread the agent was mentioned in before.

    On 2026-11-19 a person answered the agent in its own thread without
    writing @Waku again, and nothing came back: Slack sends `app_mention` for
    a mention only. With `message.channels` and `message.groups` it also sends
    every message of a channel the bot is in. A message that mentions the bot
    arrives both ways, so it is left to `event_row`. An edit, a deletion, a
    join and a bot's own message are not a person writing.
    """
    event = envelope.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("subtype") or event.get("bot_id"):
        return None
    event_id, team_id = envelope.get("event_id"), envelope.get("team_id")
    channel, ts, thread = event.get("channel"), event.get("ts"), event.get("thread_ts")
    if not all(isinstance(value, str) and value for value in (event_id, channel, ts, thread)):
        return None
    if thread == ts or event.get("user_team", team_id) != team_id:
        return None
    text = str(event.get("text") or "")
    if bot_user_id and f"<@{bot_user_id}" in text:
        return None
    return {"source": "slack", "external_id": event_id,
            "session_id": f"slack:{channel}:{thread}",
            "payload": {"text": text, "sender": {"id": str(event.get("user") or "")},
                        "reply_to": {"channel": channel, "thread_ts": thread}}}


class SlackUnreachable(RuntimeError):
    """Slack did not answer with a JSON object. The message holds no token."""


class OAuthStates:
    """The `state` of each install under way, in memory.

    A state is popped before it is checked, so it is used once whichever way
    the check goes. A tenant has one state at a time: a new start ends the
    earlier one, which also bounds this dict by the number of tenants. A
    gateway restart forgets them all, and the person starts again.
    """

    def __init__(self, now: Callable[[], float],
                 ttl: float = STATE_TTL_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        self._states: dict[str, tuple[str, float]] = {}

    def issue(self, tenant_id: str) -> str:
        now = self._now()
        for state in [s for s, (owner, expires) in self._states.items()
                      if owner == tenant_id or expires <= now]:
            self._states.pop(state, None)
        state = sessions.new_secret()
        self._states[state] = (tenant_id, now + self._ttl)
        return state

    def redeem(self, state: str) -> str | None:
        """The tenant this state was issued to, once, or None."""
        found = self._states.pop(state, None)
        if found is None:
            return None
        tenant_id, expires = found
        return tenant_id if self._now() < expires else None


_GZIP_MAGIC = b"\x1f\x8b"


class SlackApi:
    """The Slack Web API methods the gateway calls."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str = SLACK_API) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)

    async def _post(self, method: str, **kwargs) -> dict:
        try:
            # The gateway's one session does not decompress (__main__.py: it
            # passes a container's bytes through as they are), and Slack
            # compresses its answers. So this asks for gzip or nothing, and
            # opens gzip itself. On 2026-11-18 the first real install failed
            # here: the compressed bytes were read as text.
            headers = {**kwargs.pop("headers", {}), "Accept-Encoding": "gzip, identity"}
            async with self._session.post(f"{self._base}/{method}", timeout=self._timeout,
                                          headers=headers, **kwargs) as reply:
                raw = await reply.read()
            if raw[:2] == _GZIP_MAGIC:
                raw = gzip.decompress(raw)
            answer = json.loads(raw.decode("utf-8"))
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, EOFError) as exc:
            # The exception's own text is left out: aiohttp can quote a
            # request's headers, and one of them is a token.
            raise SlackUnreachable(f"{method} failed ({type(exc).__name__})") from None
        if not isinstance(answer, dict):
            raise SlackUnreachable(f"{method} answered something that is not an object")
        return answer

    async def oauth_access(self, *, code: str, redirect_uri: str, client_id: str,
                           client_secret: str) -> dict:
        """Exchange the code from the callback for the workspace's bot token.
        Slack takes the client id and secret as HTTP Basic credentials."""
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        return await self._post(
            "oauth.v2.access", data={"code": code, "redirect_uri": redirect_uri},
            headers={"Authorization": f"Basic {basic}"})

    async def post_message(self, token: str, payload: dict) -> dict:
        return await self._post("chat.postMessage", json=payload,
                                headers={"Authorization": f"Bearer {token}"})

    async def thread_replies(self, token: str, channel: str, thread_ts: str,
                             limit: int) -> dict:
        """The messages of one thread, oldest first."""
        return await self._post(
            "conversations.replies",
            data={"channel": channel, "ts": thread_ts, "limit": str(limit)},
            headers={"Authorization": f"Bearer {token}"})

    async def channel_history(self, token: str, channel: str, limit: int, *,
                              oldest: str = "", cursor: str = "") -> dict:
        """The last messages of one channel, newest first: one page of them.
        `oldest` is the earliest time to go back to, and `cursor` is the
        `next_cursor` of the page before."""
        data = {"channel": channel, "limit": str(limit)}
        if oldest:
            data["oldest"] = oldest
        if cursor:
            data["cursor"] = cursor
        return await self._post("conversations.history", data=data,
                                headers={"Authorization": f"Bearer {token}"})

    async def add_reaction(self, token: str, channel: str, ts: str, name: str) -> dict:
        return await self._post("reactions.add",
                                data={"channel": channel, "timestamp": ts, "name": name},
                                headers={"Authorization": f"Bearer {token}"})

    async def channel_info(self, token: str, channel: str) -> dict:
        return await self._post("conversations.info", data={"channel": channel},
                                headers={"Authorization": f"Bearer {token}"})

    async def user_info(self, token: str, user_id: str) -> dict:
        return await self._post("users.info", data={"user": user_id},
                                headers={"Authorization": f"Bearer {token}"})


def _install_from(answer: dict) -> tuple[str, str, str, str] | None:
    """(token, team id, team name, bot user id) from oauth.v2.access, or None
    when the answer is not one workspace's bot token. An install across a
    whole Enterprise Grid organisation has no single team, and v1 maps one
    workspace to one tenant, so it is refused."""
    team = answer.get("team")
    token = answer.get("access_token")
    if (answer.get("ok") is not True or answer.get("token_type") != "bot"
            or answer.get("is_enterprise_install") or not isinstance(team, dict)
            or not isinstance(token, str) or not token):
        return None
    team_id = team.get("id")
    if not isinstance(team_id, str) or not TEAM_ID.fullmatch(team_id):
        return None
    return (token, team_id, str(team.get("name") or team_id)[:200],
            str(answer.get("bot_user_id") or "")[:64])


# A channel, group or direct-message id, and a message's timestamp, as Slack
# writes them. The relay posts to nothing else: not a channel name, not a
# user id, which Slack would open a new direct message for.
CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{2,31}")
THREAD_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,9}")
# The errors with which Slack says the token will never work again.
TOKEN_ENDED = frozenset({"token_revoked", "account_inactive", "invalid_auth"})


class SlackRelay:
    """Post one message for a tenant, with that tenant's workspace token.

    WHICH WORKSPACE IS NOT THE CALLER'S CHOICE. The caller gives a tenant id,
    which the proxy resolved from the container's own platform token, and the
    relay uses the one token stored for that tenant. A Slack bot token posts
    only inside the workspace it was issued for, so a tenant's container can
    reach the workspace that tenant connected and no other.

    WHAT GOES BACK is `ok`, or `ok: false` with a short error code. Slack's
    answer is read here and never passed on.
    """

    def __init__(self, *, store: ControlStore, box: SecretBox, api: SlackApi) -> None:
        self._store = store
        self._box = box
        self._api = api

    async def send(self, tenant_id: str, channel: str, text: str,
                   thread_ts: str = "") -> dict:
        if (not is_tenant_id(tenant_id) or not isinstance(channel, str)
                or not CHANNEL_ID.fullmatch(channel)
                or not isinstance(text, str) or not text
                or not isinstance(thread_ts, str)
                or (thread_ts and not THREAD_TS.fullmatch(thread_ts))):
            return _refused("bad_request")
        install = self._store.slack_install(tenant_id)
        if install is None:
            return _refused("not_installed")
        try:
            token = self._box.open(install.token_sealed,
                                   bound_to=seal_binding(install.team_id))
        except Unreadable:
            _LOG.error("tenant=%s: the stored token for Slack team=%s does not open "
                       "with WAKU_SLACK_TOKEN_KEY", tenant_id, install.team_id)
            # The setup page now tells the person to reconnect.
            self._store.set_slack_reconnect(install.team_id, True)
            return _refused("token_unreadable")
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            answer = await self._api.post_message(token, payload)
        except SlackUnreachable as exc:
            _LOG.warning("tenant=%s: Slack send failed: %s", tenant_id, exc)
            return _refused("slack_unreachable")
        if answer.get("ok") is True:
            return {"ok": True}
        code = _error_code(answer) or "refused"
        _LOG.info("tenant=%s: Slack refused a message: %s", tenant_id, code)
        if code in TOKEN_ENDED:
            # The workspace removed the app or revoked the token. The row is
            # deleted, so the setup page stops saying the workspace is
            # connected and the person can add the app again.
            self._store.delete_slack_install(install.team_id)
            _LOG.info("tenant=%s: disconnected Slack team=%s", tenant_id, install.team_id)
        return _refused(code)


def _refused(code: str) -> dict:
    return {"ok": False, "error": code}


# How many messages slack_history reads when it is not told, and at most.
HISTORY_DEFAULT, HISTORY_MAX = 30, 100
# One message's text, and the whole answer as JSON, at most. The answer goes
# to the proxy as one line on gateway.sock, which holds 65,536 bytes.
HISTORY_TEXT_CHARS = 2000
HISTORY_ANSWER_BYTES = 40_000
# Reading the messages and looking up their names, together.
HISTORY_SECONDS = 8.0
# With `days`, the channel is read back to that many days ago: 30 at most.
HISTORY_MAX_DAYS = 30
# Slack gives a channel's history a page at a time. One call reads at most
# this many pages of this many messages, and starts no page after
# HISTORY_PAGING_SECONDS. The container's tool waits 15 seconds for its
# answer, so the whole call gets 12.
HISTORY_PAGES, HISTORY_PAGE_SIZE = 10, 200
HISTORY_PAGING_SECONDS = 7.0
HISTORY_WINDOW_SECONDS = 12.0
# The most messages one answer holds, before the cut to HISTORY_ANSWER_BYTES.
HISTORY_WINDOW_MESSAGES = 400
HISTORY_PERSON_CHARS = 80
HISTORY_AMBIGUOUS_NAMES = 5


class SlackHistory:
    """Read recent messages of one channel for a tenant, with that tenant's
    workspace token (spec 029, decision 5 and task D1).

    WHICH CHANNEL IS NOT A FREE CHOICE. The container names the channel of
    the run that asks, and a container runs model-chosen tools, so the
    gateway checks the name against what it knows itself: the channel has to
    be one where this tenant's agent was mentioned, which is a row the
    gateway wrote in tenant_events when Slack sent the mention. Any other
    channel is refused, including one the bot is a member of. The people in
    a channel the agent answers in already read what it reads there.

    A TIME WINDOW AND A PERSON. With `days` the channel is read from now back
    to that many days ago, in pages, and with `person` only the messages one
    author wrote are kept. The author is matched on the name people see, as
    a whole name or a first name. Two authors who both match are named, and
    no message is returned.

    AN ANSWER THAT WAS CUT SAYS SO. `cut` is true when the window held more
    than was read or more than fits, and the newest part is what is kept.

    WHAT GOES BACK is names and text: `ok`, the channel's name and each
    message's ts, its time in the tenant's time zone, author, text and count
    of replies, or `ok: false` with a short error code. No token, no user id and nothing else of Slack's
    answer is passed on.
    """

    def __init__(self, *, store: ControlStore, box: SecretBox, api: SlackApi,
                 threads: ThreadReader, now: Callable[[], float] = time.time) -> None:
        self._store = store
        self._box = box
        self._api = api
        self._threads = threads
        self._now = now

    async def read(self, tenant_id: str, channel: str, thread_ts: str = "",
                   limit: int = HISTORY_DEFAULT, days: int = 0, person: str = "") -> dict:
        if (not is_tenant_id(tenant_id) or not isinstance(channel, str)
                or not CHANNEL_ID.fullmatch(channel) or not isinstance(thread_ts, str)
                or (thread_ts and not THREAD_TS.fullmatch(thread_ts))
                or not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= HISTORY_MAX
                or not isinstance(days, int) or isinstance(days, bool)
                or not 0 <= days <= HISTORY_MAX_DAYS or (days and thread_ts)
                or not isinstance(person, str) or len(person) > HISTORY_PERSON_CHARS):
            return _refused("bad_request")
        install = self._store.slack_install(tenant_id)
        if install is None:
            return _refused("not_installed")
        if not self._store.has_slack_channel(tenant_id, channel):
            _LOG.info("tenant=%s: refused the history of a channel its agent was "
                      "never mentioned in", tenant_id)
            return _refused("other_channel")
        try:
            token = self._box.open(install.token_sealed,
                                   bound_to=seal_binding(install.team_id))
        except Unreadable:
            self._store.set_slack_reconnect(install.team_id, True)
            return _refused("token_unreadable")
        budget = HISTORY_WINDOW_SECONDS if days else HISTORY_SECONDS
        try:
            return await asyncio.wait_for(
                self._read(token, install, channel, thread_ts, limit, days,
                           " ".join(person.split())), budget)
        except SlackUnreachable as exc:
            _LOG.warning("tenant=%s: Slack history failed: %s", tenant_id, exc)
        except TimeoutError:
            _LOG.warning("tenant=%s: Slack history took longer than %ss", tenant_id, budget)
        return _refused("slack_unreachable")

    async def _window(self, token: str, channel: str, days: int) -> tuple[list | dict, bool]:
        """(the channel's messages from now back to `days` days ago, newest
        first, whether older ones in the window were left unread). A refusal
        from Slack comes back in place of the list.

        Slack hands a channel's history out a page at a time. At most
        HISTORY_PAGES pages are read, and no page is started after
        HISTORY_PAGING_SECONDS, so one call costs Slack a bounded number of
        requests however busy the channel was."""
        oldest = f"{self._now() - days * 86400:.6f}"
        started, found, cursor = time.monotonic(), [], ""
        for _ in range(HISTORY_PAGES):
            answer = await self._api.channel_history(
                token, channel, HISTORY_PAGE_SIZE, oldest=oldest, cursor=cursor)
            page = answer.get("messages")
            if answer.get("ok") is not True or not isinstance(page, list):
                return answer, False
            found.extend(page)
            meta = answer.get("response_metadata")
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else ""
            if not isinstance(cursor, str) or not cursor:
                return found, False
            if time.monotonic() - started > HISTORY_PAGING_SECONDS:
                break
        return found, True

    async def _read(self, token: str, install, channel: str, thread_ts: str,
                    limit: int, days: int, person: str) -> dict:
        cut = False
        if days:
            found, cut = await self._window(token, channel, days)
            answer = found if isinstance(found, dict) else {"ok": True, "messages": found}
        elif thread_ts:
            answer = await self._api.thread_replies(token, channel, thread_ts, limit)
        else:
            answer = await self._api.channel_history(token, channel, limit)
        found = answer.get("messages")
        if answer.get("ok") is not True or not isinstance(found, list):
            code = _error_code(answer) or "refused"
            _LOG.info("tenant=%s: Slack refused a history read: %s", install.tenant_id, code)
            return _refused(code)
        if not thread_ts:
            # The channel's own messages. A reply that was also sent to the
            # channel is left to its thread, which `replies` counts.
            found = [m for m in found if isinstance(m, dict)
                     and m.get("thread_ts") in (None, m.get("ts"))]
            # A channel's history arrives newest first, and a thread oldest first.
            found = list(reversed(found if days else found[:limit]))
        else:
            found = found[:limit]
        read = await self._threads.messages(
            token=token, team_id=install.team_id, bot_user_id=install.bot_user_id,
            messages=found)
        channel_name = await self._threads.channel_name(token, install.team_id, channel)
        tenant = self._store.tenant_by_id(install.tenant_id)
        zone = _zone(tenant.timezone if tenant is not None else "UTC")
        base = {"ok": True, "channel": channel_name, "zone": zone.key}
        if person:
            names = sorted({m["who"] for m in read if _is_person(m["who"], person)})
            if len(names) > 1:
                # Two people answer to that name. Guessing one would put one
                # person's words in another's mouth.
                return {**base, "messages": [], "cut": False,
                        "ambiguous": names[:HISTORY_AMBIGUOUS_NAMES]}
            read = [m for m in read if m["who"] in names]
        if len(read) > HISTORY_WINDOW_MESSAGES:
            read, cut = read[-HISTORY_WINDOW_MESSAGES:], True
        for message in read:
            message["text"] = message["text"][:HISTORY_TEXT_CHARS]
            message["when"] = _when(message["ts"], zone)
        # The oldest go first when the answer would not fit on the socket,
        # and the answer says that it was cut.
        kept, size = [], 2
        for message in reversed(read):
            size += len(json.dumps(message)) + 2
            if size > HISTORY_ANSWER_BYTES:
                cut = True
                break
            kept.append(message)
        read = kept[::-1]
        return {**base, "messages": read, "cut": cut}


def _zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _when(ts: str, zone: zoneinfo.ZoneInfo) -> str:
    """A message's time as its reader's clock shows it: `2026-11-12 Thu 14:05`."""
    try:
        moment = datetime.datetime.fromtimestamp(float(ts), zone)
    except (ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %a %H:%M")


def _is_person(who: str, asked: str) -> bool:
    """Whether `asked` names this author: the whole name people see, or its
    first word, whatever the capitals."""
    name, asked = " ".join(who.casefold().split()), asked.casefold()
    return bool(name) and (name == asked or name.split(" ")[0] == asked)


class SlackRoutes:
    def __init__(self, *, settings: SlackSettings, apex_host: str, store: ControlStore,
                 api: SlackApi, now: Callable[[], float] = time.time,
                 wake: Callable[[], None] = lambda: None) -> None:
        self._settings = settings
        self._apex = apex_host
        self._store = store
        self._api = api
        self._now = now
        self._wake = wake
        self._box = SecretBox(settings.token_key)
        self._states = OAuthStates(now)
        self._threads = ThreadReader(api)
        # Signed events for a workspace no tenant connected. Slack is answered
        # 200 for them, so this number is the only trace they leave.
        self.unknown_teams = 0

    @property
    def relay(self) -> SlackRelay:
        """The relay that posts with the tokens these routes stored."""
        return SlackRelay(store=self._store, box=self._box, api=self._api)

    @property
    def history(self) -> SlackHistory:
        """The reader that uses the tokens these routes stored, and the names
        these routes already looked up."""
        return SlackHistory(store=self._store, box=self._box, api=self._api,
                            threads=self._threads, now=self._now)

    def check_tokens(self) -> list[str]:
        """Find the workspaces whose stored token this gateway's key does not
        open, mark each one, and return their team ids. Called once at start.

        The key is in config/gateway.env and in no backup, so a control.db
        restored onto another VM, or a gateway.env whose key line changed,
        leaves tokens nothing can open. That must not be silent, and it must
        not stop the gateway: Slack is one feature, and the gateway serves
        every tenant. So this logs one line for the operator, each marked
        workspace's person sees "Reconnect Slack" on the setup page, and no
        failure in here is raised. A workspace whose token opens is unmarked,
        so putting the old key back needs nothing from anybody.
        """
        try:
            installs = self._store.slack_installs()
            unreadable = []
            for install in installs:
                try:
                    self._box.open(install.token_sealed,
                                   bound_to=seal_binding(install.team_id))
                    needed = False
                except Unreadable:
                    needed = True
                    unreadable.append(install.team_id)
                if needed != install.needs_reconnect:
                    self._store.set_slack_reconnect(install.team_id, needed)
        except Exception:  # noqa: BLE001 -- see the docstring: never raised
            _LOG.exception("could not check the stored Slack tokens")
            return []
        if unreadable:
            _LOG.error(
                "WAKU_SLACK_TOKEN_KEY in config/gateway.env does not open the stored "
                "Slack token of %s of %s workspaces (%s). Each of their people sees "
                '"Reconnect Slack" on /setup. If control.db came from another VM, copy '
                "that VM's WAKU_SLACK_TOKEN_KEY line into config/gateway.env and "
                "recreate the gateway: nobody then has to reconnect.",
                len(unreadable), len(installs), ", ".join(unreadable))
        else:
            _LOG.info("Slack: the key opens all %s stored tokens", len(installs))
        return unreadable

    @property
    def redirect_uri(self) -> str:
        """The one address Slack may send a browser back to. It is entered in
        the Slack app's settings, and Slack refuses any other."""
        return f"https://{self._apex}{CALLBACK_PATH}"

    # --- install --------------------------------------------------------

    def start(self, tenant_id: str) -> web.Response:
        """Send a signed-in person's browser to Slack's approval page."""
        query = urllib.parse.urlencode({
            "client_id": self._settings.client_id, "scope": ",".join(SCOPES),
            "state": self._states.issue(tenant_id), "redirect_uri": self.redirect_uri})
        return answers.redirect(f"{AUTHORIZE_URL}?{query}")

    async def callback(self, request: web.Request,
                       session_tenant: str | None) -> web.Response:
        """Slack sent the browser back. `session_tenant` is the active tenant
        the browser's own apex session names, or None.

        Like /auth/enter, this route's query holds a code, so it must never
        reach an access log.
        """
        owner = self._states.redeem(request.query.get("state", ""))
        if request.query.get("error"):
            # The person pressed Cancel. The state went with it, above.
            return self._back(DENIED)
        if owner is None or owner != session_tenant:
            _LOG.info("Slack callback refused: no state for this browser's session")
            return self._back(EXPIRED)
        try:
            answer = await self._api.oauth_access(
                code=request.query.get("code", ""), redirect_uri=self.redirect_uri,
                client_id=self._settings.client_id,
                client_secret=self._settings.client_secret)
        except SlackUnreachable as exc:
            _LOG.warning("tenant=%s: Slack install failed: %s", owner, exc)
            return self._back(FAILED)
        found = _install_from(answer)
        if found is None:
            _LOG.info("tenant=%s: Slack install refused: %s", owner,
                      _error_code(answer) or "not one workspace's bot token")
            return self._back(FAILED)
        token, team_id, team_name, bot_user_id = found
        stored = self._store.set_slack_install(
            owner, team_id=team_id, team_name=team_name, bot_user_id=bot_user_id,
            token_sealed=self._box.seal(token, bound_to=seal_binding(team_id)))
        if not stored:
            # The token is dropped and NOT revoked: Slack gives one workspace
            # one bot token for this app, so revoking it would disconnect the
            # tenant that connected the workspace first.
            _LOG.info("tenant=%s: Slack team=%s already belongs to another tenant",
                      owner, team_id)
            return self._back(TAKEN)
        _LOG.info("tenant=%s: connected Slack team=%s", owner, team_id)
        return self._back(CONNECTED)

    @staticmethod
    def _back(outcome: str) -> web.Response:
        return answers.redirect(f"{SETUP_PATH}?slack={outcome}")

    # --- events ---------------------------------------------------------

    async def events(self, request: web.Request) -> web.Response:
        """POST /v1/triggers/slack: every workspace's events arrive here.

        The signature is checked first, over the bytes as they arrived, and
        nothing is read from a body that fails it. A signed event is stored
        and answered: no container is started and no model is called here
        (spec 026, decision 1). Slack waits three seconds and then resends,
        and the event id makes that second delivery a no-op.

        NO ORIGIN CHECK, for /v1/chat's reason: the credential is the
        signature, which a browser cannot produce, and Slack sends no Origin.
        """
        if request.method != "POST":
            return answers.json_error(405, EVENTS_POST_ONLY)
        body = await request.read()
        if not signed_by_slack(
                self._settings.signing_secret, body=body, now=self._now(),
                timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
                signature=request.headers.get(SIGNATURE_HEADER, "")):
            _LOG.info("Slack event refused: bad signature or timestamp")
            return answers.json_error(403, BAD_SIGNATURE)
        try:
            envelope = json.loads(body)
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict):
            return answers.json_error(400, NOT_AN_EVENT)
        if envelope.get("type") == "url_verification":
            # Slack checks the address once, when it is entered in the app's
            # settings, and expects its own challenge back.
            return answers.json_ok({"challenge": str(envelope.get("challenge") or "")})
        if envelope.get("type") == "event_callback":
            await self._route(envelope)
        return answers.json_ok({"ok": True})

    async def _route(self, envelope: dict) -> None:
        team_id = envelope.get("team_id")
        tenant_id = self._store.slack_tenant(team_id) if isinstance(team_id, str) else None
        if tenant_id is None:
            self.unknown_teams += 1
            _LOG.info("Slack event for a team no tenant connected (%s so far)",
                      self.unknown_teams)
            return
        event = envelope.get("event")
        if isinstance(event, dict) and event.get("type") == "app_uninstalled":
            self._store.delete_slack_install(team_id)
            _LOG.info("tenant=%s: Slack team=%s removed the app", tenant_id, team_id)
            return
        row, mentioned = event_row(envelope), True
        if row is None:
            # A message with no mention, in a thread the agent is already in.
            install = self._store.slack_install(tenant_id)
            row = follow_up_row(envelope, install.bot_user_id if install else "")
            mentioned = False
            if row is not None and not self._store.has_session(tenant_id, row["session_id"]):
                row = None
        tenant = self._store.tenant_by_id(tenant_id)
        if row is None or tenant is None or tenant.status != "active":
            return
        row["payload"]["text"] = await self._with_thread(tenant_id, event, mentioned)
        if self._store.add_event(tenant_id, **row):
            # A tick now, so the mention does not wait for the next one.
            self._wake()


    async def _with_thread(self, tenant_id: str, event: dict, mentioned: bool = True) -> str:
        """The mention as the agent reads it: the thread above it and the
        names in it (slack_thread.py). The mention's own text when the stored
        token does not open."""
        install = self._store.slack_install(tenant_id)
        try:
            token = self._box.open(install.token_sealed,
                                   bound_to=seal_binding(install.team_id))
        except (Unreadable, AttributeError):
            return str(event.get("text") or "")
        text, _ = await asyncio.gather(
            self._threads.compose(token=token, team_id=install.team_id,
                                  bot_user_id=install.bot_user_id, event=event,
                                  mentioned=mentioned),
            self._seen(token, install.team_id, event))
        return text

    async def _seen(self, token: str, team_id: str, event: dict) -> None:
        """Mark the message as heard. It runs beside the reading of the
        thread and inside the same two seconds, and nothing waits on whether
        it worked: a workspace without `reactions:write` gets no mark and
        every answer."""
        try:
            answer = await asyncio.wait_for(self._api.add_reaction(
                token, str(event.get("channel") or ""), str(event.get("ts") or ""),
                SEEN_REACTION), BUDGET_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- a mark is never worth a mention
            _LOG.info("Slack message not marked for team=%s: %s", team_id, type(exc).__name__)
            return
        if answer.get("ok") is not True:
            _LOG.info("Slack message not marked for team=%s: %s", team_id,
                      _error_code(answer) or "refused")


_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


def _error_code(answer: dict) -> str:
    """Slack's own error code, or "" when the answer carries none that looks
    like one. Only this shape is ever logged or returned."""
    code = answer.get("error")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
