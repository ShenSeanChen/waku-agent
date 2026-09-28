"""DETERMINISTIC EVAL — who the Slack bot is allowed to answer.

Every member of a Slack workspace can open a DM with any app installed in it.
The bot answers out of the owner's PERSONAL memory, and chat_log feeds
consolidation, so an unlisted bot is one the whole workspace can question,
bill, and write into long-term memory. The gateway is therefore DMs only, and
only for the member ids in SLACK_ALLOWED_USER, which is required.

`should_answer`, `SeenEvents` and `config_problem` are pure so the policy can
be pinned here without a Slack connection or the [slack] extra. Every case
below is a door that must stay shut.
"""

from __future__ import annotations

import sys
import types

import pytest

from waku.gateway.slack import (
    INSTALL_HINT,
    SeenEvents,
    config_problem,
    describe_posture,
    should_answer,
    start_in_background,
)
from waku.integrations import INTEGRATIONS

ME = "U111"
STRANGER = "U999"


def dm(**over) -> dict:
    """A plain DM from me: the one shape that gets an answer."""
    return {"channel_type": "im", "user": ME, "text": "what is on my calendar?", **over}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_ALLOWED_USER", ME)
    monkeypatch.delenv("SLACK_HOME", raising=False)
    monkeypatch.delenv("SLACK_MAX_TURNS_PER_HOUR", raising=False)


# ---------- who gets an answer


def test_a_listed_user_gets_an_answer_in_a_dm():
    assert should_answer(dm(), {ME}) is True


def test_an_unlisted_user_is_ignored():
    assert should_answer(dm(user=STRANGER), {ME}) is False


def test_an_empty_allowlist_answers_nobody():
    """The empty configuration must be the safe one, never a wildcard."""
    assert should_answer(dm(), set()) is False


@pytest.mark.parametrize("channel_type", ["channel", "group", "mpim", None])
def test_nothing_but_a_one_to_one_dm_is_answered(channel_type):
    """Public channels, private channels and group DMs are all out, even for
    a listed user: other people read those replies."""
    assert should_answer(dm(channel_type=channel_type), {ME}) is False


def test_bots_are_ignored_so_waku_never_answers_itself():
    """Waku's own replies arrive as message events too. Answering them is a loop
    that bills a turn per lap."""
    assert should_answer(dm(bot_id="B123"), {ME}) is False


@pytest.mark.parametrize("subtype", ["message_changed", "message_deleted", "file_share"])
def test_edits_deletions_and_uploads_are_ignored(subtype):
    assert should_answer(dm(subtype=subtype), {ME}) is False


@pytest.mark.parametrize("text", ["", "   ", None])
def test_an_empty_message_is_not_a_question(text):
    assert should_answer(dm(text=text), {ME}) is False


# ---------- Slack resends


def test_an_event_slack_sends_twice_runs_once():
    """A resend is a second billed turn and a second copy of the reply."""
    seen = SeenEvents()
    assert seen.first_time("Ev1") is True
    assert seen.first_time("Ev1") is False
    assert seen.first_time("Ev2") is True


def test_the_seen_window_stays_bounded():
    seen = SeenEvents(size=2)
    for event_id in ("Ev1", "Ev2", "Ev3"):
        seen.first_time(event_id)
    assert seen.first_time("Ev1") is True, "the oldest id must age out"
    assert seen.first_time("Ev3") is False


# ---------- refusing to start


def test_a_complete_configuration_has_no_problem(configured):
    assert config_problem() is None


@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USER"])
def test_each_required_setting_is_named_when_missing(configured, monkeypatch, missing):
    monkeypatch.setenv(missing, "  ")
    problem = config_problem()
    assert problem is not None and missing in problem


def test_no_bot_token_means_the_dashboard_skips_slack(configured, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    assert start_in_background() is None


def test_the_dashboard_shows_the_install_hint_without_the_extra(configured, monkeypatch):
    monkeypatch.setitem(sys.modules, "slack_bolt", None)   # import now raises ImportError
    with pytest.raises(RuntimeError, match=r"\[slack\]"):
        start_in_background()
    assert "pip install" in INSTALL_HINT


def test_the_dashboard_refuses_to_start_without_an_allowlist(configured, monkeypatch):
    """Raised, not returned as None, so the card shows what to fix."""
    monkeypatch.setitem(sys.modules, "slack_bolt", types.ModuleType("slack_bolt"))
    monkeypatch.setenv("SLACK_ALLOWED_USER", "")
    with pytest.raises(RuntimeError, match="SLACK_ALLOWED_USER"):
        start_in_background()


# ---------- the dashboard card


def _slack_card():
    return next(item for item in INTEGRATIONS if item.key == "slack")


def test_the_card_requires_the_allowlist_and_hides_both_tokens():
    fields = {field.name: field for field in _slack_card().env}
    assert fields["SLACK_ALLOWED_USER"].required
    assert fields["SLACK_BOT_TOKEN"].secret and fields["SLACK_APP_TOKEN"].secret
    assert _slack_card().env[0].name == "SLACK_BOT_TOKEN", \
        "the supervisor treats the first field as the on switch"


@pytest.mark.parametrize("value", ["0", "-5", "lots"])
def test_the_card_rejects_a_turn_cap_that_is_not_a_positive_integer(value):
    with pytest.raises(ValueError, match="SLACK_MAX_TURNS_PER_HOUR"):
        _slack_card().normalize({"SLACK_MAX_TURNS_PER_HOUR": value})
    assert _slack_card().normalize({"SLACK_MAX_TURNS_PER_HOUR": "30"})


# ---------- saying what it is doing


def test_startup_says_dms_only_and_whose_memory(configured, monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USER", f"{ME}, U222")
    posture = describe_posture()
    assert "2 allowlisted user(s)" in posture
    assert "DMs only" in posture
    assert "YOUR personal memory" in posture
