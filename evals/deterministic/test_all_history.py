"""DETERMINISTIC EVAL — the chat dock's "All messages" timeline.

Sean's point: the Loop tab shows every turn across all threads, but the chat
dock only showed one thread at a time, so his history felt missing. The
read-only id="__all__" history action returns the whole cross-thread timeline
(newest last), so the dock can render the full conversation like Loop does."""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_milli
from milli.ops.dashboard import _thread_history, session_action


def _seed(app, session_id, user, assistant):
    for role, content in (("user", user), ("assistant", assistant)):
        app.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES (?, ?, ?, 'dashboard')",
            (role, content, session_id),
        )
    app.conn.commit()


def test_all_history_returns_every_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_HOME", str(tmp_path / "home"))
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    _seed(app, "dashboard-a", "hi from A", "reply A")
    _seed(app, "dashboard-b", "hi from B", "reply B")

    out = session_action({"action": "history", "id": "__all__"})
    contents = [m["content"] for m in out["history"]]
    # every thread present, ordered oldest -> newest
    assert contents == ["hi from A", "reply A", "hi from B", "reply B"]


def test_single_thread_history_is_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_HOME", str(tmp_path / "home"))
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    _seed(app, "dashboard-a", "hi from A", "reply A")
    _seed(app, "dashboard-b", "hi from B", "reply B")

    out = session_action({"action": "history", "id": "dashboard-b"})
    assert [m["content"] for m in out["history"]] == ["hi from B", "reply B"]


def test_thread_history_includes_meta(tmp_path, monkeypatch):
    """Regression: switching threads showed only text because that path dropped
    meta. Both the switch and history paths now go through _thread_history, which
    must carry the per-turn meta (gate/stats/tools/model) so cards render full."""
    monkeypatch.setenv("MILLI_HOME", str(tmp_path / "home"))
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    meta = {"gate": {"decision": "skip"}, "iterations": 1, "latency_ms": 2400,
            "tools": [], "model": "gemini-3.5-flash"}
    app.conn.execute("INSERT INTO chat_log (role, content, session_id, source) VALUES ('user','hi','t','dashboard')")
    app.conn.execute("INSERT INTO chat_log (role, content, session_id, source, meta) "
                     "VALUES ('assistant','hey','t','dashboard',?)", (json.dumps(meta),))
    app.conn.commit()

    hist = _thread_history(app.conn, "t")
    assert hist[0]["meta"] is None                     # user row
    assert hist[1]["meta"]["model"] == "gemini-3.5-flash"
    assert hist[1]["meta"]["gate"]["decision"] == "skip"
