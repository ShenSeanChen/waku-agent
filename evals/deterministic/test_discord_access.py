"""DETERMINISTIC EVAL — who the Discord bot is allowed to answer.

Written after a live incident on 2026-07-26: the bot was sitting in a 32-member
server and answering EVERY message in EVERY channel it could see. The guard read

    if allowed and str(message.author.id) != allowed:

and DISCORD_ALLOWED_USER was unset — so `allowed` was "", the condition was
never true, and the whole check was skipped. The empty configuration was the
dangerous one.

That is three separate problems, and cost is the least of them:
  * every message was a billed agent turn
  * the bot answers out of the maintainer's PERSONAL memory, so anyone in the
    server could ask what it knew about him
  * chat_log feeds consolidation, so strangers' messages become permanent facts

`should_answer` is a pure function precisely so this policy can be pinned here
without a Discord connection. Every case below is a door that must stay shut.
"""

from __future__ import annotations

from milli.gateway.discord import TurnBudget, _ids, _strip_mention, should_answer

ME = "111"
STRANGER = "999"
OPEN_CHANNEL = "555"
BUSY_CHANNEL = "777"


def policy(**over):
    """Default posture = nothing configured at all."""
    base = {"is_dm": False, "author_id": STRANGER, "channel_id": BUSY_CHANNEL,
            "mentioned": True, "allowed_users": set(), "allowed_channels": set(),
            "require_mention": True}
    return should_answer(**{**base, **over})


# ---------- the incident itself


def test_unconfigured_bot_ignores_every_server_channel():
    """THE regression. With no allowlists set, a message in a server channel —
    mentioned or not — must not produce a turn. An unset channel allowlist means
    NO channel, never every channel."""
    assert policy() is False
    assert policy(mentioned=False) is False
    assert policy(author_id=ME) is False
    assert policy(channel_id=OPEN_CHANNEL) is False


def test_unconfigured_bot_still_answers_dms():
    """The single-user default has to stay usable, or people will disable the
    guard rather than configure it."""
    assert policy(is_dm=True) is True


# ---------- opening it up, one deliberate step at a time


def test_an_allowlisted_channel_answers_only_on_a_mention():
    """Opting a channel in must not turn the bot into a firehose in that channel.
    Passive listening is what made this expensive in the first place."""
    allowed = {"allowed_channels": {OPEN_CHANNEL}, "channel_id": OPEN_CHANNEL}
    assert policy(**allowed, mentioned=True) is True
    assert policy(**allowed, mentioned=False) is False


def test_require_mention_can_be_switched_off_but_only_explicitly():
    assert policy(allowed_channels={OPEN_CHANNEL}, channel_id=OPEN_CHANNEL,
                  mentioned=False, require_mention=False) is True


def test_allowlisting_one_channel_does_not_open_the_others():
    assert policy(allowed_channels={OPEN_CHANNEL}, channel_id=BUSY_CHANNEL) is False


def test_a_user_allowlist_gates_dms_too():
    """Once you name who may use the bot, that applies everywhere — otherwise
    'restricted to me' would still leave DMs open to anyone who finds it."""
    assert policy(is_dm=True, allowed_users={ME}, author_id=ME) is True
    assert policy(is_dm=True, allowed_users={ME}, author_id=STRANGER) is False


def test_both_allowlists_must_pass_in_a_channel():
    """User allowlist AND channel allowlist AND mention. Any one failing is a no."""
    ok = {"allowed_users": {ME}, "allowed_channels": {OPEN_CHANNEL},
          "author_id": ME, "channel_id": OPEN_CHANNEL, "mentioned": True}
    assert policy(**ok) is True
    assert policy(**{**ok, "author_id": STRANGER}) is False
    assert policy(**{**ok, "channel_id": BUSY_CHANNEL}) is False
    assert policy(**{**ok, "mentioned": False}) is False


# ---------- the spend stop


def test_turn_budget_stops_at_the_ceiling():
    """A hard cap across ALL users, so no one person can run the bill up."""
    budget = TurnBudget(per_hour=3)
    assert [budget.allow(now=1000.0) for _ in range(4)] == [True, True, True, False]


def test_turn_budget_is_a_rolling_hour_not_a_fixed_window():
    budget = TurnBudget(per_hour=2)
    assert budget.allow(now=1000.0) is True
    assert budget.allow(now=1000.0) is True
    assert budget.allow(now=1000.0) is False
    assert budget.allow(now=1000.0 + 3601) is True, "turns older than an hour must expire"


# ---------- parsing


def test_id_lists_tolerate_the_way_people_actually_write_them(monkeypatch):
    monkeypatch.setenv("X", " 111, 222 ,,333 ")
    assert _ids("X") == {"111", "222", "333"}
    monkeypatch.setenv("X", "")
    assert _ids("X") == set(), "an empty value must never parse into a wildcard"


def test_mention_prefix_is_stripped_before_the_model_sees_it():
    assert _strip_mention("<@42> what is on my calendar?", "42") == "what is on my calendar?"
    assert _strip_mention("<@!42> hi", "42") == "hi"
    assert _strip_mention("<@42>", "42") == "", "a bare ping is not a question"
