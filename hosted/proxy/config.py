"""config/proxy.env, read once at startup.

PINNED IN BOTH DIRECTIONS, like the gateway's and the spawner's: every name
here is in hosted/deploy/proxy.env.example and in envfiles.sh's
waku_proxy_env, and nothing else is (evals/deterministic/hosted/
test_proxy_config.py). A misspelt name in the file would otherwise set nothing,
raise nothing, and run the free tier on a default nobody chose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hosted.proxy.prices import PRICES

REQUIRED_ENV_NAMES = (
    "WAKU_PROXY_BIND",
    "WAKU_PROXY_PORT",
    "WAKU_LEDGER_DB",
    "WAKU_GATEWAY_SOCKET",
    "WAKU_PROXY_SOCKET",
    "WAKU_PLATFORM_KEY",
    "WAKU_FREE_MODELS",
    "WAKU_FREE_MONTHLY_CAP_USD",
    "WAKU_FREE_CONCURRENT_CALLS",
    "WAKU_FREE_REQUESTS_PER_MINUTE",
    "WAKU_GLOBAL_CONCURRENT_CALLS",
    "WAKU_MAX_TOKENS_CEILING",
    "WAKU_MAX_BODY_BYTES",
    "WAKU_UPSTREAM_BASE_URL",
    # Spec 004 D: where each settled call is charged in credits.
    "WAKU_MEMORY_API_URL",
    # Spec 004 E: the platform's treg token and the ceiling on one treg call.
    # Both in MAY_BE_ABSENT below.
    "WAKU_TREG_TOKEN",
    "WAKU_TREG_MAX_CALL_USD",
)

# WRITTEN BY install.sh, PINNED LIKE THE REST, AND STILL ALLOWED TO BE ABSENT.
#
# upgrade.sh never writes config/, so a deployment installed before spec 004 E
# has a proxy.env with neither line, and a proxy that refused to start without
# them would take the free tier down on the upgrade that added treg. Absent or
# empty, WAKU_TREG_TOKEN turns the relay off (its route answers 404) and
# WAKU_TREG_MAX_CALL_USD falls back to DEFAULT_TREG_MAX_CALL_USD.
MAY_BE_ABSENT = frozenset({"WAKU_TREG_TOKEN", "WAKU_TREG_MAX_CALL_USD"})
DEFAULT_TREG_MAX_CALL_USD = 0.50
# Waku Memory refuses a single usage report above $10 (POST /agent-usage), so a
# ceiling above it would let a treg call cost more than could ever be charged.
TREG_MAX_CALL_USD_LIMIT = 10.0


@dataclass(frozen=True)
class ProxyConfig:
    bind_host: str
    port: int
    ledger_db: Path
    gateway_socket: Path
    proxy_socket: Path
    platform_key: str
    free_models: tuple[str, ...]
    monthly_cap_usd: float
    concurrent_calls: int
    requests_per_minute: int
    global_concurrent_calls: int
    max_tokens_ceiling: int
    max_body_bytes: int
    upstream_base_url: str
    memory_api_url: str = "https://api.waku.one"
    # repr=False: this is a credential for our treg balance, and a dataclass
    # repr is the kind of thing that ends up in a log line.
    treg_token: str = field(default="", repr=False)
    treg_max_call_usd: float = DEFAULT_TREG_MAX_CALL_USD


def config_from_env(env: Mapping[str, str]) -> ProxyConfig:
    missing = [name for name in REQUIRED_ENV_NAMES
               if name not in MAY_BE_ABSENT and not env.get(name)]
    if missing:
        raise ValueError(f"config/proxy.env is missing {missing}. install.sh writes "
                         "this file; every name in config.REQUIRED_ENV_NAMES must be in it.")
    models = tuple(m.strip() for m in env["WAKU_FREE_MODELS"].split(",") if m.strip())
    unpriced = [m for m in models if m not in PRICES]
    if unpriced:
        # Refused, not charged as zero: an unpriced model is a free tier with no cap.
        raise ValueError(f"WAKU_FREE_MODELS names {unpriced}, which hosted/proxy/prices.py "
                         "does not price. Add a row there or remove the model.")
    ceiling = env.get("WAKU_TREG_MAX_CALL_USD") or str(DEFAULT_TREG_MAX_CALL_USD)
    try:
        treg_max_call_usd = float(ceiling)
    except ValueError:
        treg_max_call_usd = -1.0
    if not 0 < treg_max_call_usd <= TREG_MAX_CALL_USD_LIMIT:
        raise ValueError(f"WAKU_TREG_MAX_CALL_USD is {ceiling!r}: a price in dollars above 0 "
                         f"and at most {TREG_MAX_CALL_USD_LIMIT:g}, such as 0.50.")
    return ProxyConfig(
        bind_host=env["WAKU_PROXY_BIND"],
        port=int(env["WAKU_PROXY_PORT"]),
        ledger_db=Path(env["WAKU_LEDGER_DB"]),
        gateway_socket=Path(env["WAKU_GATEWAY_SOCKET"]),
        proxy_socket=Path(env["WAKU_PROXY_SOCKET"]),
        platform_key=env["WAKU_PLATFORM_KEY"],
        free_models=models,
        monthly_cap_usd=float(env["WAKU_FREE_MONTHLY_CAP_USD"]),
        concurrent_calls=int(env["WAKU_FREE_CONCURRENT_CALLS"]),
        requests_per_minute=int(env["WAKU_FREE_REQUESTS_PER_MINUTE"]),
        global_concurrent_calls=int(env["WAKU_GLOBAL_CONCURRENT_CALLS"]),
        max_tokens_ceiling=int(env["WAKU_MAX_TOKENS_CEILING"]),
        max_body_bytes=int(env["WAKU_MAX_BODY_BYTES"]),
        upstream_base_url=env["WAKU_UPSTREAM_BASE_URL"].rstrip("/"),
        memory_api_url=env["WAKU_MEMORY_API_URL"].rstrip("/"),
        treg_token=env.get("WAKU_TREG_TOKEN", "").strip(),
        treg_max_call_usd=treg_max_call_usd,
    )
