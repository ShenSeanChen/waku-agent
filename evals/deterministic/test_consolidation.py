"""DETERMINISTIC EVAL — the diamond on the whiteboard: consolidate, but only sometimes.

`consolidation.py` is the second half of the Memory pillar and, like the
retrieval gate, it had no test. It answers "when does a chat become a memory?"
— and the honest answer is "after N exchanges, not after every message", which
is exactly the kind of rule that quietly stops working.

Offline throughout: a ScriptedClient stands in for the summarizer, so these
cases pin the THRESHOLD, the BOOKKEEPING and the FAILURE POSTURE. Whether the
model extracts good facts from a given conversation is a judgment call and lives
in evals/judge/.

Three properties matter more than the rest:
  * below the threshold, it must not call the model AT ALL (cost)
  * a summarizer failure must never mark the log consolidated (data loss)
  * consolidated rows must never be re-read (duplicate facts)
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, response, text_block
from waku.db import connect
from waku.memory.consolidation import SUMMARIZER_PROMPT, consolidate_if_due
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore

DISTILLED = json.dumps({
    "facts": [{"subject": "Alex", "content": "Alex prefers morning meetings."},
              {"subject": "acme demo", "content": "The Acme demo is on Friday."}],
    "episode": "Planned the Acme demo with Alex.",
})


@pytest.fixture
def memory(tmp_path):
    """A real sqlite home — consolidation is mostly bookkeeping, and bookkeeping
    is exactly what an in-memory fake would fail to catch."""
    conn = connect(tmp_path)
    return SimpleNamespace(
        conn=conn,
        facts=SqliteFactStore(conn),
        episodes=SqliteEpisodeStore(conn),
        tmp_path=tmp_path,
    )


def add_exchanges(conn, n: int) -> None:
    """n exchanges = 2n rows. The threshold counts EXCHANGES, not messages."""
    for i in range(n):
        conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', ?)", (f"msg {i}",))
        conn.execute("INSERT INTO chat_log (role, content) VALUES ('assistant', ?)", (f"reply {i}",))
    conn.commit()


class CountingClient(ScriptedClient):
    """A scripted client that also records how many times it was asked.

    An explicit counter, NOT "an empty script will raise" — `consolidate_if_due`
    wraps the model call in a broad `except Exception`, so a client that blows
    up is indistinguishable from one that was never called. A mutation test
    caught exactly that: breaking the threshold from `every_n * 2` to `every_n`
    left this file green, because the extra call raised IndexError and was
    quietly swallowed into `return 0`.
    """

    def __init__(self, script):
        super().__init__(script)
        self.calls = 0

    def _create(self, **kw):
        self.calls += 1
        return super()._create(**kw)


def run(memory, script, every_n=3):
    memory.client = CountingClient(script)
    return consolidate_if_due(memory.conn, memory.client, "small-model",
                              every_n, memory.facts, memory.episodes)


def unconsolidated(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated = 0").fetchone()[0]


# ---------- the threshold


@pytest.mark.parametrize("exchanges", [0, 1, 2])
def test_below_the_threshold_the_model_is_never_called(memory, exchanges):
    """The whole point of the diamond: under N exchanges costs ZERO tokens.

    `client.calls == 0` is the real assertion. Returning 0 is not enough — the
    error path also returns 0, so a broken threshold that calls the model and
    fails would look identical from the return value alone."""
    add_exchanges(memory.conn, exchanges)
    assert run(memory, script=[], every_n=3) == 0
    assert memory.client.calls == 0, "the summarizer must not run below the threshold"
    assert unconsolidated(memory.conn) == exchanges * 2, "and nothing may be marked done"


def test_the_threshold_counts_exchanges_not_messages(memory):
    """An exchange is two rows (user + assistant). every_n=3 means SIX rows, and
    the boundary is exact: five rows is not due, six is."""
    add_exchanges(memory.conn, 2)
    memory.conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', 'lone')")
    memory.conn.commit()
    assert memory.conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 5
    assert run(memory, script=[], every_n=3) == 0
    assert memory.client.calls == 0, "five rows is under three exchanges"


def test_exactly_at_the_threshold_it_fires(memory):
    add_exchanges(memory.conn, 3)
    assert run(memory, [response([text_block(DISTILLED)])], every_n=3) == 2
    assert memory.client.calls == 1, "one summarizer call per consolidation, not one per exchange"


# ---------- what it writes


def test_facts_and_the_episode_both_land(memory):
    add_exchanges(memory.conn, 3)
    assert run(memory, [response([text_block(DISTILLED)])]) == 2

    rows = memory.conn.execute("SELECT subject, content, source FROM facts ORDER BY id").fetchall()
    assert [r["subject"] for r in rows] == ["alex", "acme demo"], "subjects are normalised lowercase"
    assert [r["source"] for r in rows] == ["consolidation", "consolidation"], (
        "provenance matters — the dashboard shows which facts the agent inferred "
        "versus which the user stated"
    )
    episodes = memory.conn.execute("SELECT summary FROM episodes").fetchall()
    assert [e["summary"] for e in episodes] == ["Planned the Acme demo with Alex."]


def test_half_written_facts_are_dropped_not_stored(memory):
    """A fact with no subject or no content is noise. Storing it would put a
    blank row in the user's memory tab that they can see and cannot explain."""
    add_exchanges(memory.conn, 3)
    partial = json.dumps({"facts": [{"subject": "Alex"},
                                    {"content": "no subject here"},
                                    {"subject": "Raj", "content": "Raj plays tennis."}],
                          "episode": "Talked about friends."})
    run(memory, [response([text_block(partial)])])
    stored = [r["subject"] for r in memory.conn.execute("SELECT subject FROM facts").fetchall()]
    assert stored == ["raj"]


