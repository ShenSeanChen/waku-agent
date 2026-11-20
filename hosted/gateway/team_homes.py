"""The home of a team space's agent (spec 032, decisions 1 and 2).

waku.one calls the platform to give a team space's agent a home: a tenant of
its own, with its own container and disk, that runs on the agent's Waku Memory
key. Three things are here:

  routes    POST /v1/team-homes makes a home, or replaces the key of one that
            exists, and answers the address where an owner adds Waku to Slack.
            GET /v1/team-homes/{space_id} says which workspace the home has.
            DELETE is not built yet (spec 032 group B) and answers 405.
  tickets   HomeTickets: the `?home=<ticket>` of that address. A ticket stands
            in for a sign-in at /slack/install, because the owner's browser
            arrives from waku.one with no session here.
  keys      HomeKeys: the agent's key, sealed into control.db with the
            gateway's SecretBox.

THE CALLER IS waku.one's API, NOT A BROWSER. Every request carries
`Authorization: Bearer <WAKU_AGENT_PLATFORM_SECRET>`, the one secret the two
services share. It is compared in constant time, and anything else is 401.
With the secret unset every route answers 503 (app.py).

NOTHING HERE LOGS A REQUEST BODY, THE SECRET OR A KEY. A log line carries a
space id and a tenant id. A refusal names a field and never quotes its value.

The contract this was built from is in the Waku Memory context repository,
`specs/101-team-onboarding/agent-platform-contract.md`.
"""

from __future__ import annotations

import hmac
import json
import urllib.parse
import uuid
from collections.abc import Callable

from aiohttp import web

from hosted import log
from hosted.core.tenant import is_memory_key
from hosted.gateway import answers, sessions
from hosted.gateway.secretbox import SecretBox
from hosted.ports.control import ControlStore, TeamHome

_LOG = log.get(__name__)

PATH = "/v1/team-homes"
INSTALL_PATH = "/slack/install"
# The ticket in install_url: one use, ten minutes (spec 032, decision 2).
TICKET_TTL_SECONDS = 600
NAME_CHARS = 200
URL_CHARS = 2000

NOT_CONFIGURED = "Team homes are not set up on this server"
WRONG_SECRET = "That request does not carry the platform secret."
NOT_A_HOME_REQUEST = "Send the home as a JSON object."
NO_HOME = "This server has no home for that space."
METHODS = "Make a home with POST /v1/team-homes; read one with GET /v1/team-homes/<space id>."
DELETE_NOT_BUILT = "Removing a home is not built yet. Nothing was changed."
BAD_FIELD = "{field} is missing or is not usable."


def not_configured(problem: str) -> web.Response:
    """What every route answers until the secret and the sealing key are set.
    `problem` names what is missing and never a value."""
    return answers.json_error(503, f"{NOT_CONFIGURED}: {problem}.")


def seal_binding(space_id: str) -> str:
    """What a home's sealed key is bound to, so one row's value does not open
    in another row, or as a Slack token."""
    return f"team_home:{space_id}"


def carries_secret(request: web.Request, secret: str) -> bool:
    """Whether the request's one Authorization header is `Bearer <secret>`.
    The comparison takes the same time whatever the two values share."""
    values = request.headers.getall("Authorization", ())
    scheme, _, sent = (values[0] if len(values) == 1 else "").partition(" ")
    if scheme.lower() != "bearer" or not secret:
        return False
    return hmac.compare_digest(sent.strip().encode("utf-8"), secret.encode("utf-8"))


def _uuid(value: object) -> str | None:
    """A uuid in its one canonical spelling, or None."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _name(value: object) -> str | None:
    """A name on one line, or None when it is empty or too long."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    return name if 0 < len(name) <= NAME_CHARS else None


def _return_url(value: object) -> str | None:
    """An https address with a host, or None. http is taken for a waku.one
    that runs on the same machine. The list of hosts the browser may be sent
    to is spec 032 task B4; until then the caller holds the platform secret."""
    if not isinstance(value, str) or not 0 < len(value) <= URL_CHARS:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    if not host or parts.username or parts.password:
        return None
    local = parts.scheme == "http" and host in ("localhost", "127.0.0.1")
    return value if parts.scheme == "https" or local else None


class HomeKeys:
    """The Waku Memory key of each team space's agent, sealed.

    A person's key is stored in plaintext (store.set_memory_key says why). A
    team's key reads every team memory of its space, and control.db is copied
    off the VM every night, so this one is sealed with the key that seals a
    workspace's Slack token and bound to the space's id."""

    def __init__(self, store: ControlStore, box: SecretBox) -> None:
        self._store = store
        self._box = box

    def seal(self, space_id: str, key: str) -> bytes:
        return self._box.seal(key, bound_to=seal_binding(space_id))

    def open(self, home: TeamHome) -> str:
        """The agent's key. Raises Unreadable when the gateway's sealing key
        does not open it."""
        return self._box.open(home.key_sealed, bound_to=seal_binding(home.space_id))


