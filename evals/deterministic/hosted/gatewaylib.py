"""What every group E test needs, in one place.

THE OFFLINE TIER'S spawnerlib.py. Group C put its shared Docker constants and
helpers in evals/hosted_docker/spawnerlib.py rather than importing them out of
a test module, because importing a fixture into a module that also names it as
a test argument is ruff F811 -- seventeen times over, in group C's case. The
same rule applies here: FakeSpawner is needed by test_launcher.py,
test_auth.py, test_gateway.py and test_admin.py, so it lives here and is
imported as a module.

E2 writes this half. E1 step 11 appends the HTTP harness, the signing key and
the wire helpers; E3 step 9 appends the fake container. Nothing is defined
twice and nothing is moved after it is written.
"""

from __future__ import annotations

import asyncio
import json
import time
from http.cookies import SimpleCookie

import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec

from hosted.core import idle, quota
from hosted.core.tenant import address_for_project
from hosted.gateway.app import Gateway
from hosted.gateway.config import GatewayConfig
from hosted.gateway.identity import JwksVerifier
from hosted.gateway.launch import Launcher
from hosted.gateway.store import ControlDb
from hosted.ports.runtime import RunningContainer


class Clock:
    """A clock a test moves by hand. Every Fleet, TurnWindow, SessionCache,
    HandoffCodes and Launcher in this group takes one, so no test in the group
    ever sleeps to make time pass."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class FakeSpawner:
    """Records every request and answers from a script.

    `address` and `port` exist so E3 can point a start at a real fake
    container on loopback; left None, a start answers the fixed address the
    tenant's project id derives, which is what the real spawner answers.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.running: dict[str, RunningContainer] = {}
        self.fail_start: Exception | None = None
        self.fail_provision: Exception | None = None
        self.start_delay: float = 0.0
        self.address: str | None = None
        self.port: int = 7777
        # E3 step 14 drives the refused-connection ladder by making the FIRST
        # start answer a port nothing listens on. A list of ports, consumed in
        # order, with the last one repeating.
        self.ports: list[int] = []

    def _port(self) -> int:
        if not self.ports:
            return self.port
        return self.ports.pop(0) if len(self.ports) > 1 else self.ports[0]

    async def provision(self, tenant_id: str, project_id: int) -> None:
        self.requests.append({"op": "provision", "tenant_id": tenant_id,
                              "project_id": project_id})
        if self.fail_provision is not None:
            raise self.fail_provision

    async def start(self, tenant_id: str, project_id: int, timezone: str,
                    token: str) -> RunningContainer:
        self.requests.append({"op": "start", "tenant_id": tenant_id,
                              "project_id": project_id, "timezone": timezone,
                              "token": token})
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.fail_start is not None:
            raise self.fail_start
        container = RunningContainer(
            tenant_id=tenant_id,
            address=self.address or address_for_project(project_id),
            port=self._port())
        self.running[tenant_id] = container
        return container

    async def stop(self, tenant_id: str) -> None:
        self.requests.append({"op": "stop", "tenant_id": tenant_id})
        self.running.pop(tenant_id, None)

    async def list(self) -> list[RunningContainer]:
        self.requests.append({"op": "list"})
        return list(self.running.values())

    async def task(self, tenant_id: str, task: str, project_id: int = 0) -> dict:
        self.requests.append({"op": "task", "tenant_id": tenant_id,
                              "task": task, "project_id": project_id})
        return {"ok": True}


def ops(spawner: FakeSpawner, op: str) -> list[dict]:
    return [request for request in spawner.requests if request["op"] == op]


APEX = "agent.waku.one"
SUPABASE_URL = "https://upmikpuftlvvpwkvouqr.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
# The live project's audience. Not "authenticated" -- see
# hosted/gateway/identity.py's docstring.
AUDIENCE = "https://api.waku.one/mcp"
KID = "test-kid"


