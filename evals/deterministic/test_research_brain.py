"""DETERMINISTIC EVAL -- research reads the company brain first (spec 009 A),
and a report turn keeps findings in the report (spec 009 B).

The contract under test, from spec 009:
  A. a research turn (the research-report skill matched) with Waku Memory
     connected runs memory.search for the subject and for earlier reports
     BEFORE the model's first call, so before any treg call; the hits reach
     the model as "What the company brain already knows" with their dates,
     and are the turn's Used list: tool events waku.one reads entries from,
     plus `used` on the done payload and in the turn's meta. A failed search
     never fails the turn; a turn that is not research searches nothing.
  B. a turn that saved a report keeps at most two facts, none about a
     company the report covers, and they are the person's (scope global).

A fake bridge stands in for the MCP session, a scripted client for the model.
"""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory, brain
from waku.tools import waku_memory

GATE = response([text_block('{"retrieve": false, "query": "", "reason": "research"}')])
COMPANY = response([text_block('{"company_research": true}')])
# How the injected block opens; the skill's own text names the heading too.
BLOCK = " (from Waku Memory, searched before this turn"

EARLIER_REPORT = {"id": "rep-0915", "kind": "semantic", "scope": "project:Company brain",
                  "created_at": "2026-09-15T10:00:00+00:00",
                  "body": "<!-- waku-report v1 -->\n# Mem0 competitors, 2026-09-15\n\n## Summary\n"
                          "- Zep and Letta sell hosted agent memory."}
FACT = {"id": "fact-0920", "kind": "reference", "scope": "project:Company brain",
        "created_at": "2026-09-20T08:00:00+00:00",
        "body": "Mem0 raised a $24M Series A in 2025-10."}


class RecordingClient(ScriptedClient):
    """The scripted model, which also keeps each call's system prompt."""

    def __init__(self, script):
        super().__init__(script)
        self.systems: list[str] = []

    def _create(self, **kwargs):
        self.systems.append(kwargs.get("system", ""))
        return super()._create(**kwargs)


class FakeBridge:
    def __init__(self, config_path, search=None):
        self.config_path = config_path
        self.search = search or (lambda args: json.dumps(
            {"entries": [FACT] if "kind" not in args else [EARLIER_REPORT],
             "scope_effective": "all", "degraded": []}))
        self.calls: list[tuple[str, dict]] = []

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        self.calls.append((tool, args))
        if tool == "memory.search":
            return self.search(args)
        return json.dumps({"memory": {"id": "rep-new", "body": "x"}})

    def close(self):
        pass


def _bridge(tmp_path, search=None):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    return FakeBridge(path, search)


def _app(tmp_path, monkeypatch, client, bridge):
    import waku.app

    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    return make_waku(tmp_path / "home", client=client, consolidate_every=50)


def _turn(app, message):
    events = []
    result = app.respond(message, observer=lambda kind, ev: events.append((kind, ev)))
    return result, events


# --- A: read before researching ---------------------------------------------------

def test_a_research_turn_searches_waku_memory_before_any_treg_call(tmp_path, monkeypatch):
    client = RecordingClient([
        GATE,
        response([tool_block("treg_catalog_search", {"query": "agent memory"})], "tool_use"),
        response([text_block("Two vendors.")])])
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, client, bridge)
    result, events = _turn(app, "research the competitors of mem0")

    tools = [ev["tool"] for kind, ev in events if kind == "tool"]
    assert tools == [brain.TOOL, brain.TOOL, "treg_catalog_search"], "searched first"
    # the loop's first call; the retrieval gate's own call (spec 011 A2) names a kind
    first_llm = next(i for i, (kind, ev) in enumerate(events) if kind == "llm" and "kind" not in ev)
    last_search = max(i for i, (kind, ev) in enumerate(events)
                      if kind == "tool" and ev["tool"] == brain.TOOL)
    assert last_search < first_llm, "both searches ran before the model's first call"

    searches = [args for tool, args in bridge.calls if tool == "memory.search"]
    assert searches == [
        {"query": "competitors mem0", "scope": "all", "limit": brain.SUBJECT_LIMIT},
        {"query": "competitors mem0", "kind": "semantic", "scope": "all",
         "limit": brain.REPORT_LIMIT}]

    # the loop's first call (after the retrieval gate's) carries what was found, with dates
    system = client.systems[1]
    assert brain.HEADING + BLOCK in system
    assert 'Report "Mem0 competitors, 2026-09-15", saved 2026-09-15 (memory rep-0915' in system
    assert "2026-09-20: Mem0 raised a $24M Series A in 2025-10. (memory fact-0920)" in system
    assert "older than 30 days" in system
    assert system.index("rep-0915") < system.index("fact-0920"), "the report comes first"

    assert [u["id"] for u in result.used] == ["fact-0920", "rep-0915"]
    assert result.used[1]["report"] and result.used[1]["created_at"] == "2026-09-15"


