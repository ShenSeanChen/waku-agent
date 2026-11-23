"""DETERMINISTIC EVAL -- a turn that answers from memory does not keep that
memory again.

The bug (waku.one, 2026-10-05): a research turn saved one report. In a new
chat, "What did we find on Mem0's competitors' funding?" read that report
before the loop (memory.search twice, memory.get three times) and answered
from it with no tools. Consolidation then read the answer, extracted five
findings from it, flagged them company research, and sent all five to Waku
Memory: five copies of what the turn had just read, ten credits each.

The rule under test (waku/memory/consolidation.py, `restates`): the memory a
turn read is passed to consolidation as `recalled`, and a proposed fact whose
every name, number and date is already in it is dropped. Something new the
user says in the same turn is still kept, and a research turn keeps what it
kept before.

A fake bridge stands in for the MCP session, a scripted client for the model.
"""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from waku.memory import consolidation
from waku.tools import waku_memory

GATE = response([text_block('{"retrieve": false, "query": "", "reason": "research"}')])
COMPANY = response([text_block('{"company_research": true}')])

REPORT = {
    "id": "rep-1005", "kind": "semantic", "scope": "project:Company brain",
    "created_at": "2026-10-05T04:10:00+00:00",
    "body": ("<!-- waku-report v1 -->\n"
             "# Mem0 competitors: latest funding rounds in Aviato data, 2026-10-05\n\n"
             "## Summary\n"
             "- Letta raised a $10M seed (2024-08-01) at a $60M valuation, the largest "
             "disclosed round among Mem0's competitors.\n"
             "- Zep raised a $2.3M pre-seed on 2023-12-27.\n"
             "- Cognee raised a $1.58M pre-seed on 2024-11-20.\n"
             "- Mem0 says it raised $24M across a Seed and Series A, announced 2025-10-28.\n"
             "- Aviato's newest data for these companies is from 2024-11-20.\n"),
}

# What the summariser proposed on 2026-10-05: the answer, restated.
RESTATED = [
    {"subject": "Letta", "content": "Letta raised a $10M seed round announced 2024-08-01 at "
                                    "a $60M valuation, the largest disclosed round among "
                                    "Mem0's competitors."},
    {"subject": "Zep", "content": "Zep raised a $2.3M pre-seed round on 2023-12-27."},
    {"subject": "Cognee", "content": "Cognee raised a $1.58M pre-seed round on 2024-11-20."},
    {"subject": "Mem0", "content": "Mem0 claims it raised $24M across a Seed and Series A "
                                   "announced 2025-10-28."},
    {"subject": "Aviato", "content": "Aviato's most recent data for Mem0's competitors is "
                                     "dated 2024-11-20."},
]
NEW = {"subject": "Zep", "content": "Zep raised another round in 2026."}


def _distilled(facts) -> str:
    return json.dumps({"facts": facts, "episode": "Looked up Mem0's competitors' funding.",
                       "company_research": True})


class Prompts(ScriptedClient):
    """The scripted model, which also keeps each call's first message."""

    def __init__(self, script):
        super().__init__(script)
        self.prompts: list[str] = []

    def _create(self, **kwargs):
        self.prompts.append(str(kwargs["messages"][0]["content"]))
        return super()._create(**kwargs)


