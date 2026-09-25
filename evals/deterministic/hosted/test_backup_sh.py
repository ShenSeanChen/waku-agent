"""backup.sh's order and its refusals, with docker, restic and sqlite3 stubbed.

EVERY DEFECT designs/backup-restore-integrity.md DESCRIBES IS AN ORDERING OR A
SOURCE-OF-TRUTH DEFECT, not a shell bug: a slot cleared before its snapshot, a
snapshot taken from a slot whose backup died, a fact read off the shape of a
directory instead of off the manifest the writer declared. Those are exactly
what a stubbed run can see and what a real one cannot, because on a real run
they all look like success.

WHERE EACH REFUSAL HAS TO LAND IS ITSELF AN ASSERTION HERE. A missing restic
name is a refusal in any order; a missing restic name discovered AFTER the
staging lock is a run that has already emptied one tenant's slot to make room
for a backup it then cannot send anywhere. Several tests below assert that no
`flock` line was ever written, which is the only way that distinction shows.
"""

from __future__ import annotations

import os
import stat

import pytest
import shelllib

BACKUP = shelllib.DEPLOY / "backup.sh"

TENANT = "k3fq7x2mza4b"

# The docker stub stands for three different containers: the gateway running
# `python -m hosted.gateway.admin`, the two sqlite3 `.backup` calls, and the
# throwaway container waku_reset_staging_slot runs as UID 10001. @ADMIN@ is the
# only arm a test varies, so the other three cannot drift between fixtures.
#
# The `.backup` arm CREATES THE FILE IT WAS ASKED TO WRITE, because backup.sh
# copies it afterwards and a fixture that skipped that step would be asserting
# against a script that had already failed.
_DOCKER = """#!/bin/sh
printf '%s %s\\n' docker "$*" >> "$WAKU_CALLS"
q="'"
# THE LAST ARGUMENT, READ BY WALKING THE LIST. `${*##* }` looks like it takes
# the last word and does not: `#` and `%` applied to $* run over each
# positional parameter separately and rejoin them, so a stub written that way
# answered with the whole command line and every fixture agreed with it.
last=""
for argument in "$@"; do last=$argument; done
case "$*" in
  *"admin backup"*)
    id=$last
@ADMIN@
    ;;
  *".backup"*)
    target=${last#*$q}
    target=${target%$q}
    : > "$target" ;;
  *"PRAGMA integrity_check"*) echo @INTEGRITY@ ;;
esac
exit 0
"""

_OK = """    mkdir -p "$WAKU_STAGING/$id/home" "$WAKU_STAGING/$id/env"
    printf '{"version":1,"parts":["home","env"],"state_db":true}' \\
      > "$WAKU_STAGING/$id/manifest.json"
    echo '{"ok": true}'"""

# A backup that dies after the spawner's first line: both part directories
# exist, empty, and there is no manifest. This is the shape three rounds of
# shape checks accepted.
_DIES_HALFWAY = """    mkdir -p "$WAKU_STAGING/$id/home" "$WAKU_STAGING/$id/env"
    rm -f "$WAKU_STAGING/$id/manifest.json"
    echo '{"ok": true}'"""

# A manifest that is a SYMLINK to a perfectly good manifest. `-f` follows a
# symlink, so an existence check alone accepts this and snapshots whatever the
# link points at; the spawner's own _read_manifest refuses it for the same
# reason this does.
_MANIFEST_IS_A_SYMLINK = """    mkdir -p "$WAKU_STAGING/$id/home" "$WAKU_STAGING/$id/env"
    printf '{"version":1,"parts":["home","env"],"state_db":true}' \\
      > "$WAKU_STAGING/$id/elsewhere.json"
    ln -sf elsewhere.json "$WAKU_STAGING/$id/manifest.json"
    echo '{"ok": true}'"""

_WEDGED = """    echo '{"error": "wedged"}'
    exit 1"""

_SQLITE = """#!/bin/sh
printf '%s %s\\n' sqlite3 "$*" >> "$WAKU_CALLS"
case "$*" in
  *"from tenant"*) @ROWS@ ;;
  *) echo ok ;;
esac
exit 0
"""

_ONE_ROW = f"echo {TENANT}"
# A second tenant, sorted before the first, so the run has somewhere to go on
# after the failure.
OTHER = "aaaaaaaaaaaa"
_TWO_ROWS = f"printf '%s\\n' {OTHER} {TENANT}"

