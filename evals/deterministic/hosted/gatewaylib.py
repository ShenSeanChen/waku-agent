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
import base64
import dataclasses
import gzip
import hashlib
import json
import socket
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec

from hosted.core import idle, quota
from hosted.core.connectors import VENDORS, Vendor
from hosted.core.tenant import address_for_project
from hosted.gateway.app import Gateway
from hosted.gateway.config import GatewayConfig, SlackSettings
from hosted.gateway.connect import Connect, VendorApi
from hosted.gateway.forward import ContainerForwarder
from hosted.gateway.identity import JwksVerifier
from hosted.gateway.launch import Launcher
from hosted.gateway.slack import SlackApi, SlackRoutes
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
                    token: str, memory_key: str = "") -> RunningContainer:
        request = {"op": "start", "tenant_id": tenant_id,
                   "project_id": project_id, "timezone": timezone, "token": token}
        if memory_key:
            request["memory_key"] = memory_key
        self.requests.append(request)
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
        self.memory_keys = None
        self.session = None
        self.container = None
        self._real_forwarding = False
        # Spec 026 C: set by use_slack. `woken` counts the scheduler ticks the
        # Slack routes asked for.
        self.slack: SlackRoutes | None = None
        self.slack_settings: SlackSettings | None = None
        self._fake_slack: FakeSlack | None = None
        self.woken = 0
        # Spec 029: set by use_connect.
        self.connect: Connect | None = None
        self._fake_vendors: dict[str, FakeVendor] = {}
        # What a wake also calls, when a test wires a real Scheduler's kick.
        self.on_wake = lambda: None
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
                       plans=self.plans, memory_keys=self.memory_keys,
                       slack=self.slack, connect=self.connect, now=self.clock)

    def use_connect(self, **fakes: FakeVendor) -> None:
        """Turn the connector routes on, with each named vendor's address
        moved to a fake on loopback: use_connect(notion=fake). Before
        start(), for use_real_forwarding's reason."""
        assert self._runner is None, "call use_connect before start()"
        self._fake_vendors = dict(fakes)

    def vendors(self) -> dict[str, Vendor]:
        """The real list, with each faked vendor's address on loopback."""
        return {name: dataclasses.replace(VENDORS[name], url=fake.url + "/mcp")
                for name, fake in self._fake_vendors.items()}

    def _wire_connect(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(auto_decompress=False)
        self.connect = Connect(
            key=bytes.fromhex(SLACK_TOKEN_KEY), apex_host=APEX, store=self.store,
            api=VendorApi(self.session), vendors=self.vendors(), now=self.clock)
        self.gateway = self._build_gateway()

    def use_slack(self, fake: FakeSlack, settings: SlackSettings | None = None) -> None:
        """Turn the Slack routes on, in front of a fake Slack. Before start(),
        for use_real_forwarding's reason: the client session needs the loop."""
        assert self._runner is None, "call use_slack before start()"
        self._fake_slack = fake
        self.slack_settings = settings or slack_settings()

    def _wire_slack(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(auto_decompress=False)

        def wake() -> None:
            self.woken += 1
            self.on_wake()

        self.slack = SlackRoutes(
            settings=self.slack_settings, apex_host=APEX, store=self.store,
            api=SlackApi(self.session, base_url=self._fake_slack.url),
            now=self.clock, wake=wake)
        self.gateway = self._build_gateway()

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
        if self._fake_slack is not None:
            self._wire_slack()
        if self._fake_vendors:
            self._wire_connect()
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

    def start(self, port: int = 0) -> None:
        """Listen on `port`, or on a free one. A test that has stopped the
        container passes its old port to bring it back where the spawner
        says it is: a dashboard that binds after its container started."""
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

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class AlwaysOnContainer:
    """The two routes a tenant container answers for the gateway (spec 026):
    POST /api/events and GET /api/alwayson/next. HTTP/1.0, like the stock
    dashboard.

    It records every event it was handed, and a test sets what it answers:
    `events_status` for a hand-over, `work` for the report (None answers 404,
    which is what an image older than spec 026 answers)."""

    def __init__(self) -> None:
        self.handed: list[list[dict]] = []
        self.asked = 0
        self.events_status = 200
        self.work: dict | None = {"next_timer_at": None, "events": 0, "outbox": 0,
                                  "running": 0}
        # Called inside the report, before it answers: how a test makes a
        # request arrive while the gateway is waiting for this answer.
        self.on_ask = lambda: None
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self, port: int = 0) -> None:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                """Silence BaseHTTPRequestHandler's stderr log line."""

            def _answer(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != "/api/alwayson/next":
                    self._answer(404, {})
                    return
                recorder.asked += 1
                recorder.on_ask()
                if recorder.work is None:
                    self._answer(404, {})
                else:
                    self._answer(200, recorder.work)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/api/events":
                    self._answer(404, {})
                    return
                recorder.handed.append(body.get("events"))
                self._answer(recorder.events_status, {"accepted": len(body.get("events", []))})

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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


# --- spec 026 group C: a fake Slack --------------------------------------

# Not a real token's shape past the prefix, so no secret scanner reads it as one.
SLACK_BOT_TOKEN = "xoxb-fake-token-for-ana"
SLACK_CLIENT_ID = "1111.2222"
SLACK_CLIENT_SECRET = "fake-client-secret"
SLACK_SIGNING_SECRET = "fake-signing-secret"
SLACK_TOKEN_KEY = "0f" * 32


def slack_settings(**changes) -> SlackSettings:
    values = {"client_id": SLACK_CLIENT_ID, "client_secret": SLACK_CLIENT_SECRET,
              "signing_secret": SLACK_SIGNING_SECRET,
              "token_key": bytes.fromhex(SLACK_TOKEN_KEY)}
    return SlackSettings(**{**values, **changes})


class FakeSlack:
    """Slack's Web API on loopback: oauth.v2.access and chat.postMessage.

    It records every request with its Authorization header, and a test sets
    what each method answers. Slack answers 200 and says no in the body, and
    so does this."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.oauth_answer: dict = {
            "ok": True, "access_token": SLACK_BOT_TOKEN, "token_type": "bot",
            "scope": "app_mentions:read,chat:write", "bot_user_id": "U0BOT",
            "team": {"id": "T0ANA", "name": "Ana & Co"}}
        self.post_answer: dict = {"ok": True, "ts": "171.9"}
        # What reading a thread and looking up a person answer. The default
        # is a workspace that added the app before those scopes were asked for.
        self.replies_answer: dict = {"ok": False, "error": "missing_scope"}
        # What reading a channel's history answers (spec 029 D).
        self.history_answer: dict = {"ok": False, "error": "missing_scope"}
        # A channel's whole history, in any order. When a test sets it,
        # conversations.history serves it as Slack does: newest first, from
        # `oldest` on, `limit` to a page, with `next_cursor` while more wait.
        self.history_messages: list[dict] | None = None
        self.users: dict[str, str] = {}
        self.channels: dict[str, str] = {}
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def calls(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def history_page(self, sent: dict) -> dict:
        """One page of `history_messages`, the way conversations.history
        pages: newest first, nothing older than `oldest`."""
        oldest = float(sent.get("oldest") or 0)
        found = sorted((m for m in self.history_messages if float(m["ts"]) >= oldest),
                       key=lambda m: float(m["ts"]), reverse=True)
        start, size = int(sent.get("cursor") or 0), int(sent.get("limit") or 100)
        page, more = found[start:start + size], start + size < len(found)
        return {"ok": True, "messages": page, "has_more": more,
                "response_metadata": {"next_cursor": str(start + size) if more else ""}}

    def start(self) -> None:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                """Silence BaseHTTPRequestHandler's stderr log line."""

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8")
                if "json" in self.headers.get("Content-Type", ""):
                    sent = json.loads(raw or "{}")
                else:
                    sent = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                recorder.requests.append({
                    "path": self.path, "body": sent,
                    "authorization": self.headers.get("Authorization", "")})
                answers = {"/oauth.v2.access": recorder.oauth_answer,
                           "/chat.postMessage": recorder.post_answer,
                           "/conversations.replies": recorder.replies_answer,
                           "/conversations.history": recorder.history_answer,
                           "/reactions.add": {"ok": True}}
                answer = answers.get(self.path, {"ok": False, "error": "unknown_method"})
                if (self.path == "/conversations.history"
                        and recorder.history_messages is not None):
                    answer = recorder.history_page(sent)
                if self.path == "/conversations.info":
                    named = recorder.channels.get(sent.get("channel", ""))
                    answer = ({"ok": True, "channel": {"name": named}}
                              if named else {"ok": False, "error": "missing_scope"})
                if self.path == "/users.info":
                    known = recorder.users.get(sent.get("user", ""))
                    answer = ({"ok": True, "user": {"profile": {"display_name": known}}}
                              if known else {"ok": False, "error": "missing_scope"})
                body = json.dumps(answer).encode("utf-8")
                # Slack compresses its answers for a client that takes gzip.
                # The fake did not until 2026-11-18, and the first real
                # install failed on exactly that.
                zipped = "gzip" in self.headers.get("Accept-Encoding", "")
                if zipped:
                    body = gzip.compress(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if zipped:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# --- spec 029: a fake vendor ----------------------------------------------

# Not a real token's shape, so no secret scanner reads one as a credential.
VENDOR_CLIENT_SECRET = "fake-vendor-client-secret"


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FakeVendor:
    """A vendor's sign-in server on loopback: the two discovery documents,
    dynamic client registration and the token endpoint, with PKCE checked.

    The sign-in page itself is a person's browser, so a test plays it: it
    reads the address /connect/start redirects to and calls `approve` with
    that address's query, which returns the code the vendor would send back.

    It records every request, and it compresses an answer for a client that
    takes gzip, as a real vendor does."""

    def __init__(self, title: str = "vendor") -> None:
        self.title = title
        self.requests: list[dict] = []
        # client id -> the registration this server issued.
        self.clients: dict[str, dict] = {}
        # code -> the sign-in a person approved.
        self.codes: dict[str, dict] = {}
        # refresh token -> how many times it was issued, so each is distinct.
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()
        self.issued = 0
        self.expires_in: int | None = 3600
        # What discovery says, changed by a test that moves an address away.
        self.metadata_changes: dict = {}
        self.resource_changes: dict = {}
        # A token answer a test sets in place of the real one.
        self.token_answer: dict | None = None
        self.register_answer: dict | None = None
        self.send_refresh_token = True
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def calls(self, path: str) -> list[dict]:
        return [request for request in self.requests if request["path"] == path]

    def approve(self, query: dict) -> str:
        """The person presses Allow. `query` is what /connect/start sent the
        browser to the sign-in page with."""
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = dict(query)
        return code

    def _tokens(self) -> dict:
        self.issued += 1
        access = f"{self.title}-access-{self.issued}"
        self.access_tokens.add(access)
        answer = {"access_token": access, "token_type": "Bearer"}
        if self.expires_in is not None:
            answer["expires_in"] = self.expires_in
        if self.send_refresh_token:
            refresh = f"{self.title}-refresh-{self.issued}"
            self.refresh_tokens.add(refresh)
            answer["refresh_token"] = refresh
        return answer

    def _token(self, sent: dict) -> tuple[int, dict]:
        if self.token_answer is not None:
            return 400 if "error" in self.token_answer else 200, self.token_answer
        client = self.clients.get(sent.get("client_id", ""))
        if client is None or sent.get("client_secret") != client["client_secret"]:
            return 401, {"error": "invalid_client"}
        if sent.get("grant_type") == "authorization_code":
            approved = self.codes.pop(sent.get("code", ""), None)
            if (approved is None or approved.get("client_id") != sent["client_id"]
                    or approved.get("redirect_uri") != sent.get("redirect_uri")
                    or approved.get("code_challenge")
                    != pkce_challenge(sent.get("code_verifier", ""))):
                return 400, {"error": "invalid_grant"}
            return 200, self._tokens()
        if sent.get("grant_type") == "refresh_token":
            # Rotating: a refresh token works once.
            if sent.get("refresh_token") not in self.refresh_tokens:
                return 400, {"error": "invalid_grant"}
            self.refresh_tokens.discard(sent["refresh_token"])
            return 200, self._tokens()
        return 400, {"error": "unsupported_grant_type"}

    def _get(self, path: str) -> tuple[int, dict]:
        if path == "/.well-known/oauth-protected-resource":
            return 200, {"resource": self.url, "authorization_servers": [self.url],
                         **self.resource_changes}
        if path == "/.well-known/oauth-authorization-server":
            return 200, {
                "issuer": self.url, "authorization_endpoint": f"{self.url}/authorize",
                "token_endpoint": f"{self.url}/token",
                "registration_endpoint": f"{self.url}/register",
                "code_challenge_methods_supported": ["S256"], **self.metadata_changes}
        return 404, {"error": "not_found"}

    def _post(self, path: str, sent: dict, headers) -> tuple[int, dict]:
        if path == "/register":
            if self.register_answer is not None:
                return 400, self.register_answer
            client_id = f"client-{len(self.clients) + 1}"
            self.clients[client_id] = {**sent, "client_id": client_id,
                                       "client_secret": VENDOR_CLIENT_SECRET}
            return 201, self.clients[client_id]
        if path == "/token":
            return self._token(sent)
        return self.handle_other(path, sent, headers)

    def handle_other(self, path: str, sent: dict, headers) -> tuple[int, dict]:
        return 404, {"error": "not_found"}

    def start(self) -> None:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                """Silence BaseHTTPRequestHandler's stderr log line."""

            def _send(self, status: int, answer: dict) -> None:
                body = json.dumps(answer).encode("utf-8")
                zipped = "gzip" in self.headers.get("Accept-Encoding", "")
                if zipped:
                    body = gzip.compress(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if zipped:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                recorder.requests.append({"method": "GET", "path": self.path, "body": {},
                                          "authorization": ""})
                self._send(*recorder._get(self.path))

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8")
                if "json" in self.headers.get("Content-Type", ""):
                    sent = json.loads(raw or "{}")
                else:
                    sent = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                recorder.requests.append({
                    "method": "POST", "path": self.path, "body": sent,
                    "authorization": self.headers.get("Authorization", ""),
                    "headers": {k.lower(): v for k, v in self.headers.items()}})
                answer = recorder._post(self.path, sent, self.headers)
                if answer is not None:
                    self._send(*answer)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
