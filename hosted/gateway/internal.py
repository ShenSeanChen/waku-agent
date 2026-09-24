"""run/gateway/gateway.sock -- token lookup, and nothing else.

The proxy is the one service tenant code can reach directly, so it gets no
access to control.db. It sends a SHA-256 hash and gets back a tenant id and a
status. It cannot enumerate tenants, read a session, or issue anything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hosted import jsonsock
from hosted.ports.control import ControlStore

GATEWAY_SOCKET_MODE = 0o660


async def serve_token_lookup(path: Path, store: ControlStore) -> asyncio.Server:
    async def handler(request: dict) -> dict:
        # One allowed question, named here; everything else refused. A list of
        # forbidden ops would need editing every time the store grows a method.
        if request.get("op") != "token":
            return {"error": "this socket answers one question: op=token"}
        digest = request.get("hash")
        if not isinstance(digest, str) or len(digest) != 64:
            return {"tenant": None}
        found = store.tenant_for_token_hash(digest)
        if found is None:
            return {"tenant": None}
        tenant_id, status = found
        return {"tenant": tenant_id, "status": status}

    return await jsonsock.serve(path, handler, mode=GATEWAY_SOCKET_MODE)