class Bridge:
    """Waku Memory with one saved report: search finds it, get returns it,
    remember records what would have been sent."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.remembered: list[dict] = []

    def connected(self, server):
        return server == "waku_memory"

    def call(self, server, tool, args):
        if tool == "memory.search":
            return json.dumps({"entries": [REPORT], "scope_effective": "all"})
        if tool == "memory.get":
            return json.dumps({"memory": REPORT})
        if tool == "memory.remember":
            self.remembered.append(args)
            return json.dumps({"id": f"mem-{len(self.remembered)}"})
        return "{}"

    def close(self):
        pass


def _app(tmp_path, monkeypatch, client):
    import waku.app

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": waku_memory.URL, "auth_env": "WAKU_MEMORY_API_KEY"}]}),
        encoding="utf-8")
    bridge = Bridge(path)
    real_build = waku.app.build_registry

    def build_with_bridge(*args, **kwargs):
        registry = real_build(*args, **kwargs)
        registry.mcp_bridge = bridge
        return registry

    monkeypatch.setattr(waku.app, "build_registry", build_with_bridge)
    # hosted containers consolidate every turn
    return make_waku(tmp_path / "home", client=client, consolidate_every=1), bridge


def _kept(app, message) -> list[dict]:
    events = []
    app.respond(message, observer=lambda kind, ev: events.append((kind, ev)))
    found = [ev for kind, ev in events if kind == "consolidation"]
    return found[0]["kept"] if found else []


# --- (1) a recall-only turn keeps nothing it read -----------------------------------

def test_a_recall_turn_does_not_keep_what_it_read(tmp_path, monkeypatch):
    """The 2026-10-05 turn, end to end: read first, answer with no tools,
    and the five restated findings are neither stored nor sent."""
    client = Prompts([GATE, response([text_block("Letta raised the most: $10M.")]),
                      response([text_block(_distilled(RESTATED))])])
    app, bridge = _app(tmp_path, monkeypatch, client)
    kept = _kept(app, "What did we find on Mem0's competitors' funding?")
    assert kept == []
    assert bridge.remembered == [], "nothing was sent to Waku Memory"
    assert app.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    # the summariser was shown what the turn read, too
    assert "answered from memory that is already kept" in client.prompts[-1]
    assert "Zep raised a $2.3M pre-seed" in client.prompts[-1]


# --- (2) something new the user says is kept ----------------------------------------

def test_a_new_fact_the_user_adds_in_a_recall_turn_is_kept(tmp_path, monkeypatch):
    client = Prompts([GATE, response([text_block("Noted. Letta raised the most.")]),
                      response([text_block(_distilled([*RESTATED, NEW]))])])
    app, bridge = _app(tmp_path, monkeypatch, client)
    kept = _kept(app, "Note that Zep raised again in 2026. "
                      "What did we find on Mem0's competitors' funding?")
    assert [k["content"] for k in kept] == [NEW["content"]]
    assert [m["body"] for m in bridge.remembered] == [NEW["content"]]


# --- (3) a research turn keeps what it kept before -----------------------------------

def test_a_research_turn_still_keeps_its_report_and_its_activity_fact(tmp_path, monkeypatch):
    """The turn that does the research: it read the brain first and found
    an older report, then searched and saved a new report. The report is
    sent as before, and so is the fact about what the person did."""
    body = REPORT["body"].replace("2026-10-05", "2026-10-06").replace("$10M", "$12M")
    activity = {"subject": "Sean",
                "content": "Sean researched Mem0's competitors' funding on 2026-10-06."}
    client = Prompts([
        GATE,
        response([tool_block("treg_catalog_search", {"query": "funding"})], "tool_use"),
        response([text_block(f"Letta now leads.\n\n{body}")]),
        COMPANY,
        response([text_block(_distilled([*RESTATED, activity]))])])
    app, bridge = _app(tmp_path, monkeypatch, client)
    kept = _kept(app, "research the latest funding of mem0's competitors")
    assert [k["content"] for k in kept] == [activity["content"]]
    bodies = [m["body"] for m in bridge.remembered]
    assert any(b.lstrip().startswith("<!-- waku-report v1 -->") and "$12M" in b
               for b in bodies), "the new report was saved"
    assert activity["content"] in bodies


def test_a_turn_that_read_nothing_keeps_every_fact_as_before():
    """No recalled memory, no change: the filter has nothing to compare."""
    assert not any(consolidation.restates(f, "") for f in [*RESTATED, NEW])


# --- the rule itself -------------------------------------------------------------------

def test_a_fact_restates_memory_only_when_every_name_and_number_is_in_it():
    read = REPORT["body"]
    assert all(consolidation.restates(f, read) for f in RESTATED)
    # a new number, a new date, a new name: kept
    assert not consolidation.restates(NEW, read)
    assert not consolidation.restates(
        {"content": "Zep raised a $2.3M pre-seed round on 2024-01-05."}, read)
    assert not consolidation.restates(
        {"content": "Jane Doe led Zep's $2.3M pre-seed round."}, read)


def test_a_fact_without_a_number_is_never_dropped_by_the_rule():
    """Names alone are too common to judge by: "Sean decided to price below
    Mem0" names two things memory may already hold and is still news."""
    read = REPORT["body"] + "\nSean is preparing a video on agent memory."
    assert not consolidation.restates(
        {"content": "Sean decided to price below Mem0."}, read)


def test_memory_read_as_json_is_compared_as_text():
    """A model's own memory.get answer is JSON: escaped line breaks and
    apostrophes must not hide the words in it."""
    read = json.dumps({"memory": REPORT})
    assert "\\n" in read
    assert all(consolidation.restates(f, read) for f in RESTATED)


def test_a_date_is_one_word_so_a_year_never_matches_it():
    assert "2026" not in consolidation._words("saved 2026-10-05")
    assert consolidation._words("Mem0’s $10M, (2024-08-01)") == {"mem0", "10m", "2024-08-01"}
