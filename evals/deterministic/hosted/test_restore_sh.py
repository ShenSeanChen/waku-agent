"""restore.sh's order and its refusals.

THE ORDER IS THE WHOLE TEST. Every stage of --all is individually harmless and
the sequence is what makes it a restore rather than a data-loss event: prove
the snapshot exists while the platform is still up, stop the fleet while the
gateway that owns it is still running, replace the databases while nothing is
writing to them, and only restore tenants once the gateway can be asked to stop
each one first.

WHERE EACH REFUSAL LANDS IS ITSELF AN ASSERTION, as it is in test_backup_sh.py.
A missing manifest is a refusal in any order; a missing manifest discovered
AFTER the gateway has been asked to restore is a tenant whose live tree has
already been archived and removed. Several tests below assert that no
`admin restore`, no `stop-all` and no `flock` line was ever written, which is
the only way that distinction shows.
"""

from __future__ import annotations

import pytest
import shelllib

RESTORE = shelllib.DEPLOY / "restore.sh"

TENANT = "k3fq7x2mza4b"
OTHER = "aaaaaaaaaaaa"

# The docker stub stands for four different containers: the gateway running
# `python -m hosted.gateway.admin`, the gateway running `sqlite3` for the email
# lookup, `docker compose stop|start`, and the throwaway container
# waku_reset_staging_slot runs as UID 10001.
#
# THE REPAIR ARM REALLY EMPTIES THE DIRECTORY IT WAS GIVEN. "The slot was
# emptied before restic wrote into it" is an ordering the call log can show and
# an OUTCOME only a stub that empties can show -- and the outcome is the
# stronger of the two, because it fails when the two calls are merely swapped
# AND when the second one is aimed at the wrong path.
_DOCKER = """#!/bin/sh
printf '%s %s\\n' docker "$*" >> "$WAKU_CALLS"
mount=""
previous=""
for argument in "$@"; do
  if [ "$previous" = --volume ]; then mount=${argument%%:*}; fi
  previous=$argument
done
case "$*" in
  *"find /staging -mindepth 1 -delete"*)
    if [ -n "$mount" ] && [ -d "$mount" ]; then
      find "$mount" -mindepth 1 -delete
    fi ;;
  *"where email"*) @EMAIL@ ;;
  *"admin restore"*) @ADMIN_RESTORE@ ;;
esac
exit 0
"""

# The default: one tenant's address resolves to one tenant id.
_EMAIL_FOUND = f'echo {TENANT}'
_EMAIL_MISSING = ": "
# A control.db row that is not a tenant id. It reaches the same `find -delete`
# as a flag would, through a different door.
_EMAIL_POISONED = "echo '../../srv'"

# THE TAG, NOT A PATH, is what each arm matches on. pytest spells the test's
# own name into tmp_path, so a stub that recognised the control restore by
# looking for "control" anywhere in the command line would answer differently
# for a test whose name happened to contain that word.
_RESTIC = """#!/bin/sh
printf '%s %s\\n' restic "$*" >> "$WAKU_CALLS"
target=""
previous=""
for argument in "$@"; do
  if [ "$previous" = --include ]; then target=$argument; fi
  previous=$argument
done
case "$*" in
  *"--tag control"*)
    mkdir -p "$target"
@CONTROL@
    ;;
  *"--tag tenant:"*)
    mkdir -p "$target/home" "$target/env"
@MANIFEST@
    ;;
esac
exit 0
"""

_CONTROL_ONLY = """    printf 'SQLite format 3' > "$target/control.db\""""
_CONTROL_AND_LEDGER = _CONTROL_ONLY + """
    printf 'SQLite format 3' > "$target/ledger.db\""""
_NO_CONTROL_DB = "    : "

_MANIFEST_WRITTEN = """    printf '{"version":1,"parts":["home","env"],"state_db":true}' \\
      > "$target/manifest.json\""""
# A backup that died after the spawner's first line: both part directories
# exist, empty, and there is no manifest. This is the shape three rounds of
# shape checks accepted.
_NO_MANIFEST = "    : "
# `-f` follows a symlink, so presence alone accepts a link pointing at a
# perfectly good manifest above a slot that holds nothing of this tenant's.
_MANIFEST_IS_A_SYMLINK = """    printf '{"version":1,"parts":["home","env"],"state_db":true}' \\
      > "$target/elsewhere.json"
    ln -sf elsewhere.json "$target/manifest.json\""""

