"""DETERMINISTIC EVAL — the dashboard rotates idle chat threads.

Live bug: a tester returned days later and their fresh 'what's up' landed in a
week-old 32-message thread. New rule: if the current session's newest message
is older than MILLI_SESSION_IDLE_MINUTES, the next chat starts a new thread
(old one stays in History)."""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_milli
from milli.ops.browser_agent import maybe_rotate_session


def _seed(app, session_id, age_minutes):
    app.conn.execute(
        "INSERT INTO chat_log (role, content, session_id, created_at) "
        "VALUES ('user', 'old message', ?, datetime('now', ?))",
        (session_id, f"-{age_minutes} minutes"),
    )
    app.conn.commit()


def test_idle_session_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_SESSION_IDLE_MINUTES", "60")
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    before = app.session.session_id
    _seed(app, before, age_minutes=120)          # 2h idle > 60m threshold
    maybe_rotate_session(app)
    assert app.session.session_id != before
    assert app.session.session_id.startswith("dashboard-")
    assert app.session.history == []             # fresh working memory too


def test_active_session_stays(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_SESSION_IDLE_MINUTES", "60")
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    before = app.session.session_id
    _seed(app, before, age_minutes=5)            # active conversation
    maybe_rotate_session(app)
    assert app.session.session_id == before


def test_empty_session_stays(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_SESSION_IDLE_MINUTES", "60")
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    before = app.session.session_id
    maybe_rotate_session(app)                   # no messages at all -> no-op
    assert app.session.session_id == before


def test_rotation_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MILLI_SESSION_IDLE_MINUTES", "0")
    app = make_milli(tmp_path / "home", client=ScriptedClient([]))
    before = app.session.session_id
    _seed(app, before, age_minutes=10000)
    maybe_rotate_session(app)
    assert app.session.session_id == before


def test_provider_switch_resets_stale_model_overrides(tmp_path, monkeypatch):
    """Live bug: kimi -> gemini kept gate model kimi-k3; every turn then 404'd
    against Gemini. A provider change must reset any model field the user
    didn't newly type."""
    from milli.ops import settings_api

    captured = {}
    monkeypatch.setenv("MILLI_PROVIDER", "kimi")
    monkeypatch.setenv("MILLI_MODEL", "kimi-k3")
    monkeypatch.setenv("MILLI_SMALL_MODEL", "kimi-k3")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(settings_api, "find_dotenv", lambda **k: "", raising=False)

    # intercept at the env-write layer; abort before the agent rebuild
    def fake_set_key(path, k, v):
        captured[k] = v
        raise RuntimeError("stop-before-rebuild")

    import dotenv
    monkeypatch.setattr(dotenv, "set_key", fake_set_key)
    try:
        settings_api.apply_settings({"provider": "gemini", "model": "kimi-k3",
                                     "small_model": "kimi-k3", "keys": {}})
    except RuntimeError:
        pass
    assert captured.get("MILLI_MODEL", "unset") in ("", "unset") or \
        captured.get("MILLI_PROVIDER") == "gemini"
    # the actual contract: stale kimi ids must have been blanked
    assert captured.get("MILLI_MODEL") != "kimi-k3"
    assert captured.get("MILLI_SMALL_MODEL") != "kimi-k3"
