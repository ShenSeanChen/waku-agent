"""Plans, turn windows and reservations -- acceptance 11.

Which plan applies is decided from platform-controlled data only. A tenant
cannot choose it, and the dollar cap and the concurrency limit bind every
platform-token call whatever the tenant's plan.
"""

from __future__ import annotations

import inspect
import re
import time

import pytest

from hosted.core import quota

PLANS = quota.DEFAULT_PLANS

# The exact six names F1 will write into config/gateway.env and
# config/proxy.env, five PRs from now.
PLAN_VARIABLES = {
    "WAKU_FREE_TURNS_PER_HOUR", "WAKU_BYOK_TURNS_PER_HOUR",
    "WAKU_FREE_DOLLAR_CAP", "WAKU_FREE_CONCURRENT_CALLS",
    "WAKU_FREE_REQUESTS_PER_MINUTE", "WAKU_FREE_MAX_TOKENS",
}


def test_the_two_plans_are_the_specs_two_plans():
    free, byok = PLANS["free"], PLANS["byok"]
    assert (free.turns_per_hour, free.dollar_cap, free.concurrent_calls) == (30, 1.00, 4)
    assert (byok.turns_per_hour, byok.dollar_cap) == (120, None)
    assert free.requests_per_minute == 60
    assert free.max_tokens_ceiling == 4096


def test_plan_values_come_from_each_services_configuration():
    """control.db has no plan table: the turns live in config/gateway.env and
    the money in config/proxy.env, beside the service that enforces them."""
    plans = quota.plans_from_env({
        "WAKU_FREE_TURNS_PER_HOUR": "10",
        "WAKU_BYOK_TURNS_PER_HOUR": "200",
        "WAKU_FREE_DOLLAR_CAP": "2.50",
        "WAKU_FREE_CONCURRENT_CALLS": "8",
        "WAKU_FREE_REQUESTS_PER_MINUTE": "30",
        "WAKU_FREE_MAX_TOKENS": "2048",
    })
    assert plans["free"].turns_per_hour == 10
    assert plans["byok"].turns_per_hour == 200
    assert plans["free"].dollar_cap == 2.50
    assert plans["free"].concurrent_calls == 8
    assert plans["free"].requests_per_minute == 30
    assert plans["free"].max_tokens_ceiling == 2048
    assert plans["byok"].dollar_cap is None


def test_an_empty_environment_gives_the_defaults():
    assert quota.plans_from_env({}) == quota.DEFAULT_PLANS


def test_the_six_variable_names_are_pinned_for_group_f():
    """A misspelling here is silent by construction: plans_from_env falls back
    to the default, so the free tier would quietly run at 30 turns and $1
    whatever the operator configured, on a VM, months later. Set equality in
    both directions, so a renamed variable and an unpinned seventh one both
    fail here rather than in production."""
    source = inspect.getsource(quota.plans_from_env)
    found = set(re.findall(r'"(WAKU_[A-Z_]+)"', source))
    assert found == PLAN_VARIABLES, (
        f"plans_from_env no longer reads: {sorted(PLAN_VARIABLES - found)}\n"
        f"plans_from_env reads names nothing pins: {sorted(found - PLAN_VARIABLES)}\n"
        "F1 writes config/gateway.env and config/proxy.env from this list.")


def test_byok_needs_a_turn_this_hour_and_no_platform_call():
    assert quota.plan_for(PLANS, turns_in_last_hour=1,
                          platform_call_in_last_hour=False).name == "byok"


@pytest.mark.parametrize("turns,platform", [(0, False), (0, True), (5, True), (500, True)])
def test_everything_else_is_free(turns, platform):
    """A tenant with a recent platform-token call is on free whatever their
    provider says, and a tenant's first turn of the hour is free until the
    ledger has seen how they paid for it."""
    assert quota.plan_for(PLANS, turns_in_last_hour=turns,
                          platform_call_in_last_hour=platform).name == "free"


def test_the_thirty_first_free_turn_is_refused():
    for already in range(30):
        allowed, plan, message = quota.allow_turn(
            PLANS, turns_in_last_hour=already, platform_call_in_last_hour=True)
        assert allowed is True and plan.name == "free" and message == ""
    allowed, plan, message = quota.allow_turn(
        PLANS, turns_in_last_hour=30, platform_call_in_last_hour=True)
    assert allowed is False and plan.name == "free"
    assert message == "Turn limit reached for this hour."


def test_the_hundred_and_twenty_first_byok_turn_is_refused():
    allowed, plan, _ = quota.allow_turn(
        PLANS, turns_in_last_hour=119, platform_call_in_last_hour=False)
    assert allowed is True and plan.name == "byok"
    allowed, plan, message = quota.allow_turn(
        PLANS, turns_in_last_hour=120, platform_call_in_last_hour=False)
    assert allowed is False and plan.name == "byok"
    assert message == "Turn limit reached for this hour."


def test_the_turn_window_is_the_specs_hour():
    """The spec says 30 turns an HOUR. The behaviour test below advances the
    clock by TURN_WINDOW_SECONDS, so it is circular with respect to this
    number: set the constant to 60 and every other test in this file stays
    green while a free tenant quietly gets 30 turns a minute, sixty times the
    intended rate of platform model calls. The literal is the whole point."""
    assert quota.TURN_WINDOW_SECONDS == 3600