# THE LEDGER ARM IS FIRST, so a fixture can make one database answer `ok` and
# the other not. With one arm for both, the guard over ledger.db could be
# deleted and nothing here would notice: every test that fails an integrity
# check would fail control.db's first and never reach it.
_SQLITE = """#!/bin/sh
printf '%s %s\\n' sqlite3 "$*" >> "$WAKU_CALLS"
case "$*" in
  *"from tenant"*) @ROWS@ ;;
  *ledger.db*) @LEDGER@ ;;
  *"integrity_check"*) @INTEGRITY@ ;;
esac
exit 0
"""

_ONE_ROW = f"echo {TENANT}"
_TWO_ROWS = f"printf '%s\\n' {OTHER} {TENANT}"
# A row that is not a tenant id, joined to the staging root would make a path
# this script empties.
_POISONED_ROW = f"printf '%s\\n' '../..' {TENANT}"

_STUBS = ["docker", "restic", "sqlite3", "flock", "id", "install", "curl", "sleep",
          "rm"]

_ROOT_DIRECTORIES = ("config", "staging", "control", "ledger")
_INSTALL_ENV_FULL = ("WAKU_SERVICES_IMAGE=waku-services:current\n"
                     "WAKU_GATEWAY_ADDRESS=127.0.0.1:8787\n")


def _env(tmp_path, *, install_env_extra=_INSTALL_ENV_FULL, backup_env=None,
         password="hunter2\n"):
    root = tmp_path / "waku"
    for name in _ROOT_DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    password_file = tmp_path / "restic-password"
    password_file.write_text(password, encoding="utf-8")
    if backup_env is None:
        backup_env = (f"RESTIC_REPOSITORY=/tmp/repo\n"
                      f"RESTIC_PASSWORD_FILE={password_file}\n")
    (root / "config" / "backup.env").write_text(backup_env, encoding="utf-8")
    env_file = tmp_path / "install.env"
    env_file.write_text(
        f"WAKU_ROOT={root}\nWAKU_SRC={tmp_path}/src\n"
        f"WAKU_COMPOSE={tmp_path}/src/hosted/deploy/compose.yaml\n"
        f"WAKU_DOMAIN=example.test\n{install_env_extra}",
        encoding="utf-8")
    return root, {"WAKU_INSTALL_ENV": str(env_file),
                  "WAKU_STAGING": str(root / "staging")}


def _bodies(*, control=_CONTROL_ONLY, manifest=_MANIFEST_WRITTEN, rows=_ONE_ROW,
            integrity="echo ok", ledger="echo ok", email=_EMAIL_FOUND,
            curl="exit 0", admin_restore="exit 0"):
    return {
        "docker": (_DOCKER.replace("@EMAIL@", email)
                   .replace("@ADMIN_RESTORE@", admin_restore)),
        "restic": _RESTIC.replace("@CONTROL@", control).replace("@MANIFEST@", manifest),
        "sqlite3": (_SQLITE.replace("@ROWS@", rows).replace("@INTEGRITY@", integrity)
                    .replace("@LEDGER@", ledger)),
        "id": "#!/bin/sh\necho 0\n",
        # flock's real job is taking a lock on fd 9; the stub records the call
        # and succeeds, so the ORDER can be asserted without the test blocking
        # on a lock it also holds.
        "flock": '#!/bin/sh\nprintf "%s %s\\n" flock "$*" >> "$WAKU_CALLS"\nexit 0\n',
        "install": '#!/bin/sh\nprintf "%s %s\\n" install "$*" >> "$WAKU_CALLS"\nexit 0\n',
        "curl": f'#!/bin/sh\nprintf "%s %s\\n" curl "$*" >> "$WAKU_CALLS"\n{curl}\n',
        # NOT RECORDED, and stubbed only so the readiness loop's sixty seconds
        # do not become sixty seconds of test. What is under test is that the
        # loop GIVES UP and refuses; how long it waits first is not something
        # any tier here can measure honestly.
        "sleep": "#!/bin/sh\nexit 0\n",
        # RECORDED AND THEN REALLY RUN. The -wal removal has to happen BEFORE
        # the install that replaces the database it belongs to, and an
        # assertion that the file is gone at the end is true in either order.
        # Recording the call puts it in the log where the order shows; exec'ing
        # the real rm keeps the outcome real.
        "rm": '#!/bin/sh\nprintf "%s %s\\n" rm "$*" >> "$WAKU_CALLS"\nexec /bin/rm "$@"\n',
    }


