"""run/admin/admin.sock, and the thin command that talks to it.

EVERY CHANGE TO RUNTIME STATE GOES THROUGH THE RUNNING GATEWAY (spec, "How the
services run"). Session caches, container addresses, token issue and requests
in flight live in this process's memory, so tenant.sh, upgrade.sh --now and
restore.sh reach the spawner through here rather than around it.

AN ALLOWLIST, THE SAME SHAPE AS hosted/core/requests.py: the operation and the
exact key set of each. The socket is 0600 in a 0700 directory owned by the
gateway's own user, so this is not a guard against a hostile caller -- it is
the line that keeps a typo in tenant.sh from becoming a disabled tenant
nobody meant to disable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from hosted import jsonsock, log
from hosted.core.tenant import is_tenant_id
from hosted.gateway.app import Gateway
from hosted.gateway.launch import InMaintenance, NotActive, StartFailed
from hosted.gateway.spawner_client import SpawnerError
from hosted.ports.control import Tenant

_LOG = log.get(__name__)

ADMIN_SOCKET_MODE = 0o600

ADMIN_OPS = frozenset({"status", "disable", "enable", "delete", "restart-all",
                       "stop-all", "resolve", "backup", "restore", "inspect",
                       "inspect-stop"})
_NO_TENANT = frozenset({"op"})
_ONE_TENANT = frozenset({"op", "tenant"})
ADMIN_KEYS: dict[str, frozenset[str]] = {
    "status": _NO_TENANT,
    "restart-all": _NO_TENANT,
    "stop-all": _NO_TENANT,
    "disable": _ONE_TENANT,
    "enable": _ONE_TENANT,
    "delete": _ONE_TENANT,
    "resolve": _ONE_TENANT,
    "backup": _ONE_TENANT,
    "restore": _ONE_TENANT,
    "inspect": _ONE_TENANT,
    "inspect-stop": _ONE_TENANT,
}
# The spawner task each verb runs. A table and not a parameter: `task` on the
# spawner takes one of five names, and a verb that could name any of them is a
# remote shell into the most privileged process in the deployment.
_TASKS = {"backup": "backup", "restore": "restore",
          "inspect": "inspect", "inspect-stop": "inspect-stop"}

DEFAULT_ADMIN_SOCKET = "/srv/waku/run/admin/admin.sock"
# A restore or a backup copies a whole home directory, which is not a
# two-second answer; jsonsock.ask defaults to two seconds.
ASK_TIMEOUT_SECONDS = 900.0


def _find(gateway: Gateway, needle: str) -> Tenant | None:
    if is_tenant_id(needle):
        return gateway.store.tenant_by_id(needle)
    return gateway.store.tenant_by_email(needle)


async def handle(gateway: Gateway, payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"error": "a request is a JSON object"}
    op = payload.get("op")
    if op not in ADMIN_OPS:
        return {"error": f"op must be one of {sorted(ADMIN_OPS)}, not {op!r}"}
    unknown = sorted(set(payload) - ADMIN_KEYS[op])
    if unknown:
        return {"error": f"{op} does not take {unknown}"}
    if op == "status":
        return {"running": sorted(gateway.launcher.fleet.running())}
    if op == "restart-all":
        return {"restarted": await gateway.launcher.restart_all()}
    if op == "stop-all":
        return {"stopped": await _stop_all(gateway)}
    needle = payload.get("tenant")
    if not isinstance(needle, str) or not needle:
        return {"error": f"{op} needs a tenant id or email"}
    tenant = _find(gateway, needle)
    if tenant is None:
        return {"error": f"no tenant matches {needle!r}"}
    if op == "resolve":
        # READ-ONLY, AND IT EXISTS SO restore.sh DOES NOT NEED ITS OWN SQL.
        # `_find` is `tenant_by_id` or `tenant_by_email`, and the second pins
        # its answer with `ORDER BY created_at, id LIMIT 1` because an address
        # is not unique -- a contract the rest of the platform is tested
        # against. A shell copy of that query is a second copy of a contract,
        # and it had already drifted: no ORDER BY, no LIMIT, so an operator
        # whose address is on two rows got two lines back in the middle of a
        # disaster. One implementation, one answer, no string literal built by
        # concatenation in the most destructive script in the deployment.
        return {"ok": True, "tenant": tenant.id}
    try:
        return await _act(gateway, op, tenant)
    except (SpawnerError, NotActive, InMaintenance, StartFailed, OSError) as exc:
        _LOG.warning("admin %s on tenant=%s failed: %s", op, tenant.id, exc)
        return {"error": str(exc)}


async def _stop_all(gateway: Gateway) -> list[str]:
    """Stop every running tenant container, disabling nobody.

    restore.sh --all needs this and no existing verb does it: `restart-all`
    restarts, `disable` changes a tenant's status, and `status` reads. A
    container that survives into a restored control.db holds a proxy token
    that database may not know, and the gateway would adopt it at startup and
    forward to it. It is worse than a stale token: its bind mount is inside
    the tree the restore is about to remove, which is the dead-inode failure
    designs/backup-restore-integrity.md records -- a restore deleted a running
    container's directories and the next `docker exec` failed with "possible
    container breakout detected".

    THE LIST IS THE UNION OF WHAT THE SPAWNER HAS AND WHAT THE FLEET
    BELIEVES, because neither alone is every container.

    IT ASKS `running_tenant_ids`, NOT `resync`, AND THAT IS THE WHOLE
    CORRECTION. `resync` and the spawner's `list` answer "may the gateway
    forward to this container?", so they drop one at an address its project id
    does not derive and one whose tenant control.db does not know. Stopping
    asks "is this container ours?", which is strictly wider -- and every
    container in the difference is exactly the one whose bind mount the
    restore is about to delete. Built on `resync`'s filter, this verb was
    structurally unable to stop the containers it exists for.

    `resync` stays read-and-believe: it runs at gateway startup and on every
    refused connection, and making a consistency function destructive is a
    category change with its own blast radius.

    The fleet is the other half and not a subset of the first: it also reports
    STARTING, a container the spawner cannot name yet because it is still
    being created.

    No new Launcher method beyond the one door `running_tenant_ids` opens;
    `stop` and `fleet.forget` are calls `_act` already makes.

    WHAT THIS CANNOT CLOSE, said out loud: a sign-in that lands after this
    answered pre-warms a new container. restore.sh stops the gateway on the
    next line and asks again once it is back, so the window is narrow, but
    nothing inside the gateway can make it zero.
    """
    running = set(gateway.launcher.fleet.running())
    running.update(await gateway.launcher.running_tenant_ids())
    stopped = []
    for tenant_id in sorted(running):
        await gateway.launcher.stop(tenant_id)
        gateway.launcher.fleet.forget(tenant_id)
        stopped.append(tenant_id)
    return stopped


async def _act(gateway: Gateway, op: str, tenant: Tenant) -> dict:
    if op == "enable":
        gateway.store.set_status(tenant.id, "active")
        return {"ok": True, "tenant": tenant.id}
    if op in ("disable", "delete"):
        # Status FIRST: from this line on, a request that slips past the
        # session cache still fails tenant_by_id's status check, and start()
        # refuses to issue a token. Then the sessions, then the token and the
        # container, which launcher.stop does in that order.
        gateway.store.set_status(tenant.id, "disabled")
        gateway.end_sessions(tenant.id)
        gateway.turns.forget(tenant.id)
        await gateway.launcher.stop(tenant.id)
        gateway.launcher.fleet.forget(tenant.id)
        if op == "disable":
            return {"ok": True, "tenant": tenant.id}
        # archive BEFORE the row goes: the archive task needs the project id,
        # and delete_tenant retires it.
        archived = await gateway.launcher.spawner_task(tenant.id, "archive",
                                                       project_id=tenant.project_id)
        gateway.store.delete_tenant(tenant.id)
        gateway.launcher.forget_tenant(tenant.id)
        return {"ok": True, "tenant": tenant.id, "archive": archived.get("path", "")}
    task = _TASKS[op]
    gateway.launcher.mark_maintenance(tenant.id)
    try:
        if task in ("restore", "inspect"):
            await gateway.launcher.stop(tenant.id)
        answer = await gateway.launcher.spawner_task(
            tenant.id, task, project_id=tenant.project_id)
    finally:
        # inspect leaves the mark in place until inspect-stop, so two
        # dashboards never run on one state.db (spec, "Maintenance").
        if task != "inspect":
            gateway.launcher.clear_maintenance(tenant.id)
    return {"ok": True, "tenant": tenant.id, **answer}


async def serve_admin(path: Path, gateway: Gateway) -> asyncio.Server:
    path.parent.mkdir(parents=True, exist_ok=True)
    return await jsonsock.serve(path, lambda payload: handle(gateway, payload),
                                mode=ADMIN_SOCKET_MODE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hosted.gateway.admin")
    parser.add_argument("op", choices=sorted(ADMIN_OPS))
    parser.add_argument("tenant", nargs="?", default=None,
                        help="a tenant id or an email address")
    parser.add_argument("--socket", default=DEFAULT_ADMIN_SOCKET)
    args = parser.parse_args(argv)
    request: dict = {"op": args.op}
    if args.tenant:
        request["tenant"] = args.tenant
    try:
        answer = asyncio.run(jsonsock.ask(Path(args.socket), request,
                                          timeout=ASK_TIMEOUT_SECONDS))
    except jsonsock.Unreachable as exc:
        sys.stderr.write(f"the gateway did not answer: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(answer) + "\n")
    return 1 if "error" in answer else 0


if __name__ == "__main__":
    raise SystemExit(main())
