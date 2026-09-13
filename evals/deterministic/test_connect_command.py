"""DETERMINISTIC EVAL — the doors an unconnected integration points at exist.

From 2026-07-30 Google Calendar's "not connected" message told people, and the
agent that repeated it, to run `waku connect google` or click Connect in the
Connections tab. Neither existed: the CLI never had `connect`, and that pop-up
has only Save and Test. google_calendar.connect() was written and never called.
These tests keep the message and the doors in step. No browser opens: the
sign-in itself is replaced by a stub.
"""

from __future__ import annotations

import sys

import pytest

from waku.tools import google_calendar


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(google_calendar, "connect", lambda home: calls.append(home) or "stub: connected")
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    return calls


def test_cli_connect_google_reaches_the_sign_in(signed_in, monkeypatch, capsys):
    from waku.__main__ import main

    monkeypatch.setattr(sys, "argv", ["waku", "connect", "google"])
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 0
    assert signed_in, "`waku connect google` never reached google_calendar.connect"
    assert "stub: connected" in capsys.readouterr().out


def test_cli_connect_unknown_says_what_is_available(monkeypatch, capsys):
    from waku.__main__ import main

    monkeypatch.setattr(sys, "argv", ["waku", "connect", "nope"])
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 1
    assert "google" in capsys.readouterr().out


def test_chat_connect_google_runs_in_the_dashboard(signed_in):
    from waku.ops import commands, dashboard

    events = []
    dashboard._run_command(commands.parse("/connect google"), lambda kind, ev: events.append((kind, ev)))
    assert signed_in, "`/connect google` in the chat never reached google_calendar.connect"
    kind, ev = events[-1]
    assert kind == "done" and "stub: connected" in ev["reply"]


def test_the_not_connected_message_names_only_doors_that_exist():
    hint = google_calendar._SETUP_HINT
    assert "/connect google" in hint and "waku connect google" in hint
    assert "Connections tab" not in hint, "the Connections pop-up has Save and Test, no Connect"
