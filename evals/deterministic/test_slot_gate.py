"""DETERMINISTIC EVAL -- Jev decides which memories earn a slot (spec 005).

The video's step: retrieval finds six facts and Jev keeps the three that
change the answer. Offline: Jev is a fake that answers by the fact's text. The
gate is opt-in (WAKU_SLOT_GATE=jev + TYPESAFE_API_KEY) and fails open: off, or
any failure, and every fact goes through as before.
"""

from __future__ import annotations

import pytest

from waku.memory import slot_gate

MESSAGE = "should we cut our price like Acme did?"
FACTS = ["acme: Acme cut its price to $19.", "us: Our price is $29.",
         "us: Our margin is 40%.", "acme: Acme's CEO spoke at a conference.",
         "review: Shipping was slow (3 stars).", "review: Box arrived dented."]
SCORES = {0: 3.0, 1: 2.9, 2: 2.2, 3: 0.34, 4: 0.2, 5: 0.1}


def fake_ask(calls):
    def ask(state, questions, **_):
        calls.append((state, questions))
        return {name: {"score": SCORES[int(name.split("_")[1])]} for name in questions}
    return ask


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("WAKU_SLOT_GATE", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")


def test_jev_keeps_the_facts_that_change_the_answer(on, monkeypatch):
    calls = []
    monkeypatch.setattr(slot_gate.jev, "ask", fake_ask(calls))
    kept, verdicts = slot_gate.select(MESSAGE, FACTS)
    assert kept == FACTS[:3]
    assert [v["kept"] for v in verdicts] == [True, True, True, False, False, False]
    assert len(calls) == 1, "one Jev call scores every fact"
    assert calls[0][0] == MESSAGE


def test_the_threshold_is_a_setting(on, monkeypatch):
    monkeypatch.setattr(slot_gate.jev, "ask", fake_ask([]))
    monkeypatch.setenv("WAKU_SLOT_GATE_KEEP", "2.5")
    kept, _ = slot_gate.select(MESSAGE, FACTS)
    assert kept == FACTS[:2]


def test_off_by_default_every_fact_goes_through(monkeypatch):
    monkeypatch.delenv("WAKU_SLOT_GATE", raising=False)
    monkeypatch.setattr(slot_gate.jev, "ask", lambda *a, **k: pytest.fail("Jev called while off"))
    assert slot_gate.select(MESSAGE, FACTS) == (FACTS, [])


def test_on_without_a_key_every_fact_goes_through(monkeypatch):
    monkeypatch.setenv("WAKU_SLOT_GATE", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(slot_gate.jev, "ask", lambda *a, **k: pytest.fail("Jev called with no key"))
    assert slot_gate.select(MESSAGE, FACTS) == (FACTS, [])


def test_a_failing_jev_fails_open(on, monkeypatch):
    def boom(*a, **k):
        raise OSError("api.typesafe.ai unreachable")
    monkeypatch.setattr(slot_gate.jev, "ask", boom)
    assert slot_gate.select(MESSAGE, FACTS) == (FACTS, [])


def test_a_malformed_answer_fails_open(on, monkeypatch):
    monkeypatch.setattr(slot_gate.jev, "ask", lambda *a, **k: {"fact_0": {"nope": 1}})
    assert slot_gate.select(MESSAGE, FACTS) == (FACTS, [])


def test_nothing_to_gate_makes_no_call(on, monkeypatch):
    monkeypatch.setattr(slot_gate.jev, "ask", lambda *a, **k: pytest.fail("called for nothing"))
    assert slot_gate.select(MESSAGE, []) == ([], [])


# --- B1: the keep gate on consolidation ----------------------------------------

def test_the_keep_gate_drops_proposed_facts_no_future_answer_needs(on, monkeypatch):
    monkeypatch.setattr(slot_gate.jev, "ask", lambda state, questions, **_: {
        name: {"score": 2.6 if "price" in q["instructions"] else 0.2}
        for name, q in questions.items()})
    proposed = [{"subject": "acme", "content": "Acme cut its price to $19."},
                {"subject": "chat", "content": "The user said thanks."}]
    kept = slot_gate.keep(proposed)
    assert [f["content"] for f in kept] == ["Acme cut its price to $19."]


def test_the_keep_gate_fails_open(monkeypatch):
    monkeypatch.delenv("WAKU_SLOT_GATE", raising=False)
    proposed = [{"subject": "chat", "content": "The user said thanks."}]
    assert slot_gate.keep(proposed) == proposed


# --- the wiring: retrieval passes through the gate -----------------------------

def test_retrieval_puts_only_the_kept_facts_in_the_prompt(tmp_path, monkeypatch):
    from waku.config import Settings
    from waku.db import connect
    from waku.memory import Memory, retrieval_gate

    settings = Settings(home=tmp_path)
    settings.ensure_home()
    memory = Memory(connect(tmp_path), settings, client=None)
    memory.facts.add("acme", "Acme cut its price to $19.")
    memory.facts.add("acme", "Acme's CEO spoke at a conference.")
    monkeypatch.setattr(retrieval_gate, "should_retrieve", lambda *a: (True, "acme", "test"))
    monkeypatch.setattr(slot_gate, "select", lambda message, facts: (
        [f for f in facts if "price" in f],
        [{"fact": f, "score": 3.0 if "price" in f else 0.2, "kept": "price" in f} for f in facts]))
    events = []
    context = memory.gated_retrieve("should we cut our price?",
                                    notify=lambda kind, data: events.append((kind, data)))
    assert "Acme cut its price" in context and "conference" not in context
    assert [kind for kind, _ in events] == ["gate", "slot"]
