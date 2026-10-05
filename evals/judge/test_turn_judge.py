"""LLM-AS-JUDGE EVAL — the "Judge this turn" check grades grounding (spec 015).

The judge is the small model of the provider in use, called the way the
dashboard's route calls it. It must score the hosted rehearsal turn's reply,
which says the research cost "$0.00" while its tool output shows Tomba
charged $0.0089, below 0.7, and the same turn with a reply its tool outputs
support at 0.7 or above.

Requires the active provider's API key: the judge is a real model call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.helpers import HAS_KEY

pytestmark = pytest.mark.skipif(not HAS_KEY, reason="the turn judge needs the active provider's API key")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "turns" / "2026-10-05-rehearsal-zero-claim.jsonl"
TID = "t_5e1f0c2a9b7d4e36"
GROUNDED = ("LeadsForge's free preview returned six lookalike companies: Supermemory, Cognee, Letta, "
            "Memori, Vectorize and Agno. Tomba returned no lookalikes for mem0.ai, and that call cost "
            "$0.0089 of treg spend.")


def _judge(tmp_path, reply: str | None = None) -> float:
    from waku.config import load_settings
    from waku.ops import turn_evals

    lines = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()]
    if reply is not None:
        lines = [{**e, "reply": reply} if e["type"] == "turn_end" else e for e in lines]
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "2026-10-05.jsonl").write_text("\n".join(json.dumps(e) for e in lines))
    turn_evals._RECENT.clear()
    client, model, provider = turn_evals.judge_client(load_settings())
    return turn_evals.judge_turn(tmp_path, TID, client, model, provider)["value"]


def test_the_judge_scores_the_zero_dollar_reply_below_the_bar(tmp_path):
    assert _judge(tmp_path) < 0.7


def test_the_judge_scores_a_grounded_reply_at_or_above_the_bar(tmp_path):
    assert _judge(tmp_path, GROUNDED) >= 0.7
