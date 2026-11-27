"""A sign-in to a remote MCP server lasts past its first hour, in every process.

On 2026-11-25 Waku Memory was signed in fresh and, a few hours later, every
new connection said "needs a sign-in". Its access token lasts 3600 seconds and
comes with a refresh token, so Waku should have refreshed it silently. It
never tried: a process that loaded the token from disk did not know when the
token was issued, treated it as valid forever, sent it expired, and answered
the server's 401 with a full sign-in, which a turn refuses (spec 038). The
token file was never written again after the sign-in.

Waku Memory's authorization server also rotates the refresh token on every
refresh and refuses the old one, and several Waku processes share one token
file. These cases run every exchange against an offline fake of that server.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import pytest

pytest.importorskip("mcp", reason="the MCP connector is an optional extra")

import httpx2  # noqa: E402  (the SDK's own HTTP client, installed with mcp)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken  # noqa: E402

from waku.tools.mcp_oauth import FileTokenStorage, SignInNeeded, build_provider  # noqa: E402

URL = "https://memory.example.test/mcp"
HOUR = 3600


class FakeAuthServer:
    """The resource and its authorization server, with refresh-token rotation.

    Every refresh issues a new access token and a new refresh token, and the
    old refresh token stops working. Presenting a spent refresh token revokes
    the whole chain, the way Supabase Auth treats reuse as theft.
    """

    def __init__(self) -> None:
        self.n = 0
        self.live_access: set[str] = set()
        self.live_refresh: set[str] = set()
        self.spent_refresh: set[str] = set()
        self.refreshes: list[str] = []
        self.refused: list[str] = []
        self.on_refresh = None  # a hook a case runs before the server answers

    def sign_in(self) -> OAuthToken:
        self.n += 1
        access, refresh = f"at{self.n}", f"rt{self.n}"
        self.live_access.add(access)
        self.live_refresh.add(refresh)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=HOUR)

    def expire_access_tokens(self) -> None:
        self.live_access.clear()

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
            assert form["grant_type"] == "refresh_token", form
            presented = form["refresh_token"]
            self.refreshes.append(presented)
            if self.on_refresh is not None:
                await self.on_refresh(presented)
            if presented not in self.live_refresh:
                self.refused.append(presented)
                if presented in self.spent_refresh:
                    # Reuse of a spent token: revoke everything it led to.
                    self.live_refresh.clear()
                    self.live_access.clear()
                return httpx2.Response(400, json={"error": "invalid_grant"}, request=request)
            self.live_refresh.discard(presented)
            self.spent_refresh.add(presented)
            token = self.sign_in()
            return httpx2.Response(200, json=token.model_dump(exclude_none=True), request=request)
        if path == "/mcp":
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if bearer in self.live_access:
                return httpx2.Response(200, json={"ok": True}, request=request)
            return httpx2.Response(401, request=request)
        # Discovery documents: this fake publishes none.
        return httpx2.Response(404, request=request)


async def connect(provider, server: FakeAuthServer) -> int:
    """Send one request through the provider's auth flow, the way httpx does."""
    request = httpx2.Request("POST", URL)
    flow = provider.async_auth_flow(request)
    outgoing = await flow.__anext__()
    response = None
    try:
        while True:
            response = await server.handle(outgoing)
            outgoing = await flow.asend(response)
    except StopAsyncIteration:
        return response.status_code


def signed_in_home(tmp_path, server: FakeAuthServer) -> FileTokenStorage:
    """A home where `waku mcp login` has finished: a registration and a token."""
    storage = FileTokenStorage(tmp_path, "waku_memory", URL)
    client = OAuthClientInformationFull(client_id="waku-client",
                                        redirect_uris=["http://127.0.0.1:41765/callback"])
    asyncio.run(storage.set_client_info(client))
    asyncio.run(storage.set_tokens(server.sign_in()))
    return storage


def a_turn(tmp_path, needed: list):
    """The provider a chat turn builds: it may refresh, never sign in."""
    return build_provider(URL, "waku_memory", tmp_path, on_sign_in_needed=needed.append)


def stored_tokens(storage: FileTokenStorage) -> dict:
    return json.loads(storage._path.read_text(encoding="utf-8"))["tokens"]


def later(monkeypatch, seconds: float) -> None:
    """Move every clock the provider and the storage read forward."""
    now = time.time() + seconds
    monkeypatch.setattr(time, "time", lambda: now)


def test_a_new_process_refreshes_a_token_signed_in_an_hour_ago(tmp_path, monkeypatch):
    """The bug itself: a turn two hours after the sign-in refreshes silently."""
    server = FakeAuthServer()
    storage = signed_in_home(tmp_path, server)
    later(monkeypatch, 2 * HOUR)
    server.expire_access_tokens()

    needed: list = []
    status = asyncio.run(connect(a_turn(tmp_path, needed), server))

    assert status == 200
    assert needed == []
    assert server.refreshes == ["rt1"]
    assert stored_tokens(storage)["refresh_token"] == "rt2"


