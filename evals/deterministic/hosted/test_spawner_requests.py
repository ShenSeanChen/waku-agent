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
SPEC_OPERATIONS = {"provision", "start", "stop", "list", "tenants", "task"}
SPEC_TASKS = {"backup", "restore", "archive", "inspect", "inspect-stop"}
SPEC_KEYS = {
    "provision": {"op", "tenant_id", "project_id"},
    "start": {"op", "tenant_id", "project_id", "timezone", "token"},
    "stop": {"op", "tenant_id"},
    "list": {"op"},
    # Added in group F (F3b). `list` answers "may the gateway forward to this
    # container?" and drops one with no address on the tenant bridge or at an
    # address its project id does not derive; `tenants` answers "is this
    # container ours?", which is what stop-all needs before a restore deletes
    # the directories under it. Same empty key set; a different question.
    "tenants": {"op"},
    # Widened in group C, deliberately. The spec's spawner table writes this
    # operation as `task <tenant id> <task>`; restore additionally needs the
    # tenant's project id, because it recreates their two directories empty and
    # the spawner has no database to read the id from. Sending it is strictly
    # better than the alternatives: reading control.db is what the architecture
    # forbids, and allocating a new id gives the restored tenant a new disk
    # bucket and a new bridge address.
    "task": {"op", "tenant_id", "task", "project_id"},
}
SPEC_REQUIRED = {
    "provision": ("tenant_id", "project_id"),
    "start": ("tenant_id", "project_id", "timezone", "token"),
    "stop": ("tenant_id",),
    "list": (),
    "tenants": (),
    "task": ("tenant_id", "task"),
}


def test_the_spawner_answers_exactly_these_six_operations():
    """Add a seventh and this fails, which is the point: `exec` slipped into
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


# A valid value for every field the tables name, so a payload for any
# operation can be built FROM the tables rather than typed out per operation.
_VALUES = {
    "tenant_id": GOOD_ID,
    "project_id": 7,
    "timezone": "UTC",
    "token": GOOD_TOKEN,
    "task": "backup",
}


def payload_for(op: str, **overrides) -> dict:
    """The minimal valid payload for `op`, derived from `_KEYS`."""
    payload = {key: (op if key == "op" else _VALUES[key]) for key in requests._KEYS[op]}
    payload.update(overrides)
    return payload


# Keys no operation takes. The case variants matter: a check that normalised
# key case before comparing would let "TOKEN" through as "token" on start,
# which every other test in this file would call a pass.
_JUNK_KEYS = ["extra", "cmd", "image", "args", "env", "OP", "Op",
              "TENANT_ID", "Tenant_Id", "TOKEN", "Project_Id", "TASK", "TIMEZONE"]


@pytest.mark.parametrize("op", sorted(SPEC_OPERATIONS))
def test_parse_accepts_every_operation_the_table_names(op):
    """The tests above pin the TABLES. These drive the same tables through
    parse() itself, because a check widened at the use site -- `op not in
    OPERATIONS | {"exec"}`, say -- leaves every table assertion green. The
    packaging guard earlier in this group failed the same way: it asserted the
    exclude string while the build did something else."""
    assert requests.parse(payload_for(op)).op == op


@pytest.mark.parametrize("op", ["exec", "restart", "run", "shell", "delete", "",
                                "PROVISION", "start ", "list2", None, 0])
def test_parse_refuses_an_operation_the_table_does_not_name(op):
    assert op not in SPEC_OPERATIONS, "this parameter stopped being a negative case"
    with pytest.raises(requests.Invalid):
        requests.parse({"op": op})


@pytest.mark.parametrize("task", sorted(SPEC_TASKS))
def test_parse_accepts_every_task_the_table_names(task):
    assert requests.parse(payload_for("task", task=task)).task == task


@pytest.mark.parametrize("task", ["exec", "rm -rf", "", "inspect-start", "BACKUP",
                                  "backup ", "stop", None, 0])
def test_parse_refuses_a_task_the_table_does_not_name(task):
    assert task not in SPEC_TASKS, "this parameter stopped being a negative case"
    with pytest.raises(requests.Invalid):
        requests.parse(payload_for("task", task=task))


@pytest.mark.parametrize("op", sorted(SPEC_OPERATIONS))
def test_parse_refuses_every_key_the_operation_does_not_take(op):
    """Driven through parse(), one key at a time, so an unknown-key check that
    was widened or made case-insensitive fails here. The universe is every
    junk key plus every field another operation takes: `project_id` is legal
    on start and must not be legal on stop."""
    universe = set(_JUNK_KEYS) | {key for keys in requests._KEYS.values() for key in keys}
    for key in sorted(universe - requests._KEYS[op]):
        with pytest.raises(requests.Invalid, match="does not take"):
            requests.parse(payload_for(op, **{key: "anything"}))


@pytest.mark.parametrize("op", sorted(SPEC_OPERATIONS))
def test_parse_accepts_every_key_the_operation_does_take(op):
    """The other direction: a key allowlist NARROWED at the use site would
    start refusing a field the spawner needs, and every negative test in this
    file would still pass."""
    parsed = requests.parse(payload_for(op))
    assert parsed.op == op
    for key in requests._KEYS[op] - {"op"}:
        assert getattr(parsed, key) == _VALUES[key], key


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
