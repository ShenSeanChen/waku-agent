"""ledger.db: spend and platform-call times. The proxy's alone.

Months are UTC months. Reservations are an aggregate per tenant per month,
which is exactly what lets a crash be recovered: whatever is still reserved
when the proxy starts is settled at its full amount, because the upstream call
it was holding may already have been billed.
"""

from __future__ import annotations

import math
import threading

import pytest

from hosted.core.quota import utc_month
from hosted.proxy import ledger as ledger_module
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


def test_a_reservation_that_outlives_its_month_is_billed_once(ledger):
    """A call reserved at 23:59 on the last of the month settles at 00:00 on
    the first, and the two are different UTC months. Releasing only from the
    settle month would strand the reservation in the old one, where
    settle_leftovers charges it again at full -- one call, billed twice, on the
    number a hosted tenant reads on /account.
    """
    ledger.reserve(TENANT, "2026-09", 0.40)
    ledger.settle(TENANT, "2026-10", reserved=0.40, actual=0.11)

    assert ledger.spend(TENANT, "2026-09") == (0.0, 0.0), "the old month is not stranded"
    assert ledger.spend(TENANT, "2026-10") == (0.11, 0.0), "charged once, in the new month"
    assert ledger.settle_leftovers() == 0.0, "nothing left for a restart to bill again"


def test_a_release_that_outlives_its_month_strands_nothing(ledger):
    """The other side of the boundary: an upstream refusal at 00:00 on the
    first releases a reservation made in the month before."""
    ledger.reserve(TENANT, "2026-09", 0.40)
    ledger.release(TENANT, "2026-10", 0.40)

    assert ledger.spend(TENANT, "2026-09") == (0.0, 0.0)
    assert ledger.spend(TENANT, "2026-10") == (0.0, 0.0)
    assert ledger.settle_leftovers() == 0.0


def test_the_spill_takes_the_named_month_first(ledger):
    """Only what the named month cannot cover spills backwards, so an ordinary
    same-month settle never touches another month's reservations."""
    ledger.reserve(TENANT, "2026-09", 1.00)
    ledger.reserve(TENANT, "2026-10", 0.25)
    ledger.settle(TENANT, "2026-10", reserved=0.25, actual=0.20)

    assert ledger.spend(TENANT, "2026-09") == (0.0, 1.00), "untouched"
    assert ledger.spend(TENANT, "2026-10") == (0.20, 0.0)


def test_the_spill_never_reaches_another_tenant(ledger):
    """Reservations are fungible within one tenant and nowhere else."""
    ledger.reserve("aaaaaaaaaaaa", "2026-09", 0.40)
    ledger.settle("bbbbbbbbbbbb", "2026-10", reserved=0.40, actual=0.11)

    assert ledger.spend("aaaaaaaaaaaa", "2026-09") == (0.0, 0.40), "not mine to release"
    assert ledger.spend("bbbbbbbbbbbb", "2026-10") == (0.11, 0.0)


@pytest.mark.parametrize("call", [
    lambda led: led.reserve(TENANT, MONTH, -5.0),
    lambda led: led.settle(TENANT, MONTH, reserved=0.0, actual=-99.0),
    lambda led: led.release(TENANT, MONTH, -5.0),
])
def test_a_negative_amount_can_never_credit_a_tenant(ledger, call):
    """The subtracting paths were clamped and the adding paths were not, which
    made the file read as guarded. A negative settled raises the tenant's
    effective dollar cap by its own size."""
    call(ledger)
    settled, reserved = ledger.spend(TENANT, MONTH)
    assert settled >= 0.0 and reserved >= 0.0, (settled, reserved)


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
@pytest.mark.parametrize("call", [
    lambda led, v: led.reserve(TENANT, MONTH, v),
    lambda led, v: led.settle(TENANT, MONTH, reserved=0.0, actual=v),
    lambda led, v: led.settle(TENANT, MONTH, reserved=v, actual=0.0),
    lambda led, v: led.release(TENANT, MONTH, v),
])
def test_a_non_finite_amount_is_refused(ledger, call, bad):
    """NaN is the dangerous one: every `settled >= cap` against it is False, so
    the cap would never trip. It used to be refused only by accident, because
    SQLite stores NaN as NULL and the NOT NULL constraint caught it. Infinity
    was accepted outright, and json.dumps writes it as bare `Infinity`, which
    is not JSON and would break the answer on the spend socket."""
    with pytest.raises(ValueError):
        call(ledger, bad)
    assert ledger.spend(TENANT, MONTH) == (0.0, 0.0)


