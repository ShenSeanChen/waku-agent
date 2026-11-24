"""DETERMINISTIC EVAL -- GitHub on hosted Waku, read-only (spec 029, group F).

GitHub's MCP server takes sign-in only from a GitHub App registered at GitHub
beforehand. The gateway holds that app's client id and secret, from
config/gateway.env, and sends a person to GitHub's own sign-in address.
Without the two values GitHub is not a connector on the server.

Reading is enforced three times. This file proves the two the gateway owns:
it calls GitHub's read-only address, and it refuses every tool of GitHub's
that writes before GitHub is called. The third is the app's own permissions,
which are set at GitHub.

The tests drive the real Gateway, ControlDb, Connect, VendorApi and McpRelay.
GitHub is the FakeVendor on loopback. No test reaches GitHub.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

import pytest
from gatewaylib import (
    APEX,
    SLACK_TOKEN_KEY,
    FakeSpawner,
    FakeVendor,
    Harness,
    pkce_challenge,
    sign_in,
)

from hosted.core.connectors import VENDORS, is_connector_name
from hosted.core.provision import sync_connectors
from hosted.gateway.config import (
    GITHUB_ENV_NAMES,
    connect_clients_from,
)
from hosted.gateway.connect import Connect, ReconnectNeeded, VendorUnreachable
from hosted.gateway.connect_mcp import McpRelay
from hosted.gateway.store import ControlDb

REDIRECT_URI = f"https://{APEX}/connect/callback"
COOKIE = "__Host-waku_session"
CLIENT_ID = "Iv23liFakeClientId00"
CLIENT_SECRET = "0123456789abcdef0123456789abcdef01234567"
CLIENTS = {"github": (CLIENT_ID, CLIENT_SECRET)}

# Every tool of GitHub's server that changes something, from the tool list in
# https://github.com/github/github-mcp-server/blob/main/README.md on
# 2026-11-19. A name GitHub adds later under one of these verbs is caught by
# the same pattern; a name under a new verb is added here and to `writes`.
GITHUB_WRITES = (
    "actions_run_trigger", "update_code_scanning_alert", "assign_copilot_to_issue",
    "assign_copilot_to_issue_with_intent", "request_copilot_review",
    "create_pull_request_with_copilot", "update_dependabot_alert",
    "discussion_comment_write", "create_gist", "update_gist",
    "create_repository_ruleset", "custom_properties_write", "add_issue_comment",
    "issue_write", "sub_issue_write", "update_issue_comment", "label_write",
    "dismiss_notification", "manage_notification_subscription",
    "manage_repository_notification_subscription", "mark_all_notifications_read",
    "projects_write", "add_comment_to_pending_review",
    "add_reply_to_pull_request_comment", "create_pull_request", "merge_pull_request",
    "pull_request_review_write", "update_pull_request", "update_pull_request_branch",
    "create_branch", "create_or_update_file", "create_repository", "delete_file",
    "delete_repository", "fork_repository", "push_files", "update_secret_scanning_alert",
    "star_repository", "unstar_repository",
)
# Tools that read repositories, pull requests and issues, from the same list.
GITHUB_READS = (
    "get_file_contents", "get_commit", "list_commits", "list_branches", "search_code",
    "search_repositories", "get_repository_tree", "list_pull_requests",
    "pull_request_read", "search_pull_requests", "issue_read", "list_issues",
    "search_issues", "get_me", "actions_get", "actions_list", "projects_get",
    "list_releases", "get_label", "custom_properties_read",
)


def _call(tool: str, message_id: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call",
            "params": {"name": tool, "arguments": {"owner": "waku", "repo": "agent"}}}


@pytest.fixture
def github():
    fake = FakeVendor("github")
    # The app Sean registered at GitHub: GitHub knows it, nobody registers it.
    fake.clients[CLIENT_ID] = {"client_secret": CLIENT_SECRET}
    fake.expires_in = 28800          # GitHub's eight hours
    fake.tools = [{"name": name, "description": name,
                   "inputSchema": {"type": "object", "properties": {}}}
                  for name in ("get_file_contents", "list_pull_requests", "issue_read")]
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def notion():
    fake = FakeVendor("notion")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, github, notion):
    """A gateway whose gateway.env holds the GitHub App's two values."""
    built = Harness(tmp_path, FakeSpawner())
    built.use_connect(notion=notion, github=github)
    built.connect_clients = dict(CLIENTS)
    return built


@pytest.fixture
def bare(tmp_path, github, notion):
    """The same gateway with neither value: GitHub is not set up."""
    built = Harness(tmp_path, FakeSpawner())
    built.use_connect(notion=notion, github=github)
    return built