def _run(tmp_path, args, env, **kwargs):
    return shelllib.run(RESTORE, args, tmp_path=tmp_path, env=env,
                        stubs=_STUBS, bodies=_bodies(**kwargs))


def _at(calls, needle):
    return next(i for i, line in enumerate(calls) if needle in line)


def _lines(calls, needle):
    return [line for line in calls if needle in line]


# --- the order ----------------------------------------------------------------


def test_all_runs_the_five_stages_in_the_spec_s_order(tmp_path):
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)

    stop_fleet = _at(calls, "stop-all")
    stop_services = _at(calls, "stop gateway proxy")
    replaced = _at(calls, "install -o 10002")
    started = _at(calls, "start gateway proxy")
    tenant = _at(calls, f"admin restore {TENANT}")
    assert stop_fleet < stop_services < replaced < started < tenant


def test_the_control_snapshot_is_fetched_before_anything_is_stopped(tmp_path):
    """The spec's five stages are the DESTRUCTIVE ones, and pulling a snapshot
    into staging is not one of them. An empty repository, an unreachable one or
    a control.db that fails its integrity check must cost a refusal and no
    downtime, instead of a stopped fleet in front of a restore that cannot
    happen."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    fetched = _at(calls, "--tag control")
    checked = _at(calls, "integrity_check")
    assert fetched < checked < _at(calls, "stop-all")


def test_a_repository_with_no_control_snapshot_stops_nothing(tmp_path):
    """The reason the fetch moved above the stop. The platform is still
    serving when this refusal lands."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, control=_NO_CONTROL_DB)
    assert done.returncode != 0
    assert "holds no control.db" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not _lines(calls, "stop-all")
    assert not _lines(calls, "stop gateway proxy")
    assert not _lines(calls, "install -o")


def test_the_lock_is_taken_before_anything_is_written(tmp_path):
    """One flock, shared with backup.sh. A backup that ran during a restore
    would snapshot a half-restored tenant and call it the latest good copy."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert shelllib.calls(tmp_path)[0].startswith("flock")


# --- the manifest, and what asking the gateway costs ---------------------------


@pytest.mark.parametrize("manifest, why", [
    (_NO_MANIFEST, "a backup that died between its first mkdir and its last write"),
    (_MANIFEST_IS_A_SYMLINK, "a link standing in for the declaration"),
])
def test_a_snapshot_with_no_manifest_is_refused_before_the_gateway_is_asked(
        tmp_path, manifest, why):
    """The refusal that matters most in this file. Asking the gateway to
    restore is what archives the tenant's live tree and removes it; a snapshot
    from a backup that did not finish must be refused BEFORE that, or the
    tenant is left with nothing."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT], env, manifest=manifest)
    assert done.returncode != 0, why
    assert "no manifest.json" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "admin restore")


def test_one_tenant_never_stops_the_gateway(tmp_path):
    """A per-tenant restore is not an outage. The gateway stops that tenant's
    container itself, inside the admin verb."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert not _lines(calls, "stop gateway")
    assert not _lines(calls, "stop-all")
    assert _lines(calls, f"admin restore {TENANT}")


def test_the_slot_is_emptied_before_restic_writes_into_it(tmp_path):
    """restic merges into what is there. A slot holding a previous restore
    would hand the spawner a mixture of two backups.

    THE LEFTOVER IS THE ASSERTION, not the order of two log lines. The slot is
    planted with a file from an imaginary earlier restore, and the file has to
    be gone by the end -- which fails both when the two calls are swapped and
    when the second one is aimed at some other path.
    """
    root, env = _env(tmp_path)
    slot = root / "staging" / TENANT
    (slot / "home").mkdir(parents=True)
    (slot / "home" / "from-an-older-restore.txt").write_text("x", encoding="utf-8")
    done = _run(tmp_path, ["--tenant", TENANT], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    emptied = _at(calls, "find /staging -mindepth 1 -delete")
    restored = _at(calls, "restic restore")
    assert emptied < restored
    assert not (slot / "home" / "from-an-older-restore.txt").exists()


def test_the_snapshot_is_chosen_by_tag_and_by_host(tmp_path):
    """--host is not decoration when the snapshot is `latest`, which is the
    default. backup.sh stamps every snapshot with the VM's domain, and two VMs
    sharing one --restic-repository is a one-flag mistake nothing refuses --
    the `control` tag is the same string on both, so `latest` without --host
    can be the other VM's platform database."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    for line in _lines(calls, "restic restore"):
        assert "--host example.test" in line, line
        # --target / restores the absolute path the snapshot holds, which is
        # the slot; --include bounds it to that path.
        assert "--target /" in line, line