# The first tenant's slot is wedged and the second one's is not. A failed
# tenant is collected and the run carries on, because the alternative is that
# one wedged slot means nobody on the VM has a backup tonight.
_WEDGED_FOR_ONE = f'''    if [ "$id" = "{OTHER}" ]; then
      echo '{{"error": "wedged"}}'
      exit 1
    fi
''' + _OK

_STUBS = ["docker", "restic", "sqlite3", "flock", "find", "id"]


def _root(tmp_path, *, backup_env=None, install_env_extra="WAKU_SERVICES_IMAGE=waku-services:current\n",
          password="hunter2\n"):
    root = tmp_path / "waku"
    for name in ("config", "staging", "archive", "control/backup", "ledger/backup"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "control" / "control.db").write_bytes(b"SQLite format 3\x00")
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


# The repository answers `cat config` when WAKU_REPO_EXISTS is 1 and refuses
# when it is 0, and `init` succeeds when WAKU_INIT_RC is 0. Everything else
# restic is asked to do records and succeeds, as the plain recorder does.
_RESTIC = """#!/bin/sh
printf '%s %s\\n' restic "$*" >> "$WAKU_CALLS"
case "$*" in
  "cat config") exit "$(( 1 - ${WAKU_REPO_EXISTS:-1} ))" ;;
  "init") exit "${WAKU_INIT_RC:-0}" ;;
esac
exit 0
"""


def _bodies(admin=_OK, rows=_ONE_ROW, integrity="ok"):
    return {"docker": _DOCKER.replace("@ADMIN@", admin).replace("@INTEGRITY@", integrity),
            "sqlite3": _SQLITE.replace("@ROWS@", rows),
            "restic": _RESTIC,
            "id": "#!/bin/sh\necho 0\n",
            # flock's real job is taking a lock on fd 9; the stub records the
            # call and succeeds, so the ORDER can be asserted without the test
            # blocking on a lock it also holds.
            "flock": '#!/bin/sh\nprintf "%s %s\\n" flock "$*" >> "$WAKU_CALLS"\nexit 0\n'}


def _run(tmp_path, args, env, **kwargs):
    return shelllib.run(BACKUP, args, tmp_path=tmp_path, env=env,
                        stubs=_STUBS, bodies=_bodies(**kwargs))


def _deletes(calls):
    return [line for line in calls if "find /staging -mindepth 1 -delete" in line]


# `restic cat config` is the readiness probe backup.sh runs once, under the
# lock, before anything is staged: it READS and nothing else, and a run that
# refuses right after it has written nothing anywhere. Tests that mean "no
# snapshot was taken" say so with this rather than with `startswith("restic")`,
# which would also match the probe and turn "nothing was written" into
# "restic was never invoked" -- two different claims.
_PROBE = "restic cat config"


def _restic_writes(calls):
    return [line for line in calls
            if line.startswith("restic") and line != _PROBE]


# --- the order ---------------------------------------------------------------


def test_the_lock_is_taken_before_anything_is_written(tmp_path):
    """One flock, shared with restore.sh. A backup that ran during a restore
    would snapshot a half-restored tenant and call it the latest good copy."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert shelllib.calls(tmp_path)[0].startswith("flock")


def test_a_tenant_is_snapshotted_before_its_slot_is_cleared(tmp_path):
    """The one-slot rule: the slot is the handoff area and restic is what keeps
    copies. Clearing first is the shape that leaves nothing restorable."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    snapshot = next(i for i, line in enumerate(calls)
                    if line.startswith("restic") and f"tenant:{TENANT}" in line)
    cleared = next(i for i, line in enumerate(calls)
                   if line.startswith("docker") and "find /staging -mindepth 1 -delete" in line)
    assert snapshot < cleared


def test_a_backup_that_did_not_finish_is_not_snapshotted(tmp_path):
    """The manifest is the backup's own declaration. Two empty part directories
    are what an interrupted backup leaves AND what an empty tenant leaves, and
    no amount of looking tells them apart."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env, admin=_DIES_HALFWAY)
    assert done.returncode != 0
    assert TENANT in done.stderr
    assert not [line for line in shelllib.calls(tmp_path)
                if line.startswith("restic backup") and "tenant:" in line]


def test_a_manifest_that_is_a_symlink_is_not_a_manifest(tmp_path):
    """`-f` follows a symlink, so presence alone accepts a link pointing at
    something outside the slot and snapshots whatever it names. The spawner's
    _read_manifest refuses a symlink for the same reason."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env, admin=_MANIFEST_IS_A_SYMLINK)
    assert done.returncode != 0
    assert TENANT in done.stderr
    assert not [line for line in shelllib.calls(tmp_path)
                if line.startswith("restic backup") and "tenant:" in line]


