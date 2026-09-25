"""What every C2 Docker test needs, in one place.

THE BRIEF PUT THESE IN test_spawner.py AND THE OTHER TWO FILES IMPORTED THEM BY
NAME. That does not survive `ruff check`: importing a pytest fixture into a
module that also names it as a test argument is F811, seventeen times over. The
brief anticipated the move -- "put the three in dockerlib.py's sibling
spawnerlib.py if a second file starts redefining them" -- and two files do.

So the split is: the FIXTURES (`bridges`, `spawner`, `spawner_root`) live in
conftest.py, where pytest shares them without an import; the CONSTANTS and the
plain helpers live here and are imported as a module. Nothing is defined twice.
"""

from __future__ import annotations

import asyncio
import threading
import time

import dockerlib

from hosted import jsonsock
from hosted.core.tenant import FIRST_PROJECT_ID, tenant_dirs
from hosted.spawner import template

SPAWNER_CONTAINER = "waku-spawner-test"

TENANT_A = "aaaaaaaaaaaa"
TENANT_B = "bbbbbbbbbbbb"
PROJECT_A = FIRST_PROJECT_ID
PROJECT_B = FIRST_PROJECT_ID + 1
TOKEN_ONE = "a" * 43
TOKEN_TWO = "b" * 43

TENANT_TAG = "waku-tenant:test"
SERVICES_TAG = "waku-services:test"

# The disk limit the spawner fixture configures, which is the number
# test_a_tenant_cannot_write_past_their_disk_limit asserts against. It is NOT
# F1's 1 GB production default: the assertion is that THE CONFIGURED LIMIT IS
# THE ONE THAT BOUND, and a 1 GB test would take a minute of dd to reach.
TEST_DISK_BYTES = 64 * 1024 * 1024


def ask(spawner, payload: dict) -> dict:
    """The same client the gateway will use: one JSON object per line."""
    return asyncio.run(jsonsock.ask(spawner, payload, timeout=180))


def allowed_bind_sources(spawner_root, tenant_id: str, task: str) -> set[str]:
    """DEFAULT-DENY: the exact set of host paths a task container may mount.

    Built from the tenant's own directories and the two known shared roots, so
    an unexpected mount fails because it is NOT ON THE LIST -- not because it
    failed to contain one of three substrings.

    An earlier draft asserted

        TENANT_A in source or "/staging" in source or "/archive" in source

    which is three alternatives OR'd together, each a substring test on a path
    this service mounts as root with CAP_SYS_ADMIN. Any bind whose path
    contains "/staging" anywhere passed, another tenant's staging included, and
    `TENANT_A in source` passed on any path carrying that id as a substring.
    That is a denylist wearing an assertion's clothes, in the one place in this
    group where default-deny matters most. Everything else here whitelists;
    so does this now.
    """
    dirs = tenant_dirs(spawner_root / "tenants", tenant_id)
    allowed = {str(dirs.home), str(dirs.env)}
    if task in ("backup", "restore"):
        allowed.add(str(spawner_root / "staging" / tenant_id))
    if task in ("archive", "restore"):
        # restore packs the old tree away before it recreates the directories,
        # so its operation legitimately runs an archive container too.
        allowed.add(str(spawner_root / "archive"))
    return allowed


def capture_task_containers(spawner, payload: dict, tenant_id: str) -> list[dict]:
    """Run an operation and return `docker inspect` for EVERY container it
    created for this tenant, except the tenant's own dashboard.

    Every kind, not just KIND_TASK: a restore runs an archive container and a
    provision container on its way, and each of them mounts a tenant's data.
    Filtering to one kind would have left two of the three unexamined.

    These containers are short-lived, so this polls `docker ps -a` by the
    tenant label from a thread while the request runs, and RAISES if it never
    saw one -- "the operation made no container" must not read as "the
    operation made a correct container".
    """
    seen: list[str] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            out = dockerlib._run(
                ["ps", "-a", "--filter", f"label={template.LABEL_TENANT}={tenant_id}",
                 "--format", '{{.ID}} {{.Label "waku.kind"}}'],
                timeout=30, check=False).stdout.splitlines()
            for line in out:
                parts = line.split()
                if len(parts) == 2 and parts[1] != template.KIND_TENANT \
                        and parts[0] not in seen:
                    seen.append(parts[0])
            time.sleep(0.2)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        answer = ask(spawner, payload)
        assert "error" not in answer, answer
    finally:
        stop.set()
        thread.join(timeout=5)
    assert seen, (
        f"{payload.get('task', payload['op'])} created no labelled container. "
        "This is not a pass: every assertion below would be about a container "
        "that never existed.")
    return [dockerlib.inspect(cid) for cid in seen]
