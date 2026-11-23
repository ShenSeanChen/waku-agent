"""DETERMINISTIC EVAL -- a reply holding a research report is saved to Waku
Memory whole, and the chat keeps a short reply and a card (spec 007 A2).

The contract under test, from spec 007 section C:
  - a report (the waku-report v1 marker on a line of its own) is remembered
    ONCE, kind `semantic`, scope `project:Company brain` for company or market
    research and `global` otherwise;
  - the turn emits one `report` event {title, memory_id, scope, summary},
    which reaches the chat stream before `done`;
  - the chat reply is the text before the marker plus "Report saved";
  - no report, no Waku Memory, or a failed send: the reply is left whole and
    there is no event, and the turn never fails.

A fake bridge stands in for the MCP session, a scripted client for the model.
"""

from __future__ import annotations

import json
import logging

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.memory import reports
from waku.tools import waku_memory

PREFACE = "Three vendors sell hosted agent memory. Birchline has raised the most, $41M."
REPORT = """<!-- waku-report v1 -->
# Agent memory vendors, 2026-10-03

## Summary
- Three vendors sell hosted memory for AI agents.
- Birchline has raised the most, $41M.

## Sources
```waku-sources
[{"title": "Birchline pricing", "url": "https://birchline.example/pricing"}]
```
"""
REPLY = f"{PREFACE}\n\n{REPORT}"
GATE = response([text_block('{"retrieve": false, "query": "", "reason": "research"}')])
COMPANY = response([text_block('{"company_research": true}')])
PERSONAL = response([text_block('{"company_research": false}')])


class FakeBridge:
    def __init__(self, config_path, reply=None):
        self.config_path = config_path
        self.reply = reply if reply is not None else json.dumps(
            {"memory": {"id": "rep-1", "body": "x"}, "deduped": False})
        self.calls: list[tuple[str, str, dict]] = []

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        self.calls.append((server, tool, args))
        return self.reply

    def close(self):
        pass


def _app(tmp_path, monkeypatch, script, bridge=None):
    """A Waku whose tool registry connected `bridge` (None: no Waku Memory)."""
    import waku.app

    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    # consolidation is spec 006's and not due here: one exchange of fifty
    return make_waku(tmp_path / "home", client=ScriptedClient(script), consolidate_every=50)


def _bridge(tmp_path, reply=None):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    return FakeBridge(path, reply)


def _turn(app, message="research agent memory vendors"):
    events = []
    result = app.respond(message, observer=lambda kind, ev: events.append((kind, ev)))
    return result, [ev for kind, ev in events if kind == "report"]


def _last_assistant_row(app):
    return app.conn.execute(
        "SELECT content, meta FROM chat_log WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_company_research_is_remembered_once_as_semantic_in_the_company_project(
        tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block(REPLY)]), COMPANY], bridge)
    result, cards = _turn(app)

    assert bridge.calls == [("waku_memory", "memory.remember", {
        "body": REPORT, "kind": "semantic", "scope": "project:Company brain"})]
    assert cards == [{"title": "Agent memory vendors, 2026-10-03", "memory_id": "rep-1",
                      "scope": "project:Company brain",
                      "summary": ["Three vendors sell hosted memory for AI agents.",
                                  "Birchline has raised the most, $41M."]}]
    assert result.reply == f"{PREFACE}\n\nReport saved: Agent memory vendors, 2026-10-03."


def test_the_chat_log_keeps_the_short_reply_and_the_card(tmp_path, monkeypatch):
    """A reopened thread (and the history waku.one reads) shows what the live
    chat showed: the short reply, with the card's data in meta."""
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block(REPLY)]), COMPANY],
               _bridge(tmp_path))
    result, cards = _turn(app)
    row = _last_assistant_row(app)
    assert row["content"] == result.reply
    assert json.loads(row["meta"])["report"] == cards[0]


def test_personal_research_is_remembered_in_global(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block(REPLY)]), PERSONAL], bridge)
    _, cards = _turn(app, "research running shoes for my knees")
    assert bridge.calls[0][2]["scope"] == "global"
    assert cards[0]["scope"] == "global"


def test_a_reply_without_a_report_sends_nothing_and_has_no_card(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    # no third response: a scope question asked here would fail the turn
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block("It is 4pm.")])], bridge)
    result, cards = _turn(app, "what time is it")
    assert (bridge.calls, cards, result.reply) == ([], [], "It is 4pm.")
    assert json.loads(_last_assistant_row(app)["meta"])["report"] is None


def test_without_waku_memory_the_report_stays_in_the_reply(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block(REPLY)])], bridge=None)
    result, cards = _turn(app)
    assert app.memory.remember is None
    assert (result.reply, cards) == (REPLY, [])


def test_a_failed_send_keeps_the_turn_and_the_whole_report(tmp_path, monkeypatch, caplog):
    bridge = _bridge(tmp_path, reply="MCP call waku_memory_memory.remember failed: timed out")
    app = _app(tmp_path, monkeypatch, [GATE, response([text_block(REPLY)]), COMPANY], bridge)
    with caplog.at_level(logging.WARNING, logger="waku.memory.reports"):
        result, cards = _turn(app)
    assert len(bridge.calls) == 1
    assert (result.reply, cards) == (REPLY, [])
    assert _last_assistant_row(app)["content"] == REPLY
    assert "did not take the report" in caplog.text


def test_the_scope_question_failing_files_the_report_as_personal():
    """Never into the shared project by mistake: no JSON, or an error, is global."""
    report = reports.find(REPLY)
    assert reports.is_company_research(ScriptedClient([response([text_block("hmm")])]),
                                       "small", report) is False
    assert reports.is_company_research(ScriptedClient([]), "small", report) is False
    assert reports.is_company_research(
        ScriptedClient([response([text_block('{"company_research": "true"}')])]),
        "small", report) is True


@pytest.mark.parametrize("reply", [
    "We use the <!-- waku-report v1 --> marker for reports.",   # not on a line of its own
    "# A heading\n\n- a list\n",
])
def test_only_the_marker_on_its_own_line_makes_a_report(reply):
    assert reports.find(reply) is None


def test_with_no_preface_the_summary_becomes_the_chat_reply():
    report = reports.find(REPORT)
    assert report.preface == ""
    assert reports.chat_reply(report) == (
        "Three vendors sell hosted memory for AI agents. Birchline has raised the most, $41M."
        "\n\nReport saved: Agent memory vendors, 2026-10-03.")


def test_the_report_event_reaches_the_stream_before_done(monkeypatch):
    """/api/chat/stream (and /v1/chat through the gateway) carries the event
    as it happens, and the final `done` repeats it for /api/chat callers."""
    from types import SimpleNamespace

    from waku.ops import dashboard

    card = {"title": "T", "memory_id": "rep-1", "scope": "global", "summary": ["S."]}

    class FakeAgent:
        settings = SimpleNamespace(model="m", small_model="s", provider="p")

        def respond(self, message, observer, source, stream):
            observer("report", card)
            return SimpleNamespace(reply="Found three. Report saved: T.", tool_calls=[],
                                   iterations=1)

    monkeypatch.setattr(dashboard, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(dashboard, "maybe_rotate_session", lambda agent: None)
    emitted = []
    dashboard.chat_stream("research T", lambda kind, ev: emitted.append((kind, ev)))
    kinds = [kind for kind, _ in emitted]
    assert kinds.count("report") == 1 and kinds.index("report") < kinds.index("done")
    assert emitted[-1][1]["report"] == card