def test_a_failed_backup_resets_the_slot_and_retries_exactly_once(tmp_path):
    """A slot whose modes the tenant's own files wedged fails at the same line
    forever, and no spawner verb repairs it. Exactly once, because a loop over
    a permanent failure is a nightly run that never reaches the next tenant."""
    root, env = _root(tmp_path)
    # THE SLOT HAS TO BE THERE FOR THE REPAIR TO REACH IT: waku_reset_staging_slot
    # returns 0 on an absent one, so a fixture without this asserts nothing about
    # the retry at all -- it agrees with a script that never calls the repair.
    (root / "staging" / TENANT / "home").mkdir(parents=True)
    done = _run(tmp_path, ["--all"], env, admin=_WEDGED)
    assert done.returncode != 0
    calls = shelllib.calls(tmp_path)
    assert len([line for line in calls if "admin backup" in line]) == 2
    assert len(_deletes(calls)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        # WHAT sqlite3 ACTUALLY PRINTS, and the reason the match is anchored:
        # `broken` contains `ok`, so a substring match calls this database
        # healthy and snapshots it. The earlier fixture here was the truncated
        # first line, which carries no `ok` and so was refused by the guard AND
        # by a guard weakened to `grep -q ok` -- a fixture that reached the
        # guard and then agreed with the mutant.
        "'*** in database main *** page 4 is broken'",
        "'row 3 missing from index tenant_email'",
        # A `.backup` that was killed leaves a file sqlite3 cannot open at all,
        # and an empty answer is the shape `pipefail` cannot see.
        "''",
    ])
def test_the_control_databases_copy_is_checked_before_it_is_snapshotted(tmp_path, answer):
    """A single SQLite file carries its own completeness check, and a `.backup`
    that was killed leaves a file that fails it. The pipeline is the guard: an
    integrity_check that answers anything but the whole line `ok` must stop the
    run."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env, integrity=answer)
    assert done.returncode != 0
    assert not _restic_writes(shelllib.calls(tmp_path))


def test_retention_is_seven_daily_and_four_weekly_per_host_and_tag(tmp_path):
    """The tag half keeps one tenant's nightly run from ageing out another's
    snapshots. The HOST half keeps one VM's nightly run from ageing out
    another VM's: --group-by replaces restic's default host,paths grouping
    rather than adding to it, and this runs with --prune and no --host filter,
    so two installs sharing one repository would prune each other."""
    _, env = _root(tmp_path)
    _run(tmp_path, ["--all"], env)
    forget = next(line for line in shelllib.calls(tmp_path) if "forget" in line)
    assert "--group-by host,tags" in forget
    assert "--keep-daily 7" in forget
    assert "--keep-weekly 4" in forget
    assert "--prune" in forget


def test_one_tenant_is_one_snapshot_and_no_pruning(tmp_path):
    """--tenant is the manual snapshot before a risky change. It must not be a
    nightly run in disguise: pruning from it would apply the retention policy
    at a moment nobody chose."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--tenant", TENANT], env)
    assert done.returncode == 0, done.stderr
    slot = f"{env['WAKU_STAGING']}/{TENANT}"
    assert _restic_writes(shelllib.calls(tmp_path)) == [
        f"restic backup --tag tenant:{TENANT} --host example.test {slot}"]


def test_reset_staging_empties_one_slot_and_does_nothing_else(tmp_path):
    root, env = _root(tmp_path)
    (root / "staging" / TENANT / "home").mkdir(parents=True)
    done = _run(tmp_path, ["--reset-staging", TENANT], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith("restic")]
    assert _deletes(calls)


# --- the refusals that have to land before the lock --------------------------


