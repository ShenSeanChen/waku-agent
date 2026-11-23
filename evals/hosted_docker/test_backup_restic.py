"""The one property of restic this design leans on, measured rather than
assumed: it stores a symlink as a symlink and never follows it.

The spec says so in one clause, and the whole no-privileged-process-walks-a-
tenant's-tree argument rests on it: a restic that followed links would read a
host file as root on every nightly run. This is a library-behaviour check, and
it is in the Docker tier because that is where restic is installed.

THE LINK POINTS AT A FILE THAT EXISTS AND IS THEN REMOVED, and that is what
makes this able to fail. A link to a path that never existed comes back as a
dangling link whatever restic does, so the assertion could not tell the
behaviour from the absence. Here the target holds a sentence while the
snapshot is taken and is deleted before the restore: if restic had followed
the link it would have stored those bytes, and the restored `.env` would be a
regular file holding them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None,
    reason="restic is not installed. The hosted-docker job installs it beside "
           "xfsprogs; this module is the only thing in the tier that needs it.")

TENANT = "k3fq7x2mza4b"


def _restic(repo, password_file, *args) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["restic", "--repo", str(repo), "--password-file", str(password_file), *args],
        capture_output=True, text=True, check=False)
    assert done.returncode == 0, (
        f"restic {' '.join(args)} failed ({done.returncode}):\n{done.stderr}")
    return done


def test_a_staging_slot_round_trips_with_its_manifest_and_its_links(tmp_path):
    repo = tmp_path / "repo"
    password = tmp_path / "pw"
    password.write_text("test\n", encoding="utf-8")
    _restic(repo, password, "init")

    slot = tmp_path / "staging" / TENANT
    (slot / "home").mkdir(parents=True)
    (slot / "env").mkdir()
    (slot / "home" / "state.db").write_bytes(b"SQLite format 3\x00")
    outside = tmp_path / "outside.txt"
    outside.write_text("the host file a followed link would have read\n",
                       encoding="utf-8")
    os.symlink(str(outside), slot / "env" / ".env")
    (slot / "manifest.json").write_text(
        json.dumps({"version": 1, "parts": ["home", "env"], "state_db": True}),
        encoding="utf-8")

    _restic(repo, password, "backup", "--tag", f"tenant:{TENANT}", str(slot))
    shutil.rmtree(slot)
    # Removed AFTER the snapshot and BEFORE the restore. Anything of this
    # file's that comes back came out of the repository.
    outside.unlink()

    _restic(repo, password, "restore", "latest", "--tag", f"tenant:{TENANT}",
            "--target", "/")

    link = slot / "env" / ".env"
    assert link.is_symlink(), "restic did not store the symlink as a symlink"
    assert os.readlink(link) == str(outside)
    assert not link.exists(), (
        "the restored .env resolves to something, so restic followed the link "
        "at backup time and stored the host file's bytes")
    assert (slot / "home" / "state.db").read_bytes() == b"SQLite format 3\x00"
    manifest = json.loads((slot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state_db"] is True
    assert sorted(manifest["parts"]) == ["env", "home"]
