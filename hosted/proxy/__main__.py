"""python -m hosted.proxy -- the metering proxy (spec 004 C, spec 001 group D).

ONE HTTP SITE AND ONE SOCKET:

  <WAKU_PROXY_BIND>:<port>  model calls from tenant containers. The bind is
                            the tenant bridge's gateway address (10.88.0.1),
                            the one host address a container can reach.
  run/proxy/proxy.sock      spend reads for the gateway's /account and its turn
                            limit (internal.serve_spend).

It asks run/gateway/gateway.sock who a token belongs to, through TokenCache's
10-second cache, and it is the only process on the VM holding the platform key.

BEFORE IT SERVES, it settles every reservation a previous run left behind at
its full amount: the upstream call each was holding may already have been
billed (spec 001, step 10).

NO ACCESS LOG, for the gateway's reason: nothing here should write a tenant's
token, or anything else from a request line, to the operator's log file.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp
from aiohttp import web

from hosted import log
from hosted.proxy.app import MeteringProxy
from hosted.proxy.config import config_from_env
from hosted.proxy.gateway_client import TokenCache
from hosted.proxy.internal import serve_spend
from hosted.proxy.ledger import Ledger
from hosted.proxy.upstream import AnthropicUpstream

_LOG = log.get(__name__)


async def main() -> None:
    log.configure()
    config = config_from_env(os.environ)
    ledger = Ledger(config.ledger_db)
    moved = ledger.settle_leftovers()
    if moved:
        _LOG.warning("settled %.6f dollars of reservations a previous run left behind", moved)
    tokens = TokenCache(config.gateway_socket)
    spend_server = await serve_spend(config.proxy_socket, ledger)
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        proxy = MeteringProxy(
            config=config, ledger=ledger, resolve=tokens.resolve,
            upstream=AnthropicUpstream(session, config.upstream_base_url, config.platform_key))
        runner = web.AppRunner(proxy.build(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.bind_host, config.port).start()
        _LOG.info("metering proxy on %s:%s for %s", config.bind_host, config.port,
                  ", ".join(config.free_models))
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            spend_server.close()
            ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
