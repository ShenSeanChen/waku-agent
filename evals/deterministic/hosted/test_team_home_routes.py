"""DETERMINISTIC EVAL -- waku.one makes and reads a team home (spec 032 task
A2, decision 2).

Ana is an owner of the team space Acme on waku.one. She presses "Add Waku to
Slack", and waku.one's API calls the platform: POST /v1/team-homes with the
space, the agent and the agent's Waku Memory key. The platform answers a home
id and the address Ana's browser opens next. While Ana has not finished in
Slack, GET /v1/team-homes/{space_id} answers `slack: null`.

Each test is written from `agent-platform-contract.md` in the Waku Memory
context repository, field for field. The tests drive the real Gateway and
ControlDb over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

import pytest
from gatewaylib import (
    AGENT_MEMORY_KEY,
    APEX,
    PLATFORM_SECRET,
    SLACK_TOKEN_KEY,
    FakeSpawner,
    Harness,
)

from hosted.gateway.config import PLATFORM_SECRET_ENV, platform_secret_from
from hosted.gateway.secretbox import SecretBox
from hosted.gateway.store import ControlDb

ACME = "0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11"
AGENT = "5f2d8f0a-1c0b-4d67-a2f4-9f1f6f0f3c22"
RETURN_URL = "https://www.waku.one/spaces/acme"
NEXT_KEY = "mem_sk_" + "B" * 43
PATH = "/v1/team-homes"


def home_request(**changes) -> dict:
    """The body of call 1, with every field the contract names."""
    body = {"space_id": ACME, "space_name": "Acme", "agent_id": AGENT,
            "agent_name": "Waku", "memory_key": AGENT_MEMORY_KEY,
            "memory_api_url": "https://api.waku.one", "return_url": RETURN_URL}
    body.update(changes)
    return {name: value for name, value in body.items() if value is not ...}


@pytest.fixture
def harness(tmp_path):
    built = Harness(tmp_path, FakeSpawner())
    built.use_team_homes()
    return built


def _reopen(harness: Harness) -> ControlDb:
    """control.db as it is on disk, after the harness closed its own."""
    return ControlDb(harness.config.control_db)


def _key_of(home) -> str:
    """The agent's key, opened the way the gateway opens it."""
    return SecretBox(bytes.fromhex(SLACK_TOKEN_KEY)).open(
        home.key_sealed, bound_to=f"team_home:{home.space_id}")


def bearer(secret: str = PLATFORM_SECRET) -> dict:
    return {"Authorization": f"Bearer {secret}"}


async def call(harness: Harness, method: str, target: str, body: dict | None = None, *,
               headers: dict | list | None = None):
    """One call the way waku.one's API makes it: JSON, the secret, no Origin
    and no cookie. (status, the answer as JSON or None)."""
    head = bearer() if headers is None else headers
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        head = ([*head, ("Content-Type", "application/json")] if isinstance(head, list)
                else {**head, "Content-Type": "application/json"})
    status, _, answer = await harness.send(method, target, host=APEX, body=raw,
                                           headers=head)
    try:
        return status, json.loads(answer)
    except ValueError:
        return status, None


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


# --- the secret -------------------------------------------------------------

def test_every_route_answers_503_when_the_secret_is_not_set(tmp_path):
    """A gateway.env from before spec 032 has no WAKU_AGENT_PLATFORM_SECRET.
    The answer names the variable, whatever the caller sent."""
    harness = Harness(tmp_path, FakeSpawner())

    async def body():
        return (await call(harness, "POST", PATH, home_request()),
                await call(harness, "GET", f"{PATH}/{ACME}"),
                await call(harness, "DELETE", f"{PATH}/{ACME}"),
                await call(harness, "POST", PATH, home_request(), headers={}))

    for status, answer in _run(harness, body):
        assert status == 503
        assert PLATFORM_SECRET_ENV in answer["error"]
    assert _reopen(harness).team_home(ACME) is None


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer " + "x" * len(PLATFORM_SECRET)},
    {"Authorization": "Bearer " + PLATFORM_SECRET[:-1]},
    {"Authorization": "Bearer " + PLATFORM_SECRET + "x"},
    {"Authorization": "Basic " + PLATFORM_SECRET},
    {"Authorization": PLATFORM_SECRET},
    {"Authorization": "Bearer "},
    [("Authorization", "Bearer " + PLATFORM_SECRET), ("Authorization", "Bearer wrong")],
    {"Cookie": "__Host-waku_session=" + PLATFORM_SECRET},
])
def test_anything_but_the_secret_is_401_and_makes_nothing(harness, headers):
    async def body():
        return (await call(harness, "POST", PATH, home_request(), headers=headers),
                await call(harness, "GET", f"{PATH}/{ACME}", headers=headers))

    for status, answer in _run(harness, body):
        assert status == 401
        assert set(answer) == {"error"}
    assert _reopen(harness).team_home(ACME) is None


