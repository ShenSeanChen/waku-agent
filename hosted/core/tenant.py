"""Tenant identity: ids, project ids, fixed addresses, directories, zones.

Pure logic. A tenant id is three things at once -- a database key, a directory
name and a DNS label -- which is why it is twelve characters of lowercase
base32 and nothing else: no upper case (DNS is case-insensitive), no
underscore (not legal in a hostname), and none of 0, 1, 8 or 9 (base32's
alphabet leaves them out so nobody reads l as 1).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
TENANT_ID_RE = re.compile(r"^[a-z2-7]{12}$")

# secrets.token_urlsafe(32) is 43 characters of the URL-safe base64 alphabet.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

STATUSES = frozenset({"active", "disabled", "deleted"})

# The tenant bridge. install.sh creates it with this subnet before any service
# starts, because the proxy binds the gateway address. 10.88/16 on purpose:
# 172.31/16 is AWS's default VPC CIDR, and Docker's own default pool starts at
# 172.17/16, so both would collide on somebody's VM.
TENANT_SUBNET = ipaddress.ip_network("10.88.0.0/16")
TENANT_GATEWAY = ipaddress.ip_address("10.88.0.1")
# Docker's IPAM IPRange for dynamic allocation: the subnet's last /24. Only
# tenant containers join this bridge and they all take a fixed address, so
# nothing is allocated from here -- it exists so Docker never picks an address
# a tenant owns.
DYNAMIC_RANGE = ipaddress.ip_network("10.88.255.0/24")

# XFS reserves project id 0 for "no project", so ids start at 2 and their
# addresses start one past the bridge gateway. 65279 is 0xFEFF: 10.88.254.255,
# the last address before DYNAMIC_RANGE. About 65,000 tenants on one VM.
FIRST_PROJECT_ID = 2
LAST_PROJECT_ID = 65279


@dataclass(frozen=True)
class TenantDirs:
    """The only two host paths a tenant's container ever sees."""

    home: Path   # mounted at /data -- WAKU_HOME: state.db, SOUL.md, skills/
    env: Path    # mounted at /work -- the working directory, holding .env


def new_tenant_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(12))


def is_tenant_id(value: object) -> bool:
    return isinstance(value, str) and TENANT_ID_RE.match(value) is not None


def new_proxy_token() -> str:
    return secrets.token_urlsafe(32)


def is_proxy_token(value: object) -> bool:
    return isinstance(value, str) and TOKEN_RE.match(value) is not None


def token_hash(token: str) -> str:
    """control.db stores this, never the plaintext. The plaintext exists in
    one container's environment and in the request that put it there."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tenant_dirs(root: Path, tenant_id: str) -> TenantDirs:
    if not is_tenant_id(tenant_id):
        raise ValueError(f"not a tenant id: {tenant_id!r}")
    base = root / tenant_id
    return TenantDirs(home=base / "home", env=base / "env")


def is_project_id(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return FIRST_PROJECT_ID <= value <= LAST_PROJECT_ID


def next_project_id(used: Iterable[int]) -> int:
    """Monotonic, never reusing a freed id. A directory that is moved or
    deleted keeps its XFS project id, so a reused id would bill a new tenant
    for whatever the old one left behind."""
    highest = max((p for p in used if isinstance(p, int)), default=FIRST_PROJECT_ID - 1)
    candidate = highest + 1
    if not is_project_id(candidate):
        raise ValueError(
            f"no project id left: {candidate} is past {LAST_PROJECT_ID}. "
            "One VM holds about 65,000 tenants; this one is full.")
    return candidate


def address_for_project(project_id: int,
                        subnet: ipaddress.IPv4Network = TENANT_SUBNET) -> str:
    """The container's fixed address on the tenant bridge, every start.

    A stale address in the gateway's memory can then only reach nothing or the
    same tenant, never another tenant's unauthenticated dashboard.
    """
    if not is_project_id(project_id):
        raise ValueError(f"not a project id: {project_id!r}")
    return str(subnet.network_address + project_id)


def is_known_timezone(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except Exception:
        return False
    return True


def normalise_timezone(name: object) -> str:
    """A zone comes from the browser or from /account, so it is tenant input,
    and it ends up as TZ in a container's environment. Anything Python's
    zoneinfo does not know becomes UTC, and /account says so."""
    return name if is_known_timezone(name) else "UTC"
