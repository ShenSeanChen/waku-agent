"""DETERMINISTIC EVAL -- the XFS skip contract, driven offline.

`dockerlib.require_xfs()` is the gate in front of every tenant-disk-limit test
groups C and F will write. It has one returning path and three skipping ones,
and a skip is not a pass: a quota suite that skips everything and goes green is
the same failure as a quota suite that passes against a filesystem enforcing
nothing.

THE CASE THIS FILE EXISTS FOR is the second kind. XFS can be mounted
`-o pqnoenforce`: accounting on, every `xfs_quota` command succeeds,
`report -p` shows the limit back, and no limit is ever enforced. A test that
writes past its quota on such a mount PASSES while proving the opposite.
`.github/workflows/hosted-docker.yml` asserts `Enforcement: ON` when it makes
the filesystem, and `xfs_root()` reads back the half of that verdict a non-root
process can see -- the `prjquota` option in /proc/mounts. Neither exists on the
maintainers' machines (macOS has no /proc/mounts and no XFS), so both the
happy path and the three skips are driven here against a fixture file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals" / "hosted_docker"))

import dockerlib  # noqa: E402

# `pytest.skip()` raises Skipped, which derives from BaseException, NOT from
# Exception, so `pytest.raises(Exception)` does not catch it: the skip escapes
# and skips the test that was asserting it. Three of the tests below did
# exactly that when this file was first written, and reported "skipped", which
# reads like a pass in a `-q` summary. This name is the fix.
SKIPPED = pytest.skip.Exception

# Real lines, in /proc/mounts field order: device, mountpoint, fstype, options.
ENFORCING = "/dev/loop0 /srv/waku xfs rw,relatime,attr2,inode64,prjquota 0 0"
ACCOUNTING_ONLY = "/dev/loop0 /srv/waku xfs rw,relatime,attr2,inode64,pqnoenforce 0 0"
NO_QUOTA = "/dev/loop0 /srv/waku xfs rw,relatime,attr2,inode64,noquota 0 0"
NOT_XFS = "/dev/loop0 /srv/waku ext4 rw,relatime,prjquota 0 0"
OTHER = "/dev/sda1 / ext4 rw,relatime 0 0"


@pytest.mark.parametrize(("line", "expected"), [
    (ENFORCING, True),
    (ACCOUNTING_ONLY, False),
    (NO_QUOTA, False),
    (NOT_XFS, False),
])
def test_only_prjquota_counts_as_enforcing(line, expected):
    """One option means enforced. The other three are ways to look enforced."""
    mounts = f"{OTHER}\n{line}\n"
    assert dockerlib._prjquota_enforced(mounts, "/srv/waku") is expected


def test_a_mountpoint_that_is_not_in_proc_mounts_is_not_enforcing():
    assert dockerlib._prjquota_enforced(f"{OTHER}\n", "/srv/waku") is False


def test_a_prefix_of_the_mountpoint_is_not_the_mountpoint():
    """`/srv` is not `/srv/waku`, and a substring match would say it was."""
    mounts = "/dev/loop0 /srv xfs rw,prjquota 0 0\n"
    assert dockerlib._prjquota_enforced(mounts, "/srv/waku") is False


@pytest.fixture
def linux_with_mounts(tmp_path, monkeypatch):
    """A fake Linux carrying a real mountpoint, a real device and a /proc/mounts.

    Returns a function taking a /proc/mounts template, so each test says what
    kind of filesystem the runner has. The template's `/srv/waku` is rewritten
    to the tmpdir, because `xfs_root` also requires the path to exist.
    """
    mount = tmp_path / "srv" / "waku"
    mount.mkdir(parents=True)
    device = tmp_path / "loop0"
    device.write_bytes(b"")
    monkeypatch.setattr(dockerlib.platform, "system", lambda: "Linux")
    monkeypatch.setenv("WAKU_XFS_MOUNT", str(mount))
    monkeypatch.setenv("WAKU_XFS_DEVICE", str(device))

    def _mounts(template: str) -> tuple[Path, str]:
        proc = tmp_path / "proc_mounts"
        proc.write_text(template.replace("/srv/waku", str(mount)), encoding="utf-8")
        monkeypatch.setattr(dockerlib, "PROC_MOUNTS", proc)
        return mount, str(device)

    return _mounts


def test_require_xfs_returns_the_mount_and_device_when_quotas_enforce(linux_with_mounts):
    """The path that must exist, or every skip below proves nothing.

    This is the hosted-docker runner's world, and the only assertion in this
    file that fails if `xfs_root` grows a condition nothing can satisfy.
    """
    mount, device = linux_with_mounts(f"{OTHER}\n{ENFORCING}\n")
    assert dockerlib.require_xfs() == (mount, device)


def test_require_xfs_skips_naming_pqnoenforce_when_the_mount_only_accounts(linux_with_mounts):
    """The silent case: everything present, nothing enforced."""
    linux_with_mounts(f"{OTHER}\n{ACCOUNTING_ONLY}\n")
    with pytest.raises(SKIPPED, match="pqnoenforce") as caught:
        dockerlib.require_xfs()
    assert "hosted-docker.yml" in str(caught.value)


def test_require_xfs_skips_naming_the_variables_when_they_are_unset(
    linux_with_mounts, monkeypatch,
):
    linux_with_mounts(f"{OTHER}\n{ENFORCING}\n")
    monkeypatch.delenv("WAKU_XFS_MOUNT")
    with pytest.raises(SKIPPED, match="WAKU_XFS_MOUNT"):
        dockerlib.require_xfs()


def test_require_xfs_skips_naming_the_platform_off_linux(monkeypatch):
    """The maintainers' case. The skip says Darwin, not 'unavailable'."""
    monkeypatch.setattr(dockerlib.platform, "system", lambda: "Darwin")
    with pytest.raises(SKIPPED, match="Darwin") as caught:
        dockerlib.require_xfs()
    assert "Linux-only" in str(caught.value)


def test_xfs_root_is_none_off_linux_whatever_the_variables_say(monkeypatch, tmp_path):
    """Set the variables to real paths on macOS and it still refuses."""
    monkeypatch.setattr(dockerlib.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("WAKU_XFS_MOUNT", str(tmp_path))
    monkeypatch.setenv("WAKU_XFS_DEVICE", str(tmp_path))
    assert dockerlib.xfs_root() is None


def test_a_skip_cannot_be_caught_as_an_exception():
    """The trap above, pinned, so nobody writes `raises(Exception)` here again.

    If a future pytest makes Skipped an Exception subclass this goes red, and
    the reader is told why the distinction mattered rather than finding three
    tests that quietly stopped asserting anything.
    """
    assert not issubclass(SKIPPED, Exception), (
        "pytest.skip.Exception is now an Exception subclass; re-read every "
        "pytest.raises in this file, and every skip assertion in the suite")
