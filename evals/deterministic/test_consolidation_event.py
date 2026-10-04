"""DETERMINISTIC EVAL -- the turn says what it kept (spec 006 A6).

The `consolidation` event used to carry a count. It now also carries the kept
facts -- subject, content, project, and the Waku Memory id when the send
succeeded -- so the waku.one chat panel can list them under "Kept" and link
each one. The count stays, because the dashboard's turn footer reads it.
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
         "project": "Company brain", "memory_id": "mem-a"},
        {"subject": "Descript", "content": "Descript costs $24 a month.",
         "project": "Company brain", "memory_id": "mem-b"},
    ]})]


def test_a_failed_send_is_listed_without_an_id(tmp_path):
    memory = _memory(tmp_path, [response([text_block(RESEARCH)])])

    def remember(body, scope):
        raise RuntimeError("timed out")

    memory.remember = remember
    events = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))
    assert [k["memory_id"] for k in events[-1][1]["kept"]] == [None, None]


def test_the_done_payload_carries_the_kept_facts(monkeypatch):
    """chat_stream's final 'done' is what /v1/chat callers and /api/chat read;
    it has to pass the list through, not just the count."""
    from types import SimpleNamespace

    from waku.ops import dashboard

    kept = [{"subject": "Descript", "content": "Descript costs $24 a month.",
             "project": "Company brain", "memory_id": "mem-b"}]

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
