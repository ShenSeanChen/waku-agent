"""DETERMINISTIC EVAL — the K3-as-referee quality judge (waku.ops.judge).

We can't call a real model hermetically, so we stub the client with a canned
JSON reply and pin the parse/clamp behavior + graceful failure. The point: a
judge hiccup must degrade to None (no score), never crash a race."""

from __future__ import annotations

from waku.ops import judge as J


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Client:
    def __init__(self, text):
        self._text = text

    class _Messages:
        pass

    @property
    def messages(self):
        m = self._Messages()
        m.create = lambda **kw: _Resp(self._text)
        return m


def _stub(monkeypatch, text):
    monkeypatch.setattr(J, "get_client", lambda settings: _Client(text))


def test_parses_score_and_reason(monkeypatch):
    _stub(monkeypatch, '{"score": 8, "reason": "solid and concise"}')
    v = J.judge_reply("do X", "here is X")
    assert v["score"] == 8 and v["reason"] == "solid and concise"
    assert v["judge"] == J.JUDGE_MODEL


def test_score_is_clamped_0_10(monkeypatch):
    _stub(monkeypatch, '{"score": 99, "reason": "over"}')
    assert J.judge_reply("q", "a")["score"] == 10
    _stub(monkeypatch, '{"score": -5, "reason": "under"}')
    assert J.judge_reply("q", "a")["score"] == 0


def test_extracts_json_from_surrounding_prose(monkeypatch):
    _stub(monkeypatch, 'Sure!\n{"score": 6, "reason": "ok"}\nHope that helps')
    assert J.judge_reply("q", "a")["score"] == 6


def test_empty_reply_is_not_judged(monkeypatch):
    _stub(monkeypatch, '{"score": 5, "reason": "x"}')
    assert J.judge_reply("q", "   ") is None


def test_bad_json_degrades_to_none(monkeypatch):
    _stub(monkeypatch, "the model rambled without any json")
    assert J.judge_reply("q", "a") is None


def test_missing_judge_key_returns_none_without_retry(monkeypatch):
    calls = []

    def missing_key(settings):
        calls.append(settings)
        raise SystemExit("No API key for provider 'openai'")

    monkeypatch.setattr(J, "get_client", missing_key)
    assert J.judge_reply("q", "a", "openai", "gpt-5.6-sol") is None
    assert len(calls) == 1


def test_missing_judge_key_still_persists_race(monkeypatch, tmp_path):
    import tempfile
    from types import SimpleNamespace

    from waku import app
    from waku.ops import arena, compare_history

    class Contestant:
        def __init__(self, settings):
            self.settings = settings

        def respond(self, *args, **kwargs):
            return SimpleNamespace(reply="Offline answer", tool_calls=[], iterations=1)

    def missing_key(settings):
        raise SystemExit("No API key for provider 'openai'")

    monkeypatch.setattr(app, "Waku", Contestant)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "contestant"))
    monkeypatch.setattr(arena, "load_settings", lambda: SimpleNamespace(home=tmp_path))
    monkeypatch.setattr(J, "get_client", missing_key)
    events = []
    arena.compare_stream("hello", ["deepseek:deepseek-v4-pro"],
                         lambda kind, event: events.append(kind), judge=True,
                         judge_spec="openai:gpt-5.6-sol")

    runs = compare_history.load_runs(tmp_path)
    assert len(runs) == 1
    result = runs[0]["results"][0]
    assert result["reply"] == "Offline answer"
    assert result["error"] is None
    assert result["quality"] is None
    assert compare_history.aggregate(runs)[0]["ok"] == 1
    assert events[-1] == "done"
