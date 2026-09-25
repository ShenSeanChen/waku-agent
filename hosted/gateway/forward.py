"""What crosses the boundary, in each direction, and what does not.

THIS MODULE MAKES NO DECISION GROUP B HAS NOT ALREADY MADE.
hosted/core/policy.decide returns the verdict, hosted/core/idle.Fleet.admit
returns the action, hosted/core/quota.allow_turn returns the refusal. Every
one of them is pure, on an injected clock, and tested without a server. What
is here is the part that has no pure-logic home: two header allowlists, a
socket, and the order the three decisions run in.

BOTH LISTS ARE ALLOWLISTS, AND THE DEFAULT IS ALWAYS TO DROP. A list of
forbidden headers loses the day somebody invents a new one -- which is what
Set-Cookie, Clear-Site-Data, Link rel=preload and Refresh all are, from the
point of view of whoever wrote the list before they existed. Measured on a
real BaseHTTPRequestHandler: a Set-Cookie the handler sends DOES arrive in
the client's headers, so this is a drop that happens, not a precaution.

THE CONTAINER IS HTTP/1.0. waku/ops/dashboard.py's Handler does not set
protocol_version, so every response is HTTP/1.0 and an SSE body is delimited
by the connection closing. aiohttp reads that as a stream and iter_any()
yields each flush separately, which is what makes the pass-through unbuffered
without any special case for it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

from hosted import log
from hosted.core import idle, policy, quota
from hosted.gateway import answers, guards
from hosted.gateway.launch import InMaintenance, Launcher, NotActive, StartFailed
from hosted.gateway.proxy_client import Spend, read_spend
from hosted.ports.control import Tenant
from hosted.ports.runtime import RunningContainer

_LOG = log.get(__name__)

TURN_LIMIT_STATUS = 429
REFUSED_STATUS = 503
CONNECT_TIMEOUT_SECONDS = 10.0

# Spec, "Responses from a container are not trusted, even on its own origin":
# the gateway passes only these from a container's response, plus a relative,
# same-origin Location, which needs its own check and so is not in the tuple.
#
# Content-Encoding is on the list although nothing should ever set it: the
# forwarded request sends no Accept-Encoding (see _send's skip_auto_headers),
# so a container has nothing to negotiate against. If one compresses anyway,
# dropping the header while forwarding the compressed bytes renders binary in
# the browser. A header that describes the body has to travel with it.
RESPONSE_HEADERS = ("Content-Type", "Content-Disposition", "Content-Encoding")

# Spec, the same paragraph: "on the way in, it forwards only Content-Type,
# Accept, Content-Length and X-Waku-Background, so the tenant-host cookie and
# any other credential never reach tenant-controlled code". Content-Length is
# not in this tuple because it is not COPIED: it is recomputed from the bytes
# actually sent, below. Copying a length the caller claimed, alongside a body
# the gateway may have re-serialised, is a request smuggling primitive.
#
# THIS TUPLE IS NOT THE WHOLE STORY, AND THE DIFFERENCE WAS MEASURED.
# aiohttp adds Accept, Accept-Encoding and User-Agent to every request whose
# headers do not carry them, BELOW this dict, so a container saw:
#     Host, Content-Type, Accept, X-Waku-Background,
#     Accept-Encoding: gzip, deflate
#     User-Agent: Python/3.11 aiohttp/3.14.3
# Neither extra is a credential and neither is attacker-chosen, but an
# allowlist with two names it does not know about is not one. _send passes
# skip_auto_headers=SKIPPED_AUTO_HEADERS, which removes both; Accept stays,
# because Accept IS on this list and aiohttp's */* default is the right value
# when the browser sent none.
REQUEST_HEADERS = ("Content-Type", "Accept", policy.BACKGROUND_HEADER)
SKIPPED_AUTO_HEADERS = ("User-Agent", "Accept-Encoding")

METHODS_WITH_A_BODY = frozenset({"POST", "PUT", "PATCH"})


# A Location the gateway will pass on: one leading slash, and the character
# after it is neither "/" nor "\\".
#
# "//evil.example/x" is a protocol-relative absolute URL that starts with a
# slash. So is "/\\evil.example/x": the WHATWG URL parser treats a backslash
# as a slash in special schemes, so a browser reads it as scheme-relative and
# navigates off-origin. An earlier version of this guard tested only for "//"
# and admitted the backslash form, while the comment beside it claimed it was
# what stopped a container choosing where the browser goes.
def _is_same_origin_location(location: str | None) -> bool:
    if not location or not location.startswith("/"):
        return False
    return len(location) == 1 or location[1] not in ("/", "\\")


def holds_container(path: str) -> bool:
    """A turn, or another streaming request. These are the ones that count as
    "in flight" AND the ones with no 120-second timeout -- one predicate, two
    uses, so the two can never disagree about what a long request is.

    EXACT PATH, NOT THE MATCHED ROUTE, and the asymmetry with is_a_turn below
    is deliberate. policy.match is longest-PREFIX, so /api/chat/streamX matches
    the /api/chat/stream entry. Each question then fails closed on its own
    axis: a suffixed route is CHARGED as a turn (is_a_turn, below, reads the
    matched route) so it cannot be a free turn, and it is CAPPED at 120
    seconds (here, exact path) so it cannot hold a container awake forever.

    THE SECOND CLAUSE IS UNREACHABLE TODAY AND NO TEST DRIVES IT, which is
    said here rather than dressed up. STREAMING_ROUTES minus TURN_ROUTES is
    {/api/compare/stream, /api/memory-arena/stream, /api/judgment-arena/
    stream, /api/voice}, and policy.DECISIONS marks every one of those BLOCK
    -- so none of them ever reaches _deliver, and deleting `or path in
    policy.STREAMING_ROUTES` leaves the whole suite green. It is kept because
    the day one of them is unblocked, a stream would otherwise be cut off at
    120 seconds with no line of code saying why. Measured, not assumed:
    breaking it to `policy.is_turn(path)` alone was run and nothing went red.
    """
    return policy.is_turn(path) or path in policy.STREAMING_ROUTES


def is_a_turn(outcome: policy.Outcome) -> bool:
    """Whether this request spends one of the tenant's turns.

    THE MATCHED ROUTE, NOT THE PATH. policy.match is longest-prefix, so
    POST /api/chat/streamX matches the /api/chat/stream entry and PASSES --
    while policy.is_turn, which is an exact-membership test, says no. Counting
    from the path would make a one-character suffix a free turn. It is
    dead-ended today only because dashboard.py matches all three turn routes
    with `self.path == ...` on the raw target, so the container answers 404;
    a future route matched with startswith would open it.
    """
    return outcome.route in policy.TURN_ROUTES


def platform_call_recent(spend: Spend | None, now: float) -> bool:
    """`platform_call_in_last_hour` for quota.plan_for.

    UNKNOWN IS NOT FALSE, AND THIS IS THE WHOLE FUNCTION'S REASON.
    quota.plan_for returns byok -- 120 turns an hour instead of 30 -- when a
    tenant has a turn in the last hour and NO platform call in it. read_spend
    answers None when the proxy cannot be reached. So passing False for "I
    could not find out" hands every tenant four times their allowance exactly
    when the platform is least able to measure anything.

    The spec says what to do: "when it cannot answer, the gateway applies
    free's turn limit". True here means "treat this tenant as having spent
    platform money", which is what forces free.

    WITH GROUP D CUT, run/proxy/proxy.sock DOES NOT EXIST AND THIS IS THE ONLY
    BRANCH THAT EVER RUNS. Everybody is on free's 30 an hour until D1 lands.
    """
    if spend is None:
        return True
    last = spend.last_platform_call
    if last is None:
        return False
    return now - float(last) <= quota.TURN_WINDOW_SECONDS


class ContainerForwarder:
    def __init__(self, *, launcher: Launcher, turns: quota.TurnWindow,
                 plans: dict[str, quota.Plan], proxy_socket: Path,
                 session: aiohttp.ClientSession,
                 now: Callable[[], float] = time.time) -> None:
        self._launcher = launcher
        self._turns = turns
        self._plans = plans
        self._proxy_socket = proxy_socket
        # F4: THE SESSION'S CONTRACT, CHECKED RATHER THAN ASSUMED.
        # Content-Encoding is on RESPONSE_HEADERS because nothing should
        # arrive compressed and, if something does, the header has to travel
        # with the body it describes. Both halves rest on the session not
        # decompressing: one that did would hand this class a plain body while
        # the allowlist still copies Content-Encoding, and the browser renders
        # binary -- the exact outcome that comment says it is avoiding. Three
        # call sites build the session (__main__, gatewaylib, the Docker tier)
        # and a fourth is one forgotten keyword away, so the contract is a
        # line of code rather than a sentence.
        if session.auto_decompress:
            raise ValueError(
                "ContainerForwarder needs a session built with "
                "auto_decompress=False: a container's body is forwarded as "
                "received, and Content-Encoding travels with it.")
        self._session = session
        self._now = now

    # --- how a refusal is shaped ----------------------------------------

    def _refuse(self, request: web.Request, status: int, message: str, *,
                streaming: bool) -> web.Response:
        """Three shapes, and which one is used is never guessed.

        `streaming` comes from policy.Outcome.streaming, which policy.py sets
        from STREAMING_ROUTES so that E3 reads it rather than deciding it --
        four of the six streaming routes are blocked, and re-deriving which by
        hand is how one of them ends up answering JSON into a reader that only
        parses `data:` frames.
        """
        if streaming:
            return answers.sse_error(status, message)
        return answers.refusal(request, status, message)

    def _paused(self) -> web.Response:
        """The one body in this group that carries a `code`, and it is group
        B's, not this module's: policy.PAUSED_BODY, pinned against the page's
        own branch by test_paused_contract.py."""
        return answers.harden(web.Response(
            status=policy.PAUSED_STATUS,
            body=json.dumps(policy.PAUSED_BODY).encode("utf-8"),
            content_type="application/json"))

    # --- what crosses, in each direction --------------------------------

    @staticmethod
    def _request_headers(request: web.Request, length: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in REQUEST_HEADERS:
            value = request.headers.get(name)
            if value is not None:
                out[name] = value
        if length:
            # The length of what is actually being sent, not the length the
            # caller claimed: a filtered body was re-serialised and is a
            # different size, and the stock server reads a body only by
            # Content-Length (waku/ops/dashboard.py's _read_json).
            #
            # AIOHTTP WOULD ALSO SET THIS FROM `data`, so deleting this line
            # ON ITS OWN leaves every test green -- measured. That is not a
            # reason to delete it: the thing being refused is COPYING the
            # caller's claim, and the two lines that refuse it are this one
            # and Content-Length's absence from REQUEST_HEADERS. Undo both and
            # the container is handed a length longer than the body, blocks on
            # bytes that never arrive, and the request dies at the 120-second
            # timeout -- which is what test_a_filtered_body_is_re_serialised_
            # and_sent_with_its_own_length catches, and the shape of a request
            # smuggling primitive.
            out["Content-Length"] = str(length)
        return out

    @staticmethod
    def _copy_response_headers(upstream: aiohttp.ClientResponse,
                               downstream: web.StreamResponse) -> None:
        for name in RESPONSE_HEADERS:
            value = upstream.headers.get(name)
            if value is not None:
                downstream.headers[name] = value
        location = upstream.headers.get("Location")
        if _is_same_origin_location(location):
            downstream.headers["Location"] = location

    # --- the request flow -----------------------------------------------

    async def __call__(self, request: web.Request,
                       tenant: Tenant) -> web.StreamResponse:
        raw = request.raw_path
        path, _query = policy.split_path(raw)
        streaming = path in policy.STREAMING_ROUTES

        if self._launcher.in_maintenance(tenant.id):
            # Before policy, before the body is read: a tenant under
            # maintenance gets one answer and nothing is started for them.
            return self._refuse(request, REFUSED_STATUS,
                                idle.MAINTENANCE_MESSAGE, streaming=streaming)

        body = await request.read() if request.method in METHODS_WITH_A_BODY else b""
        payload: dict | None = None
        if request.method == "POST":
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                return self._refuse(request, guards.UNSUPPORTED_MEDIA,
                                    answers.NOT_JSON, streaming=streaming)
            if not isinstance(parsed, dict):
                return self._refuse(request, guards.UNSUPPORTED_MEDIA,
                                    answers.NOT_JSON, streaming=streaming)
            payload = parsed

        outcome = policy.decide(request.method, raw, payload)
        if outcome.verdict in ("refuse", "block"):
            return self._refuse(request, outcome.status, outcome.message,
                                streaming=outcome.streaming)
        if outcome.verdict == "rewrite":
            body = json.dumps(outcome.payload).encode("utf-8")
        elif outcome.verdict != policy.PASS:
            # F6, the other half. policy.Outcome.verdict is one of four
            # strings; a fifth added in group B would otherwise be PASSED to
            # the container, and a verdict is invented precisely when somebody
            # wants a request handled differently from PASS. Refusing what
            # this module cannot read is the only answer that cannot be the
            # wrong one.
            _LOG.error("policy.decide answered verdict %r, which forward.py "
                       "does not handle; refusing %s", outcome.verdict, path)
            return self._refuse(request, REFUSED_STATUS, answers.REFUSED,
                                streaming=streaming)

        if is_a_turn(outcome):
            spend = await read_spend(self._proxy_socket, tenant.id)
            allowed, _plan, message = quota.allow_turn(
                self._plans, turns_in_last_hour=self._turns.count(tenant.id),
                platform_call_in_last_hour=platform_call_recent(spend, self._now()))
            if not allowed:
                return self._refuse(request, TURN_LIMIT_STATUS, message,
                                    streaming=outcome.streaming)
            self._turns.record(tenant.id)

        background = policy.is_background(request.headers)
        running = await self._reach(request, tenant, background=background,
                                    streaming=outcome.streaming)
        if isinstance(running, web.Response):
            return running
        return await self._deliver(request, tenant, running, body, path,
                                   background=background,
                                   streaming=outcome.streaming)

    # --- the admission ladder -------------------------------------------

    async def _reach(self, request: web.Request, tenant: Tenant, *,
                     background: bool,
                     streaming: bool) -> RunningContainer | web.Response:
        """The spec's flowchart from "container state?" down, and nothing else.

        Fleet.admit decides; this turns the decision into a container or a
        response. The one thing it adds is the case admit() cannot see: the
        fleet says RUNNING and the address book is empty, which happens after
        a resync dropped an address the fleet had not been told about yet.

        THE "start" ARM AND THE FALL-THROUGH CONVERGE FOR A STOPPED CONTAINER
        WITH NO ADDRESS, and that is worth knowing before somebody deletes one
        of them. Narrowing this line to "evict_then_start" alone leaves
        test_a_user_driven_request_to_a_stopped_container_starts_it green,
        because the fall-through starts it too. What the arm really decides is
        the case where the fleet says STOPPED and an address is STILL in the
        book: the fall-through would forward to it, and this starts a fresh
        container instead. That is
        test_a_stopped_container_with_a_stale_address_is_started_rather_than_
        used, and it is the only thing that drives this line.
        """
        admission = self._launcher.fleet.admit(tenant.id, background=background)
        if admission.action == "paused":
            return self._paused()
        if admission.action == "at_capacity":
            return self._refuse(request, REFUSED_STATUS, admission.message,
                                streaming=streaming)
        if admission.action == "wait":
            running = await self._launcher.wait_for_start(tenant.id)
            if running is None:
                return self._refuse(request, REFUSED_STATUS,
                                    idle.START_TIMEOUT_MESSAGE, streaming=streaming)
            return running
        if admission.action in ("start", "evict_then_start"):
            if admission.evict:
                _LOG.info("at the cap: stopping tenant=%s to start tenant=%s",
                          admission.evict, tenant.id)
                await self._launcher.stop(admission.evict)
            return await self._start(request, tenant, streaming=streaming)
        if admission.action != "forward":
            # F6: A CLOSED SET, AND THE DEFAULT IS TO REFUSE. Every action
            # Fleet.admit can answer is named above or is "forward"; a sixth
            # one added in another group would otherwise fall through to the
            # address book and be forwarded -- and, for a tenant with no
            # address, STARTED, outside whatever cap the new action was
            # invented to express. Refusing an action this module cannot read
            # costs one tenant one request and a line in the log.
            _LOG.error("Fleet.admit answered %r, which forward.py does not "
                       "handle; refusing tenant=%s", admission.action, tenant.id)
            return self._refuse(request, REFUSED_STATUS, idle.CAPACITY_MESSAGE,
                                streaming=streaming)
        running = self._launcher.address(tenant.id)
        if running is not None:
            return running
        if background:
            return self._paused()
        return await self._start(request, tenant, streaming=streaming)

    async def _start(self, request: web.Request, tenant: Tenant, *,
                     streaming: bool) -> RunningContainer | web.Response:
        try:
            return await self._launcher.start(tenant)
        except InMaintenance:
            return self._refuse(request, REFUSED_STATUS, idle.MAINTENANCE_MESSAGE,
                                streaming=streaming)
        except NotActive as exc:
            return self._refuse(request, 403, str(exc), streaming=streaming)
        except StartFailed as exc:
            return self._refuse(request, REFUSED_STATUS, str(exc),
                                streaming=streaming)

    # --- delivery, the retry ladder and the timeout ----------------------

    async def _deliver(self, request: web.Request, tenant: Tenant,
                       running: RunningContainer, body: bytes, path: str, *,
                       background: bool, streaming: bool) -> web.StreamResponse:
        """Send it, and resolve a refused connection the way the spec says.

        "A container the gateway believed running that refuses the connection
        (killed for memory, or restarted by an admin action) is looked up
        again with the spawner's `list`; if it is running, the gateway
        retries, and only if it is gone does the gateway start it once more
        before the request fails."

        A ClientConnectorError is a CONNECT-time failure: nothing was
        delivered, so retrying a POST here is not a second side effect. That
        is why the retry is caught on that class alone and not on
        ClientError.
        """
        held = holds_container(path)
        if held:
            self._launcher.fleet.enter(tenant.id)
        try:
            try:
                return await self._send(request, running, body, held=held)
            except aiohttp.ClientConnectorError:
                _LOG.info("tenant=%s refused a connection at %s; re-listing",
                          tenant.id, running.address)
            await self._launcher.resync()
            second = self._launcher.address(tenant.id)
            if second is None:
                if background:
                    return self._paused()
                started = await self._start(request, tenant, streaming=streaming)
                if isinstance(started, web.Response):
                    return started
                second = started
            try:
                return await self._send(request, second, body, held=held)
            except aiohttp.ClientConnectorError:
                return self._refuse(request, REFUSED_STATUS,
                                    idle.START_TIMEOUT_MESSAGE, streaming=streaming)
        finally:
            if held:
                self._launcher.fleet.leave(tenant.id)

    async def _send(self, request: web.Request, running: RunningContainer,
                    body: bytes, *, held: bool) -> web.StreamResponse:
        """One attempt at the container, with the two allowlists on it.

        THE TIMEOUT IS READ THROUGH THE MODULE, `idle.FORWARD_TIMEOUT_SECONDS`
        AND NOT A `from ... import` AT MODULE SCOPE. The 120 seconds is what
        test_a_slow_ordinary_request_times_out_and_a_slow_turn_does_not drives,
        by monkeypatching the attribute on hosted.core.idle down to 50 ms. A
        module-scope import would bind the number once, the monkeypatch would
        reach nothing, and the test would pass on a 0.4-second delay because
        no timeout ever fired -- a test that cannot fail.
        """
        url = URL(f"http://{running.address}:{running.port}"
                  f"{policy.forward_line(request.raw_path)}", encoded=True)
        # encoded=True, so nothing re-encodes the target on the way out and
        # the container gets acceptance 22's byte-for-byte request line.
        #
        # WHAT IT ACTUALLY CHANGES, MEASURED ON yarl 1.24.2 rather than
        # assumed. A plain URL() leaves /api/events?cursor=42 and even
        # /api/models?provider= alone, so the obvious cases prove nothing --
        # the test that rests on only those passes with this argument deleted.
        # What a plain URL() does change is a percent-escape yarl considers
        # safe to resolve: ?q=%2Fhome%2Fmei arrives as ?q=/home/mei, and
        # ?q=%3F arrives as ?q=?, which hands the upstream parser a second
        # question mark to split on. Those two are the parametrised cases in
        # test_the_container_receives_the_raw_path_and_the_raw_query.
        timeout = aiohttp.ClientTimeout(
            total=None if held else idle.FORWARD_TIMEOUT_SECONDS,
            sock_connect=CONNECT_TIMEOUT_SECONDS)
        downstream: web.StreamResponse | None = None
        try:
            async with self._session.request(
                    request.method, url,
                    headers=self._request_headers(request, len(body)),
                    skip_auto_headers=SKIPPED_AUTO_HEADERS,
                    data=body or None, timeout=timeout,
                    allow_redirects=False) as upstream:
                downstream = web.StreamResponse(status=upstream.status)
                self._copy_response_headers(upstream, downstream)
                answers.harden(downstream)
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except TimeoutError:
            if downstream is not None and downstream.prepared:
                # The headers are already on the wire, so there is no status
                # left to change. Ending the body is the whole of what can be
                # done, and it is better than leaving the socket open until
                # the browser gives up.
                await downstream.write_eof()
                return downstream
            return answers.refusal(request, 504, answers.TOOK_TOO_LONG)