def test_a_turn_falls_out_of_the_window_after_an_hour_of_wall_clock():
    """The same number again, but pinned through the behaviour rather than
    through the constant, so shrinking the window inside count() fails here
    even if the constant is left alone."""
    clock = {"t": 1_000_000.0}
    window = quota.TurnWindow(lambda: clock["t"])
    window.record("abcdefghijkl")
    clock["t"] += 3599.0
    assert window.count("abcdefghijkl") == 1, "a turn expired inside the hour"
    clock["t"] += 2.0
    assert window.count("abcdefghijkl") == 0, "a turn outlived the hour"


def test_the_turn_window_forgets_an_hour_later():
    clock = {"t": 1_000_000.0}
    window = quota.TurnWindow(lambda: clock["t"])
    for _ in range(5):
        window.record("abcdefghijkl")
    assert window.count("abcdefghijkl") == 5
    clock["t"] += quota.TURN_WINDOW_SECONDS - 1
    assert window.count("abcdefghijkl") == 5
    clock["t"] += 2
    assert window.count("abcdefghijkl") == 0
    assert window.count("mnopqrstuvwx") == 0


def test_a_disabled_tenants_window_can_be_dropped():
    window = quota.TurnWindow(lambda: 0.0)
    window.record("abcdefghijkl")
    window.forget("abcdefghijkl")
    assert window.count("abcdefghijkl") == 0


PRICES = quota.Prices(input_per_mtok=3.0, output_per_mtok=15.0,
                      cache_write_per_mtok=3.75, cache_read_per_mtok=0.30)


def test_a_reservation_is_the_counted_input_plus_ten_percent_plus_the_whole_output():
    """Anthropic calls count_tokens an estimate, so the margin is real money
    rather than a rounding nicety."""
    dollars = quota.reservation_dollars(input_tokens=1_000_000, max_tokens=1_000_000,
                                        prices=PRICES)
    assert dollars == pytest.approx(3.0 * 1.10 + 15.0)


def test_settlement_prices_all_four_usage_fields():
    dollars = quota.settled_dollars(
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000,
        prices=PRICES)
    assert dollars == pytest.approx(3.0 + 15.0 + 3.75 + 0.30)


def test_the_cap_is_settled_plus_reserved():
    assert quota.at_cap(settled=0.9, reserved=0.1, cap=1.0) is True
    assert quota.at_cap(settled=0.9, reserved=0.09, cap=1.0) is False
    assert quota.at_cap(settled=99.0, reserved=0.0, cap=None) is False
    assert quota.would_pass_cap(settled=0.5, reserved=0.4, adding=0.2, cap=1.0) is True
    assert quota.would_pass_cap(settled=0.5, reserved=0.4, adding=0.05, cap=1.0) is False
    assert quota.would_pass_cap(settled=500.0, reserved=0.0, adding=9.0, cap=None) is False


def test_max_tokens_is_clamped_to_the_free_tiers_ceiling():
    """waku asks for 8192 by default (waku/config.py), so a very long
    free-tier reply is cut at 4096 tokens."""
    assert quota.clamp_max_tokens(8192, 4096) == 4096
    assert quota.clamp_max_tokens(1000, 4096) == 1000
    assert quota.clamp_max_tokens(0, 4096) == 1
    assert quota.clamp_max_tokens(-5, 4096) == 1


def test_months_are_utc_months():
    # 1790814600 is 2026-10-01T00:30:00Z: still September in Los Angeles and
    # already October in Shanghai. The ledger has one answer and it is UTC.
    assert quota.utc_month(1_790_814_600.0) == "2026-10"
    assert quota.utc_month(1_789_905_600.0) == "2026-09"   # 2026-09-20T12:00Z


@pytest.mark.skipif(not hasattr(time, "tzset"),
                    reason="time.tzset is POSIX-only; the VM and CI are both Linux")
@pytest.mark.parametrize("zone,boundary,month", [
    # 2026-10-01T00:30:00Z -- still September in Los Angeles
    ("America/Los_Angeles", 1_790_814_600.0, "2026-10"),
    # 2026-09-30T23:50:00Z -- already October in Shanghai
    ("Asia/Shanghai", 1_790_812_200.0, "2026-09"),
])
def test_a_month_does_not_move_with_the_machines_own_zone(monkeypatch, zone, boundary, month):
    """THE TEST ABOVE CANNOT FAIL ON CI. Drop the `tz=` from utc_month and it
    still passes in UTC, and `runs-on: ubuntu-latest` is UTC -- so the check
    that matters would go green on the one machine that gates the merge. The
    two timestamps here sit half an hour either side of a UTC month boundary,
    so one catches a zone behind UTC and the other a zone ahead of it, and the
    process's own zone is forced rather than inherited."""
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        assert quota.utc_month(boundary) == month
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_sentences_are_the_specs_sentences():
    assert quota.TURN_LIMIT_MESSAGE == "Turn limit reached for this hour."
    assert quota.FREE_TIER_MESSAGE == "Free tier used up. Add your own key in Models."
    assert quota.MODEL_NOT_ALLOWED_MESSAGE == (
        "This model is not in the free tier. Add your own key in Models.")
    assert quota.SPEND_UNAVAILABLE_MESSAGE == "Spend is unavailable right now."