def signing_key() -> tuple[ec.EllipticCurvePrivateKey, dict]:
    """A P-256 key and the JWKS document that publishes it, in the shape the
    live project's own document has: one ES256 EC key with a kid."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    public.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return private, {"keys": [public]}


def sign(private, *, now: float | None = None, kid: str = KID,
         algorithm: str = "ES256", **claims) -> str:
    """A token with the live project's claim shape, overridable claim by
    claim. `role` is `authenticated` and `aud` is the custom audience,
    because that is what a real token from this project carries."""
    at = time.time() if now is None else now
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "sub-mei",
            "email": "mei@example.com", "role": "authenticated",
            "is_anonymous": False, "iat": int(at), "exp": int(at) + 600}
    body.update(claims)
    return jwt.encode(body, private, algorithm=algorithm, headers={"kid": kid})


def parse_response(head: bytes, rest: bytes) -> tuple[int, dict, bytes]:
    """A raw HTTP response as (status, headers, body).

    `headers` is flattened to one value per name, with every value kept under
    the "__all__" key -- a response carries more than one Set-Cookie and the
    flattened view would hide all but the first.
    """
    text = head.decode("latin-1")
    start, _, raw_headers = text.partition("\r\n")
    status = int(start.split(" ")[1])
    headers: dict[str, list[str]] = {}
    for line in raw_headers.split("\r\n"):
        if not line:
            continue
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    flat: dict = {name: values[0] for name, values in headers.items()}
    flat["__all__"] = headers
    if flat.get("transfer-encoding") == "chunked":
        rest = _dechunk(rest)
    return status, flat, rest


def _dechunk(raw: bytes) -> bytes:
    out = b""
    while True:
        line, _, raw = raw.partition(b"\r\n")
        size = int(line.split(b";")[0] or b"0", 16)
        if size == 0:
            return out
        out += raw[:size]
        raw = raw[size + 2:]


async def send_raw(port: int, method: str, target: str, host: str, *,
                   body: bytes | None = None, headers: dict | None = None,
                   cookie: str | None = None) -> tuple[int, dict, bytes]:
    """One request, written to the socket by hand.

    By hand, and not through an HTTP client, for the same reason the cookies
    are by hand: a client normalises the request target, and //api/x,
    /api%2fx and /api/../x are three of the things under test.

    Both tiers use this. evals/hosted_docker/test_gateway_container.py imports
    it from here rather than carrying a second copy.
    """
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}", "Connection: close"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if cookie:
        lines.append(f"Cookie: {cookie}")
    payload = body or b""
    if body is not None:
        lines.append(f"Content-Length: {len(payload)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + payload
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    rest = await reader.read()
    writer.close()
    return parse_response(head, rest)


def cookie_value(headers: dict, name: str) -> str:
    """The value of one Set-Cookie, or "" when it was not sent."""
    for line in headers["__all__"].get("set-cookie", []):
        jar = SimpleCookie()
        jar.load(line)
        if name in jar:
            return jar[name].value
    return ""


def cookie_attributes(headers: dict, name: str) -> set[str]:
    """Every attribute on one Set-Cookie, lowercased, as a set.

    A set and not a substring search: `"Path=/" in line` and
    `"Domain=" not in line` are both aiohttp set_cookie DEFAULTS (path='/',
    domain=None), so neither could ever fail whatever the code did. The set is
    compared whole, so removing `secure=True` OR adding a domain OR dropping
    the path fails it.
    """
    for line in headers["__all__"].get("set-cookie", []):
        if line.startswith(name + "="):
            return {part.strip().lower() for part in line.split(";")[1:]}
    return set()


class RecordingForwarder:
    """The one thing E1 does not implement. It records what it was handed and
    answers a body a test can recognise, so "the request reached forwarding"
    is an observation rather than an inference."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, request: web.Request, tenant) -> web.StreamResponse:
        self.calls.append((tenant.id, request.raw_path))
        return web.Response(text="forwarded", content_type="text/plain")


