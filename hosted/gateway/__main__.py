"""python -m hosted.gateway.

THE ONLY PLACE THE WHOLE THING IS WIRED. Four sockets and one HTTP site:

  127.0.0.1:<port>          the dashboard traffic, behind Caddy. The spec says
                            "listens on 127.0.0.1 only", and that is not a
                            default -- it is the reason the gateway is not on
                            the tenant bridge and a container cannot reach it.
  run/gateway/gateway.sock  token lookup for the proxy (group B's
                            serve_token_lookup). NOTHING CONNECTS TO IT UNTIL
                            GROUP D LANDS, and it is served anyway: F1's
                            Compose mounts the directory, and a socket that
                            appears only when its client does is a socket
                            nobody notices is missing.
  run/admin/admin.sock      tenant.sh, upgrade.sh --now and restore.sh.
  run/proxy/proxy.sock      NOT served here -- the gateway is its CLIENT.
                            With group D cut there is nothing on the other
                            end, read_spend answers None, and every tenant is
                            on free's turn limit. That is the spec's defined
                            state, not a failure mode.

NO ACCESS LOG, AND SILENCING IT TAKES A LINE. aiohttp's default access format
is `%a %t "%r" %s %b ...`, and `%r` is the raw request line -- query string
included. GET /auth/enter?code=<43 characters> would sit in the operator's log
file as a live hand-off for sixty seconds, which is the one credential this
deployment puts in a URL. hosted/gateway/app.py's _enter says the same thing
at the other end, and E1 handed the warning forward as "do not enable it".

Leaving it alone is NOT enough, which a smoke run of this module showed: the
default access logger propagates to the root logger, log.configure() sets the
root to INFO, and every request was written with its query. So build_runner
passes access_log=None, and test_gateway.py drives both runners and reads the
difference. If an access log is ever wanted, it needs a format that drops the
query, not a default that keeps it.
"""

from __future__ import annotations

import asyncio
import os
import time

import aiohttp
from aiohttp import web

from hosted import log
from hosted.core import idle, quota
from hosted.gateway import admin, internal
from hosted.gateway.app import Gateway
from hosted.gateway.config import config_from_env
from hosted.gateway.forward import ContainerForwarder
from hosted.gateway.identity import JwksVerifier
from hosted.gateway.launch import Launcher
from hosted.gateway.spawner_client import SpawnerClient
from hosted.gateway.store import ControlDb

_LOG = log.get(__name__)


def build_runner(app: web.Application) -> web.AppRunner:
    """The gateway's AppRunner, with the access log off.

    A named function and not a keyword argument buried in main(), because it
    is the one line in this module a test can drive: main() runs until the
    process is killed, and the thing worth pinning is which runner the process
    builds. See this module's docstring for what the default writes.
    """
    return web.AppRunner(app, access_log=None)


async def main() -> None:
    log.configure()
    config = config_from_env(os.environ)
    plans = quota.plans_from_env(os.environ)
    store = ControlDb(config.control_db)
    spawner = SpawnerClient(config.spawner_socket)
    fleet = idle.Fleet(time.time, config.max_running)
    launcher = Launcher(store=store, spawner=spawner, fleet=fleet)
    verifier = JwksVerifier(jwks_url=config.supabase_jwks_url,
                            issuer=config.supabase_issuer,
                            audience=config.supabase_audience)
    turns = quota.TurnWindow(time.time)
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        forwarder = ContainerForwarder(
            launcher=launcher, turns=turns, plans=plans,
            proxy_socket=config.proxy_socket, session=session)
        gateway = Gateway(config=config, store=store, launcher=launcher,
                          verifier=verifier, forward=forwarder, turns=turns,
                          plans=plans)
        # Adopt what is already running before the first request arrives, so
        # no container is orphaned and every idle timer starts fresh.
        try:
            adopted = await launcher.resync()
            _LOG.info("adopted %s running tenant containers", len(adopted))
        except OSError as exc:
            _LOG.warning("the spawner did not answer at startup: %s", exc)
        token_server = await internal.serve_token_lookup(config.gateway_socket, store)
        admin_server = await admin.serve_admin(config.admin_socket, gateway)
        runner = build_runner(gateway.build())
        await runner.setup()
        site = web.TCPSite(runner, config.bind_host, config.port)
        await site.start()
        _LOG.info("gateway listening on %s:%s for %s",
                  config.bind_host, config.port, config.apex_host)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            for server in (token_server, admin_server):
                server.close()
                await server.wait_closed()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