def test_the_tenant_list_is_read_from_the_restored_copy(tmp_path):
    """Not from the live database. It is the same bytes, it was
    integrity-checked a moment ago, and reading it leaves no root-owned -wal
    beside a database the gateway has just opened as UID 10002."""
    root, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    listed = _lines(shelllib.calls(tmp_path), "from tenant where status")
    assert listed, shelllib.calls(tmp_path)
    assert f"{root}/staging/control/control.db" in listed[0]
    assert f"{root}/control/control.db" not in listed[0]


def test_the_snapshot_flag_reaches_restic(tmp_path):
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT, "--snapshot", "0123abcd"], env)
    assert done.returncode == 0, done.stderr
    assert _lines(shelllib.calls(tmp_path), "restic restore 0123abcd")


# --- the two platform databases ------------------------------------------------


@pytest.mark.parametrize("answer", [
    # sqlite3's real answer for a corrupt page. `broken` CONTAINS `ok`, so an
    # unanchored `grep -q ok` accepts it and installs a corrupt copy of the
    # database that names every tenant over the live one.
    "echo '*** in database main *** page 4 is broken'",
    "echo 'row 3 missing from index tenant_email'",
    # Nothing at all: the shape pipefail cannot see, because nothing failed.
    ":",
])
def test_a_control_db_that_fails_its_integrity_check_is_not_installed(
        tmp_path, answer):
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, integrity=answer)
    assert done.returncode != 0
    assert "integrity_check" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not _lines(calls, "install -o")
    assert not _lines(calls, "stop-all")


def test_the_old_write_ahead_log_goes_before_the_database_it_belongs_to(tmp_path):
    """A -wal beside the OLD database is read as part of the NEW one. Removing
    it first means a run killed in the middle leaves a database that is older
    and consistent; the other order leaves one that is newer and corrupt."""
    root, env = _env(tmp_path)
    stale = root / "control" / "control.db-wal"
    stale.write_text("the old database's log", encoding="utf-8")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert not stale.exists()
    calls = shelllib.calls(tmp_path)
    assert _at(calls, "control.db-wal") < _at(calls, "install -o 10002")


def test_a_ledger_that_fails_its_integrity_check_stops_the_restore(tmp_path):
    """The guard over the SECOND database, which every other failing-integrity
    fixture in this file cannot reach: control.db is checked first, so a
    fixture that corrupts both dies before the ledger is ever opened."""
    _, env = _env(tmp_path, )
    done = _run(tmp_path, ["--all"], env, control=_CONTROL_AND_LEDGER,
                ledger="echo '*** in database main *** page 9 is broken'")
    assert done.returncode != 0
    assert "ledger.db does not pass" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "install -o")


