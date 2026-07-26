"""DETERMINISTIC EVAL — JSONL traces have one portable UTF-8 encoding."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from waku.config import Settings
from waku.ops.dashboard import collect, events_since
from waku.ops.tracing import TraceEncodingError, Tracer

MESSAGE = "处理中文日程 " + chr(0x1F680)


def _today_trace(home: Path) -> Path:
    return home / "traces" / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def test_tracer_writes_utf8_when_platform_default_cannot(tmp_path, monkeypatch):
    """Simulate Windows GBK: an implicit append must fail on every CI OS."""
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    original_open = Path.open

    def reject_implicit_jsonl(path, mode="r", *args, encoding=None, **kwargs):
        if path.suffix == ".jsonl" and "a" in mode and encoding is None:
            raise UnicodeEncodeError("gbk", MESSAGE, len(MESSAGE) - 1, len(MESSAGE), "illegal")
        return original_open(path, mode, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "open", reject_implicit_jsonl)

    with Tracer(settings).turn(MESSAGE):
        pass

    raw = _today_trace(tmp_path).read_bytes()
    record = json.loads(raw.decode("utf-8"))
    assert record["user_message"] == MESSAGE


def test_dashboard_reads_utf8_without_platform_default(tmp_path, monkeypatch):
    """The live Dashboard endpoint must explicitly decode a UTF-8 trace."""
    home = tmp_path / "home"
    trace = _today_trace(home)
    trace.parent.mkdir(parents=True)
    trace.write_text(
        json.dumps({"type": "turn_start", "user_message": MESSAGE}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WAKU_HOME", str(home))
    original_open = Path.open

    def reject_implicit_jsonl(path, mode="r", *args, encoding=None, **kwargs):
        if path.suffix == ".jsonl" and "r" in mode and encoding is None:
            raise UnicodeDecodeError("gbk", b"\x94", 0, 1, "illegal multibyte sequence")
        return original_open(path, mode, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "open", reject_implicit_jsonl)

    result = events_since(0)

    assert result["events"][0]["user_message"] == MESSAGE
    assert "error" not in result


def test_dashboard_reports_legacy_non_utf8_trace_without_modifying_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    trace = _today_trace(home)
    trace.parent.mkdir(parents=True)
    original = (
        json.dumps({"type": "turn_start", "user_message": "中文"}, ensure_ascii=False) + "\n"
    ).encode("gbk")
    trace.write_bytes(original)
    monkeypatch.setenv("WAKU_HOME", str(home))

    data = collect()
    live = events_since(0)

    assert data["trace_errors"] == [
        {"file": trace.name, "error": live["error"]}
    ]
    assert "not valid UTF-8" in live["error"]
    assert "was not modified" in live["error"]
    assert live["events"] == []
    assert trace.read_bytes() == original


def test_tracer_refuses_to_append_to_legacy_non_utf8_trace(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    trace = _today_trace(tmp_path)
    original = (
        json.dumps({"type": "turn_start", "user_message": "中文"}, ensure_ascii=False) + "\n"
    ).encode("gbk")
    trace.write_bytes(original)

    with pytest.raises(TraceEncodingError, match="not valid UTF-8"), \
            Tracer(settings).turn("next turn"):
        pass

    assert trace.read_bytes() == original


def test_dashboard_ops_view_surfaces_trace_encoding_errors():
    views = (
        Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "js" / "views.js"
    ).read_text(encoding="utf-8")

    assert "trace_errors" in views
    assert "trace encoding error" in views