def _run(harness: Harness, body):
    """Run `body` against the started gateway, and open the store again
    afterwards so a test can read what the gateway left."""
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    try:
        return asyncio.run(run())
    finally:
        harness.store = ControlDb(harness.config.control_db, now=harness.clock)
        harness.connect = Connect(
            key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host=APEX, store=harness.store,
            api=None, vendors=harness.vendors(), clients=harness.connect_clients,
            now=harness.clock)


async def _start(harness: Harness, cookie: str, server: str = "github") -> dict:
    status, headers, body = await harness.send(
        "GET", f"/connect/start?server={server}", host=APEX, cookie=f"{COOKIE}={cookie}")
    location = headers.get("location", "")
    # An empty field is kept, so a test sees a `scope=` that was sent.
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query,
                                        keep_blank_values=True))
    return {"status": status, "location": location, "body": body, "query": query}


async def _callback(harness: Harness, cookie: str, **query) -> str:
    status, headers, _ = await harness.send(
        "GET", "/connect/callback?" + urllib.parse.urlencode(query), host=APEX,
        cookie=f"{COOKIE}={cookie}")
    assert status == 302
    return headers["location"]


async def _connect(harness: Harness, github: FakeVendor, **claims) -> tuple[str, str, str]:
    """A whole sign-in to GitHub: (tenant id, apex cookie, where the callback
    sent the browser)."""
    tenant_id, cookie, _ = await sign_in(harness, **claims)
    start = await _start(harness, cookie)
    landed = await _callback(harness, cookie, code=github.approve(start["query"]),
                             state=start["query"]["state"])
    return tenant_id, cookie, landed


async def _page(harness: Harness, cookie: str, query: str = "") -> str:
    status, _, body = await harness.send("GET", "/setup" + query, host=APEX,
                                         cookie=f"{COOKIE}={cookie}")
    assert status == 200
    return body.decode("utf-8")


def _relay(harness: Harness) -> McpRelay:
    return McpRelay(connect=harness.connect, session=harness.session, apex_host=APEX)


def _raw(harness: Harness) -> bytes:
    folder = harness.config.control_db.parent
    return b"".join(path.read_bytes() for path in folder.glob("control.db*"))


# --- the list entry ---------------------------------------------------------------

def test_github_is_called_at_its_read_only_address_and_signs_in_at_github_com():
    """The three addresses, as literals: each is from GitHub's documentation,
    and a change to one is a change someone has to mean."""
    github = VENDORS["github"]
    assert github.url == "https://api.githubcopilot.com/mcp/readonly"
    assert github.authorize_url == "https://github.com/login/oauth/authorize"
    assert github.token_url == "https://github.com/login/oauth/access_token"
    # GitHub's sign-in server names itself with a path, and says so on the
    # callback. Read from its own metadata on 2026-11-20.
    assert github.issuer == "https://github.com/login/oauth"
    # A GitHub App's sign-in takes no scope: the app's permissions decide.
    assert github.scope == ""


def test_notion_and_linear_still_register_themselves():
    for name in ("notion", "linear"):
        assert (VENDORS[name].authorize_url, VENDORS[name].token_url) == ("", "")


@pytest.mark.parametrize("tool", GITHUB_WRITES)
def test_every_github_tool_that_writes_matches_the_rule(tool):
    assert VENDORS["github"].is_write(tool)


@pytest.mark.parametrize("tool", GITHUB_READS)
def test_a_github_tool_that_reads_does_not(tool):
    assert not VENDORS["github"].is_write(tool)


def test_the_container_side_takes_github_as_a_connector_name(tmp_path):
    """The proxy's route, the spawner's request and mcp.json all check the
    name against the list (is_connector_name)."""
    assert is_connector_name("github")
    assert sync_connectors(tmp_path, "http://10.88.0.1:8788", ["github"]) is True
    assert '"url": "http://10.88.0.1:8788/connect/github/mcp"' in (
        tmp_path / "mcp.json").read_text(encoding="utf-8")


# --- the two values in config/gateway.env -----------------------------------------

