"""DETERMINISTIC EVAL -- the research-report skill ships, and its example
obeys the grammar it teaches (spec 007 A1).

The skill is a bundled one (skills/research-report/), so a fresh home finds it
the same way it finds weekly-brief: through bundled_skill_dirs(), never by a
copy into WAKU_HOME. The hosted half, a provisioned tenant, is in
evals/deterministic/hosted/test_tenant_skills.py.

The example is what the model copies, and waku.one renders what the model
writes. So the example is checked block by block against the waku-report v1
shapes in spec 007 section A: an example that drifts from the contract
teaches every report to drift with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from evals.helpers import ScriptedClient
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory

SKILL = Path(__file__).resolve().parents[2] / "skills" / "research-report" / "SKILL.md"
MARKER = "<!-- waku-report v1 -->"
SECTIONS = ["Summary", "Findings", "Comparison", "Numbers", "Timeline", "Gaps", "Sources"]
DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _example() -> str:
    text = SKILL.read_text(encoding="utf-8")
    return text[text.index("````markdown\n") + len("````markdown\n"):text.rindex("````")]


def _blocks(report: str) -> dict[str, list]:
    found: dict[str, list] = {}
    for lang, body in re.findall(r"^```(waku-[a-z]+)\n(.*?)\n```$", report, re.DOTALL | re.MULTILINE):
        found.setdefault(lang, []).append(json.loads(body))
    return found


def test_a_fresh_home_has_the_skill(tmp_path):
    settings = Settings(home=tmp_path / "home")
    settings.ensure_home()
    memory = Memory(connect(settings.home), settings, ScriptedClient([]))
    assert "research-report" in {s.name for s in memory.skills.skills}
    assert [s.name for s in memory.skills.match("research the competitors of mem0")][:1] == [
        "research-report"]


def test_the_example_is_a_report_in_section_order():
    report = _example()[_example().index(MARKER):]
    lines = report.splitlines()
    assert lines[0] == MARKER
    assert lines[1].startswith("# ")
    headings = [line[3:] for line in lines if line.startswith("## ")]
    assert headings == SECTIONS, "the example uses every section, in the contract's order"
    summary = report.split("## Summary\n")[1].split("\n## ")[0]
    assert 1 <= len([b for b in summary.splitlines() if b.startswith("- ")]) <= 3
    findings = report.split("## Findings\n")[1].split("\n## ")[0]
    assert findings.lstrip().startswith("|"), "Findings is a Markdown table"


def test_every_block_type_appears_with_its_v1_shape():
    blocks = _blocks(_example())
    assert set(blocks) == {"waku-metrics", "waku-chart", "waku-compare",
                           "waku-timeline", "waku-sources"}

    for tiles in blocks["waku-metrics"]:
        assert 2 <= len(tiles) <= 6
        for tile in tiles:
            assert isinstance(tile["label"], str) and isinstance(tile["value"], str)
            assert set(tile) <= {"label", "value", "note"}

    for chart in blocks["waku-chart"]:
        assert chart["type"] in ("bar", "line") and isinstance(chart["title"], str)
        assert set(chart) <= {"type", "title", "unit", "series"}
        values = [point["value"] for point in chart["series"]]
        assert all(isinstance(v, (int, float)) for v in values)
        if chart["type"] == "bar":
            assert values == sorted(values, reverse=True), "a bar chart is sorted"

    for table in blocks["waku-compare"]:
        for row in table["rows"]:
            assert isinstance(row["name"], str)
            assert len(row["cells"]) == len(table["columns"])
            assert all(c is None or isinstance(c, (str, bool)) for c in row["cells"])

    for timeline in blocks["waku-timeline"]:
        assert all(DATE.match(entry["date"]) and entry["event"] for entry in timeline)
        dates = [entry["date"] for entry in timeline]
        assert dates == sorted(dates, reverse=True), "newest first"

    for sources in blocks["waku-sources"]:
        for source in sources:
            assert isinstance(source["title"], str)
            assert set(source) <= {"title", "url", "via", "cost_usd"}
