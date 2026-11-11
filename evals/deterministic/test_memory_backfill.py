"""Facts reach Waku Memory once it is connected (spec 020 B): S2, S4, and the cap.

Offline: `remember` is a stand-in that records what it was sent, and the
summariser is scripted.
"""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, response, text_block
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory
from waku.memory.consolidation import DRAIN_PER_TURN
from waku.tools.waku_memory import BACKFILL_MARKER, queue_backfill

ONE_NEW = json.dumps({"facts": [{"subject": "Acme", "content": "Acme cut its price to $19."}],
                      "episode": "Talked about Acme."})
NOTHING_NEW = json.dumps({"facts": [], "episode": "Small talk."})


def _memory(tmp_path, *script) -> Memory:
    settings = Settings(home=tmp_path, consolidate_every=1)
    settings.ensure_home()
    return Memory(connect(tmp_path), settings,
                  ScriptedClient([response([text_block(s)]) for s in script]))


def _say(memory: Memory, user: str = "hi", assistant: str = "hello") -> None:
    for role, content in (("user", user), ("assistant", assistant)):
        memory.conn.execute("INSERT INTO chat_log (role, content) VALUES (?, ?)", (role, content))
    memory.conn.commit()


def test_s2_facts_saved_before_connecting_are_sent_once(tmp_path):
    """S2: three facts exist before sign-in. After it they go up, the next
    turn's fact goes up, and a second queueing and turn send nothing again."""
    memory = _memory(tmp_path, ONE_NEW, NOTHING_NEW)
    for subject, content in (("alex", "Alex prefers mornings."), ("sam", "Sam is the CFO."),
                             ("muse", "Muse is a video tool.")):
        memory.facts.add(subject, content)
    sent: list[str] = []
    memory.remember = lambda body, scope: sent.append(body) or f"m{len(sent)}"

    assert queue_backfill(tmp_path, memory.facts) == 3
    assert (tmp_path / BACKFILL_MARKER).exists()
    _say(memory)
    memory.maybe_consolidate()
    assert sent == ["Alex prefers mornings.", "Sam is the CFO.", "Muse is a video tool.",
                    "Acme cut its price to $19."]

    sent.clear()
    assert queue_backfill(tmp_path, memory.facts) == 0     # the marker: once per home
    _say(memory)
    memory.maybe_consolidate()
    assert sent == []
    assert memory.facts.unsynced() == []


def test_s4_a_failed_send_does_not_fail_the_turn_and_is_delivered_once_later(tmp_path):
    memory = _memory(tmp_path, ONE_NEW, NOTHING_NEW)
    delivered: list[str] = []

    def down(body, scope):
        raise RuntimeError("Session not found")

    memory.remember = down
    _say(memory)
    events: list = []
    memory.maybe_consolidate(notify=lambda kind, ev: events.append((kind, ev)))   # must not raise
    kept = next(ev for kind, ev in events if kind == "consolidation")["kept"]
    assert [k["sent"] for k in kept] == [False]
    assert [f["content"] for f in memory.facts.unsynced()] == ["Acme cut its price to $19."]

    memory.remember = lambda body, scope: delivered.append(body) or "m1"
    _say(memory)
    memory.maybe_consolidate()
    assert delivered == ["Acme cut its price to $19."]
    assert memory.facts.unsynced() == []

    _say(memory)
    memory.maybe_consolidate()
    assert delivered == ["Acme cut its price to $19."]     # delivered once, not again


def test_a_large_backlog_goes_up_a_few_turns_at_a_time(tmp_path):
    total = DRAIN_PER_TURN + 5
    memory = _memory(tmp_path, NOTHING_NEW, NOTHING_NEW)
    for i in range(total):
        memory.facts.add(f"fact{i}", f"Fact number {i}.")
    queue_backfill(tmp_path, memory.facts)
    sent: list[str] = []
    memory.remember = lambda body, scope: sent.append(body) or "m"

    _say(memory)
    memory.maybe_consolidate()
    assert len(sent) == DRAIN_PER_TURN
    _say(memory)
    memory.maybe_consolidate()
    assert len(sent) == total and len(set(sent)) == total
    assert memory.facts.unsynced() == []


def test_a_server_that_is_down_costs_one_failed_call_not_one_per_fact(tmp_path):
    memory = _memory(tmp_path, NOTHING_NEW)
    for i in range(10):
        memory.facts.add(f"fact{i}", f"Fact number {i}.")
    queue_backfill(tmp_path, memory.facts)
    calls = []

    def down(body, scope):
        calls.append(body)
        raise RuntimeError("down")

    memory.remember = down
    _say(memory)
    memory.maybe_consolidate()
    assert len(calls) == 1
    assert len(memory.facts.unsynced()) == 10
