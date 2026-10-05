"""DETERMINISTIC EVAL -- the chat's "Used from memory" list reads as prose.

A Waku Memory search answers with a snippet: a window into a memory's body
around the match. For a report that window often lands in its Sources or
metrics block, and the card showed it raw (rehearsal, 2026-10-05):

    Report: cognee.ai\\"}, "url": "https://docs.data.aviato.co/...", "via": "treg:aviato...
    Report: value": 20}, {"label": "<<Mem0>> Starter"...

brain.read_first now gives a report its `# ` title and its first Summary line
(from the whole report when memory.get reads it), and gives any other memory
its snippet without fenced code or JSON. Waku Memory's <<match>> marks stay
where they are in prose.
"""

from __future__ import annotations

import json

from waku.memory import brain

SOURCES_SNIPPET = ('cognee.ai\\"}, "url": "https://docs.data.aviato.co/api/companies", '
                   '"via": "treg:aviato.companies.funding_rounds", "cost_usd": 0.01}]')
METRICS_SNIPPET = 'value": 20}, {"label": "<<Mem0>> Starter", "value": "$19 a month", "note": "2026-09"}'
REPORT_BODY = ("<!-- waku-report v1 -->\n# Funding of Mem0's competitors, 2026-10-05\n\n## Summary\n"
               "- Letta has the largest disclosed round: a $10M seed in 2024-09.\n"
               "- Zep raised $2.3M.\n\n## Sources\n```json\n[{\"title\": \"cognee.ai\"}]\n```\n")


def _search(entries):
    return lambda args: json.dumps({"entries": entries if "kind" not in args else []})


def _get(bodies):
    return lambda memory_id: json.dumps({"memory": {"id": memory_id, "body": bodies[memory_id]}})


def test_a_report_shows_its_title_and_first_summary_line_not_its_sources():
    entries = [{"id": "rep-1", "kind": "semantic", "created_at": "2026-10-05T03:23:00Z",
                "snippet": SOURCES_SNIPPET},
               {"id": "rep-2", "kind": "semantic", "created_at": "2026-10-04T03:23:00Z",
                "snippet": METRICS_SNIPPET}]
    found = brain.read_first("research mem0's competitors", _search(entries),
                             _get({"rep-1": REPORT_BODY, "rep-2": REPORT_BODY}))
    for u in found.used:
        assert u["title"] == "Funding of Mem0's competitors, 2026-10-05"
        assert u["text"] == "Letta has the largest disclosed round: a $10M seed in 2024-09."


def test_a_report_that_could_not_be_read_shows_no_json():
    entries = [{"id": "rep-1", "kind": "semantic", "snippet": SOURCES_SNIPPET},
               {"id": "rep-2", "kind": "semantic", "snippet": "<!-- waku-report v1 -->\n# Mem0 pricing\n"
                "## Summary\n- Mem0's <<Starter>> plan is $19 a month."}]
    found = brain.read_first("research mem0 pricing", _search(entries), None)
    first, second = found.used
    assert first["text"] == "" and first["title"] == ""        # the card reads "Report"
    assert second["title"] == "Mem0 pricing"
    assert second["text"] == "Mem0's <<Starter>> plan is $19 a month."


def test_a_fact_loses_fenced_code_and_json_but_keeps_its_match_marks():
    entries = [
        {"id": "f-1", "kind": "reference",
         "snippet": 'Sean compares <<Mem0>> with Zep. ```json\n{"plan": "pro", "seats": 5}\n``` He prefers Zep.'},
        {"id": "f-2", "kind": "reference", "snippet": METRICS_SNIPPET},
        {"id": "f-3", "kind": "reference", "snippet": 'Mem0 raised a $24M Series A in 2025-10, per "TechCrunch": a lead.'}]
    used = brain.read_first("research mem0", _search(entries), None).used
    assert used[0]["text"] == "Sean compares <<Mem0>> with Zep. He prefers Zep."
    assert "{" not in used[1]["text"] and '":' not in used[1]["text"]
    assert used[2]["text"] == 'Mem0 raised a $24M Series A in 2025-10, per "TechCrunch": a lead.'


def test_the_prompt_reads_the_same_clean_text():
    entries = [{"id": "f-2", "kind": "reference", "created_at": "2026-10-01", "snippet": METRICS_SNIPPET}]
    found = brain.read_first("research mem0", _search(entries), None)
    assert '"label"' not in found.context and "value\":" not in found.context