def test_the_secret_is_checked_before_the_space_is_looked_up(harness):
    """A caller without the secret cannot learn which spaces have a home."""
    async def body():
        await call(harness, "POST", PATH, home_request())
        return (await call(harness, "GET", f"{PATH}/{ACME}", headers={}),
                await call(harness, "GET", f"{PATH}/{AGENT}", headers={}))

    (has_home, _), (no_home, _) = _run(harness, body)
    assert has_home == no_home == 401


@pytest.mark.parametrize("value, usable", [
    ("s" * 32, True), ("  " + "s" * 40 + "\n", True), ("", False), ("s" * 31, False),
    ("s" * 20 + " " + "s" * 20, False)])
def test_the_secret_is_read_from_gateway_env(value, usable):
    secret, problem = platform_secret_from({PLATFORM_SECRET_ENV: value})

    assert bool(secret) is usable
    assert (problem == "") is usable
    if not usable:
        assert PLATFORM_SECRET_ENV in problem
        assert value.strip() == "" or value not in problem
    else:
        assert secret == value.strip()


def test_the_secret_is_not_in_the_configs_repr(harness):
    assert harness.config.platform_secret == PLATFORM_SECRET
    assert PLATFORM_SECRET not in repr(harness.config)


# --- call 1: make a home ------------------------------------------------------