def test_a_backup_env_that_is_not_there_is_refused_before_the_lock(tmp_path):
    root, env = _root(tmp_path)
    (root / "config" / "backup.env").unlink()
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    # THE GUARD'S OWN SENTENCE. Without the check, `. "$file"` fails and bash
    # names the file too -- so asserting the PATH agrees with the mutant.
    assert f"{root}/config/backup.env is not readable" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_a_backup_env_that_cannot_be_read_is_refused_before_the_lock(tmp_path):
    """The case an existence check accepts: the file is there, and this process
    cannot open it. config/backup.env is 0600 root:root, so the process that
    cannot read it is one that should not have got this far."""
    root, env = _root(tmp_path)
    (root / "config" / "backup.env").chmod(0o000)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert f"{root}/config/backup.env is not readable" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_backup_env_with_no_repository_is_refused_before_the_lock(tmp_path):
    password_file = tmp_path / "restic-password"
    _, env = _root(tmp_path,
                   backup_env=f"RESTIC_PASSWORD_FILE={password_file}\n")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "RESTIC_REPOSITORY" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_repository_set_to_nothing_is_refused_like_one_that_is_absent(tmp_path):
    """The name is there and the value is empty, which `[ -n ]` catches and a
    plain `set -u` does not. Both halves are the same refusal here."""
    password_file = tmp_path / "restic-password"
    _, env = _root(tmp_path,
                   backup_env=f"RESTIC_REPOSITORY=\nRESTIC_PASSWORD_FILE={password_file}\n")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "RESTIC_REPOSITORY" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_an_empty_restic_password_file_is_refused_before_the_lock(tmp_path):
    """THE ONE THAT DOES NOT FAIL ON ITS OWN. restic initialises and writes a
    repository with an empty password, reports success every night, and the
    operator learns at restore time."""
    _, env = _root(tmp_path, password="")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "empty" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_password_file_that_is_not_there_is_refused_before_the_lock(tmp_path):
    """THE SENTENCE, NOT THE NAME. The empty-file refusal below names
    RESTIC_PASSWORD_FILE too, and a missing file is empty as far as `-s` is
    concerned -- so a test that asserted the NAME passed with this guard
    deleted and told the operator their password file was empty when it was
    not there at all."""
    _, env = _root(tmp_path,
                   backup_env="RESTIC_REPOSITORY=/tmp/repo\n"
                              "RESTIC_PASSWORD_FILE=/nonexistent/restic-password\n")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "cannot be opened for reading" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_a_password_file_that_cannot_be_read_is_refused_before_the_lock(tmp_path):
    """The one shape `-s` alone accepts: a file with bytes in it that this
    process cannot open. restic would fail at the repository, hours later and
    in a timer journal."""
    password_file = tmp_path / "restic-password"
    _, env = _root(tmp_path)
    password_file.chmod(0o000)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "cannot be opened for reading" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_an_install_env_missing_the_image_this_script_runs_is_refused(tmp_path):
    """backup.sh declares WAKU_SERVICES_IMAGE at its own call, because
    waku_reset_staging_slot runs the repair container from it. Undeclared, the
    run died on bash's own `set -u` message at the first wedged slot -- which
    is to say on the one night it mattered."""
    _, env = _root(tmp_path, install_env_extra="")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert "WAKU_SERVICES_IMAGE" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the closed set on an id -------------------------------------------------


def test_an_id_that_is_not_a_tenant_id_is_refused_before_a_slot_is_emptied(tmp_path):
    """`--reset-staging ..` would hand waku_reset_staging_slot the staging root
    itself, and `find -mindepth 1 -delete` inside it would empty every tenant's
    slot at once. The shape is core/tenant.TENANT_ID_RE and nothing else is a
    tenant."""
    for value in ("..", "../../srv", "k3fq7x2mza4", "k3fq7x2mza4bb", "K3FQ7X2MZA4B",
                  "k3fq7x2mza41", "", "k3fq7x2mza4b "):
        _, env = _root(tmp_path)
        done = _run(tmp_path, ["--reset-staging", value], env)
        assert done.returncode != 0, value
        assert "a tenant id is twelve characters" in done.stderr, value
        assert _deletes(shelllib.calls(tmp_path)) == [], value


def test_the_character_set_stays_closed_under_a_utf8_locale(tmp_path):
    """`k3fq7x2mza4A` IS TWELVE CHARACTERS, so the length check passes it and
    the bracket expression is the only thing that refuses it. A bracket
    expression follows LC_CTYPE, and a root login shell on Ubuntu commonly has
    a UTF-8 one, under which `a-z` widens by collation to take `A`.

    WHICH BASH THIS DISCRIMINATES ON, said plainly because it changes what the
    test is worth: bash 3.2, which is what `bash -n` and these tests run under
    on a maintainer's macOS laptop. Ubuntu 24.04's bash 5.2 has
    `globasciiranges` on by default and refuses `A` either way, so on CI this
    test passes for a second reason. Weaker there is not the same as
    impossible, and the guard is what makes the two agree.
    """
    _, env = _root(tmp_path)
    env["LC_ALL"] = "en_US.UTF-8"
    done = _run(tmp_path, ["--reset-staging", "k3fq7x2mza4A"], env)
    assert done.returncode != 0
    assert "a tenant id is twelve characters" in done.stderr
    assert _deletes(shelllib.calls(tmp_path)) == []


