"""python -m hosted.proxy -- the metering proxy (spec 004 C, spec 001 group D).

ONE HTTP SITE AND ONE SOCKET:

  <WAKU_PROXY_BIND>:<port>  model calls from tenant containers. The bind is
                            the tenant bridge's gateway address (10.88.0.1),
                            the one host address a container can reach.
  run/proxy/proxy.sock      spend reads for the gateway's /account and its turn
                            limit (internal.serve_spend).

It asks run/gateway/gateway.sock who a token belongs to, through TokenCache's
10-second cache, and it is the only process on the VM holding the platform key
-- and, when config/proxy.env carries one, the platform's treg token, behind
the /treg/mcp/ relay on the same site (spec 004 E; hosted/proxy/treg.py).

POST /slack/send, on the same site, takes a container's outgoing Slack message
and hands it to the gateway, which holds the Slack tokens (spec 026 C3;
hosted/proxy/slack.py). No Slack token is ever in this process.
POST /slack/history, beside it, takes a container's request for the recent
messages of the channel its run is in, and the gateway reads them (spec 029 D).

POST /connect/<name>/mcp takes a container's MCP message for a connector the
tenant signed in to, and hands it to the gateway, which holds that sign-in's
tokens (spec 029 B; hosted/proxy/connect.py). No vendor token is ever in this
process either.

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
from hosted.proxy.connect import ConnectCalls
from hosted.proxy.gateway_client import TokenCache
from hosted.proxy.internal import serve_spend
from hosted.proxy.ledger import Ledger
from hosted.proxy.mail import MailDoor
from hosted.proxy.slack import SlackHistory, SlackSend
from hosted.proxy.treg import TregRelay
from hosted.proxy.turns import TurnCharges
from hosted.proxy.upstream import AnthropicUpstream, BedrockUpstream, FallbackUpstream
from hosted.proxy.wallet import WakuMemoryWallet

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
        wallet = WakuMemoryWallet(session, config.memory_api_url)
        turns = TurnCharges()   # per-turn charges for the receipt, in memory only
        treg = None
        if config.treg_token:
            treg = TregRelay(session=session, token=config.treg_token,
                             max_call_usd=config.treg_max_call_usd, resolve=tokens.resolve,
                             memory_key=tokens.memory_key, wallet=wallet, turns=turns)
        # Spec 029 B: always on, for the Slack relay's reason. A gateway with
        # connectors off answers connectors_not_configured.
        connectors = ConnectCalls(resolve=tokens.resolve,
                                  gateway_socket=config.gateway_socket)
        upstream = AnthropicUpstream(session, config.upstream_base_url, config.platform_key)
        if config.bedrock_key:
            # Spec 043: Bedrock answers when Anthropic cannot, or every turn
            # when the operator set WAKU_UPSTREAM_PRIMARY=bedrock.
            upstream = FallbackUpstream(
                upstream, BedrockUpstream(session, config.bedrock_base_url, config.bedrock_key,
                                          config.bedrock_model),
                primary=config.upstream_primary)
        proxy = MeteringProxy(
            config=config, ledger=ledger, resolve=tokens.resolve, upstream=upstream,
            memory_key=tokens.memory_key, wallet=wallet,
            treg=treg.handle if treg is not None else None, turns=turns,
            # Spec 026 C3: always on. A gateway with Slack off answers
            # slack_not_configured, so this proxy has nothing to configure.
            slack=SlackSend(resolve=tokens.resolve,
                            gateway_socket=config.gateway_socket).handle,
            # Spec 029 D: the same, for reading the channel a run is in.
            slack_history=SlackHistory(resolve=tokens.resolve,
                                       gateway_socket=config.gateway_socket).handle,
            connect=connectors.handle, connect_link=connectors.link,
            # Spec 031 A: the same, for the three mail tools. A gateway with
            # no Google client answers that Gmail is not offered.
            mail=MailDoor(resolve=tokens.resolve,
                          gateway_socket=config.gateway_socket).handle)
        runner = web.AppRunner(proxy.build(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.bind_host, config.port).start()
        _LOG.info("metering proxy on %s:%s for %s", config.bind_host, config.port,
                  ", ".join(config.free_models))
        _LOG.info("bedrock %s", f"{config.bedrock_model}, primary {config.upstream_primary}"
                  if config.bedrock_key else "off: config/proxy.env has no WAKU_BEDROCK_KEY")
        _LOG.info("treg relay %s", f"on, at most ${config.treg_max_call_usd:g} a call"
                  if treg is not None else "off: config/proxy.env has no WAKU_TREG_TOKEN")
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            spend_server.close()
            ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
