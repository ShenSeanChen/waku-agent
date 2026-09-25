"""migrate.sh: the order the old VM shuts down in, and what each half refuses.

THE ORDER IS THE TEST ON THE --out SIDE. Every step is irreversible in the
sense that matters to an operator -- once the final backup has been taken, a
turn a tenant writes afterwards is on a machine nobody is going to start again
-- so the front door closes first, the fleet second, and the backup third.

THE SIBLING SCRIPTS ARE REPLACED, NOT STUBBED ON PATH. migrate.sh calls
"$here/backup.sh" and "$here/restore.sh" by path, so the whole deploy directory
is copied into tmp_path and those two files are replaced with recorders. lib.sh
beside them is the real one: waku_compose, waku_admin and waku_load_install_env
are what this script's order is made of.
"""

from __future__ import annotations

import shutil

import pytest
import shelllib

_DOCKER = """#!/bin/sh
printf '%s %s\\n' docker "$*" >> "$WAKU_CALLS"
printf '%s\\n' '{"stopped": []}'
exit 0
"""

_ROOT = "#!/bin/sh\necho 0\n"
_NOT_ROOT = "#!/bin/sh\necho 1000\n"


def _sibling(name: str) -> str:
    return (f'#!/bin/sh\nprintf \'%s %s\\n\' {name} "$*" >> "$WAKU_CALLS"\n'
            f'exit "${{WAKU_{name.split(".")[0].upper()}_RC:-0}}"\n')


_INSTALL_ENV_NAMES = {
    "WAKU_ROOT": None,               # filled in per run
    "WAKU_SRC": None,
    "WAKU_COMPOSE": None,
    "WAKU_DOMAIN": "example.test",
    "WAKU_DNS_PROVIDER": "route53",
    "WAKU_ACME_EMAIL": "ops@example.test",
}

_SPAWNER_ENV = ("WAKU_PLATFORM_MODEL=claude-sonnet-5\n"
                "WAKU_PLATFORM_SMALL_MODEL=claude-sonnet-5\n"
                "WAKU_TENANT_DISK_BYTES=1073741824\n")
_GATEWAY_ENV = ("WAKU_MAX_RUNNING=95\n"
                "WAKU_SUPABASE_URL=https://p.supabase.co\n"
                "WAKU_SUPABASE_AUDIENCE=authenticated\n"
                "WAKU_SUPABASE_PUBLISHABLE_KEY=sb_publishable_x\n")
_BACKUP_ENV = ("RESTIC_REPOSITORY=s3:s3.amazonaws.com/waku-backups\n"
               "RESTIC_PASSWORD_FILE=/srv/waku/config/restic-password\n")


def _deploy(tmp_path, *, drop_name: str | None = None,
            spawner_env: str = _SPAWNER_ENV,
            gateway_env: str = _GATEWAY_ENV,
            backup_env: str | None = _BACKUP_ENV):
    """A copy of hosted/deploy/ whose backup.sh and restore.sh only record."""
    deploy = tmp_path / "deploy"
    shutil.copytree(shelllib.DEPLOY, deploy)
    for name in ("backup.sh", "restore.sh"):
        (deploy / name).write_text(_sibling(name), encoding="utf-8")
        (deploy / name).chmod(0o755)

    root = tmp_path / "waku"
    (root / "config").mkdir(parents=True)
    (root / "config" / "spawner.env").write_text(spawner_env, encoding="utf-8")
    (root / "config" / "gateway.env").write_text(gateway_env, encoding="utf-8")
    if backup_env is not None:
        (root / "config" / "backup.env").write_text(backup_env, encoding="utf-8")

    values = dict(_INSTALL_ENV_NAMES)
    values["WAKU_ROOT"] = str(root)
    values["WAKU_SRC"] = str(tmp_path / "src")
    values["WAKU_COMPOSE"] = str(tmp_path / "src" / "compose.yaml")
    if drop_name is not None:
        del values[drop_name]
    env_file = tmp_path / "install.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()),
                        encoding="utf-8")
    return deploy, {"WAKU_INSTALL_ENV": str(env_file)}


def _run(tmp_path, args, *, root=True, extra=None, **kwargs):
    deploy, env = _deploy(tmp_path, **kwargs)
    env.update(extra or {})
    return shelllib.run(deploy / "migrate.sh", args, tmp_path=tmp_path, env=env,
                        stubs=["docker", "id"],
                        bodies={"docker": _DOCKER,
                                "id": _ROOT if root else _NOT_ROOT})


def _at(calls: list[str], needle: str) -> int:
    for index, line in enumerate(calls):
        if line.endswith(needle):
            return index
    raise AssertionError(f"{needle!r} is not in the call log:\n" + "\n".join(calls))


# --- arguments ----------------------------------------------------------------


