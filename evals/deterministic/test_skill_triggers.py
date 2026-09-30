"""DETERMINISTIC EVAL — skills load on the right messages, and only those.

A skill is chosen by keyword overlap (waku/memory/procedural/loader.py): it
loads when a message shares 2+ words of 3+ letters with its name +
description, at most two per turn, highest overlap first. Review of two skill
PRs (#149, #159) found the two ways that goes wrong:

  1. Precision. A description built on question words (why, what, should,
     for) matches almost any message. "why did Alex move our meeting" loaded
     a metric-anomaly skill, and 28 lines of Shapley decomposition rode along.
  2. Hijacking. A new skill ties an existing one on shared words, and the tie
     goes to folder order, not relevance — so "prep me for tomorrow's standup"
     loaded interview-prep ahead of meeting-prep.

Both checks run over EVERY skill in the repo, so a skill PR is covered without
its author writing any Python. MESSAGES is kept by reviewers: when a skill
lands, add a few messages it should answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from waku.memory.procedural.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[2]
LOADER = SkillLoader([ROOT / "skills"])

HINT = ("Anchor the description on words specific to the skill's domain "
        "(DAU, interview, standup) — question words like why / what / should / "
        "for appear in almost every message.")

# Messages that should reach the plain loop with no skill at all. Keep this to
# things no reasonable skill would claim; anything borderline belongs in a
# per-skill decision, not here.
EVERYDAY = [
    "my weight dropped this month, why",
    "why did my package get delayed",
    "what caused the change to my flight",
    "what should I revise in this email",
    "what should I revise in my essay draft",
    "help me revise the pitch deck",
    "did I save the note about Alex",
    "remind me why I booked this",
    "what's the weather tomorrow",
    "tell me a joke",
    "thanks, that's all",
    "translate this sentence into French",
    "what is 17 times 23",
    "summarize this article for me",
    "who won the game last night",
]

# Each skill must be the FIRST one loaded for its own messages. With every
# skill loaded together, this is also what catches a new skill taking over an
# existing skill's messages.
MESSAGES = {
    "schedule-meeting": [
        "schedule coffee with Alex tomorrow at 9am",
        "book a call with Ian on Friday at 3pm",
        "put a dentist appointment on my calendar next Tuesday",
        "set up a meeting with the team on Monday",
    ],
    "meeting-prep": [
        "prep me for my call with Alex",
        "prep me for tomorrow's standup",
        "get me ready for the board meeting",
        "who am I meeting today",
        "brief me on my call with the investors",
        "what should I know before my meeting with Ian",
    ],
    "weekly-brief": [
        "brief me on my week",
        "what's on my week",
        "what should I focus on today",
        "how does my day look",
        "catch me up on this week",
        "give me my morning briefing",
    ],
    "da-anomaly-analysis": [
        "why did DAU drop last week",
        "GMV spiked yesterday, what caused it",
        "conversion rate fell 20%, help me with metric attribution",
        "retention dropped after the release",
    ],
    "interview-prep": [
        "prep me for my interview tomorrow",
        "I have a technical interview at Google on Friday",
        "help me practice behavioral interview questions",
        "mock interview me",
        "what should I revise for my placement interview",
    ],
    "waku-memory": [
        "connect waku memory",
        "how do I share my memory with claude code",
        "use the same memory in codex and grok bot",
        "export my waku skills to claude code",
    ],
}


def _loaded(message: str) -> list[str]:
    return [skill.name for skill in LOADER.match(message)]


@pytest.mark.parametrize("message", EVERYDAY)
def test_no_skill_loads_on_an_everyday_message(message):
    loaded = _loaded(message)
    assert loaded == [], f"{message!r} loaded {loaded}. {HINT}"


@pytest.mark.parametrize("skill,message", [(s, m) for s, ms in MESSAGES.items() for m in ms])
def test_each_skill_is_first_for_its_own_messages(skill, message):
    loaded = _loaded(message)
    assert loaded[:1] == [skill], (
        f"{message!r} should load {skill} first, got {loaded}. "
        "If another skill is first, one of the two descriptions is claiming "
        f"words that belong to the other. {HINT}"
    )


def test_every_skill_named_here_still_exists():
    """A renamed or deleted skill must not leave a table row passing vacuously."""
    real = {skill.name for skill in LOADER.skills}
    stale = sorted(set(MESSAGES) - real)
    assert not stale, f"MESSAGES names skills that no longer exist: {stale}"
