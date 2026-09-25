"""What provisioning writes, and what mode it writes it at.

B4's test_provision_contract.py is the better test of the CONTENTS -- it runs
waku's own loader against the rendered file. This one covers the two things
that test cannot see, because they are about the file rather than the text:
the mode `.env` is created at, and what a second provision does to a file that
is already there.

`.env` holds one non-secret line today. It is also where a BYOK key lands the
day the free tier stops being the only tier, which is why the mode is pinned
against the literal 0o600 rather than against provision.ENV_MODE.
"""

from __future__ import annotations

import os
import stat

import pytest

from hosted.core.provision import provision, render_env
from hosted.core.tenant import TenantDirs

SOUL_TEMPLATE_TEXT = "You are Waku.\n"


@pytest.fixture
def dirs(tmp_path):
    return TenantDirs(home=tmp_path / "data", env=tmp_path / "work")


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "SOUL.md"
    path.write_text(SOUL_TEMPLATE_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def permissive_umask():
    """The worst case the container could be started with. write_text would
    create the file at 0666 here and only tighten it afterwards, which is the
    instant this test exists to remove."""
    previous = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_two_files_are_written_on_a_first_provision(dirs, template):
    written = provision(dirs, template)
    assert written == [dirs.env / ".env", dirs.home / "SOUL.md"]
    assert (dirs.env / ".env").read_text(encoding="utf-8") == render_env()
    assert (dirs.home / "SOUL.md").read_text(encoding="utf-8") == SOUL_TEMPLATE_TEXT


def test_the_env_file_ends_at_0600_under_any_umask(dirs, template, permissive_umask):
    assert provision(dirs, template)
    assert _mode(dirs.env / ".env") == 0o600


def test_the_env_file_never_exists_at_a_looser_mode_even_for_an_instant(
        monkeypatch, dirs, template, permissive_umask):
    """THE TEST ABOVE CANNOT FAIL ON THE OLD CODE. write_text() under umask 0
    creates .env at 0666 and the chmod that follows closes it a moment later,
    so by the time any assertion runs the file is at 0600 either way -- the
    end state is identical and the window is invisible.

    The only way to see the window is to look while it is open, so this
    records the mode the file already has each time chmod is called. Create it
    with the mode and chmod finds 0600 and has nothing to do; create it with
    write_text and chmod finds 0666, which is the instant a key would have
    been readable by anything else in the container.
    """
    env_file = dirs.env / ".env"
    seen: list[int] = []
    real_chmod = os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        if os.path.abspath(path) == os.path.abspath(env_file):
            seen.append(stat.S_IMODE(os.lstat(path).st_mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    provision(dirs, template)

    assert all(mode == 0o600 for mode in seen), (
        f".env existed at {[oct(m) for m in seen]} before it was chmodded to 0600; "
        "it has to be created with the mode, not corrected into it")
    assert _mode(env_file) == 0o600


def test_a_loosened_env_file_has_its_mode_repaired_on_the_next_start(dirs, template):
    """Provisioning runs before every start. A tenant who chmods their own
    .env to 0666 inside their container gets it back at 0600 next time."""
    provision(dirs, template)
    os.chmod(dirs.env / ".env", 0o666)
    assert provision(dirs, template) == [], "an existing file was rewritten"
    assert _mode(dirs.env / ".env") == 0o600


def test_a_second_provision_leaves_existing_contents_alone(dirs, template):
    """The contract is "only creates what is missing". A tenant who edited
    their own .env or SOUL.md keeps their edit."""
    provision(dirs, template)
    (dirs.env / ".env").write_text("WAKU_PROVIDER=waku-platform\nMINE=1\n", encoding="utf-8")
    (dirs.home / "SOUL.md").write_text("mine\n", encoding="utf-8")
    assert provision(dirs, template) == []
    assert "MINE=1" in (dirs.env / ".env").read_text(encoding="utf-8")
    assert (dirs.home / "SOUL.md").read_text(encoding="utf-8") == "mine\n"


def test_a_deleted_file_comes_back(dirs, template):
    provision(dirs, template)
    (dirs.home / "SOUL.md").unlink()
    assert provision(dirs, template) == [dirs.home / "SOUL.md"]


def test_a_deleted_directory_comes_back(dirs, template):
    provision(dirs, template)
    (dirs.home / "SOUL.md").unlink()
    dirs.home.rmdir()
    provision(dirs, template)
    assert (dirs.home / "SOUL.md").is_file()


def test_an_env_symlink_is_left_alone_rather_than_followed(dirs, template, tmp_path):
    """A tenant can plant symlinks in their own mounts -- the module docstring
    says so, and it is why provisioning runs as UID 10001 in a throwaway
    container. The mode repair must not chase one: chmod through a symlink to
    a file this process does not own raises PermissionError, and provisioning
    would then fail on every start, locking the tenant out of their own
    container for good."""
    dirs.env.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "somewhere-else"
    outside.write_text("not mine\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    (dirs.env / ".env").symlink_to(outside)

    assert provision(dirs, template) == [dirs.home / "SOUL.md"]
    assert _mode(outside) == 0o644, "the mode repair followed a symlink"
    assert outside.read_text(encoding="utf-8") == "not mine\n"


def test_a_broken_env_symlink_does_not_create_the_target(dirs, template, tmp_path):
    """is_symlink() is what refuses this, and it has to be: exists() is False
    for a dangling link, so without that check provisioning would fall into
    the create path and O_CREAT would follow the link and write the file it
    points at. The create path is never reached here, and its O_EXCL and
    O_NOFOLLOW only cover a link planted after the check."""
    dirs.env.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "does-not-exist"
    (dirs.env / ".env").symlink_to(target)

    provision(dirs, template)
    assert not target.exists(), "provisioning wrote through a dangling symlink"


def test_a_broken_soul_symlink_does_not_create_the_target(dirs, template, tmp_path):
    """The SOUL.md half of the .env case directly above. exists() follows a
    link, so a DANGLING one reads as absent and write_text opens the target."""
    dirs.home.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "gotcha.txt"
    (dirs.home / "SOUL.md").symlink_to(target)
    written = provision(dirs, template)
    assert not target.exists(), (
        "provisioning wrote through a planted symlink to a path outside both "
        "tenant directories")
    assert (dirs.home / "SOUL.md") not in written


def test_a_soul_symlink_to_an_existing_file_is_left_alone(dirs, template, tmp_path):
    """The other half. exists() already reported this one as present, so it
    was never written through -- and it must stay that way once the branch
    above starts short-circuiting on is_symlink()."""
    dirs.home.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "mine.txt"
    target.write_text("UNTOUCHED\n", encoding="utf-8")
    (dirs.home / "SOUL.md").symlink_to(target)
    provision(dirs, template)
    assert target.read_text(encoding="utf-8") == "UNTOUCHED\n"