def test_github_needs_both_values_and_the_reason_quotes_neither():
    assert GITHUB_ENV_NAMES == ("WAKU_GITHUB_CLIENT_ID", "WAKU_GITHUB_CLIENT_SECRET")
    full = {"WAKU_GITHUB_CLIENT_ID": f"  {CLIENT_ID} ", "WAKU_GITHUB_CLIENT_SECRET": CLIENT_SECRET}
    assert connect_clients_from(full) == ({"github": (CLIENT_ID, CLIENT_SECRET)}, "")
    assert connect_clients_from({}) == ({}, (
        "config/gateway.env is missing WAKU_GITHUB_CLIENT_ID, WAKU_GITHUB_CLIENT_SECRET"))
    for name in GITHUB_ENV_NAMES:
        clients, problem = connect_clients_from(full | {name: ""})
        assert clients == {} and problem == f"config/gateway.env is missing {name}"
    # A line pasted wrong: a space or a quote inside the value.
    clients, problem = connect_clients_from(
        full | {"WAKU_GITHUB_CLIENT_SECRET": 'abc def"zzzzzz'})
    assert clients == {}
    assert "WAKU_GITHUB_CLIENT_SECRET" in problem and "zzzz" not in problem


def test_a_gateway_env_without_the_two_lines_still_starts():
    """upgrade.sh never rewrites config/, so the VM's gateway.env has neither
    line until Sean adds them."""
    from test_embed import _example_env

    from hosted.gateway.config import config_from_env

    config = config_from_env(_example_env())
    assert dict(config.connect_clients) == {}
    assert "WAKU_GITHUB_CLIENT_ID" in config.github_problem
    config = config_from_env(_example_env() | {
        "WAKU_GITHUB_CLIENT_ID": CLIENT_ID, "WAKU_GITHUB_CLIENT_SECRET": CLIENT_SECRET})
    assert dict(config.connect_clients) == CLIENTS
    assert CLIENT_SECRET not in repr(config)


# --- without the app, GitHub is not a connector here ------------------------------

def test_without_the_app_github_is_not_on_the_servers_list(bare, github):
    async def body():
        tenant_id, cookie, _ = await sign_in(bare)
        start = await _start(bare, cookie)
        relay = _relay(bare)
        return (start, await _page(bare, cookie), relay.link(tenant_id, "github"),
                await relay.call(tenant_id, "github", _call("get_file_contents")),
                bare.connect.states(tenant_id))

    start, page, link, call, states = _run(bare, body)
    assert start["status"] == 404
    assert b"That is not a connector Waku can connect." in start["body"]
    assert 'data-connector="github"' not in page and "GitHub" not in page
    assert 'data-connector="notion"' in page
    # What "connect GitHub" in chat is answered with, and what a hosted agent
    # reads when it builds its tools: GitHub is not among the names.
    assert link == {"ok": False, "error": "not_listed", "available": ["Notion"]}
    assert call == {"ok": False, "error": "bad_request"}
    assert states == {"notion": "todo"}
    assert github.requests == []


def test_connect_github_in_a_hosted_chat_says_what_the_server_has(bare, harness, monkeypatch):
    """The MIT agent's own words (waku/tools/connectors.py), on a server
    without the app and on one with it."""
    from waku.tools import connectors

    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", "http://10.88.0.1:8788")
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "wkp_test")

    def asked_of(which: Harness):
        async def body():
            tenant_id, _, _ = await sign_in(which)
            relay = _relay(which)
            return connectors.hosted_connect(
                "GitHub", post=lambda url, sent, headers: relay.link(tenant_id, sent["server"]))

        return _run(which, body)

    assert asked_of(bare) == ("Not connected: on hosted Waku the connectors are Notion. "
                              "Another server cannot be added here.")
    assert asked_of(harness) == (
        "Open this link and sign in to GitHub. It asks you to sign in to Waku first if "
        f"you are not signed in:\nhttps://{APEX}/connect/start?server=github\nWhen the "
        "page says GitHub is connected, its tools are there in my next answer.")


# --- the sign-in ------------------------------------------------------------------

def test_the_start_sends_a_person_to_github_with_the_apps_client_id(harness, github):
    async def body():
        _, cookie, _ = await sign_in(harness)
        return await _start(harness, cookie)

    start = _run(harness, body)
    assert start["status"] == 302
    assert start["location"].startswith(github.url + "/authorize?")
    query = start["query"]
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 32
    # No scope: a GitHub App's permissions are the app's own. No `resource`:
    # GitHub's page names no such field.
    assert set(query) == {"response_type", "client_id", "redirect_uri", "state",
                          "code_challenge", "code_challenge_method"}
    assert CLIENT_SECRET not in start["location"]
    # Nothing was discovered and nothing was registered: GitHub was not asked.
    assert github.requests == []


