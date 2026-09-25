"""DETERMINISTIC EVAL, THIRD TIER -- the spawner, against a real daemon.

This is the GUARD for the container template. test_container_template.py reads
the dict; this reads /proc/self/status inside the running container and then
tries what each field is supposed to stop.

The `spawner` fixture -- the real service, in a container, as root -- and its
`bridges` and `spawner_root` companions are in conftest.py, so all three C2
Docker modules share one spawner container per module run. The constants and
the plain helpers are in spawnerlib.py. Neither is defined twice.
"""

from __future__ import annotations

import time

import dockerlib
import pytest
from spawnerlib import (
    PROJECT_A,
    SERVICES_TAG,
    SPAWNER_CONTAINER,
    TENANT_A,
    TENANT_TAG,
    TOKEN_ONE,
    TOKEN_TWO,
    allowed_bind_sources,
    ask,
    capture_task_containers,
)

from hosted.core import tenant
from hosted.spawner import template


def test_start_puts_the_container_at_the_address_the_project_id_derives(spawner):
    answer = ask(spawner, {"op": "start", "tenant_id": TENANT_A,
                           "project_id": PROJECT_A, "timezone": "UTC",
                           "token": TOKEN_ONE})
    assert answer == {"address": tenant.address_for_project(PROJECT_A),
                      "port": template.DASHBOARD_PORT}, answer
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    networks = dockerlib.inspect(name)["NetworkSettings"]["Networks"]
    assert networks[tenant.TENANT_NETWORK]["IPAddress"] == answer["address"]
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)


def test_a_second_start_replaces_the_first_and_only_one_container_exists(spawner):
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_TWO})
    listed = ask(spawner, {"op": "list"})["containers"]
    mine = [c for c in listed if c["tenant_id"] == TENANT_A]
    assert len(mine) == 1, f"two containers for one tenant: {listed}"
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    env = dict(entry.split("=", 1)
               for entry in dockerlib.inspect(name)["Config"]["Env"])
    assert env["WAKU_PLATFORM_TOKEN"] == TOKEN_TWO, (
        "the surviving container holds the OLD token, so the gateway's revoke "
        "and the container's credential disagree")


def test_the_kernel_honoured_capdrop_and_no_new_privileges(spawner):
    """The kernel's own view, not `docker inspect`. A field can be set and
    ignored; /proc/self/status is what the process actually has."""
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)
    status = dockerlib.exec_in(name, ["cat", "/proc/self/status"]).stdout
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert fields["CapEff"].strip() == "0000000000000000", fields["CapEff"]
    assert fields["CapPrm"].strip() == "0000000000000000", fields["CapPrm"]
    assert fields["NoNewPrivs"].strip() == "1"
    proc = dockerlib.exec_in(
        name, ["python", "-c", "import os; os.setuid(0)"], check=False)
    assert proc.returncode != 0 and "PermissionError" in proc.stderr


def test_the_root_filesystem_is_read_only_and_tmp_is_a_256mb_tmpfs(spawner):
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)
    denied = dockerlib.exec_in(name, ["python", "-c",
                                      "open('/etc/waku-probe','w')"], check=False)
    assert denied.returncode != 0, "the root filesystem is writable"
    allowed = dockerlib.exec_in(name, ["python", "-c",
                                       "open('/tmp/waku-probe','w').write('x')"])
    assert allowed.returncode == 0, "HOME=/tmp is not writable; nothing will run"
    mounts = dockerlib.exec_in(name, ["cat", "/proc/self/mounts"]).stdout
    tmp = next(line for line in mounts.splitlines() if line.split()[1] == "/tmp")
    assert tmp.split()[2] == "tmpfs", tmp
    assert "size=262144k" in tmp, f"/tmp is not 256 MB: {tmp}"


def test_the_pids_limit_binds(spawner):
    """Both halves. Without the 100-thread case, a container that could spawn
    nothing at all would pass the 300-thread case."""
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)
    program = (
        "import sys, threading\n"
        "n = int(sys.argv[1])\n"
        "stop = threading.Event()\n"
        "made = 0\n"
        "try:\n"
        "    for _ in range(n):\n"
        "        threading.Thread(target=stop.wait, daemon=True).start()\n"
        "        made += 1\n"
        "finally:\n"
        "    stop.set()\n"
        "print('MADE', made)\n"
    )
    ok = dockerlib.exec_in(name, ["python", "-c", program, "100"], check=False)
    assert ok.returncode == 0 and "MADE 100" in ok.stdout, ok.stderr[-500:]
    too_many = dockerlib.exec_in(name, ["python", "-c", program, "300"], check=False)
    assert too_many.returncode != 0 or "MADE 300" not in too_many.stdout, (
        f"300 threads succeeded under PidsLimit {template.PIDS_LIMIT}")


