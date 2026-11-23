"""tenant.sh's verb set, its closed set on the tenant, and what it prints back.

THE VERB SET IS THE TEST. tenant.sh is typed by a person at 2am, and the two
ways it can go wrong are silent: a verb it forwards without meaning to, and a
verb it silently drops the tenant from.

THE OTHER HALF IS WHAT IT PRINTS. `python -m hosted.gateway.admin` writes its
one JSON object to stdout and exits 1 when that object carries an `error` key,
so a script that captured the answer and let `set -e` end the run would show
the operator nothing at all. Every test below that asserts an exit status also
asserts what reached the terminal.
"""

from __future__ import annotations

import pytest
import shelllib

TENANT = shelllib.DEPLOY / "tenant.sh"

TENANT_ID = "k3fq7x2mza4b"
EMAIL = "mei@example.test"

# The stub answers with whatever the test put in WAKU_ADMIN_JSON and exits with
# WAKU_ADMIN_RC, so one stub covers a gateway that agreed, one that refused, and
# one that did not answer its socket at all.
_DOCKER = """#!/bin/sh
printf '%s %s\\n' docker "$*" >> "$WAKU_CALLS"
printf '%s\\n' "$WAKU_ADMIN_JSON"
exit "$WAKU_ADMIN_RC"
"""

_ROOT = "#!/bin/sh\necho 0\n"
_NOT_ROOT = "#!/bin/sh\necho 1000\n"


def _env(tmp_path, *, answer='{"ok": true, "tenant": "k3fq7x2mza4b"}', rc="0"):
    env_file = tmp_path / "install.env"
    env_file.write_text(
        f"WAKU_ROOT={tmp_path}/waku\nWAKU_SRC={tmp_path}/src\n"
        f"WAKU_COMPOSE={tmp_path}/src/compose.yaml\nWAKU_DOMAIN=example.test\n",
        encoding="utf-8")
    return {"WAKU_INSTALL_ENV": str(env_file),
            "WAKU_ADMIN_JSON": answer,
            "WAKU_ADMIN_RC": rc}


def _run(tmp_path, args, *, answer='{"ok": true, "tenant": "k3fq7x2mza4b"}',
         rc="0", root=True):
    return shelllib.run(TENANT, args, tmp_path=tmp_path,
                        env=_env(tmp_path, answer=answer, rc=rc),
                        stubs=["docker", "id"],
                        bodies={"docker": _DOCKER,
                                "id": _ROOT if root else _NOT_ROOT})


def _admin(tmp_path) -> list[str]:
    return [line for line in shelllib.calls(tmp_path)
            if "hosted.gateway.admin" in line]


@pytest.mark.parametrize("verb", ["disable", "enable", "delete", "inspect",
                                  "inspect-stop"])
@pytest.mark.parametrize("who", [TENANT_ID, EMAIL])
def test_each_verb_reaches_the_gateway_with_the_tenant(tmp_path, verb, who):
    """One call, with the verb and the tenant it was given and nothing else.

    Both halves of the accepted set are parametrised, because an id and an
    address take different arms of the closed set and only the id arm would be
    exercised by a test that used one value.
    """
    done = _run(tmp_path, [verb, who],
                answer='{"ok": true, "tenant": "k3fq7x2mza4b", "port": 34567}')
    assert done.returncode == 0, done.stderr
    sent = _admin(tmp_path)
    assert len(sent) == 1
    assert sent[0].endswith(f"hosted.gateway.admin {verb} {who}")


def test_status_reaches_the_gateway_with_no_tenant(tmp_path):
    done = _run(tmp_path, ["status"], answer='{"running": []}')
    assert done.returncode == 0, done.stderr
    sent = _admin(tmp_path)
    assert len(sent) == 1
    assert sent[0].endswith("hosted.gateway.admin status")