def test_a_tenant_flag_that_is_not_a_tenant_id_is_refused(tmp_path):
    """The same path reaches the same repair container: --tenant's retry arm
    calls waku_reset_staging_slot on the slot it built from this value."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--tenant", "../../srv"], env)
    assert done.returncode != 0
    assert "a tenant id is twelve characters" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_control_db_row_that_is_not_an_id_is_reported_and_not_touched(tmp_path):
    """The list comes from a copy of the platform's own database, which is why
    it is trusted for CONTENT -- but it is joined to the staging root to make a
    path this script empties, so it is checked for SHAPE like everything else
    that reaches that line."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env, rows="echo ../../srv")
    assert done.returncode != 0
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if "admin backup" in line]
    assert _deletes(calls) == []


# --- the argument parser -----------------------------------------------------


def test_an_unknown_argument_is_refused(tmp_path):
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--everything"], env)
    assert done.returncode != 0
    assert "unknown argument: --everything" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_flag_given_without_a_value_says_so(tmp_path):
    for flag in ("--tenant", "--snapshot-staged", "--reset-staging"):
        _, env = _root(tmp_path)
        done = _run(tmp_path, [flag], env)
        assert done.returncode != 0, flag
        assert f"{flag} needs a value" in done.stderr, flag
        assert shelllib.calls(tmp_path) == [], flag


# --- the guards the code has and the tests above did not reach ---------------


def test_a_failed_tenant_does_not_stop_the_run(tmp_path):
    """One wedged staging slot must not mean nobody on the VM has a backup
    tonight. The failure is collected, the next tenant is attempted, and the
    exit code at the end is what tells the operator."""
    root, env = _root(tmp_path)
    (root / "staging" / OTHER / "home").mkdir(parents=True)
    done = _run(tmp_path, ["--all"], env, admin=_WEDGED_FOR_ONE, rows=_TWO_ROWS)
    assert done.returncode != 0
    assert OTHER in done.stderr
    assert TENANT not in done.stderr
    snapshots = [line for line in shelllib.calls(tmp_path)
                 if line.startswith("restic backup") and "tenant:" in line]
    slot = f"{env['WAKU_STAGING']}/{TENANT}"
    assert snapshots == [f"restic backup --tag tenant:{TENANT} --host example.test {slot}"]


def test_a_stale_database_in_the_control_slot_is_not_snapshotted_as_current(tmp_path):
    """DEFECT FIVE OF THE FIVE, in the one slot root owns. Staging was never
    cleared between backups, so the directories held the union of every backup
    ever taken while the manifest described the latest. Here the union would be
    a ledger.db from a deployment that has since removed group D's proxy,
    snapshotted every night afterwards as though it were tonight's."""
    root, env = _root(tmp_path)
    stale = root / "staging" / "control" / "ledger.db"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"a ledger from a previous deployment")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert not stale.exists()


def test_the_ledger_is_copied_inside_the_proxys_container_when_it_exists(tmp_path):
    """No process on this VM can open both databases, which is the point of the
    one-writer rule: control.db as UID 10002 in the gateway's container,
    ledger.db as UID 10003 in the proxy's. backup.sh, as root, only ever copies
    the finished files out."""
    root, env = _root(tmp_path)
    (root / "ledger" / "ledger.db").write_bytes(b"SQLite format 3\x00")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    # ANCHORED ON THE CONTAINER AND ON WHOLE PATHS. A needle of "ledger" alone
    # matched the control.db line too, because pytest's tmp_path spells the
    # test's own name and this test's name has "ledger" in it.
    copies = [line for line in shelllib.calls(tmp_path)
              if ".backup " in line and "--user 10003:10003" in line]
    assert len(copies) == 1
    assert f"proxy sqlite3 {root}/ledger/ledger.db" in copies[0]
    assert copies[0].endswith(f".backup '{root}/ledger/backup/ledger.db'")
    assert (root / "staging" / "control" / "ledger.db").exists()


