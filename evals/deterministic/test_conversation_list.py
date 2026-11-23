"""DETERMINISTIC EVAL — the container half of chat history over the front door.

Spec 007 D: waku.one lists and opens a person's conversations by asking the
gateway, which forwards to the container's own /api/session. The two reads go
as GET (`?action=list`, `?action=history&id=<id>`), because the gateway turns
`GET /v1/conversations` into a request with no body. These pin the list's
shape, that GET serves only the reads, and that history on GET is the same
rows the dock's POST history action returns.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from evals.helpers import ScriptedClient, make_waku
from waku.ops import browser_agent, dashboard


def _seed(app, session_id, *pairs):
    for role, content in pairs:
        app.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES (?, ?, ?, 'dashboard')",
            (role, content, session_id))
    app.conn.commit()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(browser_agent, "_dashboard_session", "s-20261003-091500")
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    _seed(app, "s-20261003-091500", ("user", "  research the competitors\n of mem0  "),
          ("assistant", "Two paragraphs."))
    _seed(app, "telegram-42", ("user", "hi"), ("assistant", "hello"), ("user", "again"))
    return app


def test_list_answers_id_title_last_at_and_count(home):
    out = dashboard.session_action({"action": "list"})
    assert out["ok"] is True
    assert out["current"] == "s-20261003-091500"
    by_id = {c["id"]: c for c in out["conversations"]}
    assert set(by_id) == {"s-20261003-091500", "telegram-42"}
    first = by_id["s-20261003-091500"]
    assert set(first) == {"id", "title", "last_at", "count"}
    assert first["title"] == "research the competitors of mem0"
    assert first["count"] == 2 and by_id["telegram-42"]["count"] == 3
    assert first["last_at"]


@pytest.fixture
def server(home):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(base: str, target: str) -> dict:
    with urllib.request.urlopen(base + target, timeout=10) as response:
        return json.loads(response.read())


def test_get_serves_the_list_and_one_conversations_history(server):
    listed = _get(server, "/api/session?action=list")
    assert {c["id"] for c in listed["conversations"]} == {"s-20261003-091500", "telegram-42"}

    read = _get(server, "/api/session?action=history&id=telegram-42")
    assert read == dashboard.session_action({"action": "history", "id": "telegram-42"})
    assert [m["content"] for m in read["history"]] == ["hi", "hello", "again"]


def test_get_never_starts_or_switches_a_conversation(server, monkeypatch):
    def must_not_build():
        raise AssertionError("a GET built the agent")

    monkeypatch.setattr(dashboard, "get_agent", must_not_build)
    for action in ("new", "switch", ""):
        assert "error" in _get(server, f"/api/session?action={action}&id=telegram-42")
