"""One call to a System One model. Sixty lines, stdlib only, no SDK.

Jev takes a block of state and a set of named questions, answers all of them in
one parallel pass, and hands back typed values with probabilities. It never
writes a sentence, so there is nothing to parse and nothing to retry.

    from jev import ask, noul, choice, score

    answers = ask("hey, still waiting on this - it's been three days.", {
        "angry":   noul("The customer who wrote this message is angry."),
        "topic":   choice("What is this about?", {"delay": "...", "refund": "..."}),
        "urgency": score("How fast does a human need to reply?", ["no rush", "today", "now"]),
    })

Verified against: jev-1.13.0, 2026-09-20
Needs TYPESAFE_API_KEY. Get one at typesafe.ai; it is read from the environment
or from a .env file beside you, and never written anywhere.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000   # output tokens are free


def noul(instructions: str) -> dict:
    """Probability that a statement about the state is true. No confidence field:
    the probability is the answer. P(x) and P(not x) do not add up to 1 -- each is
    judged on its own, so ask both if you need both."""
    return {"type": "noul", "instructions": instructions}


def choice(instructions: str, options: dict[str, str]) -> dict:
    """One option wins. `options` maps the key your code will switch on to the
    description the model reads. Include a none-of-these key: with no way out it
    must still pick something. Max 255."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    """A position on an ordered rubric, 2-10 levels, each describing a concrete
    situation. The answer is the probability-weighted mean, so 1.4 sits between
    level 1 and level 2 -- it is not a level, and the legend is 0-indexed."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


def _key() -> str:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    for parent in (Path.cwd(), *Path(__file__).resolve().parents):
        env = parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("set TYPESAFE_API_KEY (get one at typesafe.ai)")


def ask(state, questions: dict[str, dict], *, model: str = MODEL) -> dict:
    """Send one request, get every answer back. Returns the answers dict, with
    `_ms` (round trip) and `_cost` (dollars) added for the measurements."""
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as exc:                    # 422 tells you which
        raise SystemExit(f"jev {exc.code}: {exc.read().decode()}") from exc
    answers = out["answers"]
    answers["_ms"] = (time.perf_counter() - started) * 1000
    answers["_cost"] = out["usage"]["input_tokens"] * PRICE_PER_INPUT_TOKEN
    return answers


def value(answer: dict):
    """The one number or string you actually switch on."""
    return answer.get("noul", answer.get("choice", answer.get("score")))