def test_the_control_slot_is_readable_only_by_root(tmp_path):
    """It holds a copy of every tenant's row and, once group D lands, of every
    metered call. It is created by this script and by nothing else."""
    root, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE((root / "staging" / "control").stat().st_mode) == 0o700


def test_archives_are_pruned_by_age_and_nothing_is_descended_into(tmp_path):
    """The archive directory holds only the .tar.zst FILES the spawner writes
    (spec, "archive on delete", 30 days). -maxdepth 1 -type f is what keeps a
    prune from walking into anything."""
    _, env = _root(tmp_path)
    _run(tmp_path, ["--all"], env)
    prune = next(line for line in shelllib.calls(tmp_path)
                 if line.startswith("find") and "archive" in line)
    assert "-maxdepth 1" in prune
    assert "-type f" in prune
    assert "-name *.tar.zst" in prune
    assert "-mtime +30" in prune
    assert "-delete" in prune


def test_the_run_is_refused_when_it_is_not_root(tmp_path):
    """It reads an env_file only root can read, copies files owned by three
    different users and talks to the Docker socket."""
    _, env = _root(tmp_path)
    bodies = _bodies()
    bodies["id"] = "#!/bin/sh\necho 1000\n"
    done = shelllib.run(BACKUP, ["--all"], tmp_path=tmp_path, env=env,
                        stubs=_STUBS, bodies=bodies)
    assert done.returncode != 0
    assert "run this as root" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the lock itself ---------------------------------------------------------


def test_a_lock_another_run_holds_stops_this_one(tmp_path):
    """backup.sh and restore.sh share one lock so they never run at once: a
    backup that ran during a restore would snapshot a half-restored tenant and
    call it the latest good copy."""
    _, env = _root(tmp_path)
    bodies = _bodies()
    bodies["flock"] = ('#!/bin/sh\nprintf "%s %s\\n" flock "$*" >> "$WAKU_CALLS"\nexit 1\n')
    done = shelllib.run(BACKUP, ["--all"], tmp_path=tmp_path, env=env,
                        stubs=_STUBS, bodies=bodies)
    assert done.returncode != 0
    assert "another backup or restore holds" in done.stderr
    assert not [line for line in shelllib.calls(tmp_path) if line.startswith("restic")]


def test_a_manual_repair_does_not_wait_an_hour_behind_the_nightly_run(tmp_path):
    """--reset-staging is what an operator runs while looking at a failed
    backup. The nightly run waits an hour for the lock; a person does not."""
    root, env = _root(tmp_path)
    (root / "staging" / TENANT).mkdir(parents=True)
    _run(tmp_path, ["--reset-staging", TENANT], env)
    assert shelllib.calls(tmp_path)[0] == "flock -w 60 9"

    _, other = _root(tmp_path / "nightly")
    shelllib.run(BACKUP, ["--all"], tmp_path=tmp_path / "nightly", env=other,
                 stubs=_STUBS, bodies=_bodies())
    assert shelllib.calls(tmp_path / "nightly")[0] == "flock -w 3600 9"


def test_a_missing_staging_directory_is_named_rather_than_a_file_descriptor(tmp_path):
    """Without the check this is bash's own redirection error on fd 9, which
    names neither the directory nor the script that wanted it."""
    root, env = _root(tmp_path)
    (root / "staging").rmdir()
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    # THE SENTENCE, NOT THE PATH: bash's redirection error names
    # <root>/staging/.lock, which contains <root>/staging, so an assertion on
    # the path alone agreed with the mutant that had no guard at all.
    assert f"{root}/staging is not a directory" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the repair container ----------------------------------------------------


def test_the_repair_runs_as_the_only_uid_allowed_inside_a_tenants_files(tmp_path):
    """The spec's rule, not caution: "no host process with more privilege than
    UID 10001 opens a path inside a tenant's directories", and a staging slot
    holds a copy of exactly those."""
    root, env = _root(tmp_path)
    (root / "staging" / TENANT).mkdir(parents=True)
    done = _run(tmp_path, ["--reset-staging", TENANT], env)
    assert done.returncode == 0, done.stderr
    repair = _deletes(shelllib.calls(tmp_path))[0]
    assert "--user 10001:10001" in repair
    assert "--network none" in repair
    assert "--cap-drop ALL" in repair
    assert "--security-opt no-new-privileges:true" in repair
    assert f"--volume {root}/staging/{TENANT}:/staging" in repair


