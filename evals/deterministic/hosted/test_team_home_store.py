"""DETERMINISTIC EVAL -- a team home in control.db (spec 032 task A1).

Acme is a team space on waku.one. Its agent's home on the platform is one
more tenant, marked as a team, and one row in `team_home` that holds what
waku.one sent: the space, the names, the return address and the agent's Waku
Memory key, sealed. A copy of control.db does not contain the key.
"""

from __future__ import annotations

import sqlite3

import pytest

from hosted.core import tenant as core_tenant
from hosted.gateway.secretbox import SecretBox
from hosted.gateway.store import ControlDb

ACME = "0b0f3c1e-8a0e-4c58-9d55-0d2b5b1f7a11"
AGENT = "5f2d8f0a-1c0b-4d67-a2f4-9f1f6f0f3c22"
RETURN_URL = "https://www.waku.one/spaces/acme"
# Not a real key: the shape Waku Memory mints, filled with one letter.
AGENT_KEY = "mem_sk_" + "A" * 43
NEXT_KEY = "mem_sk_" + "B" * 43
BOX = SecretBox(bytes.fromhex("0f" * 32))


def sealed(key: str, space: str = ACME) -> bytes:
    return BOX.seal(key, bound_to=f"team_home:{space}")


@pytest.fixture
def store(tmp_path):
    db = ControlDb(tmp_path / "control.db", now=lambda: 1_000_000.0)
    yield db
    db.close()


def make_home(store, key: str = AGENT_KEY, **changes):
    values = {"space_id": ACME, "space_name": "Acme", "agent_id": AGENT,
              "agent_name": "Waku", "return_url": RETURN_URL, "key_sealed": sealed(key)}
    return store.set_team_home(**{**values, **changes})


def test_a_team_home_is_a_tenant_marked_as_a_team(store):
    home, created = make_home(store)

    assert created is True
    tenant = store.tenant_by_id(home.tenant_id)
    assert tenant.kind == "team"
    assert tenant.status == "active"
    assert core_tenant.is_tenant_id(tenant.id)
    assert core_tenant.is_project_id(tenant.project_id)
    assert (home.space_id, home.space_name, home.agent_id, home.agent_name,
            home.return_url) == (ACME, "Acme", AGENT, "Waku", RETURN_URL)
    assert store.team_home(ACME) == home
    assert store.team_home_for_tenant(tenant.id) == home


def test_a_person_is_not_a_team(store):
    ben = store.create_tenant(sub="sub-ben", email="ben@acme.com", timezone="UTC")

    assert ben.kind == "person"
    assert store.tenant_by_id(ben.id).kind == "person"
    assert store.team_home_for_tenant(ben.id) is None
    assert store.team_home(ACME) is None


def test_a_team_and_a_person_never_share_a_project_id(store):
    ben = store.create_tenant(sub="sub-ben", email="ben@acme.com", timezone="UTC")
    home, _ = make_home(store)

    assert store.tenant_by_id(home.tenant_id).project_id != ben.project_id


def test_a_second_call_for_a_space_keeps_its_tenant_and_replaces_its_key(store):
    first, _ = make_home(store)
    second, created = make_home(store, key=NEXT_KEY, space_name="Acme Inc")

    assert created is False
    assert second.tenant_id == first.tenant_id
    kept = store.team_home(ACME)
    assert kept.space_name == "Acme Inc"
    assert BOX.open(kept.key_sealed, bound_to=f"team_home:{ACME}") == NEXT_KEY


def test_two_spaces_get_two_homes(store):
    other = "7c1a2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
    acme, _ = make_home(store)
    globex, _ = make_home(store, space_id=other, space_name="Globex",
                          key_sealed=sealed(NEXT_KEY, other))

    assert acme.tenant_id != globex.tenant_id
    assert store.team_home_for_tenant(globex.tenant_id).space_name == "Globex"


def test_a_copy_of_control_db_does_not_contain_the_key(store, tmp_path):
    home, _ = make_home(store)

    assert store.memory_key(home.tenant_id) == ""
    store.close()
    files = [path for path in tmp_path.iterdir() if path.name.startswith("control.db")]
    assert files
    for path in files:
        raw = path.read_bytes()
        assert AGENT_KEY.encode() not in raw, path.name
        assert b"mem_sk_" not in raw, path.name


def test_the_sealed_key_of_one_space_does_not_open_as_anothers(store):
    from hosted.gateway.secretbox import Unreadable

    home, _ = make_home(store)
    with pytest.raises(Unreadable):
        BOX.open(home.key_sealed, bound_to="team_home:another-space")


def test_deleting_the_tenant_deletes_the_home_and_its_key(store):
    home, _ = make_home(store)
    store.delete_tenant(home.tenant_id)

    assert store.team_home(ACME) is None
    assert store.tenant_by_id(home.tenant_id) is None


def test_a_control_db_from_before_the_kind_column_still_opens(tmp_path):
    """upgrade.sh runs no migration on control.db. A file made before spec 032
    has a `tenant` table with no `kind`, and every person in it is a person."""
    path = tmp_path / "control.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE tenant (id TEXT PRIMARY KEY, sub TEXT NOT NULL UNIQUE, "
                "email TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC', "
                "status TEXT NOT NULL DEFAULT 'active', "
                "project_id INTEGER NOT NULL UNIQUE, created_at REAL NOT NULL)")
    old.execute("INSERT INTO tenant VALUES ('abcdefghijkl', 'sub-mei', 'mei@example.com', "
                "'UTC', 'active', 2, 1.0)")
    old.commit()
    old.close()

    store = ControlDb(path)
    try:
        assert store.tenant_by_id("abcdefghijkl").kind == "person"
        home, _ = make_home(store)
        assert store.tenant_by_id(home.tenant_id).kind == "team"
    finally:
        store.close()
