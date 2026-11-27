"""What a person is told when their Waku credits run out (spec 042).

Waku Memory decides who may still call: `GET /agent-usage/balance` answers
`allowed` and `notice` (waku-memory spec 106), and the proxy obeys. This module
holds the two sentences the proxy writes itself, for the two cases where Waku
Memory has not written one:

  - an older Waku Memory that answers no `allowed`, where today's rule stands:
    a Free person at zero is refused;
  - no balance at all (no key, or Waku Memory unreachable), where the monthly
    dollar cap is the limit and its refusal needs a sentence too.

Every refusal carries the code `credits_used_up`, so a caller recognises it
without matching English text that may change.
"""

from __future__ import annotations

import datetime as dt

CREDITS_USED_UP = "credits_used_up"
PLAN_URL = "https://www.waku.one/settings/plan"

# The contract's copy, word for word. {reset} is a day like "1 Dec".
FREE_EMPTY = ("You've used this month's free Waku credits, so Waku Agent is paused until "
              "{reset}. Pro gives you 500,000 credits a month for $20: " + PLAN_URL)
PRO_EMPTY = ("You've used this month's Waku credits, so Waku Agent is paused. "
             "Add 200,000 credits for $10 to carry on: " + PLAN_URL)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def reset_day(at: float) -> str:
    """The first day of the next UTC month, as "1 Dec". The dollar cap resets
    on that day (hosted.core.quota.utc_month). Waku Memory's own period may end
    on another day; when it does, its own `notice` says so and this is unused."""
    month = dt.datetime.fromtimestamp(at, dt.UTC).month
    return f"1 {_MONTHS[month % 12]}"


def used_up_notice(plan: str, at: float) -> str:
    """The sentence for a person refused at zero, when Waku Memory sent none."""
    if plan == "free":
        return FREE_EMPTY.format(reset=reset_day(at))
    return PRO_EMPTY