def test_a_symlink_where_ledger_db_should_be_is_refused(tmp_path):
    """The same asymmetry `-f` creates for control.db, on the database whose
    absence is the normal case -- so a link is the one shape that turns "there
    is no ledger in this snapshot" into "install whatever this names"."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, control=_LEDGER_IS_A_SYMLINK)
    assert done.returncode != 0
    assert "ledger.db is a symlink" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "install -o")


def test_the_control_database_is_installed_as_the_gateways_own_user(tmp_path):
    root, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    line = _lines(shelllib.calls(tmp_path), "install -o 10002")[0]
    assert "-g 10002 -m 0600" in line
    assert line.endswith(f"{root}/control/control.db")


def test_a_ledger_in_the_snapshot_is_installed_as_the_proxys_own_user(tmp_path):
    root, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, control=_CONTROL_AND_LEDGER)
    assert done.returncode == 0, done.stderr
    line = _lines(shelllib.calls(tmp_path), "install -o 10003")[0]
    assert "-g 10003 -m 0600" in line
    assert line.endswith(f"{root}/ledger/ledger.db")


def test_a_snapshot_with_no_ledger_leaves_the_spend_ledger_alone(tmp_path):
    """Group D is deferred, so no proxy has ever written one. A database that
    is not in the snapshot is not a database to replace with nothing."""
    root, env = _env(tmp_path)
    live = root / "ledger" / "ledger.db"
    live.write_text("the ledger this VM already has", encoding="utf-8")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert not _lines(shelllib.calls(tmp_path), "install -o 10003")
    assert live.read_text(encoding="utf-8") == "the ledger this VM already has"


def test_a_ledger_left_by_an_earlier_restore_is_not_installed_as_this_one(
        tmp_path):
    """The union-of-every-backup defect, one level out. The control slot is
    emptied WHOLE rather than by `rm -f *.db`, so a ledger.db this snapshot
    does not carry cannot ride into the live tree on a previous run's copy --
    and neither can a stale -wal that belongs to a different database."""
    root, env = _env(tmp_path)
    slot = root / "staging" / "control"
    slot.mkdir(parents=True)
    (slot / "ledger.db").write_text("an older restore's ledger", encoding="utf-8")
    (slot / "control.db-wal").write_text("an older restore's log", encoding="utf-8")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert not _lines(shelllib.calls(tmp_path), "install -o 10003")
    assert not (slot / "control.db-wal").exists()


# THE LINK'S TARGET EXISTS, and that is what makes the two tests below able to
# fail. A link to a path that is not there is refused by the PRESENCE check
# whichever way the symlink check is written -- `-f` follows the link and
# answers false -- so the first version of these, pointing at /etc/hostname,
# passed with the symlink guard deleted on a machine that has no
# /etc/hostname. The decoy is created in the slot by the same stub, so it is
# there on every platform.
_CONTROL_IS_A_SYMLINK = """    printf 'SQLite format 3' > "$target/decoy.db"
    ln -sf decoy.db "$target/control.db\""""
_LEDGER_IS_A_SYMLINK = _CONTROL_ONLY + """
    printf 'SQLite format 3' > "$target/decoy.db"
    ln -sf decoy.db "$target/ledger.db\""""


def test_a_symlink_where_control_db_should_be_is_refused(tmp_path):
    """`-f` follows a link, so presence alone would let a snapshot install
    whatever the link names over the platform's own database."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, control=_CONTROL_IS_A_SYMLINK)
    assert done.returncode != 0
    assert "control.db is a symlink" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "install -o")


def test_a_symlinked_control_slot_is_refused(tmp_path):
    root, env = _env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "staging" / "control").symlink_to(elsewhere)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "is a symlink" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "restic")


# --- the gateway has to come back before any tenant is touched ------------------


def test_no_tenant_is_restored_when_the_gateway_does_not_come_back(tmp_path):
    """Between the install and the first tenant there is one thing that can go
    wrong and must not be walked past: the gateway failing to start on the
    database it was just handed. Every tenant restore below this line needs it
    to stop a container first."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, curl="exit 7")
    assert done.returncode != 0
    assert "did not answer" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "admin restore")


# --- the closed sets -----------------------------------------------------------


@pytest.mark.parametrize("value", [
    "..",
    "../tenants",
    "../../srv",
    "/srv/waku/tenants",
    "k3fq7x2mza4",            # eleven
    "k3fq7x2mza4bc",          # thirteen
    "",
    "k3fq7x2mza4A",           # twelve, and outside [a-z2-7] under LC_ALL=C
    "k3fq7x2mza,b",           # a comma is a tag separator to restic
    "mei@example",            # an address with no dot in the domain
    "mei@@example.com",
    "@example.com",
    "mei",
    "mei'@example.com",       # the quote that would end the SQL literal
    "mei example@test.com",
])
def test_tenant_takes_a_tenant_id_or_an_address_and_nothing_else(tmp_path, value):
    """A CLOSED SET WITH DEFAULT-DENY, and it is the one guard between an
    operator's typo and a directory that is not a staging slot being emptied.
    `--tenant ..` would hand waku_reset_staging_slot the staging root itself.

    Refused BEFORE root, so no test here needs to be root and no refusal needs
    a container to have been started to be visible.
    """
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", value], env)
    assert done.returncode != 0
    assert "--tenant takes a tenant id" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize("value", [
    "--target",               # a leading dash is a restic FLAG, not an id
    "-1",
    "latest extra",
    "0123abc",                # seven
    "0123abcg",               # not hex
    "0" * 65,
    "",
])
def test_snapshot_takes_latest_or_a_hex_id_and_nothing_else(tmp_path, value):
    """It is the positional argument of `restic restore`, and restic's restore
    flags include --target, which is where the snapshot's contents land."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all", "--snapshot", value], env)
    assert done.returncode != 0
    assert "a snapshot is 'latest' or 8 to 64 hex characters" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_control_db_row_that_is_not_a_tenant_id_is_not_acted_on(tmp_path):
    """A poisoned row is the same hole through a different door. The row is
    trusted for its content and checked for its shape, because it is joined to
    the staging root to make a path this restore empties -- and the tenants
    after it still get their data back."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--all"], env, rows=_POISONED_ROW)
    assert done.returncode != 0
    assert "is not a tenant id" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not _lines(calls, "admin restore ../..")
    assert not _lines(calls, "staging/../..")
    assert _lines(calls, f"admin restore {TENANT}")


