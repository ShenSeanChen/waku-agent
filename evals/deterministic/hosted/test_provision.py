"""What provisioning writes, and what mode it writes it at.

B4's test_provision_contract.py is the better test of the CONTENTS -- it runs
waku's own loader against the rendered file. This one covers the two things
that test cannot see, because they are about the file rather than the text:
the mode `.env` is created at, and what a second provision does to a file that
is already there.

`.env` holds two non-secret lines today. It is also where a BYOK key lands the
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


def test_the_three_files_are_written_on_a_first_provision(dirs, template):
    written = provision(dirs, template)
    # mcp.json is spec 004: the tenant's Waku Memory server.
    assert written == [dirs.env / ".env", dirs.home / "SOUL.md", dirs.home / "mcp.json"]
    assert (dirs.env / ".env").read_text(encoding="utf-8") == render_env()
    assert (dirs.home / "SOUL.md").read_text(encoding="utf-8") == SOUL_TEMPLATE_TEXT


def test_a_hosted_agent_consolidates_every_turn(dirs, template):
    """Spec 006 D. Laptops keep the default of 6; a one-question demo on the
    hosted agent would never consolidate, so nothing would reach Waku Memory."""
    provision(dirs, template)
    lines = (dirs.env / ".env").read_text(encoding="utf-8").splitlines()
    assert "WAKU_CONSOLIDATE_EVERY=1" in lines
    assert "WAKU_PROVIDER=waku-platform" in lines


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
    (dirs.home / "mcp.json").unlink()
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

    assert provision(dirs, template) == [dirs.home / "SOUL.md", dirs.home / "mcp.json"]
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


# --- spec 004 A4: every tenant's mcp.json names their Waku Memory -------------
# The container reaches Waku Memory through a waku_memory server whose
# credential is the person's own key, passed in as WAKU_MEMORY_API_KEY.

import json  # noqa: E402

from hosted.core.provision import WAKU_MEMORY_SERVER  # noqa: E402


def _servers(dirs):
    return json.loads((dirs.home / "mcp.json").read_text())["servers"]


def test_a_new_tenant_gets_an_mcp_json_with_waku_memory(dirs, template):
    written = provision(dirs, template)
    assert dirs.home / "mcp.json" in written
    assert _servers(dirs) == [WAKU_MEMORY_SERVER]
    assert WAKU_MEMORY_SERVER == {"name": "waku_memory", "url": "https://api.waku.one/mcp",
                                  "auth_env": "WAKU_MEMORY_API_KEY"}


def test_a_tenants_own_servers_are_kept_and_waku_memory_is_added(dirs, template):
    dirs.home.mkdir(parents=True)
    treg = {"name": "treg", "url": "https://treg.to/mcp/", "oauth": True}
    (dirs.home / "mcp.json").write_text(json.dumps({"servers": [treg]}))
    provision(dirs, template)
    assert _servers(dirs) == [treg, WAKU_MEMORY_SERVER]


def test_a_waku_memory_entry_the_tenant_already_has_is_left_alone(dirs, template):
    dirs.home.mkdir(parents=True)
    theirs = {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True}
    (dirs.home / "mcp.json").write_text(json.dumps({"servers": [theirs]}))
    written = provision(dirs, template)
    assert dirs.home / "mcp.json" not in written
    assert _servers(dirs) == [theirs]


def test_an_mcp_json_that_is_not_json_is_left_alone(dirs, template):
    dirs.home.mkdir(parents=True)
    (dirs.home / "mcp.json").write_text("{not json")
    provision(dirs, template)
    assert (dirs.home / "mcp.json").read_text() == "{not json"


def test_a_symlinked_mcp_json_is_never_followed(dirs, template, tmp_path):
    dirs.home.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"servers": []}))
    (dirs.home / "mcp.json").symlink_to(target)
    provision(dirs, template)
    assert json.loads(target.read_text()) == {"servers": []}


def test_provisioning_twice_adds_waku_memory_once(dirs, template):
    provision(dirs, template)
    provision(dirs, template)
    assert _servers(dirs) == [WAKU_MEMORY_SERVER]


# --- spec 004 E: treg, through the metering proxy --------------------------------

from hosted.core.provision import ensure_treg, treg_server  # noqa: E402

PROXY = "http://10.88.0.1:8788"
TREG = {"name": "treg", "url": "http://10.88.0.1:8788/treg/mcp/",
        "auth_env": "WAKU_PLATFORM_TOKEN"}


def test_with_the_relay_on_a_new_tenant_gets_treg_beside_waku_memory(dirs, template):
    written = provision(dirs, template, treg_base_url=PROXY)
    assert written.count(dirs.home / "mcp.json") == 1
    assert _servers(dirs) == [WAKU_MEMORY_SERVER, TREG]
    assert treg_server(PROXY + "/") == TREG


def test_without_the_relay_there_is_no_treg_entry(dirs, template):
    provision(dirs, template)
    assert [s["name"] for s in _servers(dirs)] == ["waku_memory"]
    assert ensure_treg(dirs.home, "") is False


def test_an_existing_tenant_gets_treg_on_the_next_start_and_keeps_their_servers(dirs, template):
    provision(dirs, template)
    written = provision(dirs, template, treg_base_url=PROXY)
    assert written == [dirs.home / "mcp.json"]
    assert _servers(dirs) == [WAKU_MEMORY_SERVER, TREG]
    provision(dirs, template, treg_base_url=PROXY)
    assert _servers(dirs) == [WAKU_MEMORY_SERVER, TREG]


def test_a_treg_entry_the_tenant_already_has_is_left_alone(dirs, template):
    dirs.home.mkdir(parents=True)
    theirs = {"name": "treg", "url": "https://treg.to/mcp/", "oauth": True}
    (dirs.home / "mcp.json").write_text(json.dumps({"servers": [theirs, WAKU_MEMORY_SERVER]}))
    written = provision(dirs, template, treg_base_url=PROXY)
    assert dirs.home / "mcp.json" not in written
    assert _servers(dirs) == [theirs, WAKU_MEMORY_SERVER]


def test_treg_never_follows_a_symlinked_mcp_json(dirs, template, tmp_path):
    dirs.home.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"servers": []}))
    (dirs.home / "mcp.json").symlink_to(target)
    assert ensure_treg(dirs.home, PROXY) is False
    assert json.loads(target.read_text()) == {"servers": []}


# --- spec 014: a tenant's own treg key --------------------------------------------
# Provisioning never reads the key and never rewrites the treg entry; waku's
# treg.resolve() decides at connect time. Evals may import both sides.

def test_a_saved_key_leaves_the_relay_entry_on_disk_and_clearing_it_switches_back(
        dirs, template, monkeypatch):
    from waku.tools.treg import KEY_ENV, URL, resolve

    provision(dirs, template, treg_base_url=PROXY)
    (dirs.env / ".env").write_text(f"{KEY_ENV}=theirs\n", encoding="utf-8")
    monkeypatch.setenv(KEY_ENV, "theirs")
    provision(dirs, template, treg_base_url=PROXY)
    assert _servers(dirs) == [WAKU_MEMORY_SERVER, TREG], "provisioning only adds"
    resolved = resolve(_servers(dirs))
    assert resolved[1] == {"name": "treg", "url": URL, "auth_env": KEY_ENV}
    monkeypatch.delenv(KEY_ENV)
    assert resolve(_servers(dirs)) == [WAKU_MEMORY_SERVER, TREG], "back through the relay"


def test_a_tenant_s_own_treg_entry_survives_a_key_and_a_reprovision(dirs, template, monkeypatch):
    from waku.tools.treg import KEY_ENV, resolve

    dirs.home.mkdir(parents=True)
    theirs = {"name": "treg", "url": "https://treg.to/mcp/", "oauth": True}
    (dirs.home / "mcp.json").write_text(json.dumps({"servers": [theirs, WAKU_MEMORY_SERVER]}))
    monkeypatch.setenv(KEY_ENV, "theirs")
    provision(dirs, template, treg_base_url=PROXY)
    assert _servers(dirs) == [theirs, WAKU_MEMORY_SERVER]
    monkeypatch.delenv(KEY_ENV)
    assert resolve(_servers(dirs)) == [theirs, WAKU_MEMORY_SERVER]
