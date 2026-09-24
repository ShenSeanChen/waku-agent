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


async def serve(path: Path,
                handler: Callable[[dict], Awaitable[dict]],
                *, mode: int) -> asyncio.Server:
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
                answer = (await handler(request) if isinstance(request, dict)
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
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout)
    except (TimeoutError, OSError) as exc:
        raise Unreachable(f"{path} did not answer") from exc
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (TimeoutError, OSError) as exc:
        raise Unreachable(f"{path} did not answer") from exc
    finally:
        writer.close()
    if not line:
        raise Unreachable(f"{path} closed without answering")
    answer = json.loads(line)
    if not isinstance(answer, dict):
        raise Unreachable(f"{path} answered with {type(answer).__name__}, not an object")
    return answer