def test_a_token_file_from_before_issue_times_were_kept_refreshes(tmp_path):
    """A file written by an older Waku records no issue time, so its expiry is
    unknown. The provider refreshes it once rather than send it blind."""
    server = FakeAuthServer()
    storage = signed_in_home(tmp_path, server)
    data = json.loads(storage._path.read_text(encoding="utf-8"))
    data.pop("expires_at", None)
    storage._path.write_text(json.dumps(data), encoding="utf-8")
    server.expire_access_tokens()

    needed: list = []
    status = asyncio.run(connect(a_turn(tmp_path, needed), server))

    assert (status, needed, server.refreshes) == (200, [], ["rt1"])
    assert "expires_at" in json.loads(storage._path.read_text(encoding="utf-8"))


def test_a_fresh_token_is_used_as_it_is(tmp_path):
    server = FakeAuthServer()
    signed_in_home(tmp_path, server)
    needed: list = []
    assert asyncio.run(connect(a_turn(tmp_path, needed), server)) == 200
    assert server.refreshes == []


def test_a_second_process_uses_the_tokens_the_first_one_refreshed(tmp_path, monkeypatch):
    """Two dashboards loaded the same token. The first refreshes it; the
    second reads the file again before refreshing and finds the new token,
    rather than spend the refresh token the server has already rotated."""
    server = FakeAuthServer()
    signed_in_home(tmp_path, server)
    needed: list = []
    first, second = a_turn(tmp_path, needed), a_turn(tmp_path, needed)

    async def both_connected_then_an_hour_passes():
        assert await connect(first, server) == 200
        assert await connect(second, server) == 200
        later(monkeypatch, 2 * HOUR)
        server.expire_access_tokens()
        return await connect(first, server), await connect(second, server)

    assert asyncio.run(both_connected_then_an_hour_passes()) == (200, 200)
    assert needed == []
    assert server.refreshes == ["rt1"]
    assert server.refused == []


def test_two_processes_refreshing_at_once_spend_the_token_once(tmp_path, monkeypatch):
    """The cross-process lock: the second waits while the first refreshes,
    then takes the first one's result. Two refreshes with one token would make
    the server revoke the sign-in for both."""
    server = FakeAuthServer()
    signed_in_home(tmp_path, server)
    later(monkeypatch, 2 * HOUR)
    server.expire_access_tokens()

    async def slow(_presented):
        await asyncio.sleep(0.3)

    server.on_refresh = slow
    needed: list = []

    async def at_once():
        return await asyncio.gather(connect(a_turn(tmp_path, needed), server),
                                    connect(a_turn(tmp_path, needed), server))

    assert asyncio.run(at_once()) == [200, 200]
    assert needed == []
    assert server.refreshes == ["rt1"]


def test_a_refused_refresh_takes_tokens_another_process_wrote(tmp_path, monkeypatch):
    """An older Waku on the same home, which takes no lock, refreshes the
    token while this process's refresh is in flight. The server refuses this
    one; the provider reads the file again and uses the newer token rather
    than report a sign-in."""
    server = FakeAuthServer()
    storage = signed_in_home(tmp_path, server)
    later(monkeypatch, 2 * HOUR)
    server.expire_access_tokens()

    async def another_process_wins(presented):
        server.on_refresh = None
        server.live_refresh.discard(presented)
        await storage.set_tokens(server.sign_in())

    server.on_refresh = another_process_wins
    needed: list = []
    status = asyncio.run(connect(a_turn(tmp_path, needed), server))

    assert (status, needed) == (200, [])
    assert stored_tokens(storage)["refresh_token"] == "rt2"


def test_a_refresh_the_server_refuses_still_never_opens_a_browser(tmp_path, monkeypatch):
    """Spec 038 holds: a sign-in that truly lapsed is reported, not opened."""
    server = FakeAuthServer()
    signed_in_home(tmp_path, server)
    later(monkeypatch, 2 * HOUR)
    server.expire_access_tokens()
    server.live_refresh.clear()

    needed: list = []
    with pytest.raises(SignInNeeded):
        asyncio.run(connect(a_turn(tmp_path, needed), server))
    assert needed == ["waku_memory"]


def test_refreshed_tokens_are_written_private(tmp_path, monkeypatch):
    server = FakeAuthServer()
    storage = signed_in_home(tmp_path, server)
    later(monkeypatch, 2 * HOUR)
    server.expire_access_tokens()
    asyncio.run(connect(a_turn(tmp_path, []), server))
    assert storage._path.stat().st_mode & 0o777 == 0o600
    assert not storage._path.with_suffix(".tmp").exists()
