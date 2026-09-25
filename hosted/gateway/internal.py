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

# The whole request, field for field, the way core.requests does it for the
# spawner. An extra field is a caller that thinks this socket takes something
# it does not, and answering it anyway teaches that it does.
TOKEN_FIELDS = frozenset({"op", "hash"})


async def serve_token_lookup(path: Path, store: ControlStore) -> asyncio.Server:
    async def handler(request: dict) -> dict:
        # One allowed question, named here; everything else refused. A list of
        # forbidden ops would need editing every time the store grows a method.
        if request.get("op") != "token":
            return {"error": "this socket answers one question: op=token"}
        unknown = sorted(set(request) - TOKEN_FIELDS)
        if unknown:
            return {"error": f"token does not take {unknown}"}
        digest = request.get("hash")
        if not isinstance(digest, str) or len(digest) != 64:
            return {"tenant": None}
        found = store.tenant_for_token_hash(digest)
        if found is None:
            return {"tenant": None}
        tenant_id, status = found
        return {"tenant": tenant_id, "status": status}

    # The directory, the way admin.serve_admin does it. F1's Compose mounts
    # run/gateway/ so the deployment never needs this -- but without it the
    # first person to run `python -m hosted.gateway` by hand gets a bare
    # OSError out of asyncio.start_unix_server, pointing at the socket layer
    # rather than at the missing directory. The two socket servers in this
    # package should not differ on it.
    path.parent.mkdir(parents=True, exist_ok=True)
    return await jsonsock.serve(path, handler, mode=GATEWAY_SOCKET_MODE)