def test_the_admin_command_runs_inside_the_gateway_as_its_own_user(tmp_path):
    """Spec, "Deploy and operate": tenant.sh runs the admin command inside the
    gateway's container as uid 10002. admin.sock is 0600 in a 0700 directory
    owned by that user, so any other user reaches nothing."""
    done = _run(tmp_path, ["status"], answer='{"running": []}')
    assert done.returncode == 0, done.stderr
    sent = _admin(tmp_path)[0]
    assert "exec -T --user 10002:10002 gateway" in sent


@pytest.mark.parametrize("verb", ["backup", "restore", "restart-all",
                                  "stop-all", "resolve", "provision",
                                  "disabled", "Delete", ""])
def test_the_verb_set_is_closed(tmp_path, verb):
    """backup and restore are NOT here: they have their own scripts, which take
    the staging lock. A second path to them that did not lock is a backup
    running during a restore. restart-all, stop-all and resolve are real admin
    verbs that belong to upgrade.sh, restore.sh and restore.sh in turn, and
    none of them is a thing an operator does TO one tenant.

    The last three are the shapes a typo takes: a verb that is nearly one of
    ours, one with the wrong case, and an empty argument from an unset shell
    variable.
    """
    args = [verb, EMAIL] if verb else [EMAIL]
    done = _run(tmp_path, args)
    assert done.returncode != 0
    assert "unknown verb" in done.stderr
    assert _admin(tmp_path) == []


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_the_usage_and_asks_the_gateway_nothing(tmp_path, flag):
    done = _run(tmp_path, [flag])
    assert done.returncode == 0, done.stderr
    assert "usage: tenant.sh status" in done.stdout
    assert _admin(tmp_path) == []


@pytest.mark.parametrize("verb", ["disable", "enable", "delete", "inspect",
                                  "inspect-stop"])
def test_a_verb_that_needs_a_tenant_refuses_without_one(tmp_path, verb):
    """`tenant.sh disable` with no argument must not reach the gateway at all:
    the admin command answers "disable needs a tenant id or email", but by then
    an operator has already been told their command ran."""
    done = _run(tmp_path, [verb])
    assert done.returncode != 0
    assert "needs an email address or a tenant id" in done.stderr
    assert _admin(tmp_path) == []


def test_a_verb_that_needs_a_tenant_refuses_two_of_them(tmp_path):
    """Arity is part of the closed set. An ignored third argument is how
    `tenant.sh delete mei@example.test kenji@example.test` deletes one of the
    two people it names and says nothing about the other."""
    done = _run(tmp_path, ["delete", EMAIL, "kenji@example.test"])
    assert done.returncode != 0
    assert "takes one tenant" in done.stderr
    assert _admin(tmp_path) == []


def test_status_refuses_a_tenant_rather_than_ignoring_one(tmp_path):
    """`tenant.sh status mei@example.test` reads like a question about one
    person. Answered for the whole fleet, it is a wrong answer an operator has
    no way to spot."""
    done = _run(tmp_path, ["status", EMAIL])
    assert done.returncode != 0
    assert "status takes no tenant" in done.stderr
    assert _admin(tmp_path) == []


@pytest.mark.parametrize("value", [
    "-h",                     # argparse in the admin command reads it as a FLAG
    "--socket",
    "..",
    "/srv/waku/tenants",
    "k3fq7x2mza4",            # eleven
    "k3fq7x2mza4bc",          # thirteen
    "k3fq7x2mza4A",           # twelve, and outside [a-z2-7] under LC_ALL=C
    "k3fq7x2mza,b",
    "mei@example",            # an address with no dot in the domain
    "mei@@example.com",
    "@example.com",
    "mei",
    "mei'@example.com",
    "mei example@test.com",
    "mei@example.test\nrm -rf /",
])
def test_the_tenant_is_a_closed_set(tmp_path, value):
    """A CLOSED SET WITH DEFAULT-DENY, applied before the gateway is asked
    anything.

    Three things this script does with the value are its own. It becomes one
    argv word of `python -m hosted.gateway.admin`, whose parser reads a leading
    dash as an option, so `tenant.sh disable -h` would print that command's
    help and exit 0 with the operator told their command ran. After `inspect`
    it is printed back inside a command line the operator copies and pastes.
    And `delete` is the one verb here that cannot be undone.
    """
    done = _run(tmp_path, ["delete", value])
    assert done.returncode != 0
    assert "takes a tenant id" in done.stderr
    assert _admin(tmp_path) == []


