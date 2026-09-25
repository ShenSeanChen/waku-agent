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
import json
import subprocess
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


def ask_ok(spawner, payload: dict) -> dict:
    """Ask, and refuse to carry on if the spawner said no.

    THE SETUP CALL THAT IGNORES ITS ANSWER IS THE ONE THAT COSTS THE MOST. In
    run 1 a `start` used as setup dropped its answer; the start did not happen;
    and the failure surfaced two calls later as an OCI exec error against
    whatever container still held the name. The log said "possible container
    breakout detected" about a start that never ran.

    Every call whose answer the test does not examine goes through this. Calls
    that are ABOUT the answer -- a busy refusal, a bad request -- keep using
    `ask` directly, because for those the error IS the assertion.
    """
    answer = ask(spawner, payload)
    assert "error" not in answer, (
        f"{payload.get('op')} {payload.get('task', '')} was refused: {answer}. "
        "This call is setup for the assertions below, so the test stops here "
        "rather than measuring a state that was never reached.")
    return answer


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

    Every kind, not just KIND_TASK: a restore runs an archive container, an
    empty container and a provision container on its way, and each of them
    mounts a tenant's data. Filtering to one kind would leave three of four
    unexamined.

    IT READS `docker events`, NOT `docker ps`, AND IT INSPECTS ON SIGHT.
    The first version polled `docker ps -a` every 200 ms and inspected the ids
    afterwards. The spawner's throwaway containers are created, run and removed
    inside one `_run_to_completion`, so that lost both ways, and run 1 showed
    both: `archive` and `inspect` were missed entirely ("created no labelled
    container"), and `backup` and `restore` were seen and then inspected after
    removal ("Error: No such object").

    `docker events --since <before the request>` is REPLAYED by the daemon from
    that timestamp, so detection cannot miss a container however short-lived it
    was -- the reader starting late is fine. Inspection still races the
    spawner's own removal, because the spawner removes what it makes and
    nothing here can hold it open; so each id is inspected the instant its
    create event arrives, and an id that loses that race is reported AS a lost
    race rather than as a bare DockerError from somewhere else.
    """
    since = f"{time.time():.3f}"
    seen: dict[str, dict] = {}
    lost: list[str] = []
    stop = threading.Event()

    events = subprocess.Popen(
        ["docker", "events", "--since", since,
         "--filter", f"label={template.LABEL_TENANT}={tenant_id}",
         "--filter", "event=create", "--format", "{{json .}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def watch():
        for line in events.stdout:               # blocks; closed by terminate()
            if stop.is_set():
                return
            try:
                event = json.loads(line)
            except ValueError:
                continue
            container = event.get("id") or event.get("Actor", {}).get("ID", "")
            kind = event.get("Actor", {}).get("Attributes", {}).get(template.LABEL_KIND)
            if not container or kind == template.KIND_TENANT or container in seen:
                continue
            try:
                seen[container] = dockerlib.inspect(container)
            except dockerlib.DockerError:
                # Removed between its create event and this inspect. Recorded,
                # not swallowed: "I saw it and could not read it" and "there
                # was nothing" are different answers.
                lost.append(container)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        answer = ask(spawner, payload)
        assert "error" not in answer, answer
    finally:
        # Give the stream a moment to deliver events for containers created
        # just before the answer came back, then close it.
        time.sleep(0.5)
        stop.set()
        events.terminate()
        thread.join(timeout=10)
        events.wait(timeout=10)

    what = payload.get("task", payload["op"])
    assert seen or lost, (
        f"{what} created no labelled container. This is not a pass: every "
        "assertion below would be about a container that never existed.")
    assert not lost, (
        f"{what} created {len(lost)} container(s) that were removed before "
        f"they could be inspected: {lost}. The spawner removes its own "
        "throwaway containers, so this is a lost race and not a missing "
        f"container -- {len(seen)} other(s) were read successfully.")
    return list(seen.values())


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


def wait_until_the_spawner_answers(socket_path, *, timeout: float = 60.0) -> None:
    """Block until the spawner ANSWERS, not until its socket file exists.

    THE FILE IS NOT THE SERVICE. `<root>/run/spawner/spawner.sock` is on a bind
    mount under the shared root, so it outlives the container that made it: the
    previous module's socket file is sitting there, with nothing behind it,
    before the new spawner has finished importing. An `exists()` check passes
    on that file immediately and hands the tests a socket that answers
    ECONNREFUSED -- which is what run 1's first fifteen failures were.

    So this asks the spawner a question and waits for an answer. `list` is the
    cheapest one: it takes no arguments, touches no tenant, and its refusal
    surface is already covered offline.

    IT ALSO WATCHES THE CONTAINER. A spawner that exited -- a bad config, a
    missing device, an unreadable seccomp profile -- would otherwise be a
    60-second wait ending in a timeout that says nothing. If the container is
    gone, this says so at once, with its logs.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        state = dockerlib._run(
            ["inspect", "--format", "{{.State.Running}}", SPAWNER_CONTAINER],
            timeout=30, check=False)
        if state.returncode != 0 or state.stdout.strip() != "true":
            raise AssertionError(
                f"the spawner container is not running ({state.stdout.strip()!r}), "
                "so nothing below this line is a test of anything:\n"
                + dockerlib.logs(SPAWNER_CONTAINER)[-4000:])
        try:
            answer = asyncio.run(jsonsock.ask(socket_path, {"op": "list"}, timeout=5))
        except jsonsock.Unreachable as exc:      # not bound yet
            last = exc
            time.sleep(0.2)
            continue
        assert "containers" in answer, (
            f"the spawner answered {answer!r} to a list, which is not the shape "
            "the spec's table names. Everything below would be testing a "
            "service that is not this one.")
        return
    raise AssertionError(
        f"the spawner never answered on {socket_path} within {timeout}s "
        f"(last: {last!r}). It is running, so it is stuck before serve():\n"
        + dockerlib.logs(SPAWNER_CONTAINER)[-4000:])
