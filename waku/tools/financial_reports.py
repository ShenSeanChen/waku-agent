"""analyze_quarterly_report — read the two most recent quarterly report PDFs
for a stock symbol and hand their text to the model for comparison.

Reports live at docs/financial-reports/{SYMBOL}/{SYMBOL}-Q{n}-{year}.pdf,
checked into git as a demo stand-in for a real document store (S3 in
production). Only AMZN is populated today.

This tool does the deterministic part only — resolve symbol to a folder,
pick the right files, extract their text — and returns that text as the tool
result. The model does the actual analysis in its next turn, same as every
other tool in this codebase (call -> tool -> call).

Text extraction uses pypdf (pure Python, MIT license, no system binary) —
NOT native multimodal PDF input. WAKU_PROVIDER=openrouter (and any other
openai-wire provider) goes through OpenAICompatClient in loop/models.py,
whose _to_openai() has no case for document/image content blocks: it would
silently drop an attached PDF rather than error. Extracting text in Python
sidesteps that entirely — it works the same regardless of provider or which
model OpenRouter happens to route to.

Scope, on purpose: whole-PDF text only, no chunking/embeddings/vector store,
hardcoded to docs/financial-reports/{symbol}/, quarterly reports only, always
the two most recent quarters. Annual/10-K reports and year-over-year
comparisons are a different tool, not an extension of this one.
"""

from __future__ import annotations

import re
from pathlib import Path

from waku.tools.registry import Tool

# waku/tools/financial_reports.py -> parents[2] is the repo root (same pattern
# as experimental.py's _project_pi_flags).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPORTS_ROOT = _REPO_ROOT / "docs" / "financial-reports"

_FILENAME_RE = re.compile(r"-Q(\d+)-(\d{4})\.pdf$", re.IGNORECASE)
_MAX_CHARS_PER_REPORT = 15_000  # keep one report from blowing the whole context


def _available_symbols() -> list[str]:
    if not _REPORTS_ROOT.is_dir():
        return []
    return sorted(p.name for p in _REPORTS_ROOT.iterdir() if p.is_dir())


def _quarter_files(folder: Path) -> list[tuple[int, int, Path]]:
    """(year, quarter, path) for every file matching the naming convention,
    newest first. Parses ints out of the filename instead of sorting the
    string, so Q10 (if it ever exists) doesn't sort before Q2."""
    found = []
    for path in folder.iterdir():
        match = _FILENAME_RE.search(path.name)
        if match:
            quarter, year = int(match.group(1)), int(match.group(2))
            found.append((year, quarter, path))
    return sorted(found, key=lambda t: (t[0], t[1]), reverse=True)


def _extract_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > _MAX_CHARS_PER_REPORT:
        text = text[:_MAX_CHARS_PER_REPORT] + "\n[report truncated for length]"
    return text.strip()


def make_tool() -> Tool:
    def analyze_quarterly_report(symbol: str = "") -> str:
        symbol = symbol.strip().upper()
        if not symbol:
            return "analyze_quarterly_report needs a stock ticker symbol, e.g. 'AMZN'."

        folder = _REPORTS_ROOT / symbol
        if not folder.is_dir():
            available = _available_symbols()
            listed = ", ".join(available) if available else "none loaded yet"
            return f"No quarterly reports found for '{symbol}'. Available: {listed}."

        files = _quarter_files(folder)
        if not files:
            return (f"'{symbol}' has a reports folder but no quarterly report PDFs in it "
                     f"(expected filenames like {symbol}-Q2-2026.pdf).")

        try:
            latest = files[:2]
            sections = []
            for year, quarter, path in latest:
                sections.append(
                    f"=== Q{quarter} {year} ({path.name}) ===\n{_extract_text(path)}"
                )
        except Exception as exc:
            return f"Couldn't read the report PDF for '{symbol}': {exc}."

        if len(latest) == 1:
            year, quarter, _ = latest[0]
            return (
                f"Only one quarterly report is available for '{symbol}' — Q{quarter} {year}. "
                f"The next quarter hasn't been uploaded yet, so this is a single-quarter "
                f"summary, not a comparison:\n\n{sections[0]}"
            )

        return (
            f"Two most recent quarterly reports for '{symbol}' (newest first), "
            f"for comparison:\n\n" + "\n\n".join(sections)
        )

    return Tool(
        name="analyze_quarterly_report",
        description=(
            "Get the text of a company's most recent quarterly financial report(s) for "
            "analysis or comparison. Pass the stock ticker symbol (e.g. AMZN for Amazon) — "
            "resolve a company name to its ticker yourself. Returns the raw text of the two "
            "most recent quarters on file (or one, with a disclaimer, if only one exists) so "
            "you can summarize and compare them for the user. This is a demo data set: only "
            "a small, fixed set of symbols and quarters is loaded, not a live document store. "
            "When you present the result: write for a beginner investor, not an analyst — 2-3 "
            "plain-language sentences on what changed and why it matters, then a short "
            "metric-by-quarter comparison table (e.g. revenue, operating income, notable "
            "growth areas). No jargon without a plain-English gloss, no wall of text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "stock ticker symbol, e.g. 'AMZN'"},
            },
            "required": ["symbol"],
        },
        fn=analyze_quarterly_report,
    )