def test_the_database_is_in_wal_mode(ledger):
    assert ledger.journal_mode() == "wal"


def declared(pragmas):
    """name -> value, parsed from the tuple the store actually applies.

    Two assertions are needed per pragma and neither is enough alone. The
    readback catches a WRONG VALUE -- `=NORMAL`, or the `=FUL` typo that does
    not error and silently lands on NORMAL. This catches REMOVAL, which the
    readback cannot: SQLite's own default synchronous is already 2, so
    deleting the line leaves every readback unchanged.

    What ties this tuple to the database is test_the_database_is_in_wal_mode:
    SQLite's default journal_mode is `delete`, so WAL is the canary that the
    `for pragma in PRAGMAS` loop ran at all.
    """
    out = {}
    for statement in pragmas:
        name, _, value = statement.removeprefix("PRAGMA ").partition("=")
        out[name.strip().lower()] = value.strip()
    return out


def test_the_store_declares_synchronous_full():
    """Deleting the pragma is the mutation the readback below cannot see."""
    assert declared(ledger_module.PRAGMAS).get("synchronous") == "FULL"


def test_the_database_is_synchronous_full(ledger):
    """2 is FULL. This reads the value in effect rather than the text that was
    sent, which is the only thing that separates declared from applied: a typo
    such as `synchronous=FUL` does not error, it silently lands on NORMAL (1)
    while the source still reads as a durability declaration. What NORMAL can
    lose to a power cut here is settled spend."""
    assert ledger.synchronous() == 2


def test_the_store_declares_a_busy_timeout():
    assert declared(ledger_module.PRAGMAS).get("busy_timeout") == "5000"


def test_the_database_has_a_busy_timeout(ledger):
    """The pragma is the only thing setting this: sqlite3.connect's `timeout`
    parameter would otherwise set the same 5000 ms by default, two sources
    agreeing by coincidence, and deleting the pragma would change nothing that
    any readback could see."""
    assert ledger.busy_timeout() == 5000


def test_one_connection_shared_by_threads_keeps_every_dollar(ledger):
    """check_same_thread=False is set here as it is on ControlDb, so the same
    executor that makes that one reachable makes this one reachable. Measured
    lock-free, eight threads each reserving a dollar once, twenty rounds: 12
    ended with the wrong total and 10 raised. The failure that matters is the
    quiet one -- an operator sees a tenant spending past the cap with nothing
    anywhere saying why.

    Twenty-five reserves per thread rather than one, because one round of eight
    single reserves catches the lock-free version only 25 times in 40. A guard
    that proves itself three times in five is not a guard. At eight by
    twenty-five it is 40 in 40, and with the lock it is exact every time.
    """
    start = threading.Barrier(8)
    raised: list[BaseException] = []
    guard = threading.Lock()

    def reserve() -> None:
        start.wait()
        for _ in range(25):
            try:
                ledger.reserve(TENANT, MONTH, 1.00)
            except BaseException as exc:        # noqa: BLE001 - reported, not swallowed
                with guard:
                    raised.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert raised == [], f"reserve raised under threads: {raised}"
    assert ledger.spend(TENANT, MONTH)[1] == pytest.approx(8 * 25.00)


def test_the_month_helper_is_the_one_the_proxy_uses():
    """The proxy picks the month with this and the ledger stores that string,
    so a second month format anywhere would split one tenant's spend in two."""
    assert utc_month(1_789_905_600.0) == "2026-09"