def test_a_mode_is_required(tmp_path):
    done = _run(tmp_path, [])
    assert done.returncode != 0
    assert "--out (on the old VM) or --in (on the new one) is required" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize("args", [
    ["--out", "--in"],
    ["--in", "--out"],
    ["--out", "--out"],
])
def test_a_second_mode_flag_is_refused_and_not_taken(tmp_path, args):
    """Under a last-one-wins rule `migrate.sh --out --in` is a RESTORE over the
    machine the operator meant to leave: --in runs restore.sh --all, which
    stops the fleet, replaces both databases and re-creates every tenant's
    tree."""
    done = _run(tmp_path, args)
    assert done.returncode != 0
    assert "two halves of a migration" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_an_unknown_argument_is_refused(tmp_path):
    done = _run(tmp_path, ["--out", "--force"])
    assert done.returncode != 0
    assert "unknown argument: --force" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_snapshot_given_without_a_value_is_refused(tmp_path):
    """Without waku_needs_value this dies on bash's own `$2: unbound variable`,
    which names a shell parameter rather than the flag the operator typed."""
    done = _run(tmp_path, ["--in", "--snapshot"])
    assert done.returncode != 0
    assert "--snapshot needs a value" in done.stderr
    assert "unbound variable" not in done.stderr
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
def test_the_snapshot_is_a_closed_set(tmp_path, value):
    """The value is handed to restore.sh, which hands it to restic as the
    positional argument of `restic restore`. restic's restore flags include
    --target, which is where the snapshot's contents land. Checked here as well
    so the refusal is reachable without root."""
    done = _run(tmp_path, ["--in", "--snapshot", value])
    assert done.returncode != 0
    assert "a snapshot is 'latest' or 8 to 64 hex characters" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_snapshot_is_refused_with_out_rather_than_ignored(tmp_path):
    """`migrate.sh --out --snapshot 1a2b3c4d` reads like "migrate from that
    snapshot". --out does not read a snapshot, it makes one, so a flag silently
    dropped here is a flag the operator believes took effect."""
    done = _run(tmp_path, ["--out", "--snapshot", "1a2b3c4d"])
    assert done.returncode != 0
    assert "--snapshot is for --in only" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_the_arguments_are_checked_before_root_is_demanded(tmp_path):
    """Two-sided, like tenant.sh's: a test that accepted either message would
    pass with the whole snapshot set deleted."""
    done = _run(tmp_path, ["--in", "--snapshot", "--target"], root=False)
    assert done.returncode != 0
    assert "a snapshot is 'latest'" in done.stderr
    assert "run this as root" not in done.stderr


def test_a_good_argument_line_still_has_to_be_root(tmp_path):
    done = _run(tmp_path, ["--in"], root=False)
    assert done.returncode != 0
    assert "run this as root" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- what each half declares it reads -----------------------------------------


def test_out_refuses_an_install_env_missing_a_name_it_prints(tmp_path):
    """--out dereferences WAKU_DNS_PROVIDER to tell the new VM's install.sh
    what to build Caddy with. install.env is written once and never rewritten,
    so a file written before a name existed survives every rerun -- which is
    the F2 finding this group records. Undeclared, the name arrives as bash's
    own `set -u` message, after Caddy and the fleet have been stopped."""
    done = _run(tmp_path, ["--out"], drop_name="WAKU_DNS_PROVIDER")
    assert done.returncode != 0
    assert "WAKU_DNS_PROVIDER" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_in_does_not_refuse_over_a_name_only_out_reads(tmp_path):
    """The mirror of the finding above, and restore.sh has the same pair: a
    list that is a fixed superset makes one half refuse to run a disaster
    recovery over a value it never reads."""
    done = _run(tmp_path, ["--in"], drop_name="WAKU_ACME_EMAIL")
    assert done.returncode == 0, done.stderr
    assert _at(shelllib.calls(tmp_path), "restore.sh --all --snapshot latest") >= 0


# --- the new VM ---------------------------------------------------------------


def test_in_restores_everything_and_passes_the_snapshot_through(tmp_path):
    done = _run(tmp_path, ["--in", "--snapshot", "1a2b3c4d"])
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    assert _at(calls, "restore.sh --all --snapshot 1a2b3c4d") >= 0
    assert [line for line in calls if line.startswith("backup.sh")] == []


def test_out_names_migrate_sh_by_the_path_it_was_run_from(tmp_path):
    """Step 4 of the printed instructions is a command on the NEW VM, and
    `install.sh` creates no symlink into any directory on PATH. A bare
    `migrate.sh --in` is a line that does not work from wherever the operator
    is standing."""
    deploy, env = _deploy(tmp_path)
    done = shelllib.run(deploy / "migrate.sh", ["--out"], tmp_path=tmp_path,
                        env=env, stubs=["docker", "id"],
                        bodies={"docker": _DOCKER, "id": _ROOT})
    assert done.returncode == 0, done.stderr
    assert f"sudo {deploy}/migrate.sh --in" in done.stdout


