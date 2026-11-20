"""A tenant's calls to a connector, made by the gateway (spec 029, group B).

A hosted container holds no Notion token. Its MCP client sends each message
to the metering proxy, the proxy finds the tenant from the container's
platform token, and asks the gateway over gateway.sock. The gateway makes the
call here with that tenant's token and hands back the answer:

  container --POST /connect/notion/mcp--> proxy --gateway.sock--> gateway --> Notion

THE CONTAINER'S MESSAGE IS READ, NEVER PASSED THROUGH. One JSON-RPC message
comes in, and what it may be is a closed set:

  initialize              the gateway opens its own session with the vendor
                          and answers with the vendor's own answer
  tools/list              sent to the vendor
  tools/call              sent to the vendor, unless the tool changes
                          something there (below)
  ping                    answered here
  notifications/initialized, notifications/cancelled
                          taken and dropped
  anything else           refused with JSON-RPC's "method not found"

A TOOL THAT WRITES IS NOT RUN. Spec 025 lets a connector's write tools run
after the owner's yes, and a hosted person has nowhere to say yes until the
spec's group C is built. So a tool whose name matches the vendor's `writes`
patterns (hosted/core/connectors.py) is answered here with a sentence that
says so, and the vendor is never called. The decision is the gateway's: a
container that is broken into cannot make a write run.

THE GATEWAY OWNS THE VENDOR'S SESSION. A Streamable HTTP server hands out an
`Mcp-Session-Id` at `initialize` and wants it on every later request. The
gateway keeps it, per tenant and vendor, in memory, and the container never
sees it. A session the vendor forgot (HTTP 404), and a gateway that restarted
and forgot the id, both end the same way: the gateway opens a new session and
sends the call once more.

THE VENDOR'S ANSWER IS READ TO ITS END HERE. It arrives as JSON or as a
Server-Sent Events stream, compressed or not, and the gateway's session does
not decompress (__main__.py). The one message that answers the call is taken
out and returned as plain JSON, at most MAX_ANSWER_BYTES long.

Nothing here logs a token, a tool's arguments or a vendor's answer. A log line
carries a tenant id, a vendor's name, a method and a status.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
import zlib
from dataclasses import dataclass

import aiohttp

from hosted import log
from hosted.core.connectors import Vendor
from hosted.core.tenant import is_tenant_id
from hosted.gateway.connect import (
    Connect,
    NotConnected,
    ReconnectNeeded,
    VendorUnreachable,
)

_LOG = log.get(__name__)

# One call to a vendor, from the request to the end of its answer. The
# container's own MCP client waits 120 seconds for a tool.
CALL_TIMEOUT = aiohttp.ClientTimeout(total=100, sock_connect=10)
# The most of one answer the gateway reads. The answer goes back over
# gateway.sock as one line, and a model reads it, so it is bounded.
MAX_ANSWER_BYTES = 1024 * 1024
# The MCP revision the gateway asks for when the container names none.
PROTOCOL_VERSION = "2025-06-18"
_PROTOCOL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CLIENT_INFO = {"name": "waku-agent", "title": "Waku", "version": "hosted"}

FORWARDED = frozenset({"tools/list", "tools/call"})
DROPPED = frozenset({"notifications/initialized", "notifications/cancelled"})

# JSON-RPC's own codes, and one of this server's.
METHOD_NOT_FOUND, INVALID_PARAMS, SERVER_ERROR = -32601, -32602, -32000

NOT_AVAILABLE = "'{method}' is not available for a connector on hosted Waku."
NO_TOOL_NAME = "A tool call names its tool."
NOT_CONNECTED = "{title} is not connected. Connect it at https://{apex}/setup."
RECONNECT = ("Waku lost its sign-in to {title}. Connect it again at "
             "https://{apex}/setup.")
UNREACHABLE = "{title} did not answer. Try again in a moment."
TOO_LARGE = ("{title}'s answer was too large to read here. Ask for less: a narrower "
             "search, or one page.")
WRITE_REFUSED = ("Not run: '{tool}' changes something in {title}, and a change needs "
                 "your yes first. Saying yes is not built on hosted Waku yet. Reading "
                 "from {title} works.")

_GZIP_MAGIC = b"\x1f\x8b"


class _TooLarge(ValueError):
    """The vendor's answer is longer than MAX_ANSWER_BYTES."""


