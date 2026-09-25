"""One JSON object per line, over a Unix socket. Two questions, two answers.

Not aiohttp on purpose: the spec keeps aiohttp in the outermost HTTP layer of
gateway/, proxy/ and spawner/, and these sockets are not that layer. They are
one question each between two processes the platform controls, and the whole
protocol fits on a page.

Who may connect is decided by the filesystem, not by this file: each socket
sits in its own directory, install.sh creates that directory owned by the
peer's group with the setgid bit, and the serving process chmods the socket
right after binding.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from hosted import log

# A peer that never sends a newline must not be able to grow the proxy's
# memory. Both requests are well under a hundred bytes.
#
# This is the longest line the reader will buffer, newline included, and it is
# passed to start_unix_server explicitly rather than left to asyncio's default
# stream limit -- which happens to be the same number today. Checking the
# length again after readline() would be dead code: the reader has already
# refused anything longer, so such a check could never fire, and a guard that
# cannot fire is worse than none, because it reads like the guard.
MAX_LINE = 65536


class Unreachable(OSError):
    """The socket is not there, or the peer did not answer in time.

    The proxy answers 529 overloaded_error on this rather than 401: during an
    upgrade the gateway is briefly gone, and a 401 would read to the SDK, and
    then to the tenant, as a bad key.
    """


HANDLER_FAILED = "the handler failed"


async def _answer(handler: Callable[[dict], Awaitable[dict]], request: dict) -> dict:
    """Never let a raising handler become silence on the wire.

    The only things that raise inside these two handlers are sqlite3 errors:
    the database locked past busy_timeout, a full disk, a corrupt file. Letting
    that close the connection without a line would reach the caller as
    Unreachable, which the proxy answers 529 overloaded_error and the SDK
    retries -- so a gateway with a sick database would answer every tenant with
    an endless retry loop while its own log filled with one traceback per
    request. An error body is one reportable answer instead.

    The body is deliberately the same shape as every other refusal here, and
    carries nothing about what failed: the peer is another service, but the
    request that reached it came from a tenant.
    """
    try:
        return await handler(request)
    except Exception:                          # noqa: BLE001 - answered, not swallowed
        # The wire stays opaque (see this function's docstring); the operator
        # gets the traceback. Before this line, a spawner whose Docker socket
        # had gone away answered every request with "the handler failed" and
        # printed nothing anywhere -- the review that asked for a logging
        # convention named exactly this case.
        log.get(__name__).exception("handler failed for op=%r", request.get("op"))
        return {"error": HANDLER_FAILED}


async def serve(path: Path,
                handler: Callable[[dict], Awaitable[dict]],
                *, mode: int) -> asyncio.Server:
    """Serve one JSON object per line. ONE REQUEST PER CONNECTION: the first
    line is answered and the connection is closed, so a peer that pipelines a
    second gets nothing for it. `ask` opens a fresh connection per call, which
    is what makes that invisible in practice and why it is written down here.
    """
    async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await reader.readline()
            except ValueError:
                # More than MAX_LINE bytes with no newline among them. Drop the
                # peer rather than answer it: there is no request here to
                # refuse, and writing a reply to a flood is work it asked for.
                return
            if not line:
                return
            try:
                request = json.loads(line)
            except ValueError:
                answer = {"error": "not JSON"}
            else:
                answer = (await _answer(handler, request) if isinstance(request, dict)
                          else {"error": "not an object"})
            writer.write(json.dumps(answer).encode("utf-8") + b"\n")
            await writer.drain()
        finally:
            writer.close()

    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(_client, path=str(path), limit=MAX_LINE)
    # Bind first, then chmod: between the two the socket carries the
    # directory's group (install.sh sets setgid) and the process umask, and
    # the directory is already 2750 so nobody outside that group can reach it.
    os.chmod(path, mode)
    return server


async def ask(path: Path, request: dict, *, timeout: float = 2.0) -> dict:
    """Ask one question and return the answer object.

    EVERY failure is Unreachable, including an answer that will not parse. A
    peer killed between its write and its newline leaves a truncated line, and
    a JSONDecodeError escaping from here would land in a caller that is only
    prepared for Unreachable -- which is exactly the case the class was written
    for, since "during an upgrade the gateway is briefly gone" is how a process
    dies mid-write.
    """
    try:
        # limit=MAX_LINE on this side too. The server's bound was made explicit
        # and the client's was left riding asyncio's default, which merely
        # happens to be the same number -- the same implicit coupling, half
        # fixed. With both pinned, MAX_LINE can move and both sides follow.
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=MAX_LINE), timeout)
    except (TimeoutError, OSError) as exc:
        raise Unreachable(f"{path} did not answer") from exc
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (TimeoutError, OSError) as exc:
        raise Unreachable(f"{path} did not answer") from exc
    except ValueError as exc:
        raise Unreachable(f"{path} answered with more than {MAX_LINE} bytes") from exc
    finally:
        writer.close()
    if not line:
        raise Unreachable(f"{path} closed without answering")
    try:
        answer = json.loads(line)
    except ValueError as exc:
        raise Unreachable(f"{path} answered with something that is not JSON") from exc
    if not isinstance(answer, dict):
        raise Unreachable(f"{path} answered with {type(answer).__name__}, not an object")
    return answer