def test_in_tells_the_operator_to_move_dns_last(tmp_path):
    """The certificate is issued by DNS-01, so the new VM holds it before any
    traffic moves. An operator who moves the records first takes the platform
    down for as long as the challenge takes."""
    deploy = tmp_path / "deploy"
    done = _run(tmp_path, ["--in"])
    assert done.returncode == 0, done.stderr
    # The full path, because install.sh puts nothing on PATH and this is a
    # line the operator runs on a machine they have just built.
    assert f"sudo {deploy}/tenant.sh status" in done.stdout
    assert "Then move the apex and the wildcard records to this VM." in done.stdout


def test_a_failed_restore_is_a_failed_migration(tmp_path):
    done = _run(tmp_path, ["--in"], extra={"WAKU_RESTORE_RC": "1"})
    assert done.returncode != 0
    assert "Restored." not in done.stdout


# --- the old VM ---------------------------------------------------------------


def test_out_closes_the_front_door_before_it_takes_the_final_backup(tmp_path):
    """THE ORDER IS THE WHOLE CORRECTNESS OF THIS HALF. `stop-all` stops every
    tenant container and the gateway keeps serving, so with Caddy still up a
    sign-in landing in that window starts a container, the tenant writes a
    turn, and the final backup -- already taken for them -- does not have it.
    Their data is then on a VM nobody is going to start again.
    """
    done = _run(tmp_path, ["--out"])
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    caddy = _at(calls, "--project-name waku stop caddy")
    fleet = _at(calls, "hosted.gateway.admin stop-all")
    backup = _at(calls, "backup.sh --all")
    everything = _at(calls, "--project-name waku stop")
    assert caddy < fleet < backup < everything


def test_out_leaves_the_stack_up_when_the_final_backup_fails(tmp_path):
    """A migration whose final backup failed has nothing on the new VM to
    restore from, so the honest thing is to stop with the old VM's data intact.
    The refusal hands the operator a way back to serving and nothing
    destructive: this group's ledger records that a refusal offering a
    destructive next step is worse than one that leaves them stuck."""
    done = _run(tmp_path, ["--out"], extra={"WAKU_BACKUP_RC": "1"})
    assert done.returncode != 0
    assert "NOTHING HAS BEEN MOVED" in done.stderr
    assert "start caddy" in done.stderr
    calls = shelllib.calls(tmp_path)
    assert [line for line in calls if line.endswith("--project-name waku stop")] == []


def test_out_prints_every_flag_the_new_vms_install_sh_needs(tmp_path):
    """The values live in four files on a machine that is about to be turned
    off. An operator who has to go back for one of them has to start the stack
    again to read it."""
    done = _run(tmp_path, ["--out"])
    assert done.returncode == 0, done.stderr
    for value in ("example.test", "route53", "ops@example.test",
                  "claude-sonnet-5", "95", "1073741824",
                  "https://p.supabase.co", "authenticated",
                  "sb_publishable_x", "s3:s3.amazonaws.com/waku-backups",
                  "/srv/waku/config/restic-password"):
        assert value in done.stdout, value


def test_out_names_the_three_config_files_to_copy_and_not_install_env(tmp_path):
    """install.env carries this VM's --data-device and its checkout path, so a
    copy of it onto the new VM is a spawner pointed at a block device that is
    not there."""
    done = _run(tmp_path, ["--out"])
    assert done.returncode == 0, done.stderr
    for name in ("backup.env", "caddy.env", "proxy.env"):
        assert f"config/{name}" in done.stdout, name
    assert "NOT install.env" in done.stdout


def test_out_says_the_three_credential_flags_are_still_required(tmp_path):
    """proxy.env and caddy.env come across in step 2 and install.sh keeps
    them -- and it still refuses without --platform-key-file, --dns-env-file
    and --free-model, because those are required flags. An operator who read
    "the config came across" and threw the source files away is stuck on the
    new VM with the old one stopped."""
    done = _run(tmp_path, ["--out"])
    assert done.returncode == 0, done.stderr
    assert "--platform-key-file" in done.stdout
    assert "--dns-env-file" in done.stdout
    assert "WAKU_PLATFORM_KEY in proxy.env" in done.stdout


@pytest.mark.parametrize("kwargs, missing", [
    ({"spawner_env": "WAKU_TENANT_DISK_BYTES=1\n"}, "WAKU_PLATFORM_MODEL"),
    ({"gateway_env": "WAKU_MAX_RUNNING=95\n"}, "WAKU_SUPABASE_URL"),
    ({"backup_env": None}, "backup.env"),
])
def test_out_refuses_before_it_stops_anything_when_a_value_is_missing(
        tmp_path, kwargs, missing):
    """The values are printed into a command line the operator pastes on the
    new VM, and awk over a missing file prints nothing INSIDE a heredoc
    expansion, where neither errexit nor pipefail can see it. Unchecked, the
    operator is handed `--free-model` with nothing after it and finds out on
    the new machine with the old one already stopped."""
    done = _run(tmp_path, ["--out"], **kwargs)
    assert done.returncode != 0
    assert missing in done.stderr
    assert shelllib.calls(tmp_path) == []