class _Refused(RuntimeError):
    """The vendor answered with an HTTP status that is not an answer."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@dataclass
class _Session:
    session_id: str
    protocol: str
    opened: dict      # the vendor's answer to `initialize`


def _error(message_id: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def _answer_in(parsed: object, message_id: object) -> dict | None:
    """The JSON-RPC message that answers `message_id`, or None. A vendor may
    send requests and notifications of its own on the same stream, and a
    batch: only an answer to this call is taken."""
    for item in parsed if isinstance(parsed, list) else (parsed,):
        if (isinstance(item, dict) and item.get("id") == message_id
                and ("result" in item or "error" in item)):
            return item
    return None


class _Events:
    """Reads a Server-Sent Events stream as it arrives and gives back each
    event's data."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        self._pending += text.replace("\r\n", "\n").replace("\r", "\n")
        *blocks, self._pending = self._pending.split("\n\n")
        found = []
        for block in blocks:
            data = [line[5:].removeprefix(" ") for line in block.split("\n")
                    if line.startswith("data:")]
            if data:
                found.append("\n".join(data))
        return found


async def read_answer(reply: aiohttp.ClientResponse, message_id: object) -> dict | None:
    """The message that answers `message_id` in a vendor's HTTP answer, or
    None when the body holds none.

    The body is JSON or an event stream, and gzip or not. It is opened as it
    arrives, so a stream the vendor keeps open after the answer is left as
    soon as the answer has come. Raises _TooLarge past MAX_ANSWER_BYTES and
    ValueError on bytes that are not what the headers say.
    """
    stream = "text/event-stream" in reply.headers.get("Content-Type", "")
    inflate = None
    events, held, seen = _Events(), bytearray(), 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    first = True
    async for chunk in reply.content.iter_any():
        if first and chunk:
            first = False
            if (chunk[:2] == _GZIP_MAGIC
                    or "gzip" in reply.headers.get("Content-Encoding", "").lower()):
                inflate = zlib.decompressobj(wbits=31)
        if inflate is not None:
            try:
                # max_length bounds what one compressed chunk may become.
                chunk = inflate.decompress(chunk, MAX_ANSWER_BYTES + 1 - seen)
            except zlib.error as exc:
                raise ValueError("the answer did not decompress") from exc
            if inflate.unconsumed_tail:
                raise _TooLarge()
        seen += len(chunk)
        if seen > MAX_ANSWER_BYTES:
            raise _TooLarge()
        if not stream:
            held.extend(chunk)
            continue
        for data in events.feed(decoder.decode(chunk)):
            try:
                found = _answer_in(json.loads(data), message_id)
            except ValueError:
                continue
            if found is not None:
                return found
    if stream:
        return None
    return _answer_in(json.loads(bytes(held).decode("utf-8")), message_id)