def test_the_tenant_set_is_checked_before_root_is_demanded(tmp_path):
    """The refusal an operator gets for a mistyped tenant must be about the
    tenant, on a machine where they are not root.

    THE ASSERTION IS TWO-SIDED ON PURPOSE. A test that accepted either message
    would be satisfied by the root refusal and would pass with the whole closed
    set deleted -- which is exactly the cannot-fail test this group's ledger
    records as round 2 of task F1.
    """
    done = _run(tmp_path, ["delete", ".."], root=False)
    assert done.returncode != 0
    assert "takes a tenant id" in done.stderr
    assert "run this as root" not in done.stderr
    assert _admin(tmp_path) == []


def test_a_tenant_that_passes_the_set_still_has_to_be_root(tmp_path):
    """The other side of the ordering above: the root check is still there, and
    the previous test is not passing because nothing checks root at all."""
    done = _run(tmp_path, ["delete", TENANT_ID], root=False)
    assert done.returncode != 0
    assert "run this as root" in done.stderr
    assert _admin(tmp_path) == []


@pytest.mark.parametrize("rc", ["1", "2"])
def test_a_gateway_that_refuses_is_shown_to_the_operator_with_its_status(
        tmp_path, rc):
    """The admin command prints its object to STDOUT and exits 1 on an error
    object, 2 when the socket did not answer. A bare `answer=$(waku_admin ...)`
    under `set -e` ends the run with that object inside the dead subshell and
    nothing on the terminal, so the operator sees an exit code and no reason.
    """
    done = _run(tmp_path, ["disable", EMAIL], rc=rc,
                answer='{"error": "no tenant matches \'mei@example.test\'"}')
    assert done.returncode == int(rc)
    assert "no tenant matches" in done.stdout


def test_a_gateway_that_refuses_delete_prints_no_archive(tmp_path):
    """The archive note is what tells an operator where the only copy of a
    deleted tenant went. Printed after a refusal it names an archive that was
    never written."""
    done = _run(tmp_path, ["delete", EMAIL], rc="1",
                answer='{"error": "tenant k3fq7x2mza4b has a inspect container running"}')
    assert done.returncode == 1
    assert "ONLY COPY" not in done.stdout


def test_delete_names_the_archive_and_says_it_is_the_only_copy(tmp_path):
    """`backup.sh` gives archives 30 days and puts them in no restic snapshot,
    so an operator who deletes a tenant and does nothing else has one copy of
    that person's data, on this VM, until the timer removes it."""
    path = "/srv/waku/archive/k3fq7x2mza4b/k3fq7x2mza4b-20260925T031700Z"
    done = _run(tmp_path, ["delete", EMAIL],
                answer=f'{{"ok": true, "tenant": "k3fq7x2mza4b", "archive": "{path}"}}')
    assert done.returncode == 0, done.stderr
    assert f"{path}-home.tar.zst" in done.stdout
    assert f"{path}-env.tar.zst" in done.stdout
    assert "30 days" in done.stdout
    # THE SENTENCE THIS TEST IS NAMED FOR. Without it the claim was asserted
    # only by a SIBLING's negative (`"ONLY COPY" not in done.stdout` after a
    # refusal), so deleting the sentence left every test here green and the
    # sibling silently unable to fail for the reason it states.
    assert "THAT IS THE ONLY COPY." in done.stdout


