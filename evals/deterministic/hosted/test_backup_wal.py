"""A tenant's backup holds every committed row, including the rows that are
still in the write-ahead file -- spec 024, acceptance 10.

Spec 023 turned on WAL mode for state.db, so that `waku serve` and the
dashboard can share it. In WAL mode a committed row sits in `state.db-wal`
until a checkpoint moves it into `state.db`. A file copy of `state.db` taken
between the two is a database without that row.

The spawner's backup script copies state.db through SQLite's own `.backup`
and keeps state.db, `-wal` and `-shm` out of the file copy. This eval runs
that script and the restore script for real -- bash, tar and the sqlite3
command, with /data, /work and /staging pointed at temporary directories --
while a writer holds rows in the write-ahead file.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hosted.spawner import docker as docker_mod

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "tar", "sqlite3")),
    reason="needs bash, tar and the sqlite3 command")

CHECKPOINTED, IN_WAL = 3, 5


def _run(script: str, staging: Path, data: Path, work: Path) -> None:
    """One of the spawner's scripts, with its three mounts in temporary
    directories. The mount names are swapped in one pass, so a temporary path
    that contains "/work" is not rewritten a second time."""
    mounts = {"/staging": str(staging), "/data": str(data), "/work": str(work)}
    script = re.sub(r"/(staging|data|work)\b", lambda found: mounts[found[0]], script)
    done = subprocess.run(["bash", "-euc", script], capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr


def _writer(data: Path) -> sqlite3.Connection:
    """A tenant's state.db in WAL mode with 3 rows in the database file and 5
    more committed rows that are only in the write-ahead file. The returned
    connection is the dashboard that is still running."""
    data.mkdir(parents=True)
    conn = sqlite3.connect(data / "state.db")
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")   # nothing moves the rows by itself
    conn.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
    conn.executemany("INSERT INTO chat_log (content) VALUES (?)",
                     [(f"checkpointed {n}",) for n in range(CHECKPOINTED)])
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.executemany("INSERT INTO chat_log (content) VALUES (?)",
                     [(f"in the write-ahead file {n}",) for n in range(IN_WAL)])
    conn.commit()
    assert (data / "state.db-wal").stat().st_size > 0
    return conn


def _rows(database: Path) -> int:
    conn = sqlite3.connect(database)
    try:
        return conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def tenant(tmp_path):
    data = tmp_path / "data"
    writer = _writer(data)
    (data / "SOUL.md").write_text("You are Waku.", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / ".env").write_text("WAKU_PROVIDER=platform\n", encoding="utf-8")
    yield data
    writer.close()


def test_a_file_copy_of_state_db_misses_the_rows_in_the_write_ahead_file(tmp_path, tenant):
    """The hazard itself, so that the eval below is known to measure it."""
    copy = tmp_path / "copy" / "state.db"
    copy.parent.mkdir()
    shutil.copyfile(tenant / "state.db", copy)

    assert _rows(copy) == CHECKPOINTED, "the 5 newest rows are not in the copied file"


def test_a_backup_taken_while_a_writer_holds_rows_in_the_wal_restores_every_row(
        tmp_path, tenant):
    """Acceptance 10."""
    _run(docker_mod._BACKUP_SCRIPT, tmp_path / "staging", tenant, tmp_path / "work")
    staged = tmp_path / "staging" / "home"

    assert _rows(staged / "state.db") == CHECKPOINTED + IN_WAL
    assert sorted(p.name for p in staged.iterdir()) == ["SOUL.md", "state.db"], (
        "the staged database is whole, so no -wal or -shm file travels with it")
    check = sqlite3.connect(staged / "state.db")
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    assert '"state_db":true' in (tmp_path / "staging" / "manifest.json").read_text(encoding="utf-8")

    restored, env = tmp_path / "restored", tmp_path / "restored-env"
    restored.mkdir()
    env.mkdir()
    _run(docker_mod._RESTORE_SCRIPT, tmp_path / "staging", restored, env)

    assert _rows(restored / "state.db") == CHECKPOINTED + IN_WAL
    assert (restored / "SOUL.md").read_text(encoding="utf-8") == "You are Waku."
    assert (env / ".env").exists()


def test_the_backup_script_copies_state_db_through_sqlite_and_not_as_a_file():
    """What the eval above depends on, pinned in the script's own words."""
    script = docker_mod._BACKUP_SCRIPT
    assert """sqlite3 /data/state.db ".backup '/staging/home/state.db'\"""" in script
    home_copy = next(part for part in script.split("tar --create")
                     if "--directory /data" in part)
    for name in ("'state.db'", "'*-wal'", "'*-shm'"):
        assert f"--exclude {name}" in home_copy