class McpRelay:
    """Make one tenant's MCP call to one vendor, with that tenant's token.

    WHICH TENANT IS NOT THE CALLER'S CHOICE. The proxy gives the tenant id it
    resolved from the container's own platform token, and the relay uses the
    one sign-in stored for that tenant and that vendor. A call with another
    tenant's platform token is that other tenant's call.
    """

    def __init__(self, *, connect: Connect, session: aiohttp.ClientSession,
                 apex_host: str) -> None:
        self._connect = connect
        self._http = session
        self._apex = apex_host
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._opening: dict[tuple[str, str], asyncio.Lock] = {}

    def forget(self, tenant_id: str, server: str) -> None:
        """Drop the vendor session kept for this person: they disconnected,
        or signed in again as someone else."""
        self._sessions.pop((tenant_id, server), None)

    def link(self, tenant_id: str, server: object) -> dict:
        """Where a person starts the sign-in to one vendor, for "connect
        Notion" in chat (task B3). Nothing is started here: the link is the
        page a signed-in person opens."""
        vendors = self._connect.vendors
        vendor = vendors.get(server) if isinstance(server, str) else None
        if vendor is None or not is_tenant_id(tenant_id):
            return {"ok": False, "error": "not_listed",
                    "available": [v.title for v in vendors.values()]}
        return {"ok": True, "title": vendor.title,
                "url": f"https://{self._apex}/connect/start?server={vendor.name}",
                "connected": self._connect.states(tenant_id)[vendor.name] == "connected"}

    async def call(self, tenant_id: str, server: object, message: object) -> dict:
        """One JSON-RPC message from a container. Answers {"ok": True,
        "message": <the JSON-RPC answer, or None for a notification>}, or
        {"ok": False, "error": <code>} for a message that is not one."""
        vendor = self._connect.vendor(server)
        # A vendor that is not an MCP server (Gmail) has no address to relay to.
        if (vendor is None or not vendor.mcp
                or not is_tenant_id(tenant_id) or not isinstance(message, dict)
                or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)):
            return {"ok": False, "error": "bad_request"}
        method = message["method"]
        if "id" not in message:
            # A notification has no answer. The two a client sends are taken;
            # the gateway told the vendor `initialized` itself.
            return ({"ok": True, "message": None} if method in DROPPED
                    else {"ok": False, "error": "bad_request"})
        message_id = message["id"]
        if not isinstance(message_id, (str, int)) or isinstance(message_id, bool):
            return {"ok": False, "error": "bad_request"}
        return {"ok": True,
                "message": await self._answer(tenant_id, vendor, method, message_id, message)}

    async def _answer(self, tenant_id: str, vendor: Vendor, method: str,
                      message_id: object, message: dict) -> dict:
        words = {"title": vendor.title, "apex": self._apex}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": message_id, "result": {}}
        if method != "initialize" and method not in FORWARDED:
            return _error(message_id, METHOD_NOT_FOUND, NOT_AVAILABLE.format(method=method))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "tools/call":
            tool = params.get("name")
            if not isinstance(tool, str) or not tool:
                return _error(message_id, INVALID_PARAMS, NO_TOOL_NAME)
            if vendor.is_write(tool):
                _LOG.info("tenant=%s: a write to %s was not run", tenant_id, vendor.name)
                return {"jsonrpc": "2.0", "id": message_id, "result": {
                    "content": [{"type": "text",
                                 "text": WRITE_REFUSED.format(tool=tool, **words)}],
                    "isError": True}}
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                opened = await self._open(
                    tenant_id, vendor,
                    wanted if isinstance(wanted, str) and _PROTOCOL.fullmatch(wanted)
                    else PROTOCOL_VERSION)
                return {"jsonrpc": "2.0", "id": message_id, "result": opened.opened}
            sent = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
            return await self._forward(tenant_id, vendor, sent)
        except NotConnected:
            return _error(message_id, SERVER_ERROR, NOT_CONNECTED.format(**words))
        except ReconnectNeeded:
            self.forget(tenant_id, vendor.name)
            return _error(message_id, SERVER_ERROR, RECONNECT.format(**words))
        except _TooLarge:
            _LOG.info("tenant=%s: %s answered %s with more than %s bytes",
                      tenant_id, vendor.name, method, MAX_ANSWER_BYTES)
            return _error(message_id, SERVER_ERROR, TOO_LARGE.format(**words))
        except (VendorUnreachable, _Refused, aiohttp.ClientError, TimeoutError, OSError,
                ValueError, EOFError) as exc:
            # The exception's own text is left out: aiohttp can quote a
            # request's headers, and one of them is a token.
            _LOG.warning("tenant=%s: %s to %s failed (%s)", tenant_id, method, vendor.name,
                         f"HTTP {exc.status}" if isinstance(exc, _Refused)
                         else type(exc).__name__)
            return _error(message_id, SERVER_ERROR, UNREACHABLE.format(**words))

    # --- the vendor's side --------------------------------------------------

    async def _post(self, vendor: Vendor, token: str, message: dict,
                    session: _Session | None) -> tuple[int, dict | None, str]:
        """One POST to the vendor: (status, the answer to this message or
        None, the session id the vendor named)."""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Accept-Encoding": "gzip, identity"}
        if session is not None:
            headers["MCP-Protocol-Version"] = session.protocol
            if session.session_id:
                headers["Mcp-Session-Id"] = session.session_id
        async with self._http.post(vendor.url, json=message, headers=headers,
                                   timeout=CALL_TIMEOUT, allow_redirects=False) as reply:
            named = reply.headers.get("Mcp-Session-Id", "")
            if reply.status >= 300 or reply.status == 202 or "id" not in message:
                return reply.status, None, named
            return reply.status, await read_answer(reply, message["id"]), named

    async def _send(self, tenant_id: str, vendor: Vendor, message: dict,
                    session: _Session | None) -> tuple[int, dict | None, str]:
        """_post with this person's token, renewed once when the vendor
        answers 401 for it."""
        token = await self._connect.access_token(tenant_id, vendor.name)
        status, answer, named = await self._post(vendor, token, message, session)
        if status == 401:
            token = await self._connect.access_token(tenant_id, vendor.name, refused=token)
            status, answer, named = await self._post(vendor, token, message, session)
        return status, answer, named

    async def _open(self, tenant_id: str, vendor: Vendor,
                    protocol: str = PROTOCOL_VERSION) -> _Session:
        """A new session with the vendor for this person, in place of the one
        kept: `initialize`, then `notifications/initialized`."""
        key = (tenant_id, vendor.name)
        self._sessions.pop(key, None)
        status, answer, named = await self._send(tenant_id, vendor, {
            "jsonrpc": "2.0", "id": "waku-open", "method": "initialize",
            "params": {"protocolVersion": protocol, "capabilities": {},
                       "clientInfo": CLIENT_INFO}}, None)
        result = answer.get("result") if answer is not None else None
        if status >= 300 or not isinstance(result, dict):
            raise _Refused(status)
        agreed = result.get("protocolVersion")
        session = _Session(named, agreed if isinstance(agreed, str)
                           and _PROTOCOL.fullmatch(agreed) else protocol, result)
        status, _, _ = await self._send(tenant_id, vendor, {
            "jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        if status >= 300:
            raise _Refused(status)
        self._sessions[key] = session
        return session

    async def _session(self, tenant_id: str, vendor: Vendor,
                       stale: _Session | None = None) -> _Session:
        """The session kept for this person, or a new one. Calls that all
        find it missing, or all find `stale` dead, open one between them."""
        key = (tenant_id, vendor.name)
        lock = self._opening.setdefault(key, asyncio.Lock())
        async with lock:
            kept = self._sessions.get(key)
            if kept is not None and kept is not stale:
                return kept
            return await self._open(tenant_id, vendor,
                                    stale.protocol if stale else PROTOCOL_VERSION)

    async def _forward(self, tenant_id: str, vendor: Vendor, message: dict) -> dict:
        session = await self._session(tenant_id, vendor)
        status, answer, _ = await self._send(tenant_id, vendor, message, session)
        if status == 404:
            # The vendor no longer knows the session. It refused the call
            # before running it, so sending it again cannot run it twice.
            session = await self._session(tenant_id, vendor, stale=session)
            status, answer, _ = await self._send(tenant_id, vendor, message, session)
        if status >= 300 or answer is None:
            raise _Refused(status)
        return {"jsonrpc": "2.0", "id": message["id"],
                **{key: answer[key] for key in ("result", "error") if key in answer}}
