"""DETERMINISTIC EVAL — analyze_quarterly_report, offline.

No real PDF parsing: _extract_text is monkeypatched to a canned string keyed
by filename, matching stocks.py's eval contract of "no network/IO in
evals/deterministic/". What's pinned: symbol -> folder resolution, picking the
right two most-recent quarters by a real (year, quarter) comparator (not a
filename string sort — Q10 must not sort before Q2), the single-quarter
disclaimer path, the honest "not found" messages, and that the tool is always
registered (no API key gate, unlike the Finnhub-backed tools).
"""

from __future__ import annotations

from waku.config import Settings
from waku.db import connect
from waku.tools import build_registry
from waku.tools import financial_reports as fr


def _make_report(folder, filename: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(b"%PDF-fake")


def _stub_extract(monkeypatch):
    monkeypatch.setattr(fr, "_extract_text", lambda path: f"TEXT[{path.name}]")


def test_returns_two_most_recent_quarters_newest_first(tmp_path, monkeypatch):
    root = tmp_path / "financial-reports"
    _make_report(root / "AMZN", "AMZN-Q1-2026.pdf")
    _make_report(root / "AMZN", "AMZN-Q2-2026.pdf")
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)
    _stub_extract(monkeypatch)

    tool = fr.make_tool()
    out = tool.fn(symbol="amzn")

    q2_pos = out.index("=== Q2 2026")
    q1_pos = out.index("=== Q1 2026")
    assert q2_pos < q1_pos  # newest first
    assert "TEXT[AMZN-Q2-2026.pdf]" in out
    assert "TEXT[AMZN-Q1-2026.pdf]" in out


def test_takes_latest_two_when_three_or_more_exist(tmp_path, monkeypatch):
    root = tmp_path / "financial-reports"
    for q in ("Q1-2026", "Q2-2026", "Q3-2026"):
        _make_report(root / "AMZN", f"AMZN-{q}.pdf")
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)
    _stub_extract(monkeypatch)

    out = fr.make_tool().fn(symbol="AMZN")

    assert "Q3 2026" in out
    assert "Q2 2026" in out
    assert "Q1 2026" not in out  # oldest silently dropped


def test_quarter_comparator_is_numeric_not_string(tmp_path, monkeypatch):
    """A string sort would put 'Q10' before 'Q2' (lexicographic '1' < '2').
    The real (year, quarter) comparator must treat Q10 as newer than Q2."""
    root = tmp_path / "financial-reports"
    _make_report(root / "AMZN", "AMZN-Q2-2026.pdf")
    _make_report(root / "AMZN", "AMZN-Q10-2026.pdf")
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)
    _stub_extract(monkeypatch)

    out = fr.make_tool().fn(symbol="AMZN")

    assert out.index("Q10 2026") < out.index("Q2 2026")


def test_single_quarter_gives_disclaimer_not_comparison(tmp_path, monkeypatch):
    root = tmp_path / "financial-reports"
    _make_report(root / "AMZN", "AMZN-Q1-2026.pdf")
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)
    _stub_extract(monkeypatch)

    out = fr.make_tool().fn(symbol="AMZN")

    assert out.startswith("Only one quarterly report is available for 'AMZN'")
    assert "next quarter hasn't been uploaded yet" in out
    assert "TEXT[AMZN-Q1-2026.pdf]" in out


def test_unknown_symbol_lists_whats_available(tmp_path, monkeypatch):
    root = tmp_path / "financial-reports"
    _make_report(root / "AMZN", "AMZN-Q1-2026.pdf")
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)

    out = fr.make_tool().fn(symbol="MSFT")

    assert "No quarterly reports found for 'MSFT'" in out
    assert "Available: AMZN" in out


def test_no_reports_loaded_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "_REPORTS_ROOT", tmp_path / "does-not-exist")

    out = fr.make_tool().fn(symbol="AMZN")

    assert "Available: none loaded yet" in out


def test_symbol_folder_exists_but_has_no_pdfs(tmp_path, monkeypatch):
    root = tmp_path / "financial-reports"
    (root / "AMZN").mkdir(parents=True)
    monkeypatch.setattr(fr, "_REPORTS_ROOT", root)

    out = fr.make_tool().fn(symbol="AMZN")

    assert "has a reports folder but no quarterly report PDFs" in out


def test_empty_symbol_is_an_honest_message_not_a_crash():
    out = fr.make_tool().fn(symbol="")
    assert "needs a stock ticker symbol" in out


def test_always_registered_no_api_key_needed(tmp_path):
    settings = Settings(home=tmp_path / "nokey", finnhub_api_key="")
    settings.ensure_home()
    conn = connect(settings.home)
    assert "analyze_quarterly_report" in build_registry(conn, settings, None)._tools
