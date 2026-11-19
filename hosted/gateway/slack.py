"""The public Waku Slack app (spec 026 group C, decision 5).

One Slack app serves every workspace. Three things happen here:

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

import base64
import gzip
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from collections.abc import Callable

import aiohttp
from aiohttp import web

from hosted import log
from hosted.core.tenant import is_tenant_id
from hosted.gateway import answers, sessions
from hosted.gateway.config import SlackSettings
from hosted.gateway.secretbox import SecretBox, Unreadable
from hosted.ports.control import ControlStore

_LOG = log.get(__name__)

SLACK_API = "https://slack.com/api"
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
INSTALL_PATH = "/slack/install"
CALLBACK_PATH = "/slack/oauth/callback"
EVENTS_PATH = "/v1/triggers/slack"
SETUP_PATH = "/setup"
# What the bot may do: hear a mention of itself, and post a message.
SCOPES = ("app_mentions:read", "chat:write")
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
    """The two Slack Web API methods the gateway calls."""

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
        # Signed events for a workspace no tenant connected. Slack is answered
        # 200 for them, so this number is the only trace they leave.
        self.unknown_teams = 0

    @property
    def relay(self) -> SlackRelay:
        """The relay that posts with the tokens these routes stored."""
        return SlackRelay(store=self._store, box=self._box, api=self._api)

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
            self._route(envelope)
        return answers.json_ok({"ok": True})

    def _route(self, envelope: dict) -> None:
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
        row = event_row(envelope)
        tenant = self._store.tenant_by_id(tenant_id)
        if row is None or tenant is None or tenant.status != "active":
            return
        if self._store.add_event(tenant_id, **row):
            # A tick now, so the mention does not wait for the next one.
            self._wake()


_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


def _error_code(answer: dict) -> str:
    """Slack's own error code, or "" when the answer carries none that looks
    like one. Only this shape is ever logged or returned."""
    code = answer.get("error")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