def test_an_approved_sign_in_keeps_the_tokens_sealed_and_stores_no_client(harness, github):
    async def body():
        tenant_id, cookie, landed = await _connect(harness, github)
        token = await harness.connect.access_token(tenant_id, "github")
        return tenant_id, landed, token, await _page(harness, cookie), _raw(harness)

    tenant_id, landed, token, page, raw = _run(harness, body)
    assert landed == "/setup?connect=connected&server=github"
    assert token == "github-access-1"
    assert harness.connect.states(tenant_id) == {"notion": "todo", "github": "connected"}
    [exchange] = github.calls("/token")
    # The fields GitHub's page lists, the grant type, and nothing else.
    assert set(exchange["body"]) == {"grant_type", "code", "code_verifier", "redirect_uri",
                                     "client_id", "client_secret"}
    assert exchange["body"]["client_id"] == CLIENT_ID
    assert exchange["body"]["client_secret"] == CLIENT_SECRET
    assert exchange["body"]["redirect_uri"] == REDIRECT_URI
    # GitHub answers a form unless it is asked for JSON.
    assert exchange["headers"]["accept"] == "application/json"
    for secret in (b"github-access-1", b"github-refresh-1", CLIENT_SECRET.encode()):
        assert secret not in raw
    # The app's client is the file's, never a row: no registration is kept.
    assert harness.store.connect_client("github") is None
    assert 'data-connector="github" data-state="done"' in page
    assert "It reads the repositories the Waku app is installed on at GitHub." in page


def test_the_code_is_exchanged_with_the_verifier_of_its_own_start(harness, github):
    async def body():
        _, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        await _callback(harness, cookie, code=github.approve(start["query"]),
                        state=start["query"]["state"])
        return start["query"]["code_challenge"]

    challenge = _run(harness, body)
    [exchange] = github.calls("/token")
    assert pkce_challenge(exchange["body"]["code_verifier"]) == challenge


def test_a_callback_that_names_github_as_github_names_itself_is_taken(harness, github):
    """GitHub sends `iss=https://github.com/login/oauth` with the code. The
    first real sign-in, on 2026-11-19, was refused: the gateway expected the
    bare origin, and answered "GitHub did not finish the sign-in"."""
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        landed = await _callback(harness, cookie, code=github.approve(start["query"]),
                                 state=start["query"]["state"],
                                 iss="https://github.com/login/oauth")
        return tenant_id, landed

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=connected&server=github"
    assert harness.connect.states(tenant_id)["github"] == "connected"


@pytest.mark.parametrize("issuer", ["https://github.com", "https://example.com/login/oauth"])
def test_a_callback_that_names_another_sign_in_server_is_refused(harness, github, issuer):
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        start = await _start(harness, cookie)
        landed = await _callback(harness, cookie, code=github.approve(start["query"]),
                                 state=start["query"]["state"], iss=issuer)
        return tenant_id, landed

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=failed&server=github"
    assert harness.connect.states(tenant_id)["github"] == "todo"
    assert github.calls("/token") == []


def test_a_callback_without_our_state_stores_nothing(harness, github):
    """What GitHub sends when "Request user authorization (OAuth) during
    installation" is ticked on the app: a code, an installation id, and no
    state this gateway issued."""
    async def body():
        tenant_id, cookie, _ = await sign_in(harness)
        landed = await _callback(harness, cookie, code="code-from-an-install",
                                 installation_id="42", setup_action="install")
        return tenant_id, landed

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=expired&server="
    assert harness.connect.states(tenant_id)["github"] == "todo"
    assert github.calls("/token") == []


def test_a_wrong_client_secret_fails_the_sign_in_and_stores_nothing(harness, github):
    async def body():
        github.token_answer = {"error": "incorrect_client_credentials"}
        tenant_id, _, landed = await _connect(harness, github)
        return tenant_id, landed

    tenant_id, landed = _run(harness, body)
    assert landed == "/setup?connect=failed&server=github"
    assert harness.connect.states(tenant_id)["github"] == "todo"


def test_no_log_line_holds_the_client_secret_or_a_token(harness, github, caplog):
    async def body():
        tenant_id, _, _ = await _connect(harness, github)
        harness.clock.t += 28800
        await harness.connect.access_token(tenant_id, "github")

    with caplog.at_level(logging.DEBUG):
        _run(harness, body)
    logged = "\n".join(record.getMessage() for record in caplog.records
                       if record.name.startswith("hosted"))
    assert "connected github" in logged
    for secret in (CLIENT_SECRET, "github-access-1", "github-access-2", "github-refresh-1"):
        assert secret not in logged


# --- GitHub's tokens end after eight hours ----------------------------------------