def test_a_staging_slot_that_is_a_symlink_is_not_emptied(tmp_path):
    """`find -delete` inside a container follows nothing, but the BIND MOUNT is
    resolved on the host before the container starts: a slot that is a symlink
    to /srv/waku/tenants would mount that and empty it."""
    root, env = _root(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (root / "staging" / TENANT).symlink_to(elsewhere)
    done = _run(tmp_path, ["--reset-staging", TENANT], env)
    assert done.returncode != 0
    assert "symlink" in done.stderr
    assert _deletes(shelllib.calls(tmp_path)) == []


def test_a_slot_that_is_not_there_is_already_empty(tmp_path):
    """--reset-staging on a tenant that has never been backed up is a no-op and
    not an error: the operator running it is repairing, and a refusal here
    would send them looking for a problem that is not there."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--reset-staging", TENANT], env)
    assert done.returncode == 0, done.stderr
    assert _deletes(shelllib.calls(tmp_path)) == []


# --- what a refusal may print ------------------------------------------------


def test_a_refusal_about_backup_env_never_prints_what_is_in_it(tmp_path):
    """config/backup.env holds the credential that can read every tenant's data
    out of the object store. An error message about a secret is a place the
    secret escapes -- into a timer journal at 03:17, which is the one place
    nobody is watching and everything is recorded."""
    secret = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
    password_file = tmp_path / "restic-password"
    _, env = _root(tmp_path,
                   backup_env=f"RESTIC_PASSWORD_FILE={password_file}\n"
                              f"AWS_SECRET_ACCESS_KEY={secret}\n")
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode != 0
    assert secret not in done.stderr
    assert secret not in done.stdout


# --- what is already staged, sent without re-copying anything -----------------
#
# The mode exists because restore.sh refuses to delete a slot that holds a
# finished-but-unsent backup and has to name a remedy. `--tenant` is not that
# remedy: it runs the spawner's _BACKUP_SCRIPT, whose first two lines empty the
# slot and re-copy the tenant's CURRENT live tree -- which in the case that
# matters is the bad one, because a bad live tree is why anyone is restoring.


def _staged(root, *, manifest=True):
    """A finished backup sitting in the slot, unsent."""
    slot = root / "staging" / TENANT
    (slot / "home").mkdir(parents=True)
    (slot / "env").mkdir(parents=True, exist_ok=True)
    (slot / "home" / "state.db").write_text("the staged copy", encoding="utf-8")
    if manifest:
        (slot / "manifest.json").write_text(
            '{"version":1,"parts":["home","env"],"state_db":true}',
            encoding="utf-8")
    return slot


def test_snapshot_staged_sends_the_slot_without_asking_the_gateway(tmp_path):
    """The whole point: no `admin backup`, so nothing re-copies the live tree
    over the staged copy before it is sent."""
    root, env = _root(tmp_path)
    _staged(root)
    done = _run(tmp_path, ["--snapshot-staged", TENANT], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if "admin backup" in line], (
        "it asked the gateway to back up, which empties the slot and re-copies "
        "the live tree -- the exact thing this mode exists to avoid")
    assert [line for line in calls
            if line.startswith("restic backup") and f"tenant:{TENANT}" in line]


def test_snapshot_staged_clears_the_slot_only_after_restic_has_it(tmp_path):
    """Same one-slot rule as the nightly path, and it shares the code that
    enforces it rather than carrying a second copy."""
    root, env = _root(tmp_path)
    _staged(root)
    done = _run(tmp_path, ["--snapshot-staged", TENANT], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    snapshot = next(i for i, line in enumerate(calls)
                    if line.startswith("restic") and f"tenant:{TENANT}" in line)
    cleared = next(i for i, line in enumerate(calls)
                   if "find /staging -mindepth 1 -delete" in line)
    assert snapshot < cleared


def test_snapshot_staged_refuses_a_slot_with_no_manifest(tmp_path):
    """The same declaration the nightly path reads. A slot with two empty part
    directories is what an interrupted backup leaves, and sending it would
    make it the latest good copy for that tag."""
    root, env = _root(tmp_path)
    _staged(root, manifest=False)
    done = _run(tmp_path, ["--snapshot-staged", TENANT], env)
    assert done.returncode != 0
    assert "not a finished backup" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert not [line for line in calls if line.startswith("restic backup")]
    assert not _deletes(calls)


@pytest.mark.parametrize("value", ["..", "../../srv", "", "k3fq7x2mza4A"])
def test_snapshot_staged_takes_a_tenant_id_and_nothing_else(tmp_path, value):
    """It joins the value to the staging root exactly as the other two id
    flags do, and reaches the same `find -delete`."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--snapshot-staged", value], env)
    assert done.returncode != 0
    assert "a tenant id is twelve characters" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the repository has to exist before there is anywhere to back up to -------