def test_delete_says_nothing_about_an_archive_the_gateway_did_not_name(tmp_path):
    """`_act` sends `archived.get("path", "")`, so an empty string is a real
    answer. Two file names built from it would point at `-home.tar.zst` in the
    root directory."""
    done = _run(tmp_path, ["delete", EMAIL],
                answer='{"ok": true, "tenant": "k3fq7x2mza4b", "archive": ""}')
    assert done.returncode == 0, done.stderr
    assert "tar.zst" not in done.stdout


def test_inspect_prints_the_tunnel_and_how_to_end_it(tmp_path):
    """The dashboard is on the host's loopback only and the tenant is in
    maintenance until inspect-stop. An operator who is not told the second half
    leaves a tenant unable to start their own container.

    THE PORT IS READ OUT OF THE ANSWER, not stubbed into place: the number
    below appears nowhere in the script and nowhere in the environment, so the
    only way it can reach stdout is through the expression that parses the
    gateway's JSON.
    """
    done = _run(tmp_path, ["inspect", EMAIL],
                answer='{"ok": true, "tenant": "k3fq7x2mza4b", '
                       '"port": 34567, "address": "127.0.0.1"}')
    assert done.returncode == 0, done.stderr
    assert "ssh -N -L 7777:127.0.0.1:34567" in done.stdout
    # THE COMMAND AS THE OPERATOR CAN RUN IT. `install.sh` puts nothing on
    # PATH, so a bare `tenant.sh inspect-stop` is a line that does not work
    # from the directory an operator is standing in -- and this one is printed
    # at the moment a tenant has just been put into maintenance.
    assert f"sudo {shelllib.DEPLOY}/tenant.sh inspect-stop {EMAIL}" in done.stdout


@pytest.mark.parametrize("answer", [
    '{"ok": true, "tenant": "k3fq7x2mza4b"}',          # no port at all
    '{"ok": true, "port": "34567"}',                   # a string, not a number
    '{"ok": true, "port": 34567abc}',                  # not a JSON number
    '{"ok": true, "port": 0}',
    '{"ok": true, "port": 034567}',                    # ssh reads it as octal
    '{"ok": true, "port": 65536}',
])
def test_inspect_refuses_a_port_it_cannot_put_in_a_command(tmp_path, answer):
    """The port goes straight into a command line the operator pastes into
    their own shell, so it is a port number or it is refused. The container is
    already running by this point, so the refusal names the verb that removes
    it rather than leaving the tenant in maintenance with no way out."""
    done = _run(tmp_path, ["inspect", EMAIL], answer=answer)
    assert done.returncode != 0
    assert "did not name a usable port" in done.stderr
    assert f"sudo {shelllib.DEPLOY}/tenant.sh inspect-stop {EMAIL}" in done.stderr
    assert "ssh -N -L" not in done.stdout


def test_only_inspect_prints_a_tunnel(tmp_path):
    """`inspect-stop` answers `{"ok": true}` and every other verb answers
    without a port. A tunnel printed after `disable` is an instruction to
    connect to a container that was just stopped."""
    done = _run(tmp_path, ["inspect-stop", EMAIL],
                answer='{"ok": true, "tenant": "k3fq7x2mza4b", "port": 34567}')
    assert done.returncode == 0, done.stderr
    assert "ssh -N -L" not in done.stdout


def test_only_delete_prints_an_archive_note(tmp_path):
    """The gateway's answer is data this script did not write, and what it
    prints is decided by the verb the OPERATOR typed rather than by whichever
    keys that answer happened to carry. `inspect-stop` does not archive
    anything, so an archive path in its answer names a file that verb did not
    create."""
    path = "/srv/waku/archive/k3fq7x2mza4b/k3fq7x2mza4b-20260925T031700Z"
    done = _run(tmp_path, ["inspect-stop", EMAIL],
                answer=f'{{"ok": true, "archive": "{path}"}}')
    assert done.returncode == 0, done.stderr
    assert "ONLY COPY" not in done.stdout
    assert "tar.zst" not in done.stdout
