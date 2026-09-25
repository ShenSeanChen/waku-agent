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

# THE PLATFORM'S OWN KEY, planted in the spawner's environment by the
# `spawner` fixture so that acceptance 2 is an assertion about a value that
# EXISTS somewhere it must not spread from -- not an absence asserted against a
# string nobody ever set, which passes on a typo and passes on a template that
# leaks every variable it has. The spawner is the process that builds every
# container's Env, so its own environment is the right place to plant it.
#
# Not a real key and not a real shape anybody would mistake for one.
PLANTED_PLATFORM_KEY = "sk-ant-PLANTED-PLATFORM-KEY-do-not-ship"

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
        #
        # THIS TENANT'S archive directory, not the shared archive root. The
        # shared root would put every other tenant's archives inside a
        # container running tenant-owned code -- and before GC-1 the archive
        # container was handed exactly that, root-owned at 0755, so it could
        # not write to it at all and every archive and every restore failed.
        allowed.add(str(spawner_root / "archive" / tenant_id))
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


def remove_every_waku_container() -> None:
    """Every container carrying the spawner's kind label, whatever its state.

    The `spawner` fixture's teardown runs BEFORE `bridges`', and a network with
    a live endpoint cannot be removed -- so a tenant container left running by
    any test takes the whole next module down at fixture setup. Removing by
    LABEL rather than by a list of names is what makes this total: the spawner
    names its containers itself, and a test does not know which it created.
    """
    listed = dockerlib._run(
        ["ps", "-aq", "--filter", f"label={template.LABEL_KIND}"],
        timeout=60, check=False).stdout.split()
    for container in listed:
        dockerlib.remove(container)


def remove_network_or_say_why(name: str) -> None:
    """Remove a Docker network, and fail loudly if it is still there.

    `dockerlib.network_remove` is check=False, which is right for "it may not
    exist yet". It is wrong here: a network that will not go is a container
    still attached to it, and the next module's `network_create` then fails
    with "already exists" -- an error about the wrong thing entirely.
    """
    dockerlib.network_remove(name)
    still = dockerlib._run(["network", "ls", "--filter", f"name=^{name}$",
                            "--format", "{{.Name}}"], timeout=60, check=False)
    assert name not in still.stdout.split(), (
        f"the {name} network survived its teardown, which means something is "
        "still attached to it. The next module's network_create will fail "
        "with 'already exists', which is an error about the wrong thing. "
        "Containers still present: "
        + dockerlib._run(["ps", "-a", "--filter", f"network={name}",
                          "--format", "{{.Names}}"],
                         timeout=60, check=False).stdout.strip())
