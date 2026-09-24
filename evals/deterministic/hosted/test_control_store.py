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


def test_two_tenants_can_share_an_email_and_the_lookup_is_pinned(store):
    """Only sub and project_id are unique, in the schema and in the spec. An
    anonymous account later upgraded, an address reused after a delete, or a
    second Supabase identity all put two rows on one address.

    `tenant.sh disable <email>` and `tenant.sh delete <email>` are built on
    this lookup, so which row it answers has to be stated and not left to
    whichever SQLite reached first: disabling the wrong tenant, or archiving
    the wrong tenant's files, is found out afterwards.

    The second row is created with an EARLIER created_at than the first, so
    insertion order and creation order disagree. Without the ORDER BY, SQLite
    hands back the first row it reaches, which is the first inserted -- so a
    test whose two rows agreed on both orders would pass with the pin deleted.
    """
    store.clock["t"] = 2_000.0
    later = store.create_tenant(sub="sub-1", email="mei@example.com", timezone="UTC")
    store.clock["t"] = 1_000.0
    earlier = store.create_tenant(sub="sub-2", email="mei@example.com", timezone="UTC")

    assert later.id != earlier.id
    assert earlier.created_at < later.created_at
    assert store.tenant_by_email("mei@example.com") == earlier, "the earliest created"
    for _ in range(5):
        assert store.tenant_by_email("mei@example.com").id == earlier.id, "always the same"


@pytest.mark.parametrize("bad", ["", "not-a-tenant-id", "ABCDEFGHIJKL",
                                 "abcdefghijkl1", "abcdefghijk", None, 12])
@pytest.mark.parametrize("method,args", [
    ("set_status", ("active",)),
    ("set_timezone", ("UTC",)),
    ("delete_sessions", ()),
    ("issue_token", ()),
    ("revoke_tokens", ()),
    ("delete_tenant", ()),
])
def test_a_method_that_changes_a_row_refuses_a_mangled_tenant_id(store, method, args, bad):
    """Every one of these used to take any string and silently affect zero
    rows, so `tenant.sh disable` on a truncated id reported success and
    disabled nobody. Format only: a well-formed id belonging to nobody still
    affects no rows, and existence is the caller's question."""
    with pytest.raises(ValueError):
        getattr(store, method)(bad, *args)


@pytest.mark.parametrize("bad", ["", "not-a-tenant-id", None])
def test_create_session_refuses_a_mangled_tenant_id(store, bad):
    with pytest.raises(ValueError):
        store.create_session(tenant_id=bad, value="c", expires_at=store.clock["t"] + 10)


def test_a_lookup_answers_none_for_a_mangled_id_rather_than_raising(store):
    """The guard is on the methods that change a row. A lookup is a question,
    and the answer to "is this nonsense a tenant" is no, not an exception."""
    assert store.tenant_by_id("not-a-tenant-id") is None
    assert store.tenant_by_sub("nobody") is None
    assert store.tenant_by_email("nobody@example.com") is None


def test_the_database_is_in_wal_mode(store):
    assert store.journal_mode() == "wal"


def test_the_database_is_synchronous_full(store):
    """2 is FULL. This reads the value in effect rather than the text that was
    sent, which is the only thing that separates declared from applied: a typo
    such as `synchronous=FUL` does not error, it silently lands on NORMAL (1)
    while the source still reads as a durability declaration. What NORMAL can
    lose to a power cut here is a token revocation or a tenant disable."""
    assert store.synchronous() == 2


@pytest.mark.parametrize("round_", range(3))
def test_one_connection_shared_by_threads_still_issues_exactly_one_live_token(store, round_):
    """check_same_thread=False lets the connection outlive the thread that made
    it, and E1 will put one of these calls in an executor. Without the lock,
    issue_token's revoke and insert interleave and two tokens end up live --
    which is the whole rule this table exists to hold.

    Three rounds, because one catches the lock-free version 39 times in 40 and
    three catch it 40 in 40. A single burst per thread rather than a loop:
    looping is worse here, not better, because a late issue revokes everything
    before it and hands the invariant back by accident -- eight threads issuing
    twenty-five times each catches it only 9 times in 40.
    """
    t = store.create_tenant(sub=f"sub-{round_}", email="mei@example.com", timezone="UTC")
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
