"""DETERMINISTIC EVAL -- the turn says what it kept (spec 006 A6).

The `consolidation` event used to carry a count. It now also carries the kept
facts -- subject, content, project, and the Waku Memory id when the send
succeeded -- so the waku.one chat panel can list them under "Kept" and link
each one. The count stays, because the dashboard's turn footer reads it.

Each kept fact also says whether Waku Memory took it (`sent`). On 2026-10-04
a research turn on agent.waku.one showed six facts under "Kept in memory"
while Waku Memory had refused every send ("Session not found"), and its
database held none of them. The card has to say which facts stayed on the
agent, and the next consolidation has to send them.
"""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, response, text_block
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory

RESEARCH = json.dumps({
    "facts": [{"subject": "Notion AI", "content": "Notion AI launched agents on 2026-09-30."},
              {"subject": "Descript", "content": "Descript costs $24 a month."}],
    "episode": "Researched the competitors of muse.ai.",
    "company_research": True,
})


def _memory(tmp_path, script) -> Memory:
    settings = Settings(home=tmp_path, consolidate_every=1)
    settings.ensure_home()
    conn = connect(tmp_path)
    conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', 'research muse.ai')")
    conn.execute("INSERT INTO chat_log (role, content) VALUES ('assistant', 'Five competitors.')")
    conn.commit()
    return Memory(conn, settings, ScriptedClient(script))


def test_the_event_lists_each_kept_fact_with_its_waku_memory_id(tmp_path):
    memory = _memory(tmp_path, [response([text_block(RESEARCH)])])
    ids = iter(["mem-a", "mem-b"])
    memory.remember = lambda body, scope: next(ids)
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    # Spec 011 A2: the summariser's model call is reported first, as an `llm`
    # event; test_turn_receipt.py pins that one.
    assert events[0][0] == "llm" and events[0][1]["kind"] == "consolidation"
    events = events[1:]
    assert events == [("consolidation", {"new_facts": 2, "kept": [
        {"subject": "Notion AI", "content": "Notion AI launched agents on 2026-09-30.",
         "project": "Company brain", "memory_id": "mem-a", "sent": True},
        {"subject": "Descript", "content": "Descript costs $24 a month.",
         "project": "Company brain", "memory_id": "mem-b", "sent": True},
    ]})]


def test_a_failed_send_is_listed_without_an_id(tmp_path):
    memory = _memory(tmp_path, [response([text_block(RESEARCH)])])

    def remember(body, scope):
        raise RuntimeError("timed out")

    memory.remember = remember
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    assert [k["memory_id"] for k in events[-1][1]["kept"]] == [None, None]
    # the first send failed and the second waited: neither reached Waku Memory
    assert [k["sent"] for k in events[-1][1]["kept"]] == [False, False]


def test_a_send_that_works_without_an_id_still_counts_as_sent(tmp_path):
    """memory_id is None both when a send failed and when the server took the
    fact and named no id, so only `sent` can tell the card which it was."""
    memory = _memory(tmp_path, [response([text_block(RESEARCH)])])
    memory.remember = lambda body, scope: None
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    assert [(k["memory_id"], k["sent"]) for k in events[-1][1]["kept"]] == [(None, True)] * 2


def test_without_waku_memory_nothing_claims_a_send(tmp_path):
    memory = _memory(tmp_path, [response([text_block(RESEARCH)])])
    memory.remember = None
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    assert [k["sent"] for k in events[-1][1]["kept"]] == [None, None]


def test_facts_waku_memory_refused_are_sent_by_the_next_consolidation(tmp_path):
    """The 2026-10-04 turn, end to end: Waku Memory refuses the first turn's
    facts, the card says so, and the next turn's consolidation sends them
    before its own."""
    memory = _memory(tmp_path, [response([text_block(RESEARCH)]),
                                response([text_block(json.dumps({"facts": []}))])])
    sent: list[tuple[str, str]] = []
    up = False

    def remember(body, scope):
        if not up:
            raise RuntimeError("MCP call waku_memory_memory.remember failed: Session not found")
        sent.append((body, scope))
        return f"mem-{len(sent)}"

    memory.remember = remember
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    assert [k["sent"] for k in events[-1][1]["kept"]] == [False, False]
    assert sent == []

    up = True
    memory.conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', 'thanks')")
    memory.conn.execute("INSERT INTO chat_log (role, content) VALUES ('assistant', 'Welcome.')")
    memory.conn.commit()
    memory.maybe_consolidate(notify=lambda kind, ev: None)
    assert sent == [("Notion AI launched agents on 2026-09-30.", "project:Company brain"),
                    ("Descript costs $24 a month.", "project:Company brain")]
    assert memory.facts.unsynced() == []


def test_the_done_payload_carries_the_kept_facts(monkeypatch):
    """chat_stream's final 'done' is what /v1/chat callers and /api/chat read;
    it has to pass the list through, not just the count."""
    from types import SimpleNamespace

    from waku.ops import dashboard

    kept = [{"subject": "Descript", "content": "Descript costs $24 a month.",
             "project": "Company brain", "memory_id": "mem-b", "sent": True}]

    class FakeAgent:
        settings = SimpleNamespace(model="m", small_model="s", provider="p")

        def respond(self, message, observer, source, stream):
            observer("consolidation", {"new_facts": 1, "kept": kept})
            return SimpleNamespace(reply="ok", tool_calls=[], iterations=1)

    monkeypatch.setattr(dashboard, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(dashboard, "maybe_rotate_session", lambda agent: None)
    done = {}
    dashboard.chat_stream("research muse.ai",
                          lambda kind, ev: done.update(ev) if kind == "done" else None)
    assert done["consolidation"] == {"new_facts": 1, "kept": kept}