class HomeTickets:
    """The ticket in each install_url, in memory.

    A ticket is random, names one space, works once and ends after ten
    minutes. A space has one ticket at a time: a new POST ends the earlier
    one, which also bounds this dict by the number of spaces. A gateway
    restart forgets them all, and the owner presses the button again."""

    def __init__(self, now: Callable[[], float],
                 ttl: float = TICKET_TTL_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        self._tickets: dict[str, tuple[str, float]] = {}

    def issue(self, space_id: str) -> str:
        now = self._now()
        for ticket in [t for t, (space, expires) in self._tickets.items()
                       if space == space_id or expires <= now]:
            self._tickets.pop(ticket, None)
        ticket = sessions.new_secret()
        self._tickets[ticket] = (space_id, now + self._ttl)
        return ticket

    def redeem(self, ticket: str) -> str | None:
        """The space this ticket was issued for, once, or None. It is popped
        before it is checked, so it is used whichever way the check goes."""
        found = self._tickets.pop(ticket, None)
        if found is None:
            return None
        space_id, expires = found
        return space_id if self._now() < expires else None


class TeamHomeRoutes:
    def __init__(self, *, secret: str, apex_host: str, store: ControlStore,
                 keys: HomeKeys, now: Callable[[], float]) -> None:
        self._secret = secret
        self._apex = apex_host
        self._store = store
        self._keys = keys
        self._tickets = HomeTickets(now)

    def redeem_ticket(self, ticket: str) -> TeamHome | None:
        """The home an install_url's ticket names, once, or None."""
        space_id = self._tickets.redeem(ticket)
        return self._store.team_home(space_id) if space_id else None

    async def handle(self, request: web.Request, path: str) -> web.Response:
        """Every request under /v1/team-homes. The secret is checked before
        anything is read from the path or the body."""
        if not carries_secret(request, self._secret):
            _LOG.info("team-homes request refused: no platform secret")
            return answers.json_error(401, WRONG_SECRET)
        rest = path[len(PATH):]
        if not rest:
            if request.method != "POST":
                return answers.json_error(405, METHODS)
            return await self._make(request)
        space_id = _uuid(rest[1:])
        if space_id is None:
            return answers.json_error(404, NO_HOME)
        if request.method == "GET":
            return self._read(space_id)
        if request.method == "DELETE":
            return answers.json_error(405, DELETE_NOT_BUILT)
        return answers.json_error(405, METHODS)

    async def _make(self, request: web.Request) -> web.Response:
        try:
            sent = json.loads(await request.read())
        except ValueError:
            sent = None
        if not isinstance(sent, dict):
            return answers.json_error(400, NOT_A_HOME_REQUEST)
        values = {"space_id": _uuid(sent.get("space_id")),
                  "space_name": _name(sent.get("space_name")),
                  "agent_id": _uuid(sent.get("agent_id")),
                  "agent_name": _name(sent.get("agent_name")),
                  "return_url": _return_url(sent.get("return_url"))}
        key = sent.get("memory_key")
        if not is_memory_key(key):
            values["memory_key"] = None
        # Where the key works. The platform has one Waku Memory address in
        # its own configuration and sends a key nowhere else, so this is read
        # for its type only. Refusing another address is spec 032 task B4.
        if not isinstance(sent.get("memory_api_url", ""), str):
            values["memory_api_url"] = None
        bad = sorted(field for field, value in values.items() if value is None)
        if bad:
            # The field's name, never what was sent: one of them is a key.
            return answers.json_error(422, BAD_FIELD.format(field=bad[0]))
        home, created = self._store.set_team_home(
            space_id=values["space_id"], space_name=values["space_name"],
            agent_id=values["agent_id"], agent_name=values["agent_name"],
            return_url=values["return_url"],
            key_sealed=self._keys.seal(values["space_id"], key))
        _LOG.info("space=%s: %s the home tenant=%s", home.space_id,
                  "made" if created else "replaced the key of", home.tenant_id)
        ticket = self._tickets.issue(home.space_id)
        response = answers.json_ok({
            "home_id": home.tenant_id,
            "install_url": f"https://{self._apex}{INSTALL_PATH}?home={ticket}"})
        response.set_status(201)
        return response

    def _read(self, space_id: str) -> web.Response:
        home = self._store.team_home(space_id)
        if home is None:
            return answers.json_error(404, NO_HOME)
        install = self._store.slack_install(home.tenant_id)
        return answers.json_ok({
            "home_id": home.tenant_id,
            "slack": ({"team_id": install.team_id, "team_name": install.team_name}
                      if install is not None else None),
            # `workspace_taken` is spec 032 task B3.
            "refused": None})
