"""The gateway in front of a real tenant container.

THE SPAWNER IS GROUP C'S FIXTURE, RUNNING AS ROOT IN A CONTAINER. The gateway
under test runs in THIS process, talking to that spawner over the bind-mounted
socket -- which is exactly the deployment's shape, where the gateway is
unprivileged and the spawner is not.

THE VERIFIER IS A STUB, ON PURPOSE. Every JWT rule is covered offline in
test_auth.py against a locally signed JWKS; what this tier is for is what
happens AFTER a sign-in, with a real container on the other side.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import dockerlib
import pytest
from aiohttp import ClientSession, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "deterministic" / "hosted"))

from gatewaylib import cookie_value, send_raw  # noqa: E402

from hosted.core import idle, quota  # noqa: E402
from hosted.gateway.app import Gateway  # noqa: E402
from hosted.gateway.config import GatewayConfig  # noqa: E402
from hosted.gateway.forward import ContainerForwarder  # noqa: E402
from hosted.gateway.launch import Launcher  # noqa: E402
from hosted.gateway.spawner_client import SpawnerClient  # noqa: E402
from hosted.gateway.store import ControlDb  # noqa: E402
from hosted.ports.identity import Identity  # noqa: E402
from hosted.spawner import template  # noqa: E402

APEX = "agent.waku.one"
OTHER_TENANT = "bbbbbbbbbbbb"
SUPABASE_URL = "https://upmikpuftlvvpwkvouqr.supabase.co"
# secrets.token_urlsafe(32), which is what hosted/core/tenant.new_proxy_token
# calls. 32 bytes base64url with no padding is 43 characters.
PROXY_TOKEN_LENGTH = 43


class OneUser:
    """A verifier that signs one person in."""

    def __init__(self, sub: str, email: str) -> None:
        self._identity = Identity(sub=sub, email=email, is_anonymous=False)

    def verify(self, access_token: str) -> Identity:
        return self._identity


@pytest.fixture
def deployment(spawner, tmp_path):
    """A gateway wired to group C's real spawner. `spawner` yields the
    bind-mounted socket path, so this process reaches the privileged service
    exactly as the deployed gateway does."""
    store = ControlDb(tmp_path / "control.db")
    config = GatewayConfig(
        apex_host=APEX, bind_host="127.0.0.1", port=0,
        control_db=tmp_path / "control.db", spawner_socket=spawner,
        gateway_socket=tmp_path / "gateway.sock",
        # Nothing listens here: group D is cut, read_spend answers None, and
        # every tenant is on free's turn limit.
        proxy_socket=tmp_path / "proxy.sock",
        admin_socket=tmp_path / "admin.sock", max_running=4,
        supabase_url=SUPABASE_URL,
        supabase_issuer=f"{SUPABASE_URL}/auth/v1",
        supabase_jwks_url=f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
        supabase_audience="https://api.waku.one/mcp",
        supabase_publishable_key="sb_publishable_test")
    yield store, config
    store.close()


async def _serve(store, config):
    """Start the gateway in this process. Returns everything a test has to
    close, plus the port it bound."""
    fleet = idle.Fleet(time.time, config.max_running)
    launcher = Launcher(store=store, spawner=SpawnerClient(config.spawner_socket),
                        fleet=fleet)
    turns = quota.TurnWindow(time.time)
    session = ClientSession(auto_decompress=False)
    forwarder = ContainerForwarder(launcher=launcher, turns=turns,
                                   plans=quota.DEFAULT_PLANS,
                                   proxy_socket=config.proxy_socket,
                                   session=session)
    gateway = Gateway(config=config, store=store, launcher=launcher,
                      verifier=OneUser("sub-a", "a@example.com"),
                      forward=forwarder, turns=turns,
                      plans=quota.DEFAULT_PLANS)
    runner = web.AppRunner(gateway.build())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return launcher, runner, session, runner.addresses[0][1]


async def _sign_in(port: int) -> tuple[str, str]:
    """(tenant host, tenant-host cookie header) after a real sign-in and a
    real hand-off."""
    status, _headers, body = await send_raw(
        port, "POST", "/auth/session", APEX,
        headers={"Content-Type": "application/json",
                 "Origin": f"https://{APEX}"},
        body=json.dumps({"access_token": "stub",
                         "timezone": "Asia/Shanghai"}).encode("utf-8"))
    assert status == 200, body
    enter = json.loads(body)["enter"]
    tenant_id = enter.split("//", 1)[1].split(".", 1)[0]
    code = enter.split("code=", 1)[1]
    host = f"{tenant_id}.{APEX}"
    _s, entered, _b = await send_raw(port, "GET", f"/auth/enter?code={code}", host)
    value = cookie_value(entered, "__Host-waku_tenant")
    assert value, "the hand-off set no tenant-host cookie"
    return host, f"__Host-waku_tenant={value}"


async def _wait_for_dashboard(port: int, host: str, cookie: str,
                              deadline: float) -> tuple[int, dict, bytes]:
    """The container has to boot a Python process and bind a port. The
    gateway's own start budget is 15 seconds and it starts the container on
    the first request; this polls the GATEWAY, not the container, so what it
    waits for is the thing under test answering."""
    while True:
        answer = await send_raw(port, "GET", "/api/data", host, cookie=cookie)
        if answer[0] == 200 or time.time() > deadline:
            return answer
        await asyncio.sleep(1.0)


def test_a_real_dashboard_answers_and_another_tenants_cookie_does_not(deployment):
    """The cookie half of acceptance 1 and the logs half of acceptance 2."""
    store, config = deployment

    async def run():
        _launcher, runner, session, port = await _serve(store, config)
        try:
            host, cookie = await _sign_in(port)
            data = await _wait_for_dashboard(port, host, cookie,
                                             time.time() + 90)
            elsewhere = await send_raw(port, "GET", "/api/data",
                                       f"{OTHER_TENANT}.{APEX}", cookie=cookie)
            unknown = await send_raw(port, "GET", "/api/data", "evil.example",
                                     cookie=cookie)
            return host.split(".", 1)[0], data, elsewhere, unknown
        finally:
            await runner.cleanup()
            await session.close()

    tenant_id, data, elsewhere, unknown = asyncio.run(run())

    # A stock waku dashboard, through the gateway's two allowlists.
    status, headers, body = data
    assert status == 200, body
    assert json.loads(body)["home"], "not a real /api/data payload"
    assert headers["__all__"].get("set-cookie", []) == []
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]

    # Acceptance 1, the cookie half: this tenant's cookie, another tenant's
    # host, and a host nobody serves.
    assert elsewhere[0] == 401
    assert unknown[0] == 421

    # Acceptance 2, the logs half. The token's VALUE, not its variable name:
    # `"WAKU_PLATFORM_TOKEN" not in logs` passes while the value is printed on
    # every line. The test has no copy of the token -- the launcher issued it
    # and control.db holds only a hash -- so it is read back out of the
    # container the launcher started.
    name = template.container_name(tenant_id, template.KIND_TENANT)
    environment = dockerlib.inspect(name)["Config"]["Env"]
    token = next(value.split("=", 1)[1] for value in environment
                 if value.startswith("WAKU_PLATFORM_TOKEN="))
    assert len(token) == PROXY_TOKEN_LENGTH, "not a proxy token; the template changed"
    assert token not in dockerlib.logs(name)
    labels = dockerlib.inspect(name)["Config"]["Labels"] or {}
    assert token not in json.dumps(labels)
