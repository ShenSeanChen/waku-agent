"""run/proxy/proxy.sock -- spend reads, and nothing else.

The gateway renders /account from this. When the proxy cannot answer, the
gateway applies free's turn limit and /account says "Spend is unavailable
right now." -- it never guesses a number.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from hosted import jsonsock
from hosted.core.quota import utc_month
from hosted.proxy.ledger import Ledger

PROXY_SOCKET_MODE = 0o660

# The whole request, field for field, the way core.requests does it for the
# spawner. An extra field is a caller that thinks this socket takes something
# it does not -- a `month`, say -- and answering it anyway teaches that it does.
SPEND_FIELDS = frozenset({"op", "tenant"})


async def serve_spend(path: Path, ledger: Ledger,
                      now: Callable[[], float] = time.time) -> asyncio.Server:
    async def handler(request: dict) -> dict:
        # One allowed question, named here; everything else refused. This
        # socket reads, and a reserve or a settle arriving on it is a bug in
        # the caller, not a feature to add.
        if request.get("op") != "spend":
            return {"error": "this socket answers one question: op=spend"}
        unknown = sorted(set(request) - SPEND_FIELDS)
        if unknown:
            return {"error": f"spend does not take {unknown}"}
        tenant_id = request.get("tenant")
        if not isinstance(tenant_id, str):
            return {"error": "tenant must be a string"}
        at = now()
        month = utc_month(at)
        settled, reserved = ledger.spend(tenant_id, month)
        return {"month": month, "settled": settled, "reserved": reserved,
                "last_platform_call": ledger.last_platform_call(tenant_id)}

    return await jsonsock.serve(path, handler, mode=PROXY_SOCKET_MODE)
