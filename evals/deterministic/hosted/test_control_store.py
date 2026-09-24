"""control.db: tenants, sessions and proxy tokens, on a temp file.

The gateway is its only writer. File ownership enforces that on the VM
(control/ is UID 10002, mode 0700); this is the behaviour behind it.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hosted.core import tenant as core_tenant
from hosted.gateway.store import SESSION_TTL_SECONDS, ControlDb


@pytest.fixture
def store(tmp_path):
    clock = {"t": 1_000_000.0}
    db = ControlDb(tmp_path / "control.db", now=lambda: clock["t"])
    db.clock = clock            # the test's handle on the injected clock
    yield db
    db.close()


def test_a_first_login_creates_a_tenant_with_an_id_and_a_project_id(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="Asia/Shanghai")
    assert core_tenant.is_tenant_id(t.id)
    assert t.project_id == core_tenant.FIRST_PROJECT_ID
    assert t.status == "active"
    assert t.timezone == "Asia/Shanghai"
    assert store.tenant_by_sub("sub-1") == t
    assert store.tenant_by_id(t.id) == t
    assert store.tenant_by_email("mei@example.com") == t


def test_project_ids_do_not_repeat(store):
    ids = [store.create_tenant(sub=f"sub-{i}", email=f"{i}@x", timezone="UTC").project_id
           for i in range(5)]
    assert ids == sorted(set(ids))
    assert len(set(ids)) == 5


def test_one_sub_is_one_tenant(store):
    """The UNIQUE on sub is what makes a second login the same tenant rather
    than a second one; naming the error keeps this from passing on a typo."""
    store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_tenant(sub="sub-1", email="someone-else@example.com", timezone="UTC")


def test_an_unknown_zone_is_stored_as_utc(store):
    """Acceptance 4. The browser sends this and /account sends this."""
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="Mars/Olympus")
    assert t.timezone == "UTC"
    store.set_timezone(t.id, "Nowhere/Nothing")
    assert store.tenant_by_id(t.id).timezone == "UTC"
    store.set_timezone(t.id, "Europe/Lisbon")
    assert store.tenant_by_id(t.id).timezone == "Europe/Lisbon"


def test_a_session_resolves_until_it_expires(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.create_session(tenant_id=t.id, value="cookie-value",
                         expires_at=store.clock["t"] + SESSION_TTL_SECONDS)
    assert store.session_tenant("cookie-value", store.clock["t"]) == t.id
    assert store.session_tenant("cookie-value",
                                store.clock["t"] + SESSION_TTL_SECONDS + 1) is None
    assert store.session_tenant("not-a-cookie", store.clock["t"]) is None


def test_the_cookie_value_is_not_in_the_database(store, tmp_path):
    """Only its hash. A stolen control.db must not be a set of live cookies."""
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.create_session(tenant_id=t.id, value="cookie-value",
                         expires_at=store.clock["t"] + 10)
    store.close()
    assert b"cookie-value" not in (tmp_path / "control.db").read_bytes()


def test_logout_ends_every_session_of_the_tenant(store):
    """Apex and tenant host alike, so both cookies stop working at once."""
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    for value in ("apex-cookie", "tenant-cookie"):
        store.create_session(tenant_id=t.id, value=value,
                             expires_at=store.clock["t"] + 10)
    store.delete_sessions(t.id)
    assert store.session_tenant("apex-cookie", store.clock["t"]) is None
    assert store.session_tenant("tenant-cookie", store.clock["t"]) is None


def test_issuing_a_token_revokes_the_previous_one(store):
    """The gateway issues one each time it asks the spawner to start a
    container, so exactly one token per tenant is live."""
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    first = store.issue_token(t.id)
    second = store.issue_token(t.id)
    assert first != second
    assert store.tenant_for_token_hash(core_tenant.token_hash(first)) is None
    assert store.tenant_for_token_hash(core_tenant.token_hash(second)) == (t.id, "active")


def test_the_token_plaintext_is_not_in_the_database(store, tmp_path):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    token = store.issue_token(t.id)
    store.close()
    assert token.encode() not in (tmp_path / "control.db").read_bytes()


def test_revoking_stops_the_token(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    token = store.issue_token(t.id)
    store.revoke_tokens(t.id)
    assert store.tenant_for_token_hash(core_tenant.token_hash(token)) is None


def test_a_token_lookup_carries_the_status_so_the_proxy_can_refuse(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    token = store.issue_token(t.id)
    store.set_status(t.id, "disabled")
    assert store.tenant_for_token_hash(core_tenant.token_hash(token)) == (t.id, "disabled")


def test_disabling_a_tenant_ends_their_sessions_and_their_token(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.create_session(tenant_id=t.id, value="c", expires_at=store.clock["t"] + 10)
    token = store.issue_token(t.id)
    store.set_status(t.id, "disabled")
    store.delete_sessions(t.id)
    store.revoke_tokens(t.id)
    assert store.session_tenant("c", store.clock["t"]) is None
    assert store.tenant_for_token_hash(core_tenant.token_hash(token)) is None


@pytest.mark.parametrize("bad", ["gone", "", "ACTIVE", None])
def test_a_status_outside_the_three_is_refused(store, bad):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    with pytest.raises(ValueError):
        store.set_status(t.id, bad)


def test_deleting_a_tenant_leaves_nothing_behind(store):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.create_session(tenant_id=t.id, value="c", expires_at=store.clock["t"] + 10)
    token = store.issue_token(t.id)
    store.delete_tenant(t.id)
    assert store.tenant_by_id(t.id) is None
    assert store.tenant_by_sub("sub-1") is None
    assert store.session_tenant("c", store.clock["t"]) is None
    assert store.tenant_for_token_hash(core_tenant.token_hash(token)) is None


def test_the_database_is_in_wal_mode(store):
    assert store.journal_mode() == "wal"


def test_one_connection_shared_by_threads_still_issues_exactly_one_live_token(store):
    """check_same_thread=False lets the connection outlive the thread that made
    it, and E1 will put one of these calls in an executor. Without the lock,
    issue_token's revoke and insert interleave and two tokens end up live --
    which is the whole rule this table exists to hold.
    """
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    start = threading.Barrier(8)
    tokens: list[str] = []
    raised: list[BaseException] = []
    guard = threading.Lock()

    def issue() -> None:
        start.wait()
        try:
            token = store.issue_token(t.id)
        except BaseException as exc:            # noqa: BLE001 - reported, not swallowed
            with guard:
                raised.append(exc)
        else:
            with guard:
                tokens.append(token)

    threads = [threading.Thread(target=issue) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert raised == [], f"issue_token raised under threads: {raised}"
    live = [tok for tok in tokens
            if store.tenant_for_token_hash(core_tenant.token_hash(tok)) is not None]
    assert len(live) == 1, f"{len(live)} live tokens after 8 concurrent issues"


def test_reopening_the_file_keeps_everything(store, tmp_path):
    t = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.close()
    again = ControlDb(tmp_path / "control.db")
    try:
        assert again.tenant_by_id(t.id).email == "mei@example.com"
    finally:
        again.close()