def test_a_repository_that_does_not_answer_stops_the_run_before_anything_is_staged(
        tmp_path):
    """NOTHING ELSE ON THIS VM CREATES THE REPOSITORY. install.sh cannot: the
    object store's own credentials are appended to config/backup.env by hand
    after it has run, so at the moment it runs there is nothing to authenticate
    with. Unchecked, the first thing ever to touch the repository is
    `restic backup` at 03:17 in a timer unit, and the operator believes they
    have backups until the day they need one."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], {**env, "WAKU_REPO_EXISTS": "0"})
    assert done.returncode != 0
    assert "cannot be opened" in done.stderr
    assert "backup.sh --init-repository" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert _restic_writes(calls) == []
    assert not _deletes(calls)
    assert not [line for line in calls if ".backup" in line]


def test_an_ordinary_run_never_creates_the_repository_itself(tmp_path):
    """The reason this refuses instead of initialising: RESTIC_REPOSITORY
    mistyped by one character does not answer either, so a run that created
    what it could not open would make a second, empty repository at the typo,
    back up into it every night, report success, and leave every real snapshot
    somewhere restore.sh will not look. `restic cat config` cannot tell an
    absent repository from a misaddressed one; an operator can."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], {**env, "WAKU_REPO_EXISTS": "0"})
    assert done.returncode != 0
    assert "restic init" not in shelllib.calls(tmp_path)


def test_init_repository_creates_it_and_says_it_is_empty(tmp_path):
    """The operator runs this once, after adding the object store credentials
    to backup.env. Saying the repository is EMPTY is the point of the message:
    an operator who pointed this at the wrong address has one line telling them
    their old snapshots are not in what they just made."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--init-repository"], {**env, "WAKU_REPO_EXISTS": "0"})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert "restic init" in calls
    assert "It is EMPTY" in done.stdout
    # It stages nothing, so it takes no lock and empties no slot.
    assert not [line for line in calls if line.startswith("flock")]
    assert not _deletes(calls)


def test_init_repository_on_an_existing_repository_creates_nothing(tmp_path):
    """Idempotent, like install.sh. An operator who runs it twice, or who runs
    it on a VM restored from a migration, must not be handed a second
    repository."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--init-repository"], {**env, "WAKU_REPO_EXISTS": "1"})
    assert done.returncode == 0, done.stderr
    assert "restic init" not in shelllib.calls(tmp_path)
    assert "already exists" in done.stdout


def test_an_init_that_fails_is_a_refusal_and_not_a_backup(tmp_path):
    """restic refuses to init over a repository whose config it can already
    see, and it refuses when the credentials do not reach the object store.
    Either way the operator has no repository, and a run that carried on would
    stage a tenant for a snapshot with nowhere to go."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--init-repository"],
                {**env, "WAKU_REPO_EXISTS": "0", "WAKU_INIT_RC": "1"})
    assert done.returncode != 0
    assert "could not create the restic repository" in done.stderr
    assert not _deletes(shelllib.calls(tmp_path))


def test_init_repository_takes_no_tenant(tmp_path):
    """--all and --init-repository take none; the other three take exactly one.
    A tenant id accepted here and ignored is one an operator would believe
    scoped what they ran."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--init-repository", TENANT], env)
    assert done.returncode != 0
    assert "unknown argument" in done.stderr


def test_the_probe_runs_under_the_lock_and_before_the_first_database_copy(tmp_path):
    """Its position is the assertion. Above the lock it would be one more name
    checked before a slot can be emptied, which is where the two restic NAMES
    are checked; below the first `.backup` it would be a probe that ran after
    the run had already written into control/backup/."""
    _, env = _root(tmp_path)
    done = _run(tmp_path, ["--all"], env)
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    lock = next(i for i, line in enumerate(calls) if line.startswith("flock"))
    probe = calls.index(_PROBE)
    first_copy = next(i for i, line in enumerate(calls) if ".backup" in line)
    assert lock < probe < first_copy