def test_an_address_is_turned_into_an_id_before_it_becomes_a_path(tmp_path):
    """restore.sh --tenant mei@example.com is in the operator guide, and an
    email is not a staging slot, a restic tag or a bind mount. It is resolved
    inside the gateway's own container, as UID 10002 -- a root sqlite3 on the
    live control.db would leave a root-owned -wal in a directory the gateway
    has to write."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", "mei@example.com"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    lookup = _lines(calls, "where email")
    assert lookup, calls
    assert "--user 10002:10002 gateway sqlite3" in lookup[0]
    assert _lines(calls, f"--tag tenant:{TENANT}")
    assert _lines(calls, f"admin restore {TENANT}")
    assert not _lines(calls, "mei@example.com --")


def test_an_address_that_matches_nobody_is_a_refusal_not_a_path(tmp_path):
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", "nobody@example.com"], env,
                email=_EMAIL_MISSING)
    assert done.returncode != 0
    assert "has the email nobody@example.com" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "restic")


def test_an_address_that_resolves_to_something_that_is_not_an_id_is_refused(
        tmp_path):
    """The funnel is what makes the lookup safe, not the lookup. Whatever comes
    back out of control.db is still only a candidate."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", "mei@example.com"], env,
                email=_EMAIL_POISONED)
    assert done.returncode != 0
    assert "is not a tenant id" in done.stderr
    assert not _lines(shelllib.calls(tmp_path), "restic")


# --- the arguments -------------------------------------------------------------


def test_neither_mode_is_a_default(tmp_path):
    """--all and --tenant do very different things, and a restore.sh with no
    argument that picked one of them is a mistake waiting for a tired
    operator."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, [], env)
    assert done.returncode != 0
    assert "--tenant or --all is required" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize("args", [
    ["--all", "--tenant", TENANT],
    ["--tenant", TENANT, "--all"],
    ["--tenant", TENANT, "--tenant", OTHER],
])
def test_two_modes_on_one_line_are_refused_rather_than_ranked(tmp_path, args):
    """Neither is a narrowing of the other, so last-one-wins would silently
    pick one of two readings -- and one of them takes the whole platform
    down."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, args, env)
    assert done.returncode != 0
    assert "two different restores" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize("args, refusal", [
    (["--tenant"], "--tenant needs a value"),
    (["--all", "--snapshot"], "--snapshot needs a value"),
    (["--wipe"], "unknown argument: --wipe"),
])
def test_an_argument_that_is_not_one_is_refused(tmp_path, args, refusal):
    _, env = _env(tmp_path)
    done = _run(tmp_path, args, env)
    assert done.returncode != 0
    assert refusal in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_help_exits_cleanly_and_touches_nothing(tmp_path):
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--help"], env)
    assert done.returncode == 0, done.stderr
    assert "--snapshot ID" in done.stdout
    assert shelllib.calls(tmp_path) == []


def test_it_refuses_to_run_as_anyone_but_root(tmp_path):
    _, env = _env(tmp_path)
    done = shelllib.run(RESTORE, ["--all"], tmp_path=tmp_path, env=env,
                        stubs=_STUBS,
                        bodies={**_bodies(), "id": "#!/bin/sh\necho 1000\n"})
    assert done.returncode != 0
    assert "run this as root" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- what has to be in place before the lock is taken ---------------------------


