"""DETERMINISTIC EVAL -- what the spawner will and will not act on.

THE PRIVILEGED SURFACE. The spawner is root with CAP_SYS_ADMIN and the data
disk's block device. core/requests.py already allowlists the five operations,
the five tasks and each operation's exact key set (B2, test_spawner_requests.py
covers the parsing). What THIS file proves is the half that matters at the
service: that a refused request never reaches the runtime at all. The fake
runtime records every call, and for each refusal the recording is empty.
"""

from __future__ import annotations

import asyncio

import pytest

from hosted.core.requests import _KEYS, OPERATIONS
from hosted.ports.runtime import RunningContainer
from hosted.spawner import service
from hosted.spawner.docker import Busy


class FakeRuntime:
    """Records what it was asked to do. Does nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def provision(self, tenant_id, project_id):
        self.calls.append(("provision", tenant_id, project_id))

    async def start(self, tenant_id, project_id, timezone, token):
        self.calls.append(("start", tenant_id, project_id, timezone, token))
        return RunningContainer(tenant_id=tenant_id, address="10.88.0.2", port=7777)

    async def stop(self, tenant_id):
        self.calls.append(("stop", tenant_id))

    async def list(self):
        self.calls.append(("list",))
        return [RunningContainer(tenant_id="k3fq7x2mza4b", address="10.88.0.2", port=7777)]

    async def task(self, tenant_id, task, project_id=0):
        self.calls.append(("task", tenant_id, task, project_id))
        if task == "restore":
            return {"ok": True}
        return {"path": f"/srv/waku/staging/{tenant_id}"}


TENANT = "k3fq7x2mza4b"
TOKEN = "x" * 43


def ask(payload: dict, runtime=None):
    runtime = runtime or FakeRuntime()
    return asyncio.run(service.handle(runtime, payload)), runtime


def test_each_operation_answers_the_shape_the_specs_table_names():
    assert ask({"op": "provision", "tenant_id": TENANT, "project_id": 2})[0] == {"ok": True}
    answer, _ = ask({"op": "start", "tenant_id": TENANT, "project_id": 2,
                     "timezone": "Asia/Shanghai", "token": TOKEN})
    assert answer == {"address": "10.88.0.2", "port": 7777}
    assert ask({"op": "stop", "tenant_id": TENANT})[0] == {"ok": True}
    assert ask({"op": "list"})[0] == {
        "containers": [{"tenant_id": TENANT, "address": "10.88.0.2", "port": 7777}]}
    assert ask({"op": "task", "tenant_id": TENANT, "task": "backup"})[0] == {
        "path": f"/srv/waku/staging/{TENANT}"}
    assert ask({"op": "task", "tenant_id": TENANT, "task": "restore",
                "project_id": 4242})[0] == {"ok": True}


@pytest.mark.parametrize("payload", [
    # An operation outside the allowlist. "exec" and "run" are the two a
    # compromised gateway would reach for first.
    {"op": "exec", "tenant_id": TENANT, "command": ["sh"]},
    {"op": "run", "tenant_id": TENANT},
    {"op": "", "tenant_id": TENANT},
    # A well-formed operation carrying one extra key. An IGNORED key is how a
    # caller comes to believe it asked for something it did not get, which is
    # why core/requests.py refuses rather than drops.
    {"op": "stop", "tenant_id": TENANT, "image": "evil:latest"},
    {"op": "start", "tenant_id": TENANT, "project_id": 2, "timezone": "UTC",
     "token": TOKEN, "binds": ["/:/host"]},
    {"op": "task", "tenant_id": TENANT, "task": "backup", "command": ["sh"]},
    {"op": "task", "tenant_id": TENANT, "task": "restore", "project_id": 0},
    {"op": "task", "tenant_id": TENANT, "task": "restore", "project_id": 65280},
    # A task outside the allowlist.
    {"op": "task", "tenant_id": TENANT, "task": "shell"},
    {"op": "task", "tenant_id": TENANT, "task": "inspect-start"},
    # A tenant id that is a path.
    {"op": "stop", "tenant_id": "../../etc"},
    {"op": "provision", "tenant_id": TENANT + "/x", "project_id": 2},
    # A project id outside the range, which would land the container outside
    # the tenant subnet or inside the dynamic range.
    {"op": "provision", "tenant_id": TENANT, "project_id": 0},
    {"op": "provision", "tenant_id": TENANT, "project_id": 65280},
    {"op": "provision", "tenant_id": TENANT, "project_id": True},
    # Not an object at all.
    {"op": "list", "extra": 1},
])
def test_a_refused_request_never_reaches_the_runtime(payload):
    answer, runtime = ask(payload)
    assert "error" in answer, f"{payload} was accepted"
    assert runtime.calls == [], (
        f"{payload} was refused on the wire and the runtime was called anyway: "
        f"{runtime.calls}. The refusal has to happen BEFORE the privileged "
        "process does anything, not after.")


def test_a_busy_tenant_is_refused_with_a_code_and_not_an_exception():
    class BusyRuntime(FakeRuntime):
        async def start(self, *args):
            raise Busy("tenant k3fq7x2mza4b has a inspect container running.")

    answer, _ = ask({"op": "start", "tenant_id": TENANT, "project_id": 2,
                     "timezone": "UTC", "token": TOKEN}, BusyRuntime())
    assert answer["code"] == "busy"
    assert "inspect" in answer["error"]


# A valid payload for each operation, derived from _KEYS rather than written
# out, so an operation that gains a key gets it here automatically. The same
# shape B2's test_spawner_requests.py::_minimal uses.
_VALID = {
    "tenant_id": TENANT,
    "project_id": 2,
    "timezone": "UTC",
    "token": TOKEN,
    "task": "backup",
}


def _valid_payload(op: str) -> dict:
    return {key: (op if key == "op" else _VALID[key]) for key in _KEYS[op]}


@pytest.mark.parametrize("op", sorted(OPERATIONS))
def test_every_operation_the_allowlist_admits_reaches_the_runtime(op):
    """DRIVES handle(). An operation in OPERATIONS with no dispatch branch
    falls through to the 'no handler' refusal, and this is what notices.

    An earlier draft read handle()'s SOURCE and looked for the literal
    `request.op == "<op>"`. That test passed against a branch that existed and
    did nothing, and it would have failed FALSELY the day somebody refactored
    the dispatch into a dict lookup -- a perfectly good refactor. It guarded
    nothing and obstructed. It is the same shape as the `_project_id_of`
    name-in-source test deleted in the same round, and it survived that round
    because nobody was looking for a second one.
    """
    answer, runtime = ask(_valid_payload(op))
    assert answer != {"error": f"no handler for {op}"}, (
        f"{op} is in OPERATIONS and parse() accepts it, but handle() has no "
        "branch for it, so a request the allowlist admits falls through to a "
        "refusal at runtime.")
    assert "error" not in answer, answer
    assert runtime.calls and runtime.calls[0][0] == op, (
        f"{op} was answered without the runtime being called: {runtime.calls}")


def test_a_runtime_failure_is_not_turned_into_a_plausible_error_body():
    """What handle() does with an exception that is NOT Busy, which nothing
    offline had ever observed while the dispatch test only read source.

    It propagates, on purpose. jsonsock._answer catches it, logs the traceback
    for the operator, and answers the opaque HANDLER_FAILED on the wire. If
    handle() caught broadly and returned {"error": str(exc)}, the operator
    would lose the traceback and a Docker or xfs_quota message -- which can
    name host paths -- would reach a caller whose own request came from a
    tenant. Asserting the propagation is what keeps a future `except
    Exception` from looking like an improvement.
    """
    class BrokenRuntime(FakeRuntime):
        async def stop(self, tenant_id):
            raise RuntimeError("/srv/waku/tenants/k3fq7x2mza4b: no such device")

    with pytest.raises(RuntimeError):
        asyncio.run(service.handle(BrokenRuntime(),
                                   {"op": "stop", "tenant_id": TENANT}))


def test_restore_carries_the_tenants_own_project_id_through_to_the_runtime():
    """DRIVES THE CALL. An earlier draft of this plan asserted that the string
    "_project_id_of" appeared in _restore's source -- a test that passes
    against a stub of a method that cannot be written, and the tenth
    cannot-fail test on this project. This one records what the runtime was
    actually handed.

    If restore allocated a new project id instead, the restored tenant would
    get a new XFS accounting bucket AND a new fixed bridge address, and the one
    claim the fixed-address scheme rests on -- that a stale address in the
    gateway's memory can only reach nothing or the same tenant -- would be
    false.
    """
    answer, runtime = ask({"op": "task", "tenant_id": TENANT, "task": "restore",
                           "project_id": 4242})
    assert "error" not in answer, answer
    assert runtime.calls == [("task", TENANT, "restore", 4242)], (
        f"the runtime was handed {runtime.calls}; restore must receive the "
        "tenant's own project id, unchanged.")


def test_restore_without_a_project_id_is_refused_and_touches_nothing():
    """The per-task requirement, enforced at the service because _REQUIRED is
    per-operation. Refused BEFORE the privileged process does anything."""
    answer, runtime = ask({"op": "task", "tenant_id": TENANT, "task": "restore"})
    assert "project_id" in answer["error"]
    assert runtime.calls == []


@pytest.mark.parametrize("task", ["backup", "archive", "inspect", "inspect-stop"])
def test_the_other_four_tasks_need_no_project_id(task):
    """The widening is one optional key for one task, not a new required field
    on every task."""
    answer, runtime = ask({"op": "task", "tenant_id": TENANT, "task": task})
    assert "error" not in answer, answer
    assert runtime.calls == [("task", TENANT, task, 0)]


def test_the_runtime_the_service_talks_to_is_the_one_the_port_describes():
    """FakeRuntime stands in for DockerRuntime in every test above, so the two
    have to have the same five methods with the same signatures -- otherwise
    this file passes against a shape the real runtime does not have.

    DockerRuntime is compared against the Protocol rather than against
    FakeRuntime, because the Protocol is the thing E2 and E3 will write their
    own callers against.
    """
    import inspect as _inspect

    from hosted.ports.runtime import TenantRuntime
    from hosted.spawner.docker import DockerRuntime

    for name in ("provision", "start", "stop", "list", "task"):
        expected = _inspect.signature(getattr(TenantRuntime, name))
        actual = _inspect.signature(getattr(DockerRuntime, name))
        assert actual == expected, (
            f"DockerRuntime.{name}{actual} does not match "
            f"TenantRuntime.{name}{expected}")
        assert _inspect.signature(getattr(FakeRuntime, name)).parameters.keys() == \
            expected.parameters.keys(), (
            f"FakeRuntime.{name} does not take what TenantRuntime.{name} takes, "
            "so every test in this file is driving a shape the spawner has not "
            "got.")

    extra = {name for name in vars(DockerRuntime)
             if not name.startswith("_") and callable(vars(DockerRuntime)[name])}
    assert extra == {"provision", "start", "stop", "list", "task"}, (
        f"DockerRuntime has public methods the Protocol does not name: "
        f"{sorted(extra - {'provision', 'start', 'stop', 'list', 'task'})}. "
        "The spawner is root with CAP_SYS_ADMIN; every public verb here is a "
        "privileged verb, and there are exactly five.")
