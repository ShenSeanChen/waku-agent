"""Every spawner request is validated before the spawner acts on it.

The spawner runs as root with CAP_SYS_ADMIN and the data disk's block device.
It is the most privileged process in the deployment, and the only thing that
talks to it is the gateway -- so this is not a guard against a hostile caller,
it is a guard against a bug in the gateway becoming a bug in the spawner.
"""

from __future__ import annotations

import pytest

from hosted.core import requests

GOOD_ID = "abcdefghijkl"
GOOD_TOKEN = "T" * 43


# The spawner's whole surface, from the spec's operation table. Every negative
# case in this file is a NARROWING check -- it proves a bad payload is refused.
# None of them notices the allowlists being WIDENED, which is the direction that
# matters for a process running as root with CAP_SYS_ADMIN and the data disk's
# block device. These three literals are the closure, in both directions.
SPEC_OPERATIONS = {"provision", "start", "stop", "list", "task"}
SPEC_TASKS = {"backup", "restore", "archive", "inspect", "inspect-stop"}
SPEC_KEYS = {
    "provision": {"op", "tenant_id", "project_id"},
    "start": {"op", "tenant_id", "project_id", "timezone", "token"},
    "stop": {"op", "tenant_id"},
    "list": {"op"},
    "task": {"op", "tenant_id", "task"},
}
SPEC_REQUIRED = {
    "provision": ("tenant_id", "project_id"),
    "start": ("tenant_id", "project_id", "timezone", "token"),
    "stop": ("tenant_id",),
    "list": (),
    "task": ("tenant_id", "task"),
}


def test_the_spawner_answers_exactly_these_five_operations():
    """Add a sixth and this fails, which is the point: `exec` slipped into
    OPERATIONS leaves every other test in this file green."""
    assert requests.OPERATIONS == SPEC_OPERATIONS


def test_a_task_is_exactly_one_of_these_five():
    assert requests.TASKS == SPEC_TASKS


def test_each_operation_takes_exactly_its_own_keys():
    """A key added to an operation's allowlist is a new field the spawner will
    read and act on. `project_id` quietly added to `stop` changes nothing that
    any narrowing test can see."""
    assert {op: set(keys) for op, keys in requests._KEYS.items()} == SPEC_KEYS
    assert requests._REQUIRED == SPEC_REQUIRED


def test_no_operation_is_half_wired():
    """Three tables keyed by operation. An operation present in one and absent
    from another is a KeyError on a live request, or a required field nobody
    checks."""
    assert set(requests._KEYS) == requests.OPERATIONS
    assert set(requests._REQUIRED) == requests.OPERATIONS
    for op, required in requests._REQUIRED.items():
        assert set(required) <= requests._KEYS[op], op
        assert "op" not in required, op


def test_a_start_request_carries_everything_the_template_needs():
    parsed = requests.parse({"op": "start", "tenant_id": GOOD_ID, "project_id": 7,
                             "timezone": "Asia/Shanghai", "token": GOOD_TOKEN})
    assert parsed.op == "start"
    assert parsed.tenant_id == GOOD_ID
    assert parsed.project_id == 7
    assert parsed.timezone == "Asia/Shanghai"
    assert parsed.token == GOOD_TOKEN


def test_list_needs_nothing_else():
    assert requests.parse({"op": "list"}).op == "list"


def test_a_task_names_one_of_the_five():
    for task in ("backup", "restore", "archive", "inspect", "inspect-stop"):
        parsed = requests.parse({"op": "task", "tenant_id": GOOD_ID, "task": task})
        assert parsed.task == task


@pytest.mark.parametrize("payload", [
    {},
    {"op": "restart"},
    {"op": "start"},                                          # no tenant
    {"op": "start", "tenant_id": "../../etc", "project_id": 7,
     "timezone": "UTC", "token": GOOD_TOKEN},
    {"op": "start", "tenant_id": GOOD_ID, "project_id": 0,
     "timezone": "UTC", "token": GOOD_TOKEN},
    {"op": "start", "tenant_id": GOOD_ID, "project_id": 7,
     "timezone": "Mars/Olympus", "token": GOOD_TOKEN},
    {"op": "start", "tenant_id": GOOD_ID, "project_id": 7,
     "timezone": "UTC", "token": "short"},
    {"op": "task", "tenant_id": GOOD_ID, "task": "rm -rf"},
    {"op": "task", "tenant_id": GOOD_ID},                     # no task
    {"op": "stop"},                                           # no tenant
    {"op": "stop", "tenant_id": GOOD_ID, "extra": "field"},   # unknown key
    {"op": "list", "tenant_id": GOOD_ID},                     # a key list does not take
    ["op", "list"],
    "start",
    None,
])
def test_anything_else_is_invalid(payload):
    """An allowlist of operations, of tasks, and of keys per operation. A
    payload with a key the operation does not take is refused rather than
    ignored, because an ignored key is how a caller thinks it asked for
    something it did not get."""
    with pytest.raises(requests.Invalid):
        requests.parse(payload)


def test_the_message_names_what_was_wrong():
    with pytest.raises(requests.Invalid) as exc:
        requests.parse({"op": "start", "tenant_id": "nope", "project_id": 7,
                        "timezone": "UTC", "token": GOOD_TOKEN})
    assert "tenant_id" in str(exc.value)


def test_a_timezone_is_refused_here_rather_than_normalised():
    """The gateway normalises an unknown zone to UTC when it STORES it
    (acceptance 4). By the time it reaches the spawner it has been through
    that, so an unknown zone here means the gateway has a bug and the spawner
    should say so rather than quietly run the container in UTC."""
    with pytest.raises(requests.Invalid):
        requests.parse({"op": "start", "tenant_id": GOOD_ID, "project_id": 7,
                        "timezone": "Mars/Olympus", "token": GOOD_TOKEN})