@pytest.mark.parametrize("kwargs, refusal", [
    ({"backup_env": "RESTIC_PASSWORD_FILE=/nowhere\n"},
     "does not set RESTIC_REPOSITORY"),
    ({"backup_env": "RESTIC_REPOSITORY=/tmp/repo\n"},
     "does not set RESTIC_PASSWORD_FILE"),
    ({"password": ""}, "is empty"),
])
def test_a_missing_restic_name_is_refused_with_the_lock_untaken(
        tmp_path, kwargs, refusal):
    """backup.sh's ordering, for the same reason turned around: a restic name
    discovered after the lock is a run that has already emptied a staging slot
    for a restore it cannot fetch. Nothing at all has run when this lands."""
    _, env = _env(tmp_path, **kwargs)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert refusal in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_all_refuses_an_install_env_that_predates_the_gateway_address(tmp_path):
    """install.env is written once and never rewritten, so a file written
    before a name existed survives every rerun. --all dereferences
    WAKU_GATEWAY_ADDRESS in the readiness probe, which runs AFTER both
    databases have been replaced -- the worst place on this script's path to
    die on bash's own `set -u` message."""
    _, env = _env(tmp_path,
                  install_env_extra="WAKU_SERVICES_IMAGE=waku-services:current\n")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "WAKU_GATEWAY_ADDRESS" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_one_tenant_does_not_refuse_over_a_name_only_all_reads(tmp_path):
    """The other half of the F2 finding, which a fixed superset reproduces in
    a mirror: a --tenant restore never probes the gateway's address, and
    refusing to give one person their data back over a name this path does not
    read would be the same defect wearing the other face."""
    _, env = _env(tmp_path,
                  install_env_extra="WAKU_SERVICES_IMAGE=waku-services:current\n")
    done = _run(tmp_path, ["--tenant", TENANT], env)
    assert done.returncode == 0, done.stderr
    assert _lines(shelllib.calls(tmp_path), f"admin restore {TENANT}")


# --- a failed tenant does not stop the run --------------------------------------


def test_a_tenant_that_cannot_be_restored_does_not_stop_the_others(tmp_path):
    """After a disaster, one unrestorable snapshot must not mean nobody else
    on the VM gets their data back. The run still exits non-zero and names
    them."""
    _, env = _env(tmp_path)
    fails_for_one = f'''    if [ "$target" = "$WAKU_STAGING/{OTHER}" ]; then
      :
    else
{_MANIFEST_WRITTEN}
    fi'''
    done = _run(tmp_path, ["--all"], env, rows=_TWO_ROWS, manifest=fails_for_one)
    assert done.returncode != 0
    assert f"these tenants were not restored: {OTHER}" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not _lines(calls, f"admin restore {OTHER}")
    assert _lines(calls, f"admin restore {TENANT}")


def test_the_character_set_stays_closed_under_a_utf8_locale(tmp_path):
    """LC_ALL=C in is_tenant_id, and it has a fixture because the length check
    does NOT refuse this input: `k3fq7x2mza4A` is exactly twelve characters, so
    the bracket expression is the only thing between it and a bind mount.

    STATED HONESTLY, because it changes what this test is worth: it
    discriminates on bash 3.2, which is what a maintainer's laptop runs and
    what `bash -n` is checked against. Ubuntu 24.04's bash 5.2 has
    globasciiranges on by default and refuses the uppercase letter either way,
    so on CI this passes for a second reason. Weaker there is not nothing.
    """
    _, env = _env(tmp_path)
    env = {**env, "LC_ALL": "en_US.UTF-8"}
    done = _run(tmp_path, ["--tenant", "k3fq7x2mza4A"], env)
    assert done.returncode != 0
    assert "--tenant takes a tenant id" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_gateway_that_refuses_the_restore_is_a_failure_not_a_success(tmp_path):
    """`waku_admin restore` answers a JSON object and exits 1 when it carries
    an error. A restore that walked past that would clear the staging slot --
    the only copy on this VM of what it was trying to put back -- and log
    `restored <id>`."""
    _, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT], env, admin_restore="exit 1")
    assert done.returncode != 0
    assert TENANT in done.stderr
    assert "restored" not in done.stdout


def test_the_slot_is_cleared_once_the_tenant_has_their_data_back(tmp_path):
    """The slot is a handoff area, not a copy. One left full is one tenant's
    worth of disk held for nothing, on the filesystem the tenants live on, and
    the next backup merges into it."""
    root, env = _env(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT], env)
    assert done.returncode == 0, done.stderr
    slot = root / "staging" / TENANT
    assert list(slot.iterdir()) == []
    calls = shelllib.calls(tmp_path)
    assert _at(calls, f"admin restore {TENANT}") < _at(
        calls, "find /staging -mindepth 1 -delete")
