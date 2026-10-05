"""One call to TypeSafe's Jev, a System One model (spec 005).

Graduated from lab/jev-system-one/jev.py, copied rather than imported (hard
rule 5). Two changes from the lab: the key comes from the environment only, so
nothing here reads a file, and any failure raises instead of exiting, so the
caller can fail open. Stdlib only (hard rule 3). Called only when the person
set WAKU_SLOT_GATE=jev and TYPESAFE_API_KEY (hard rule 2: no hidden calls).

Jev takes a block of state and named questions and answers them all in one
parallel pass, with probabilities instead of text, so there is nothing to parse
and nothing to retry. Measured in the lab: about 260 ms and $0.02 per 1000
decisions.
"""

from __future__ import annotations

import json
import os
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_SECONDS = 10


def score(instructions: str, levels: list[str]) -> dict:
    """A position on an ordered rubric. The answer is the probability-weighted
    mean, so 1.4 sits between level 1 and level 2; the legend is 0-indexed."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


def ask(state: str, questions: dict[str, dict], *, model: str = MODEL) -> dict:
    """Every answer, keyed like `questions`. Raises on any failure."""
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    request = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())["answers"]
