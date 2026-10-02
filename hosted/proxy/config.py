"""config/proxy.env, read once at startup.

PINNED IN BOTH DIRECTIONS, like the gateway's and the spawner's: every name
here is in hosted/deploy/proxy.env.example and in envfiles.sh's
waku_proxy_env, and nothing else is (evals/deterministic/hosted/
test_proxy_config.py). A misspelt name in the file would otherwise set nothing,
raise nothing, and run the free tier on a default nobody chose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
)


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


def config_from_env(env: Mapping[str, str]) -> ProxyConfig:
    missing = [name for name in REQUIRED_ENV_NAMES if not env.get(name)]
    if missing:
        raise ValueError(f"config/proxy.env is missing {missing}. install.sh writes "
                         "this file; every name in config.REQUIRED_ENV_NAMES must be in it.")
    models = tuple(m.strip() for m in env["WAKU_FREE_MODELS"].split(",") if m.strip())
    unpriced = [m for m in models if m not in PRICES]
    if unpriced:
        # Refused, not charged as zero: an unpriced model is a free tier with no cap.
        raise ValueError(f"WAKU_FREE_MODELS names {unpriced}, which hosted/proxy/prices.py "
                         "does not price. Add a row there or remove the model.")
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
    )