def test_the_tool_events_carry_the_entries_waku_one_reads_as_used(tmp_path, monkeypatch):
    """waku.one's panel lists as Used the `entries` of every
    waku_memory_memory_search tool event, ids included."""
    client = RecordingClient([GATE, response([text_block("Two vendors.")])])
    app = _app(tmp_path, monkeypatch, client, _bridge(tmp_path))
    _, events = _turn(app, "research the competitors of mem0")
    found = [e["id"] for kind, ev in events if kind == "tool" and ev["tool"] == brain.TOOL
             for e in json.loads(ev["output"])["entries"]]
    assert found == ["fact-0920", "rep-0915"]


def test_the_turn_meta_keeps_the_used_list_and_the_searches(tmp_path, monkeypatch):
    client = RecordingClient([GATE, response([text_block("Two vendors.")])])
    app = _app(tmp_path, monkeypatch, client, _bridge(tmp_path))
    _turn(app, "research the competitors of mem0")
    row = app.conn.execute(
        "SELECT content, meta FROM chat_log WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    meta = json.loads(row["meta"])
    assert [u["id"] for u in meta["used"]] == ["fact-0920", "rep-0915"]
    assert [t["tool"] for t in meta["tools"]] == [brain.TOOL, brain.TOOL]
    # the searches are not folded into the chat log, which consolidation reads:
    # memories already kept must not come back as new facts
    assert "fact-0920" not in row["content"] and "Series A" not in row["content"]


def test_the_done_payload_carries_used_and_the_searches_first(monkeypatch):
    from types import SimpleNamespace

    from waku.ops import dashboard

    search = {"tool": brain.TOOL, "args": {"query": "mem0"}, "read_first": True,
              "output": json.dumps({"entries": [FACT]})}
    used = [{"id": "fact-0920", "text": FACT["body"], "created_at": "2026-09-20",
             "kind": "reference", "report": False, "title": ""}]

    class FakeAgent:
        settings = SimpleNamespace(model="m", small_model="s", provider="p")

        def respond(self, message, observer, source, stream):
            return SimpleNamespace(reply="ok", iterations=1, read_first=[search], used=used,
                                   tool_calls=[{"tool": "treg_call", "args": {}, "output": "{}"}])

    monkeypatch.setattr(dashboard, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(dashboard, "maybe_rotate_session", lambda agent: None)
    done = dashboard.chat("research mem0")
    assert [t["tool"] for t in done["tools"]] == [brain.TOOL, "treg_call"]
    assert done["tools"][0]["read_first"] is True and "read_first" not in done["tools"][1]
    assert done["used"] == used


def test_a_turn_that_is_not_research_searches_nothing(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    app = _app(tmp_path, monkeypatch, RecordingClient([GATE, response([text_block("4pm.")])]),
               bridge)
    result, _ = _turn(app, "what time is it")
    assert bridge.calls == [] and result.used == []
    assert BLOCK not in app.client.systems[1]


def test_a_failed_search_never_fails_the_turn(tmp_path, monkeypatch):
    def down(args):
        return "Error: Waku Memory timed out"   # the bridge reports failures as text

    client = RecordingClient([GATE, response([text_block("Two vendors.")])])
    app = _app(tmp_path, monkeypatch, client, _bridge(tmp_path, down))
    result, events = _turn(app, "research the competitors of mem0")
    assert result.reply == "Two vendors."
    assert result.used == [] and [ev for kind, ev in events if kind == "tool"] == []
    assert BLOCK not in client.systems[1]


def test_without_waku_memory_nothing_is_searched(tmp_path, monkeypatch):
    client = RecordingClient([GATE, response([text_block("Two vendors.")])])
    app = _app(tmp_path, monkeypatch, client, bridge=None)
    result, _ = _turn(app, "research the competitors of mem0")
    assert app.brain_search is None and result.used == []


def test_the_subject_drops_only_the_words_that_ask_for_research():
    assert brain.subject("Research the competitors of mem0") == "competitors mem0"
    assert brain.subject("can you compare Zep vs Letta pricing?") == "zep letta pricing"
    assert brain.subject("research") == "research", "never an empty query"


def test_the_skill_says_how_to_use_what_is_known_and_to_spend_little():
    from pathlib import Path

    skill = " ".join((Path(__file__).resolve().parents[2] / "skills" / "research-report"
                      / "SKILL.md").read_text(encoding="utf-8").split())
    # A: start from what is known
    assert brain.HEADING in skill
    assert "earlier report and its date" in skill and "older than 30 days" in skill
    # D: cheap first
    assert "free and preview endpoints" in skill
    assert "`catalog_get`'s price before any paid call" in skill
    assert "keep `limit` small" in skill
    assert "$0.25" in skill


# --- B: no fact flood --------------------------------------------------------------

REPORT_BODY = """<!-- waku-report v1 -->
# Mem0 competitors, 2026-10-03

## Summary
- Zep and Letta sell hosted agent memory; Mem0 has raised the most.

## Sources
```waku-sources
[{"title": "Zep pricing", "url": "https://zep.example/pricing"}]
```
"""

FLOOD = json.dumps({
    "facts": [
        {"subject": "Mem0", "content": "Mem0 raised $24M."},
        {"subject": "Zep", "content": "Zep costs $25 a month."},
        {"subject": "Letta", "content": "Letta is open source."},
        {"subject": "Sean", "content": "Sean is preparing a video on agent memory."},
        {"subject": "Sean", "content": "Sean decided to price below Mem0."},
        {"subject": "Sean's team", "content": "Sean's team ships on Fridays."},
    ],
    "episode": "Researched mem0's competitors.",
    "company_research": True,
})


def _memory(tmp_path, script) -> tuple[Memory, ScriptedClient]:
    settings = Settings(home=tmp_path, consolidate_every=1)
    settings.ensure_home()
    conn = connect(tmp_path)
    conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', 'research mem0')")
    conn.execute("INSERT INTO chat_log (role, content) VALUES ('assistant', 'Report saved.')")
    conn.commit()
    client = RecordingPrompts(script)
    return Memory(conn, settings, client), client


class RecordingPrompts(ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.prompts: list[str] = []

    def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return super()._create(**kwargs)


def test_a_report_turn_keeps_at_most_two_facts_none_about_its_companies(tmp_path):
    memory, client = _memory(tmp_path, [response([text_block(FLOOD)])])
    sent = []
    memory.remember = lambda body, scope: sent.append((body, scope)) or f"mem-{len(sent)}"
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: kind == "consolidation" and events.append(ev), report=REPORT_BODY)

    kept = events[0]["kept"]
    assert len(kept) <= 2
    assert [k["content"] for k in kept] == ["Sean is preparing a video on agent memory.",
                                            "Sean decided to price below Mem0."]
    assert all(k["subject"] not in ("Mem0", "Zep", "Letta") for k in kept)
    # what is left is the person's own, so it is filed as theirs
    assert {scope for _, scope in sent} == {"global"}
    assert all(k["project"] is None for k in kept)
    # and the summariser was told why
    assert "saved a research report" in client.prompts[0] and "Mem0 competitors" in client.prompts[0]


def test_a_batch_whose_earlier_row_saved_a_report_is_a_report_batch(tmp_path):
    """A laptop consolidates every six exchanges, so the report may have been
    saved turns before; the chat log's meta remembers it."""
    memory, _ = _memory(tmp_path, [response([text_block(FLOOD)])])
    memory.conn.execute(
        "UPDATE chat_log SET meta = ? WHERE role = 'assistant'",
        (json.dumps({"report": {"title": "Mem0 competitors, 2026-10-03",
                                "summary": ["Zep and Letta sell hosted agent memory."]}}),))
    memory.conn.commit()
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: kind == "consolidation" and events.append(ev))
    subjects = [k["subject"] for k in events[0]["kept"]]
    assert len(subjects) <= 2 and not {"Zep", "Letta"} & set(subjects)


def test_a_turn_without_a_report_consolidates_as_before(tmp_path):
    memory, client = _memory(tmp_path, [response([text_block(FLOOD)])])
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: kind == "consolidation" and events.append(ev))
    assert len(events[0]["kept"]) == 6
    assert "saved a research report" not in client.prompts[0]


def test_the_turn_that_saves_a_report_consolidates_with_it(tmp_path, monkeypatch):
    """End to end through app.respond: the saved report reaches consolidation."""
    reply = f"Two vendors compete with Mem0.\n\n{REPORT_BODY}"
    client = RecordingClient([GATE, response([text_block(reply)]), COMPANY,
                              response([text_block(FLOOD)])])
    import waku.app

    real_build = waku.app.build_registry
    bridge = _bridge(tmp_path, lambda args: json.dumps({"entries": []}))

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    app = make_waku(tmp_path / "home", client=client, consolidate_every=1)
    _, events = _turn(app, "research the competitors of mem0")
    kept = next(ev for kind, ev in events if kind == "consolidation")["kept"]
    assert len(kept) <= 2 and not {"Mem0", "Zep", "Letta"} & {k["subject"] for k in kept}
