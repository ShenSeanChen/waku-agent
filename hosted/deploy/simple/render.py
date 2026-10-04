"""Render a small, static deployment with one password and container per account.

This deployment is Elastic License 2.0, like the rest of hosted/.
It uses separate Docker networks and directories, without XFS quotas or Supabase.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def render(accounts: list[dict[str, str]]) -> tuple[dict, str]:
    if not accounts:
        raise ValueError("At least one account is required")
    services = {}
    networks = {}
    sites = []
    manual_tls = False
    names = set()
    hosts = set()
    for account in accounts:
        name, host, password_hash = (
            account[key] for key in ("username", "hostname", "password_hash")
        )
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
            raise ValueError("Username must start with a lowercase letter")
        if name in names:
            raise ValueError("Duplicate username")
        names.add(name)
        labels = host.split(".")
        if len(labels) < 2 or len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("Hostname must be a lowercase DNS name")
        if host in hosts:
            raise ValueError("Duplicate hostname")
        hosts.add(host)
        if not re.fullmatch(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}", password_hash):
            raise ValueError("Password hash must be bcrypt")
        tls_mode = account.get("tls", "automatic")
        if tls_mode not in {"automatic", "manual"}:
            raise ValueError("TLS mode must be automatic or manual")
        tls = ""
        if tls_mode == "manual":
            manual_tls = True
            tls = (f"    tls /certificates/{name}/fullchain.pem "
                   f"/certificates/{name}/privkey.pem\n")
        service = f"waku-{name}"
        network = f"account-{name}"
        networks[network] = {}
        services[service] = {
            "image": "waku-simple:local",
            "restart": "unless-stopped",
            "user": "10001:10001",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 128,
            "mem_limit": "512m",
            "cpus": 1.0,
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=128m,mode=1777"],
            "environment": {
                "WAKU_HOME": "/data",
                "WAKU_DASHBOARD_HOST": "0.0.0.0",
                "WAKU_DASHBOARD_PORT": "7777",
                "TZ": "Asia/Shanghai",
                "HOME": "/tmp",
            },
            "volumes": [
                f"./tenants/{name}/home:/data",
                f"./tenants/{name}/env:/work",
            ],
            "networks": [network],
        }
        sites.append(
            f"{host} {{\n"
            f"{tls}"
            f"    basic_auth {{\n        {name} {password_hash}\n    }}\n"
            "    header Cache-Control \"no-store\"\n"
            "    header X-Robots-Tag \"noindex, nofollow\"\n"
            f"    reverse_proxy {service}:7777 {{\n"
            "        header_up -Authorization\n"
            "        flush_interval -1\n    }\n}\n"
        )
    services["caddy"] = {
        "image": "caddy:2.10.2-alpine",
        "restart": "unless-stopped",
        "ports": ["80:80", "443:443"],
        "volumes": [
            "./Caddyfile:/etc/caddy/Caddyfile:ro",
            "caddy-data:/data",
            "caddy-config:/config",
        ],
        "networks": list(networks),
    }
    if manual_tls:
        services["caddy"]["volumes"].append("./tls:/certificates:ro")
    return {
        "name": "waku-simple",
        "services": services,
        "networks": networks,
        "volumes": {"caddy-data": {}, "caddy-config": {}},
    }, "\n".join(sites)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("accounts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compose, caddy = render(json.loads(args.accounts.read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "compose.json").write_text(json.dumps(compose, indent=2) + "\n")
    (args.output / "Caddyfile").write_text(caddy)


if __name__ == "__main__":
    main()
