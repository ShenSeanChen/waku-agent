"""Exercise the reported chat steps through the dashboard's HTTP endpoints.

The negative control disables history restoration to reproduce the old bug.
Only the model and Apple probe are stubbed; chat, settings, storage and the
local calendar tool run normally in a temporary home.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from evals.helpers import ScriptedClient, response, text_block, tool_block
from waku import integrations
from waku.memory import Memory
from waku.ops import browser_agent, dashboard
from waku.runtime.session import Session
from waku.tools import calendar

CREATE = "add a food festival on Oct 1 from 2 to 3 PM"
FOLLOWUP = "can u add the food festival event to my apple calender now"
EVENT = {"title": "Food festival", "start": "2026-10-01T14:00:00",
         "end": "2026-10-01T15:00:00"}
SAVED = "Food festival is on Oct 1, 2026, 2 to 3 PM, saved locally and not synced."
REMEMBERED = "Food festival is on Oct 1, 2026, 2 to 3 PM."
FORGOTTEN = "I need the event title, date and time first."


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as result:
        return json.load(result)


@pytest.mark.parametrize("transition", ["settings", "restart"])
@pytest.mark.parametrize("restore_history", [True, False], ids=["fixed", "negative-control"])
def test_reported_steps(tmp_path, monkeypatch, transition, restore_history):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    for key, value in {"WAKU_HOME": str(home), "WAKU_PROVIDER": "anthropic",
                       "WAKU_APPLE_CALENDAR": "", "WAKU_GOOGLE_CALENDAR": "",
                       "WAKU_APPLE_TOOLS": "", "WAKU_GRAPH_WORKFLOWS": "",
                       "WAKU_EXPERIMENTAL": "", "WAKU_EPISODIC_STORE": "sqlite",
                       "WAKU_SESSION_IDLE_MINUTES": "60"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(browser_agent, "_agent", None)
    monkeypatch.setattr(browser_agent, "_dashboard_session", None)
    monkeypatch.setattr(integrations, "_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(integrations, "_HEALTH", None)
    monkeypatch.setattr(calendar, "probe_apple_calendar", lambda: None)
    monkeypatch.setattr(Memory, "gated_retrieve", lambda *a, **k: "")
    monkeypatch.setattr(Memory, "maybe_consolidate", lambda *a, **k: None)
    if not restore_history:
        # Match the original gateway behavior: retain the id, leave history empty.
        monkeypatch.setattr(Session, "switch", Session.start_new)

    followup_prompts = []

    class ContextClient(ScriptedClient):
        def _create(self, **kwargs):
            messages = kwargs["messages"]
            if messages[-1]["content"] == CREATE:
                return response([tool_block("create_event", EVENT)], "tool_use")
            if messages[-1]["content"] == FOLLOWUP:
                followup_prompts.append(list(messages))
                history = str(messages[:-1])
                known = all(detail in history for detail in
                            ("Food festival", "2026-10-01T14:00", "2026-10-01T15:00"))
                return response([text_block(REMEMBERED if known else FORGOTTEN)])
            return response([text_block(SAVED)])

    monkeypatch.setattr("waku.app.get_client", lambda settings: ContextClient([]))

    with _server() as base:
        created = _request(base, "/api/chat", {"message": CREATE})
        assert created["reply"] == SAVED
        assert created["tools"][0]["tool"] == "create_event"
        assert "Not synced to any calendar app" in created["tools"][0]["output"]
        assert "SUMMARY:Food festival" in (home / "calendar.ics").read_text()
        sid = browser_agent.current().session.session_id
        if transition == "settings":
            saved = _request(base, "/api/connections", {
                "key": "apple_calendar", "values": {"WAKU_APPLE_CALENDAR": "1"}})
            assert saved["ok"], saved
            assert browser_agent.current().settings.apple_calendar
            result = _followup(base, sid)

    if transition == "restart":
        browser_agent.current().close()
        monkeypatch.setattr(browser_agent, "_agent", None)
        monkeypatch.setattr(browser_agent, "_dashboard_session", None)
        with _server() as base:
            result = _followup(base, sid)

    assert result["reply"] == (REMEMBERED if restore_history else FORGOTTEN)
    if restore_history:
        assert followup_prompts[0][0] == {"role": "user", "content": CREATE}
        assert "[tools used: create_event(" in followup_prompts[0][1]["content"]
    else:
        assert followup_prompts[0] == [{"role": "user", "content": FOLLOWUP}]


def _followup(base, sid):
    # This is the same history endpoint the dock uses to restore its visible chat.
    visible = _request(base, "/api/session", {"action": "history", "id": sid})
    assert len(visible["history"]) == 2
    assert visible["history"][0]["content"] == CREATE
    assert visible["history"][1]["content"].startswith(SAVED)
    return _request(base, "/api/chat", {"message": FOLLOWUP})