def test_stop_removes_the_container(spawner):
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    assert ask(spawner, {"op": "stop", "tenant_id": TENANT_A}) == {"ok": True}
    listed = ask(spawner, {"op": "list"})["containers"]
    assert not [c for c in listed if c["tenant_id"] == TENANT_A]
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    gone = dockerlib._run(["inspect", name], timeout=30, check=False)
    assert gone.returncode != 0, "AutoRemove did not remove the container"


def test_list_ignores_a_container_that_is_not_ours(spawner, tenant_image):
    """The gateway forwards to whatever `list` returns, so a container
    carrying our kind label and a tenant id that is not one must not be
    adopted. This is not tidiness."""
    try:
        dockerlib.start_detached(
            tenant_image, ["sleep", "300"], name="waku-impostor",
            network=tenant.TENANT_NETWORK, read_only=False,
            extra=["--label", f"{template.LABEL_KIND}={template.KIND_TENANT}",
                   "--label", f"{template.LABEL_TENANT}=NOT-AN-ID"])
        listed = ask(spawner, {"op": "list"})["containers"]
        assert not [c for c in listed if c["tenant_id"] == "NOT-AN-ID"]
        assert "ignoring container" in dockerlib.logs(SPAWNER_CONTAINER)
    finally:
        dockerlib.remove("waku-impostor")


def test_start_is_refused_while_an_inspect_container_holds_the_tenant(spawner):
    """Refused must mean DID NOTHING, not created-it-and-then-complained."""
    ask(spawner, {"op": "stop", "tenant_id": TENANT_A})
    assert "port" in ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                                   "task": "inspect"})
    try:
        answer = ask(spawner, {"op": "start", "tenant_id": TENANT_A,
                               "project_id": PROJECT_A, "timezone": "UTC",
                               "token": TOKEN_ONE})
        assert answer.get("code") == "busy", answer
        listing = dockerlib._run(
            ["ps", "-a", "--filter", f"label={template.LABEL_TENANT}={TENANT_A}",
             "--filter", f"label={template.LABEL_KIND}={template.KIND_TENANT}",
             "--format", "{{.Names}}"], timeout=30, check=True).stdout.strip()
        assert not listing, f"a refused start created {listing}"
    finally:
        ask(spawner, {"op": "task", "tenant_id": TENANT_A, "task": "inspect-stop"})


def test_a_tenants_own_start_is_not_refused_by_its_own_provisioning(spawner):
    """I-2. provision runs inside every start and labels its container
    KIND_PROVISION, which _refuse_if_busy ignores. With KIND_TASK there, the
    gateway's documented retry -- 'a start that does not answer within 15
    seconds ... Try again.' -- lands on Busy and the tenant is told they are
    under maintenance by their own first request."""
    ask(spawner, {"op": "stop", "tenant_id": TENANT_A})
    for token in (TOKEN_ONE, TOKEN_TWO):
        answer = ask(spawner, {"op": "start", "tenant_id": TENANT_A,
                               "project_id": PROJECT_A, "timezone": "UTC",
                               "token": token})
        assert "code" not in answer, answer
        assert answer["address"] == tenant.address_for_project(PROJECT_A)


# Which container kinds each task may produce, and what each kind must look
# like. A TABLE OF KINDS, NOT OF COUNTS, and that is C2-2: the first version
# asserted exactly one KIND_TASK container per task, which `restore` can never
# satisfy -- it runs an archive, an empty and the restore itself, plus a
# provision -- so that parameter was red on the first real daemon run. Counting
# would also have been flaky whatever the number: capture_task_containers polls
# `docker ps -a` every 200 ms and _run_to_completion removes each container in
# a `finally`, so a short-lived one can be created and reaped between polls.
#
# What does NOT depend on the poller catching every container: every container
# it DID catch must be one of the kinds this task is allowed to produce, and
# must have that kind's network and image. A task that starts producing a
# different kind, or puts a task container on the tenant bridge, fails here.
_TASK_KINDS = {
    "backup":  {template.KIND_TASK},
    # restore archives, empties, provisions and restores.
    "restore": {template.KIND_TASK, template.KIND_PROVISION},
    "archive": {template.KIND_TASK},
    "inspect": {template.KIND_INSPECT},
}

# The kind that defines the task -- at least one must be seen, or the operation
# did not do its own work.
_TASK_OWNER_KIND = {
    "backup": template.KIND_TASK,
    "restore": template.KIND_TASK,
    "archive": template.KIND_TASK,
    "inspect": template.KIND_INSPECT,
}

_KIND_SHAPE = {
    template.KIND_TASK: ("none", SERVICES_TAG),
    template.KIND_PROVISION: ("none", SERVICES_TAG),
    template.KIND_INSPECT: (tenant.INSPECT_NETWORK, TENANT_TAG),
}


