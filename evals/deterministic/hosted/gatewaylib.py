"""What every group E test needs, in one place.

THE OFFLINE TIER'S spawnerlib.py. Group C put its shared Docker constants and
helpers in evals/hosted_docker/spawnerlib.py rather than importing them out of
a test module, because importing a fixture into a module that also names it as
a test argument is ruff F811 -- seventeen times over, in group C's case. The
same rule applies here: FakeSpawner is needed by test_launcher.py,
test_auth.py, test_gateway.py and test_admin.py, so it lives here and is
imported as a module.

E2 writes this half. E1 step 11 appends the HTTP harness, the signing key and
the wire helpers; E3 step 9 appends the fake container and Harness's real
forwarder. Nothing is defined twice and nothing is moved after it is written.

THE FIXTURES ARE IN conftest.py, NOT HERE, and `wired` is no exception. A
fixture is only collected from a test module or a conftest: imported into a
test module it is also an unused name to ruff, and `pytest_plugins` in a
non-root conftest is an error on pytest 9. So `harness`, `wired` and
`wired_one_slot` all sit beside each other in conftest.py, and this module
owns the classes they build.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec

from hosted.core import idle, quota
from hosted.core.tenant import address_for_project
from hosted.gateway.app import Gateway
from hosted.gateway.config import GatewayConfig
from hosted.gateway.forward import ContainerForwarder
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
    `list` always answers the derived address -- see `start`.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.running: dict[str, RunningContainer] = {}
        self.fail_start: Exception | None = None
        self.fail_provision: Exception | None = None
        # A stop the spawner refuses. The container survives, and its token
        # has already been revoked -- the drift resync exists to repair.
        self.fail_stop: Exception | None = None
        # EVERY CALL YIELDS, EVEN AT ZERO. SpawnerClient.start and .stop are
        # unix socket round trips, so the real ones always suspend; a fake
        # that returns without yielding cannot show an interleaving the real
        # one has, and the running cap was broken for exactly that window.
        self.start_delay: float = 0.0
        self.stop_delay: float = 0.0
        self.address: str | None = None
        self.port: int = 7777
        # E3 step 14 drives the refused-connection ladder by making the FIRST
        # start answer a port nothing listens on. A list of ports, consumed in
        # order, with the last one repeating.
        self.ports: list[int] = []
        # Tenant ids the spawner has containers for but `list` would never
        # report: see tenant_ids below.
        self.labelled_only: list[str] = []

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
        await asyncio.sleep(self.start_delay)
        if self.fail_start is not None:
            raise self.fail_start
        container = RunningContainer(
            tenant_id=tenant_id,
            address=self.address or address_for_project(project_id),
            port=self._port())
        # WHAT `list` WILL REPORT IS THE ADDRESS THE PROJECT ID DERIVES, even
        # when `address` overrode what this call ANSWERS. That is not a
        # discrepancy for its own sake: the real spawner puts every container
        # on 10.88.0.0/16 and reports it there, and Launcher.resync refuses to
        # adopt any other address -- while a pytest process can open a socket
        # to loopback and to nothing else. So `address` moves the address E3's
        # forwarder CONNECTS to, and leaves the address the fleet is told
        # about where the real one would be. With `address` unset the two are
        # the same object's worth of values and nothing changes.
        self.running[tenant_id] = RunningContainer(
            tenant_id=tenant_id, address=address_for_project(project_id),
            port=container.port)
        return container

    async def stop(self, tenant_id: str) -> None:
        self.requests.append({"op": "stop", "tenant_id": tenant_id})
        await asyncio.sleep(self.stop_delay)
        if self.fail_stop is not None:
            # The container is NOT removed from `running`: that is the whole
            # point of a stop that failed.
            raise self.fail_stop
        self.running.pop(tenant_id, None)

    async def list(self) -> list[RunningContainer]:
        self.requests.append({"op": "list"})
        return list(self.running.values())

    async def tenant_ids(self) -> list[str]:
        """Every labelled tenant container, UNFILTERED -- the real spawner's
        `tenants` op applies the kind label and the id shape and nothing else.

        `labelled_only` is how a test says "the spawner has a container the
        gateway would refuse to forward to": a tenant at an address its
        project id does not derive, or one control.db has lost. Those never
        appear in `list`, which is the whole reason this operation exists.
        """
        self.requests.append({"op": "tenants"})
        return sorted(set(self.running) | set(self.labelled_only))

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
                   body: bytes | None = None,
                   headers: dict | list | tuple | None = None,
                   cookie: str | None = None) -> tuple[int, dict, bytes]:
    """One request, written to the socket by hand.

    By hand, and not through an HTTP client, for the same reason the cookies
    are by hand: a client normalises the request target, and //api/x,
    /api%2fx and /api/../x are three of the things under test.

    Both tiers use this. evals/hosted_docker/test_gateway_container.py imports
    it from here rather than carrying a second copy.
    """
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}", "Connection: close"]
    # A dict OR a sequence of pairs. The pairs are how a test sends the SAME
    # header twice -- `Sec-Fetch-Site` in front of `Sec-Fetch-Site` is a real
    # shape on the wire and a dict cannot express it.
    pairs = headers.items() if isinstance(headers, dict) else (headers or ())
    for name, value in pairs:
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
        self._real_forwarding = False
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

    def use_real_forwarding(self, container: FakeContainer) -> None:
        """Point the harness at a fake container and swap the recording
        forwarder for the real one.

        MUST be called before start(). A test that calls it after has a
        running app built from the old Gateway, so every assertion under it
        would be about the recording forwarder -- and would pass. That is the
        shape of a test that cannot fail, so it is an assertion, not a note.

        THE SESSION IS BUILT IN start(), NOT HERE. aiohttp.ClientSession needs
        a running event loop at construction ("RuntimeError: no running event
        loop" on 3.14), and this is called from a synchronous pytest fixture.
        So this records the intent and start(), which is async, does the
        wiring -- which is also why `_wire_real_forwarding` is not a second
        place that decides anything.
        """
        assert self._runner is None, "call use_real_forwarding before start()"
        self.container = container
        self.spawner.address = "127.0.0.1"
        self.spawner.port = container.port
        self._real_forwarding = True

    def _wire_real_forwarding(self) -> None:
        """Inside the loop: the session, the forwarder, the Gateway."""
        self.session = aiohttp.ClientSession(auto_decompress=False)
        self.forwarder = ContainerForwarder(
            launcher=self.launcher, turns=self.turns, plans=self.plans,
            proxy_socket=self.config.proxy_socket, session=self.session,
            now=self.clock)
        self.gateway = self._build_gateway()

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
        if self._real_forwarding:
            self._wire_real_forwarding()
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
                   body: bytes | None = None,
                   headers: dict | list | tuple | None = None,
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


async def sign_in(harness: Harness, **claims) -> tuple[str, str, str]:
    """(tenant id, apex cookie, hand-off code) from a real POST
    /auth/session. Three test modules call it, so it lives here.

    `claims` overrides what the signed token carries, which is how a test
    signs in a SECOND person: a different `sub` is a different tenant. E3's
    cap test needs three, and three copies of this function is how they would
    otherwise arrive.
    """
    token = sign(harness.private, now=harness.clock.t, **claims)
    status, headers, body = await harness.json_post(
        "/auth/session", {"access_token": token, "timezone": "Asia/Shanghai"},
        host=APEX)
    assert status == 200, body
    enter = json.loads(body)["enter"]
    tenant_id = enter.split("//", 1)[1].split(".", 1)[0]
    return (tenant_id, cookie_value(headers, "__Host-waku_session"),
            enter.split("code=", 1)[1])


async def signed_in_on_the_tenant_host(harness: Harness,
                                       **claims) -> tuple[str, str]:
    """(tenant host, tenant-host cookie header) after a real sign-in and a
    real hand-off. Here rather than in test_gateway.py because test_admin.py
    needs it too, and importing a helper across test modules is how the F811
    that created spawnerlib.py started."""
    tenant_id, _apex, code = await sign_in(harness, **claims)
    host = f"{tenant_id}.{APEX}"
    _s, headers, _b = await harness.send("GET", f"/auth/enter?code={code}", host=host)
    return host, f"__Host-waku_tenant={cookie_value(headers, '__Host-waku_tenant')}"


class FakeContainer:
    """A stock-shaped dashboard: HTTP/1.0, SSE by connection close, and a
    Set-Cookie on every answer.

    NOT an aiohttp server. waku/ops/dashboard.py is a BaseHTTPRequestHandler
    with no protocol_version, so every real container answers HTTP/1.0 and
    delimits a stream by closing the socket. An aiohttp stand-in would answer
    HTTP/1.1 with chunked encoding, and the pass-through would be tested
    against a wire shape no tenant container has.

    It records the exact request target it was given, which is what
    acceptance 22 is about, and it sets a cookie on every response, which is
    what acceptance 14 is about.
    """

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.bodies: list[bytes] = []
        self.headers: list[dict] = []
        self.delay = 0.0
        # Seconds between SSE frames, and an Event set after the last one.
        # Together they are how a test can say "the browser had frame one
        # before the container had written frame three", which is the whole
        # of what "unbuffered" means and is not a timing assertion.
        self.frame_delay = 0.0
        self.finished = threading.Event()
        # Extra bytes to CLAIM in Content-Length and then not send, before
        # hanging up. A container killed for memory halfway through a
        # response looks like this: the headers arrived, the body did not.
        self.truncate = 0
        # The Location the container tries to send. Off-origin by default,
        # because that is the one acceptance 14 is about; the tests that drive
        # forward._is_same_origin_location set it case by case.
        self.location = "https://evil.example/"
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self) -> None:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                """Silence BaseHTTPRequestHandler's stderr log line."""

            def _record(self) -> bytes:
                recorder.targets.append(self.path)
                recorder.headers.append(dict(self.headers))
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                recorder.bodies.append(body)
                return body

            def do_GET(self):
                self._record()
                if recorder.delay:
                    time.sleep(recorder.delay)
                if self.path.startswith("/api/chat/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Set-Cookie", "evil=1")
                    self.end_headers()
                    for index in range(3):
                        if index and recorder.frame_delay:
                            time.sleep(recorder.frame_delay)
                        self.wfile.write(f"data: {{\"n\": {index}}}\n\n".encode())
                        self.wfile.flush()
                    recorder.finished.set()
                    return
                payload = json.dumps({"path": self.path}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length",
                                 str(len(payload) + recorder.truncate))
                self.send_header("Set-Cookie", "evil=1")
                self.send_header("Clear-Site-Data", '"cookies"')
                self.send_header("Location", recorder.location)
                self.send_header("X-Secret", "leaked")
                self.end_headers()
                self.wfile.write(payload)

            do_POST = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class AcceptsThenCloses:
    """A socket that accepts a connection and closes it without answering.

    A CONTAINER BEING STOPPED LOOKS EXACTLY LIKE THIS from the gateway's side,
    and it is not the same as a refused connection: the connect succeeded, so
    aiohttp raises ServerDisconnectedError rather than ClientConnectorError,
    and the retry ladder must NOT run -- the request may already have been
    delivered.
    """

    def __init__(self) -> None:
        self._server: socket.socket | None = None
        self.port = 0

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]

        def serve() -> None:
            while True:
                try:
                    connection, _ = self._server.accept()
                except OSError:
                    return
                connection.close()

        threading.Thread(target=serve, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
