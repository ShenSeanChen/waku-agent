"""DETERMINISTIC EVAL — the browser gateway's shared agent survives a swap.

The dashboard is the one gateway that is both multi-threaded and long-lived, so
it keeps a single Milli behind `milli.ops.browser_agent`. Two callers mutate it:
dashboard.py builds it on the first chat, settings_api rebuilds it when you
change provider or model. This pins the part that is easy to get wrong.

The bug this was written for: changing ANY setting rebuilt the agent, and a
fresh Milli starts on the eternal 'default' session. So one visit to the Settings
tab silently moved you into a different conversation — your chat was still in
the database, just no longer the thread the dock was showing. It looked like
data loss and was reported as one.
"""

from __future__ import annotations

import pytest

from milli.ops import browser_agent


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A throwaway home + a clean module global, so these tests never see (or
    leave behind) the real dashboard's agent."""
    monkeypatch.setenv("MILLI_HOME", str(tmp_path))
    monkeypatch.setenv("MILLI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(browser_agent, "_agent", None)
    monkeypatch.setattr(browser_agent, "_dashboard_session", None)
    return tmp_path


def test_rebuild_keeps_the_conversation(isolated):
    """A settings change swaps the BRAIN, not the thread."""
    first = browser_agent.get_agent()
    session = first.session.session_id
    assert session != "default", "a dashboard run must never chat into 'default'"

    assert browser_agent.rebuild() is None          # None == success
    after = browser_agent.current()

    assert after is not first, "rebuild must actually build a new agent"
    assert after.session.session_id == session, (
        "rebuild dropped the chat thread — the dock would show an empty "
        "conversation and the user's messages would look lost"
    )


def test_rebuild_without_a_prior_agent_still_gets_a_dated_session(isolated):
    """Hitting Settings before ever chatting is a legitimate first action. It
    must not leave the agent parked on 'default' either."""
    assert browser_agent.current() is None
    assert browser_agent.rebuild() is None
    assert browser_agent.current().session.session_id.startswith("dashboard-")


def test_a_failed_rebuild_keeps_the_working_agent(isolated, monkeypatch):
    """A typo'd key should cost you the switch, not the agent you already had.
    Losing both is the failure mode that makes a local tool feel broken."""
    first = browser_agent.get_agent()

    def explode(*a, **k):
        raise SystemExit("no API key found")     # what get_client actually raises

    monkeypatch.setattr("milli.app.Milli", explode)

    error = browser_agent.rebuild()
    assert error and "no API key" in error
    assert browser_agent.current() is first, "a failed swap must not orphan the user"
    assert first.session.session_id, "and the surviving agent keeps its thread"