def test_a_post_answers_201_with_the_home_id_and_the_install_url(harness):
    status, answer = _run(harness, lambda: call(harness, "POST", PATH, home_request()))

    assert status == 201
    assert set(answer) == {"home_id", "install_url"}
    home = _reopen(harness).team_home(ACME)
    assert answer["home_id"] == home.tenant_id
    assert isinstance(answer["home_id"], str)
    # "It must be on the same scheme and host as WAKU_AGENT_PLATFORM_URL."
    parts = urllib.parse.urlsplit(answer["install_url"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", APEX, "/slack/install")
    [(name, ticket)] = urllib.parse.parse_qsl(parts.query)
    assert name == "home"
    assert len(ticket) >= 43


def test_a_post_stores_every_field_and_starts_no_container(harness):
    _run(harness, lambda: call(harness, "POST", PATH, home_request()))

    home = _reopen(harness).team_home(ACME)
    assert (home.space_id, home.space_name, home.agent_id, home.agent_name,
            home.return_url) == (ACME, "Acme", AGENT, "Waku", RETURN_URL)
    assert _reopen(harness).tenant_by_id(home.tenant_id).kind == "team"
    assert _key_of(home) == AGENT_MEMORY_KEY
    assert harness.spawner.requests == []


def test_the_key_is_sealed_and_bound_to_the_space(harness, tmp_path):
    _run(harness, lambda: call(harness, "POST", PATH, home_request()))

    files = [path for path in tmp_path.iterdir() if path.name.startswith("control.db")]
    assert files
    for path in files:
        assert b"mem_sk_" not in path.read_bytes(), path.name
    store = _reopen(harness)
    home = store.team_home(ACME)
    assert _key_of(home) == AGENT_MEMORY_KEY
    assert store.memory_key(home.tenant_id) == ""


def test_a_second_post_for_a_space_replaces_the_key_and_answers_a_new_address(harness):
    """The contract: "The call is idempotent on space_id"."""
    async def body():
        first = await call(harness, "POST", PATH, home_request())
        second = await call(harness, "POST", PATH, home_request(memory_key=NEXT_KEY))
        return first, second

    (first_status, first), (second_status, second) = _run(harness, body)
    assert first_status == second_status == 201
    assert second["home_id"] == first["home_id"]
    assert second["install_url"] != first["install_url"]
    home = _reopen(harness).team_home(ACME)
    assert _key_of(home) == NEXT_KEY


def test_an_empty_memory_api_url_is_accepted(harness):
    status, _ = _run(harness, lambda: call(
        harness, "POST", PATH, home_request(memory_api_url="")))
    assert status == 201


@pytest.mark.parametrize("field, bad", [
    ("space_id", ...), ("space_id", "acme"), ("space_id", 7),
    ("space_name", ...), ("space_name", ""), ("space_name", "   "), ("space_name", "x" * 201),
    ("agent_id", ...), ("agent_id", "not-a-uuid"),
    ("agent_name", ...), ("agent_name", 3),
    ("memory_key", ...), ("memory_key", ""), ("memory_key", "sk-something-else"),
    ("memory_key", AGENT_MEMORY_KEY + "\nWAKU_HOSTED=0"),
    ("memory_api_url", 5),
    ("return_url", ...), ("return_url", ""), ("return_url", "http://www.waku.one/x"),
    ("return_url", "javascript:alert(1)"), ("return_url", "//evil.example/x"),
    ("return_url", "https://ana:pw@www.waku.one/x"), ("return_url", "https://www.waku.one/ x"),
])
def test_a_field_that_is_missing_or_unusable_is_422_and_makes_nothing(harness, field, bad):
    status, answer = _run(harness, lambda: call(
        harness, "POST", PATH, home_request(**{field: bad})))

    assert status == 422
    assert field in answer["error"]
    assert _reopen(harness).team_home(ACME) is None


def test_a_refusal_never_quotes_the_key(harness):
    key = "mem_sk_not-the-right-length"
    _, answer = _run(harness, lambda: call(
        harness, "POST", PATH, home_request(memory_key=key)))
    assert key not in json.dumps(answer)


def test_a_body_that_is_not_a_json_object_is_400(harness):
    async def body():
        raw = await harness.send("POST", PATH, host=APEX, body=b"space_id=1",
                                 headers=bearer())
        listed = await call(harness, "POST", PATH, body=None, headers=bearer())
        return raw[0], listed[0]

    assert _run(harness, body) == (400, 400)


def test_nothing_of_the_request_is_logged(harness, caplog):
    """The contract: "The platform stores it as a secret and never logs it"."""
    caplog.set_level(logging.DEBUG)

    async def body():
        await call(harness, "POST", PATH, home_request())
        await call(harness, "POST", PATH, home_request(memory_key="mem_sk_wrong"))
        await call(harness, "GET", f"{PATH}/{ACME}")
        await call(harness, "GET", f"{PATH}/{ACME}", headers=bearer("w" * 40))

    _run(harness, body)
    assert caplog.text
    for secret in (AGENT_MEMORY_KEY, "mem_sk_", PLATFORM_SECRET, "w" * 40, RETURN_URL):
        assert secret not in caplog.text


# --- call 2: read a home ------------------------------------------------------

def test_a_home_with_no_workspace_reads_slack_null(harness):
    async def body():
        _, made = await call(harness, "POST", PATH, home_request())
        return made, await call(harness, "GET", f"{PATH}/{ACME}")

    made, (status, answer) = _run(harness, body)
    assert status == 200
    assert answer == {"home_id": made["home_id"], "slack": None, "refused": None}


def test_a_home_with_a_workspace_reads_its_team_id_and_name(harness):
    async def body():
        _, made = await call(harness, "POST", PATH, home_request())
        harness.store.set_slack_install(
            made["home_id"], team_id="T0123", team_name="Acme HQ", bot_user_id="U0BOT",
            token_sealed=b"sealed")
        return made, await call(harness, "GET", f"{PATH}/{ACME}")

    made, (status, answer) = _run(harness, body)
    assert status == 200
    assert answer == {"home_id": made["home_id"],
                      "slack": {"team_id": "T0123", "team_name": "Acme HQ"},
                      "refused": None}


@pytest.mark.parametrize("space", [AGENT, "acme", "0b0f3c1e"])
def test_a_space_with_no_home_is_404(harness, space):
    async def body():
        await call(harness, "POST", PATH, home_request())
        return await call(harness, "GET", f"{PATH}/{space}")

    status, answer = _run(harness, body)
    assert status == 404
    assert set(answer) == {"error"}


# --- what is not built yet ----------------------------------------------------

def test_delete_changes_nothing_until_group_b(harness):
    """waku.one treats every answer but 204 and 404 as "nothing changed"."""
    async def body():
        await call(harness, "POST", PATH, home_request())
        return await call(harness, "DELETE", f"{PATH}/{ACME}")

    status, _ = _run(harness, body)
    assert status == 405
    assert _reopen(harness).team_home(ACME) is not None


@pytest.mark.parametrize("method, target", [
    ("GET", PATH), ("PUT", PATH), ("POST", f"{PATH}/{ACME}"), ("PATCH", f"{PATH}/{ACME}")])
def test_any_other_method_is_405(harness, method, target):
    status, _ = _run(harness, lambda: call(harness, method, target))
    assert status == 405