def test_a_conversation_worth_no_facts_still_marks_the_log_done(memory):
    """Small talk is a legitimate outcome. Returning 0 facts must NOT mean
    'try again next time' — otherwise the same chit-chat gets re-summarised
    forever, paying for a model call each round."""
    add_exchanges(memory.conn, 3)
    assert run(memory, [response([text_block('{"facts": [], "episode": ""}')])]) == 0
    assert unconsolidated(memory.conn) == 0


# ---------- bookkeeping: the expensive mistakes


def test_consolidated_rows_are_never_read_twice(memory):
    """Without this, every run re-summarises the whole history and re-adds every
    fact. The user watches their memory tab fill with duplicates."""
    add_exchanges(memory.conn, 3)
    run(memory, [response([text_block(DISTILLED)])])
    assert unconsolidated(memory.conn) == 0

    # a second run must not even ASK the model — there is nothing unconsolidated
    assert run(memory, script=[]) == 0
    assert memory.client.calls == 0
    assert memory.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_only_the_rows_it_read_are_marked(memory):
    """Messages that arrive DURING a consolidation must survive it. The UPDATE
    targets explicit ids for exactly this reason — a blanket
    'UPDATE chat_log SET consolidated=1' would swallow them unsummarised."""
    add_exchanges(memory.conn, 3)
    seen = memory.conn.execute("SELECT id FROM chat_log").fetchall()

    class LateWriter(ScriptedClient):
        def _create(self, **kw):
            add_exchanges(memory.conn, 1)      # a message lands mid-summary
            return super()._create(**kw)

    consolidate_if_due(memory.conn, LateWriter([response([text_block(DISTILLED)])]),
                       "small-model", 3, memory.facts, memory.episodes)

    still_open = memory.conn.execute(
        "SELECT id FROM chat_log WHERE consolidated = 0").fetchall()
    assert len(still_open) == 2, "the late exchange must still be waiting its turn"
    assert {r["id"] for r in still_open}.isdisjoint({r["id"] for r in seen})


# ---------- failure posture: never lose the log


def test_a_summarizer_error_leaves_the_log_untouched(memory):
    """'never lose the log' — a 429 or a dead key must cost you nothing but the
    attempt. If this marked rows consolidated, a single outage would silently
    delete a conversation from memory forever."""
    add_exchanges(memory.conn, 3)

    class Boom:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_kw):
            raise RuntimeError("rate limited")

    assert consolidate_if_due(memory.conn, Boom(), "small-model", 3,
                              memory.facts, memory.episodes) == 0
    assert unconsolidated(memory.conn) == 6, "every row must still be waiting"
    assert memory.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_unparseable_output_leaves_the_log_untouched(memory):
    add_exchanges(memory.conn, 3)
    assert run(memory, [response([text_block("I'm not sure what to extract here.")])]) == 0
    assert unconsolidated(memory.conn) == 6


def test_a_reasoning_model_that_never_reaches_text_leaves_the_log_untouched(memory):
    """Live bug (2026-08-04): kimi-k2.6 spends its whole budget on a `thinking`
    block before any JSON — on a real 40-row backlog it hit stop_reason=max_tokens
    with ZERO text blocks. `text.index("{")` on the empty joined string used to
    raise, get swallowed by the broad except, and return 0 — safe, but silently,
    forever, since the same truncation repeats every turn. The fix bumped the
    budget (600 -> 4096) AND added this explicit guard so the "no JSON at all"
    case is recognized rather than stumbled into via an exception."""
    add_exchanges(memory.conn, 3)
    thinking_only = response(
        [SimpleNamespace(type="thinking", thinking="reasoning about facts..." * 50)],
        stop_reason="max_tokens",
    )
    assert run(memory, [thinking_only]) == 0
    assert unconsolidated(memory.conn) == 6, "a thinking-only reply must not be mistaken for done"
    assert memory.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


# ---------- the prompt


def test_the_prompt_carries_the_log_and_demands_json_only():
    filled = SUMMARIZER_PROMPT.format(log="user: hi\nassistant: hello")
    assert "user: hi" in filled
    assert "ONLY this JSON" in filled
    assert '"facts"' in filled and '"episode"' in filled
    assert "worth remembering in a month" in filled, (
        "the durability instruction is what keeps chit-chat out of long-term memory"
    )
