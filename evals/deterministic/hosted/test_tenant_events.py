"""tenant_events and tenant_wakeups in control.db -- spec 026 task A1.

The gateway is the only process that is always running, so it is where an
event for a sleeping tenant is kept until its container has it. These tests
drive a real ControlDb on a temp file.
"""

from __future__ import annotations

import sqlite3

import pytest

from hosted.gateway.store import ControlDb


@pytest.fixture
def store(tmp_path):
    clock = {"t": 1_000_000.0}
    db = ControlDb(tmp_path / "control.db", now=lambda: clock["t"])
    db.clock = clock
    db.path = tmp_path / "control.db"
    yield db
    db.close()


def _tenant(store, sub: str = "sub-ana"):
    return store.create_tenant(sub=sub, email=f"{sub}@example.com", timezone="UTC")


def _add(store, tenant_id: str, external_id: str = "Ev1", source: str = "slack",
         text: str = "remind the channel on Friday") -> bool:
    return store.add_event(tenant_id, source=source, external_id=external_id,
                           session_id="slack:C1:171.5", payload={"text": text})


def test_insert_keeps_one_pending_row_with_everything_the_container_needs(store):
    ana = _tenant(store)
    store.clock["t"] = 1_000_050.0
    assert _add(store, ana.id) is True

    (event,) = store.pending_events(ana.id)
    assert (event.tenant_id, event.source, event.external_id) == (ana.id, "slack", "Ev1")
    assert event.session_id == "slack:C1:171.5"
    assert event.payload == {"text": "remind the channel on Friday"}
    assert event.status == "pending"
    assert event.received_at == 1_000_050.0
    assert store.tenants_with_pending_events() == [ana.id]


def test_insert_survives_the_gateway_restarting(store):
    """The row is on disk, not in the gateway's memory: a second connection to
    the same file reads it."""
    ana = _tenant(store)
    _add(store, ana.id)
    again = ControlDb(store.path)
    try:
        assert [e.external_id for e in again.pending_events(ana.id)] == ["Ev1"]
    finally:
        again.close()


def test_duplicate_delivery_is_refused_and_leaves_the_first_row_alone(store):
    ana = _tenant(store)
    assert _add(store, ana.id, text="first") is True
    assert _add(store, ana.id, text="resent") is False

    (event,) = store.pending_events(ana.id)
    assert event.payload == {"text": "first"}


def test_duplicate_is_decided_per_tenant_and_per_source(store):
    """UNIQUE(tenant_id, source, external_id): the same id from another source,
    or for another tenant, is another event."""
    ana, ben = _tenant(store), _tenant(store, "sub-ben")
    assert _add(store, ana.id, "Ev1", source="slack") is True
    assert _add(store, ana.id, "Ev1", source="chat") is True
    assert _add(store, ben.id, "Ev1", source="slack") is True

    assert len(store.pending_events(ana.id)) == 2
    assert len(store.pending_events(ben.id)) == 1


def test_duplicate_is_refused_by_the_table_not_only_by_the_method(store):
    """A second writer that skips add_event still cannot write the row twice."""
    ana = _tenant(store)
    _add(store, ana.id)
    raw = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO tenant_events (tenant_id, source, external_id, session_id, "
                "payload, received_at) VALUES (?, 'slack', 'Ev1', 's', '{}', 0)", (ana.id,))
    finally:
        raw.close()


def test_duplicate_of_a_handed_event_stays_handed(store):
    """Slack resends an event whose answer was slow. A resend that arrives
    after the hand-over must not put the event back in the queue."""
    ana = _tenant(store)
    _add(store, ana.id)
    (event,) = store.pending_events(ana.id)
    store.mark_events_handed(ana.id, [event.id])

    assert _add(store, ana.id) is False
    assert store.pending_events(ana.id) == []
    assert store.tenants_with_pending_events() == []


def test_insert_order_is_the_order_events_are_handed_over(store):
    ana = _tenant(store)
    for name in ("Ev1", "Ev2", "Ev3"):
        _add(store, ana.id, name)
    assert [e.external_id for e in store.pending_events(ana.id)] == ["Ev1", "Ev2", "Ev3"]


def test_handing_one_tenants_events_leaves_another_tenants_pending(store):
    ana, ben = _tenant(store), _tenant(store, "sub-ben")
    _add(store, ana.id)
    _add(store, ben.id)
    store.mark_events_handed(ana.id, [e.id for e in store.pending_events(ana.id)])
    # Ben's row id, offered under Ana's tenant id, changes nothing.
    store.mark_events_handed(ana.id, [e.id for e in store.pending_events(ben.id)])

    assert store.tenants_with_pending_events() == [ben.id]


def test_insert_refuses_an_id_that_is_not_a_tenant_id(store):
    with pytest.raises(ValueError):
        _add(store, "not-a-tenant")


def test_a_wakeup_is_due_only_once_its_time_has_come(store):
    ana = _tenant(store)
    store.set_wakeup(ana.id, 1_000_600.0)

    assert store.wakeup(ana.id) == 1_000_600.0
    assert store.due_wakeups(1_000_599.0) == []
    assert store.due_wakeups(1_000_600.0) == [ana.id]


def test_a_tenant_has_one_wakeup_and_a_new_one_replaces_it(store):
    ana = _tenant(store)
    store.set_wakeup(ana.id, 1_000_600.0)
    store.set_wakeup(ana.id, 1_009_000.0)

    assert store.due_wakeups(1_000_700.0) == []
    assert store.wakeup(ana.id) == 1_009_000.0
    store.clear_wakeup(ana.id)
    assert store.wakeup(ana.id) is None
    assert store.due_wakeups(2_000_000.0) == []


def test_deleting_a_tenant_removes_its_events_and_its_wakeup(store):
    ana, ben = _tenant(store), _tenant(store, "sub-ben")
    _add(store, ana.id)
    _add(store, ben.id)
    store.set_wakeup(ana.id, 1_000_600.0)
    store.delete_tenant(ana.id)

    assert store.tenants_with_pending_events() == [ben.id]
    assert store.due_wakeups(2_000_000.0) == []