@pytest.mark.parametrize("task", sorted(_TASK_KINDS))
def test_every_task_container_is_throwaway_and_holds_only_that_tenants_mounts(
        spawner, spawner_root, task):
    """Reads `docker inspect` on the containers the spawner actually created,
    not the dict the template built.

    EVERY container the operation creates is checked, not just the first: a
    restore runs an archive container, an empty container and a provision
    container on its way, and each of them mounts a tenant's data as UID
    10001. A test that looked only at the first would have said nothing about
    the other three.
    """
    payload = {"op": "task", "tenant_id": TENANT_A, "task": task}
    if task == "restore":
        payload["project_id"] = PROJECT_A
        # A restore extracts what a backup staged, and refuses outright when
        # nothing is staged -- so the backup is setup, not part of the test.
        ask(spawner, {"op": "task", "tenant_id": TENANT_A, "task": "backup"})
    seen = capture_task_containers(spawner, payload, TENANT_A)
    allowed = allowed_bind_sources(spawner_root, TENANT_A, task)
    try:
        kinds_seen = set()
        for container in seen:
            host = container["HostConfig"]
            labels = container["Config"]["Labels"]
            assert container["Config"]["User"] == \
                f"{template.TENANT_UID}:{template.TENANT_UID}"
            assert host["CapDrop"] == ["ALL"]
            assert host["AutoRemove"] is False
            sources = {bind.split(":", 1)[0].rstrip("/") for bind in host["Binds"]}
            unexpected = sources - allowed
            assert not unexpected, (
                f"a {task} container mounted {sorted(unexpected)}, which is not "
                f"in the allowed set {sorted(allowed)}. The spawner runs as root "
                "with CAP_SYS_ADMIN; a mount it was not supposed to have is a "
                "host path handed to tenant-owned code.")

            kind = labels.get(template.LABEL_KIND)
            assert kind in _TASK_KINDS[task], (
                f"{task} produced a {kind!r} container; it may only produce "
                f"{sorted(_TASK_KINDS[task])}.")
            kinds_seen.add(kind)
            network, image = _KIND_SHAPE[kind]
            assert host["NetworkMode"] == network, (
                f"a {kind} container is on {host['NetworkMode']!r}, not {network!r}")
            assert container["Config"]["Image"] == image

        assert _TASK_OWNER_KIND[task] in kinds_seen, (
            f"{task} produced no {_TASK_OWNER_KIND[task]} container at all: "
            f"saw {sorted(kinds_seen)}. The operation did not do its own work.")
    finally:
        if task == "inspect":
            ask(spawner, {"op": "task", "tenant_id": TENANT_A,
                          "task": "inspect-stop"})


def test_a_tenants_logs_are_capped(spawner):
    """Acceptance 16's log clause: Docker's `local` driver, 10 MB per file,
    3 files, so at most 30 MB on disk however much the tenant writes.

    IT READS THE FILES, not `docker logs`, because the cap is a property of
    what is RETAINED on disk and the stream reports what was written. That
    needs the daemon's storage directory to be on this filesystem, which it is
    not on Docker Desktop, where the daemon lives in a VM. So it skips there,
    with the reason named: this group's own rule is that an absent capability,
    a failed build and a healthy container are three distinguishable outcomes,
    and a test that ERRORS on the first machine anybody runs it on breaks that
    rule before any of the others get a chance to.
    """
    docker_root = dockerlib.require_docker_root_dir()
    ask(spawner, {"op": "start", "tenant_id": TENANT_A, "project_id": PROJECT_A,
                  "timezone": "UTC", "token": TOKEN_ONE})
    name = template.container_name(TENANT_A, template.KIND_TENANT)
    dockerlib.wait_for_listener(name, "127.0.0.1", template.DASHBOARD_PORT)

    info = dockerlib.inspect(name)
    assert info["HostConfig"]["LogConfig"] == {
        "Type": "local", "Config": {"max-size": "10m", "max-file": "3"}}

    flood = ("import sys\n"
             "line = 'x' * 1024 + '\\n'\n"
             "for _ in range(60 * 1024):\n"
             "    sys.stdout.write(line)\n")
    dockerlib.exec_in(name, ["python", "-c", flood], check=False)
    time.sleep(2)

    # The `local` driver leaves .LogPath EMPTY -- that field belongs to
    # json-file -- so the path is composed from the daemon's storage directory
    # and the container id. An earlier draft read .LogPath and would have run
    # `dirname` on an empty string, then measured /host.
    logs = docker_root / "containers" / info["Id"] / "local-logs"
    assert logs.is_dir(), (
        f"no local-logs directory at {logs}; either the driver is not `local` "
        "or the daemon stores its containers somewhere else")
    size = sum(entry.stat().st_size for entry in logs.iterdir() if entry.is_file())
    assert size <= 33 * 1024 * 1024, (
        f"{size} bytes of logs for one tenant. The cap is 10 MB across 3 files; "
        "the 3 MB of slack is the driver's own framing and the file it is "
        "mid-rotation on.")
    assert size > 1024 * 1024, (
        f"only {size} bytes of logs, so the 60 MB write never reached the "
        "driver and the ceiling above is asserting against nothing.")
