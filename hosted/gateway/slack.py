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
import re
import time
import urllib.parse
from collections.abc import Callable

import aiohttp
from aiohttp import web

from hosted import log
from hosted.gateway import answers, sessions
from hosted.gateway.config import SlackSettings
from hosted.gateway.secretbox import SecretBox
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


class SlackApi:
    """The two Slack Web API methods the gateway calls."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str = SLACK_API) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)

    async def _post(self, method: str, **kwargs) -> dict:
        try:
            async with self._session.post(f"{self._base}/{method}", timeout=self._timeout,
                                          **kwargs) as reply:
                answer = await reply.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
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


_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


def _error_code(answer: dict) -> str:
    """Slack's own error code, or "" when the answer carries none that looks
    like one. Only this shape is ever logged or returned."""
    code = answer.get("error")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
