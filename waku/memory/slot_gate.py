"""Jev decides which memories earn a slot (spec 005).

Two decisions, both opt-in and both failing open:

  select  which retrieved facts enter the prompt. One Jev call scores every
          fact against the person's message, with the question
          lab/jev-system-one measured at 12 of 12: "how much does leaving this
          one out change the answer?". Facts at or above WAKU_SLOT_GATE_KEEP
          (1.8, the lab's threshold) are kept.
  keep    which facts consolidation proposes are stored. "Would a future
          answer need this?"; at or above WAKU_KEEP_GATE_MIN are kept.

On only with WAKU_SLOT_GATE=jev and a TYPESAFE_API_KEY. Off, or any failure --
the network, a timeout, an answer without a score -- and every fact goes
through, exactly as before: a slow or broken judge must never cost a memory.
"""

from __future__ import annotations

import os

from waku.memory import jev

SLOT_LEVELS = [
    "Not at all. Irrelevant here.",
    "A little. Nice colour, not needed.",
    "A lot. The answer would be vaguer or need a question asked.",
    "Completely. Without this the request cannot be answered.",
]
KEEP_LEVELS = [
    "Never. Small talk, or true only for this moment.",
    "Rarely. A detail nobody is likely to ask about again.",
    "Often. A fact, preference or decision a later answer would use.",
    "Always. Getting this wrong later would be a real mistake.",
]
DEFAULT_SLOT_KEEP = 1.8     # the lab's measured threshold, 12/12
DEFAULT_KEEP_MIN = 1.0      # drops only what Jev rates "never"; tuned in spec 005 B2


def enabled() -> bool:
    return (os.environ.get("WAKU_SLOT_GATE", "").strip().lower() == "jev"
            and bool(os.environ.get("TYPESAFE_API_KEY")))


def _threshold(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _scores(state: str, items: list[str], instructions, levels) -> list[float] | None:
    questions = {f"fact_{i}": jev.score(instructions(item), levels)
                 for i, item in enumerate(items)}
    try:
        answers = jev.ask(state, questions)
        return [float(answers[f"fact_{i}"]["score"]) for i in range(len(items))]
    except Exception:
        return None


def select(message: str, facts: list[str]) -> tuple[list[str], list[dict]]:
    """(the facts to put in the prompt, one verdict per fact). With the gate
    off or failing, every fact and no verdicts."""
    if not facts or not enabled():
        return facts, []
    scores = _scores(message, facts, lambda fact: (
        "An assistant is answering the user's current message and can only carry "
        "a few remembered facts into it. How much does leaving this one out change "
        f"the answer? Memory: {fact}"), SLOT_LEVELS)
    if scores is None:
        return facts, []
    keep = _threshold("WAKU_SLOT_GATE_KEEP", DEFAULT_SLOT_KEEP)
    verdicts = [{"fact": f, "score": round(s, 2), "kept": s >= keep}
                for f, s in zip(facts, scores, strict=True)]
    return [v["fact"] for v in verdicts if v["kept"]], verdicts


def keep(proposed: list[dict]) -> list[dict]:
    """The facts consolidation proposed that are worth storing."""
    if not proposed or not enabled():
        return proposed
    texts = [f"{f.get('subject', '')}: {f.get('content', '')}" for f in proposed]
    scores = _scores("Facts an assistant distilled from a conversation with its user.",
                     texts, lambda fact: (
                         "How often would a later answer to this user need this fact? "
                         f"Fact: {fact}"), KEEP_LEVELS)
    if scores is None:
        return proposed
    minimum = _threshold("WAKU_KEEP_GATE_MIN", DEFAULT_KEEP_MIN)
    return [f for f, s in zip(proposed, scores, strict=True) if s >= minimum]