class Harness:
    """A running gateway, its store, its fake spawner and its forwarder."""

    def __init__(self, tmp_path, spawner: FakeSpawner, *,
                 clock: Clock | None = None, max_running: int = 4) -> None:
        self.clock = clock or Clock()
        self.private, self.jwks = signing_key()
        self.store = ControlDb(tmp_path / "control.db", now=self.clock)
        self.spawner = spawner
        self.fleet = idle.Fleet(self.clock, max_running=max_running)
        self.launcher = Launcher(store=self.store, spawner=spawner,
                                 fleet=self.fleet, now=self.clock)
        self.turns = quota.TurnWindow(self.clock)
        self.plans = quota.DEFAULT_PLANS
        self.verifier = JwksVerifier(
            jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE,
            fetch=lambda _url: self.jwks, now=self.clock)
        self.forwarder: object = RecordingForwarder()
        self.session = None
        self.container = None
        self.config = GatewayConfig(
            apex_host=APEX, bind_host="127.0.0.1", port=0,
            control_db=tmp_path / "control.db",
            spawner_socket=tmp_path / "spawner.sock",
            gateway_socket=tmp_path / "gateway.sock",
            proxy_socket=tmp_path / "proxy.sock",
            admin_socket=tmp_path / "admin.sock",
            max_running=max_running, supabase_url=SUPABASE_URL,
            supabase_issuer=ISSUER, supabase_jwks_url=JWKS_URL,
            supabase_audience=AUDIENCE,
            supabase_publishable_key="sb_publishable_test")
        self.gateway = self._build_gateway()
        self._runner: web.AppRunner | None = None
        self.port = 0

    def _build_gateway(self) -> Gateway:
        return Gateway(config=self.config, store=self.store,
                       launcher=self.launcher, verifier=self.verifier,
                       forward=self.forwarder, turns=self.turns,
                       plans=self.plans, now=self.clock)

    def use_forwarder(self, forward) -> None:
        """Swap the forwarder, before start().

        The Gateway takes its forwarder in the constructor and keeps it
        private, so this rebuilds the Gateway rather than reaching into one.
        A test that wants to see what the gateway does when forwarding raises
        -- acceptance 14 is about EVERY response, including the 500 aiohttp
        writes by itself -- needs a forwarder that raises.
        """
        self.forwarder = forward
        self.gateway = self._build_gateway()

    async def start(self) -> None:
        self._runner = web.AppRunner(self.gateway.build())
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self.session is not None:
            await self.session.close()
        self.store.close()

    async def send(self, method: str, target: str, *, host: str,
                   body: bytes | None = None, headers: dict | None = None,
                   cookie: str | None = None) -> tuple[int, dict, bytes]:
        return await send_raw(self.port, method, target, host, body=body,
                              headers=headers, cookie=cookie)

    async def json_post(self, target: str, payload: dict, *, host: str,
                        origin: str | None = None,
                        cookie: str | None = None) -> tuple[int, dict, bytes]:
        head = {"Content-Type": "application/json",
                "Origin": origin if origin is not None else f"https://{host}"}
        return await self.send("POST", target, host=host, cookie=cookie,
                               body=json.dumps(payload).encode("utf-8"),
                               headers=head)


async def sign_in(harness: Harness) -> tuple[str, str, str]:
    """(tenant id, apex cookie, hand-off code) from a real POST
    /auth/session. Three test modules call it, so it lives here."""
    token = sign(harness.private, now=harness.clock.t)
    status, headers, body = await harness.json_post(
        "/auth/session", {"access_token": token, "timezone": "Asia/Shanghai"},
        host=APEX)
    assert status == 200, body
    enter = json.loads(body)["enter"]
    tenant_id = enter.split("//", 1)[1].split(".", 1)[0]
    return (tenant_id, cookie_value(headers, "__Host-waku_session"),
            enter.split("code=", 1)[1])


async def signed_in_on_the_tenant_host(harness: Harness) -> tuple[str, str]:
    """(tenant host, tenant-host cookie header) after a real sign-in and a
    real hand-off. Here rather than in test_gateway.py because test_admin.py
    needs it too, and importing a helper across test modules is how the F811
    that created spawnerlib.py started."""
    tenant_id, _apex, code = await sign_in(harness)
    host = f"{tenant_id}.{APEX}"
    _s, headers, _b = await harness.send("GET", f"/auth/enter?code={code}", host=host)
    return host, f"__Host-waku_tenant={cookie_value(headers, '__Host-waku_tenant')}"
