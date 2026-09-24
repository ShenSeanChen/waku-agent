"""ledger.db: spend and platform-call times. The proxy's alone.

Months are UTC months. Reservations are an aggregate per tenant per month,
which is exactly what lets a crash be recovered: whatever is still reserved
when the proxy starts is settled at its full amount, because the upstream call
it was holding may already have been billed.
"""

from __future__ import annotations

import pytest

from hosted.core.quota import utc_month
from hosted.proxy.ledger import Ledger

MONTH = "2026-09"
TENANT = "abcdefghijkl"


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.db")
    yield db
    db.close()


def test_a_new_tenant_has_spent_nothing(ledger):
    assert ledger.spend(TENANT, MONTH) == (0.0, 0.0)


def test_a_reservation_shows_as_reserved_and_settles_into_settled(ledger):
    ledger.reserve(TENANT, MONTH, 0.40)
    assert ledger.spend(TENANT, MONTH) == (0.0, 0.40)
    ledger.settle(TENANT, MONTH, reserved=0.40, actual=0.11)
    assert ledger.spend(TENANT, MONTH) == (0.11, 0.0)


def test_a_released_reservation_charges_nothing(ledger):
    """An upstream non-2xx with no usage: Anthropic bills no tokens for a
    request it refused."""
    ledger.reserve(TENANT, MONTH, 0.40)
    ledger.release(TENANT, MONTH, 0.40)
    assert ledger.spend(TENANT, MONTH) == (0.0, 0.0)


def test_usage_above_the_reservation_is_settled_at_its_real_price(ledger):
    """count_tokens is an estimate, so this can happen, and it is the only way
    settled spend passes the cap."""
    ledger.reserve(TENANT, MONTH, 0.40)
    ledger.settle(TENANT, MONTH, reserved=0.40, actual=0.90)
    assert ledger.spend(TENANT, MONTH) == (0.90, 0.0)


def test_reservations_never_go_negative(ledger):
    """A double release, or a settle for a reservation another path already
    released, must not hand the tenant free credit."""
    ledger.reserve(TENANT, MONTH, 0.10)
    ledger.release(TENANT, MONTH, 0.10)
    ledger.release(TENANT, MONTH, 0.10)
    assert ledger.spend(TENANT, MONTH) == (0.0, 0.0)


def test_calls_in_flight_add_up(ledger):
    for _ in range(4):
        ledger.reserve(TENANT, MONTH, 0.20)
    assert ledger.spend(TENANT, MONTH)[1] == pytest.approx(0.80)


def test_months_are_separate(ledger):
    ledger.settle(TENANT, "2026-09", reserved=0.0, actual=0.50)
    ledger.settle(TENANT, "2026-10", reserved=0.0, actual=0.10)
    assert ledger.spend(TENANT, "2026-09") == (0.50, 0.0)
    assert ledger.spend(TENANT, "2026-10") == (0.10, 0.0)


def test_tenants_are_separate(ledger):
    ledger.settle("aaaaaaaaaaaa", MONTH, reserved=0.0, actual=0.50)
    assert ledger.spend("bbbbbbbbbbbb", MONTH) == (0.0, 0.0)


def test_leftovers_from_a_previous_run_are_settled_at_startup(ledger, tmp_path):
    ledger.reserve("aaaaaaaaaaaa", MONTH, 0.30)
    ledger.reserve("bbbbbbbbbbbb", MONTH, 0.05)
    ledger.close()

    again = Ledger(tmp_path / "ledger.db")
    try:
        assert again.settle_leftovers() == pytest.approx(0.35)
        assert again.spend("aaaaaaaaaaaa", MONTH) == (0.30, 0.0)
        assert again.spend("bbbbbbbbbbbb", MONTH) == (0.05, 0.0)
        assert again.settle_leftovers() == 0.0      # nothing left the second time
    finally:
        again.close()


def test_a_platform_call_time_is_the_latest_one(ledger):
    assert ledger.last_platform_call(TENANT) is None
    ledger.record_platform_call(TENANT, 1000.0)
    ledger.record_platform_call(TENANT, 2000.0)
    assert ledger.last_platform_call(TENANT) == 2000.0


def test_an_earlier_time_never_moves_it_back(ledger):
    """The proxy records this before every refusal, and calls can land out of
    order. The turn limit reads "a platform call in the past hour", so an
    older timestamp overwriting a newer one would hand a free tenant byok's
    120 turns."""
    ledger.record_platform_call(TENANT, 2000.0)
    ledger.record_platform_call(TENANT, 1000.0)
    assert ledger.last_platform_call(TENANT) == 2000.0


def test_the_database_is_in_wal_mode(ledger):
    assert ledger.journal_mode() == "wal"


def test_the_month_helper_is_the_one_the_proxy_uses():
    """The proxy picks the month with this and the ledger stores that string,
    so a second month format anywhere would split one tenant's spend in two."""
    assert utc_month(1_789_905_600.0) == "2026-09"