def test_a_github_token_is_renewed_in_its_last_minute_and_the_new_pair_is_kept(
        harness, github):
    async def body():
        tenant_id, _, _ = await _connect(harness, github)
        harness.clock.t += 28800 - 61
        early = await harness.connect.access_token(tenant_id, "github")
        harness.clock.t += 31
        # Three calls find it ending at once. GitHub ends a refresh token the
        # moment it is used, so one renewal must serve all three.
        together = await asyncio.gather(*[
            harness.connect.access_token(tenant_id, "github") for _ in range(3)])
        harness.clock.t += 28800
        return early, together, await harness.connect.access_token(tenant_id, "github")

    early, together, later = _run(harness, body)
    assert early == "github-access-1"
    assert together == ["github-access-2"] * 3
    assert later == "github-access-3"
    renewals = [call["body"] for call in github.calls("/token")
                if call["body"]["grant_type"] == "refresh_token"]
    assert [sent["refresh_token"] for sent in renewals] == [
        "github-refresh-1", "github-refresh-2"]
    # The four fields GitHub's page lists for a refresh.
    assert set(renewals[0]) == {"grant_type", "refresh_token", "client_id", "client_secret"}


def test_a_refresh_token_github_ended_asks_the_person_to_connect_again(harness, github):
    async def body():
        tenant_id, cookie, _ = await _connect(harness, github)
        github.token_answer = {"error": "bad_refresh_token"}
        harness.clock.t += 28800
        with pytest.raises(ReconnectNeeded):
            await harness.connect.access_token(tenant_id, "github")
        return tenant_id, await _page(harness, cookie)

    tenant_id, page = _run(harness, body)
    assert harness.connect.states(tenant_id)["github"] == "reconnect"
    assert "Reconnect GitHub" in page


def test_a_wrong_client_secret_at_renewal_does_not_end_anyones_sign_in(harness, github):
    """The file is wrong, not the person's sign-in: correcting the file is
    the repair, so nobody is asked to connect again."""
    async def body():
        tenant_id, _, _ = await _connect(harness, github)
        github.token_answer = {"error": "incorrect_client_credentials"}
        harness.clock.t += 28800
        with pytest.raises(VendorUnreachable):
            await harness.connect.access_token(tenant_id, "github")
        return tenant_id

    tenant_id = _run(harness, body)
    assert harness.connect.states(tenant_id)["github"] == "connected"


# --- the agent's calls: reads run, writes are refused here ------------------------

def test_a_read_reaches_github_with_that_persons_token(harness, github):
    async def body():
        ana, _, _ = await _connect(harness, github)
        bob, _, _ = await sign_in(harness, sub="sub-bob", email="bob@example.com")
        relay = _relay(harness)
        return (await relay.call(ana, "github", _call("get_file_contents")),
                await relay.call(bob, "github", _call("get_file_contents")))

    ana, bob = _run(harness, body)
    assert ana["message"]["result"]["content"][0]["text"] == "github ran get_file_contents"
    [call] = github.mcp_calls("tools/call")
    assert call["authorization"] == "Bearer github-access-1"
    assert call["body"]["params"]["name"] == "get_file_contents"
    # Acceptance 4: a second tenant has no sign-in of the first to use.
    assert bob["message"]["error"]["message"] == (
        f"GitHub is not connected. Connect it at https://{APEX}/setup.")


def test_no_github_tool_that_writes_reaches_github(harness, github):
    async def body():
        tenant_id, _, _ = await _connect(harness, github)
        relay = _relay(harness)
        await relay.call(tenant_id, "github", _call("issue_read"))
        return [await relay.call(tenant_id, "github", _call(tool, number))
                for number, tool in enumerate(GITHUB_WRITES, start=10)]

    answers = _run(harness, body)
    assert len(answers) == len(GITHUB_WRITES) == 39
    for tool, answer in zip(GITHUB_WRITES, answers, strict=True):
        result = answer["message"]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"].startswith(
            f"Not run: '{tool}' changes something in GitHub")
    # GitHub saw the one read and none of the 39 writes.
    assert [call["body"]["params"]["name"]
            for call in github.mcp_calls("tools/call")] == ["issue_read"]


def test_a_token_github_refuses_mid_call_is_renewed_and_the_read_runs(harness, github):
    async def body():
        tenant_id, _, _ = await _connect(harness, github)
        relay = _relay(harness)
        await relay.call(tenant_id, "github", _call("list_pull_requests"))
        github.access_tokens.clear()        # GitHub ended it before its eight hours
        return await relay.call(tenant_id, "github", _call("list_pull_requests", 8))

    answer = _run(harness, body)
    assert answer["message"]["result"]["content"][0]["text"] == (
        "github ran list_pull_requests")
    assert github.mcp_calls("tools/call")[-1]["authorization"] == "Bearer github-access-2"
