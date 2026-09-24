"""The Judgment Arena — race several judges over the SAME cases, live.

Third sibling of the two races already here. arena.py holds the harness
constant and varies the model; memory_arena.py varies where facts live; this
one holds the harness, the cases AND the policy constant and varies *who makes
the call*: a System One model that only ever returns a typed answer, against an
ordinary LLM asked to produce the same answer as JSON.

    44 labelled cases ──┬─→ Jev        ─→ typed answers ──┐
                        ├─→ a model    ─→ JSON, parsed  ──┼─→ same decide() ─→ SSE
                        └─→ another    ─→ JSON, parsed  ──┘

One dial, so a result means something. Both contestants feed the identical
policy function from judgment_cases, so a difference in score is a difference
in judgment and never a difference in plumbing.

Three numbers per contestant, none of them the same question:

    agreement   how often its answer matched the label written by hand
    latency     p50 of the real round trip
    cost        dollars per 1000 decisions, from the tokens actually billed

Nothing here touches `.waku/`: there is no agent, no memory and no tool call in
a race, only judgments over fixed text. Results land in their own JSONL.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from waku.config import Settings, load_settings
from waku.loop.models import PROVIDERS, get_client
from waku.ops import judgment_cases as cases
from waku.ops.pricing import price_for

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_SPEC = "typesafe:jev-latest"
JEV_IN_PER_MTOK = 0.042          # output tokens are free
BOARD = "judgment_arena.jsonl"


# ── the two kinds of judge ──────────────────────────────────────────────────
def _jev(state: str, questions: dict) -> tuple[dict, float, float]:
    key = (os.getenv("TYPESAFE_API_KEY") or "").strip()
    if not key:
        raise SystemExit("TYPESAFE_API_KEY is not set. Get one at typesafe.ai, "
                         "put it in .env, and restart the dashboard.")
    body = json.dumps({"model": "jev-latest", "state": state,
                       "questions": questions}).encode()
    req = urllib.request.Request(JEV_ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"jev {exc.code}: {exc.read().decode()[:160]}") from exc
    ms = (time.perf_counter() - started) * 1000
    cost = out["usage"]["input_tokens"] / 1e6 * JEV_IN_PER_MTOK
    return out["answers"], ms, cost


def _prompt_for(questions: dict) -> str:
    lines = []
    for qid, q in questions.items():
        if q["type"] == "noul":
            lines.append(f'"{qid}": a probability from 0 to 1 that this is true -- '
                         f'{q["instructions"]}')
        elif q["type"] == "choice":
            opts = "; ".join(f"{k} = {v}" for k, v in q["criteria"].items())
            lines.append(f'"{qid}": exactly one of [{", ".join(q["criteria"])}] -- '
                         f'{q["instructions"]} ({opts})')
        else:
            levels = "; ".join(f"{i} = {lv}" for i, lv in enumerate(q["criteria"]))
            lines.append(f'"{qid}": a number from 0 to {len(q["criteria"]) - 1}, decimals '
                         f'allowed -- {q["instructions"]} ({levels})')
    return "\n".join(lines)


def _llm(spec: str, state: str, questions: dict) -> tuple[dict, float, float]:
    provider, _, model = spec.partition(":")
    client = get_client(Settings(provider=provider, model=model, small_model=""))
    started = time.perf_counter()
    msg = client.messages.create(
        model=model, max_tokens=400,
        messages=[{"role": "user", "content":
                   "Answer every question about the state below. Reply with one JSON "
                   f"object and nothing else.\n\nSTATE:\n{state}\n\n"
                   f"QUESTIONS:\n{_prompt_for(questions)}"}])
    ms = (time.perf_counter() - started) * 1000
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    raw = json.loads(text)                     # the step a System One model does not have
    answers = {}
    for qid, q in questions.items():
        v = raw[qid]
        answers[qid] = ({"noul": float(v)} if q["type"] == "noul" else
                        {"choice": str(v)} if q["type"] == "choice" else
                        {"score": float(v)})
    pin, pout = price_for(provider, model)
    cost = (msg.usage.input_tokens / 1e6 * pin + msg.usage.output_tokens / 1e6 * pout)
    return answers, ms, cost


# ── the race ────────────────────────────────────────────────────────────────
# Chosen by default because each one answers a different question: can a System
# One model do this at all, can a frontier model do it better, and is a cheap
# fast model enough. A fourth from another vendor keeps it from reading as an
# Anthropic house race.
# The flagship and the fast model of each vendor, checked against their own
# /models endpoints on 2026-09-21. A race wants both tiers: the flagship says
# whether the judgment is possible at all, the fast one says whether it is worth
# paying for. Re-check these when they age -- an id that 404s loses a column.
JUDGE_TIERS = (
    ("anthropic", "claude-opus-5", "claude-haiku-4-5-20251001"),
    ("openai", "gpt-5.5", "gpt-5.4-mini"),
    ("xai", "grok-4.7", "grok-4.20-0309-non-reasoning"),
)
# Four columns by default: the System One model, a frontier model, a fast one,
# and a second vendor so it does not read as one company's house race.
DEFAULT_PICKS = ("anthropic:claude-opus-5", "anthropic:claude-haiku-4-5-20251001",
                 "openai:gpt-5.4-mini")
DEFAULT_ALSO = ()
# Hidden from THIS race only -- still pinned, still in the chat switcher and the
# model race. Unpin in Setup > Models to remove one everywhere.
HIDE_PROVIDERS = frozenset({"kimi", "gemini"})


def contestants(settings=None) -> list[dict]:
    """Every model the user has pinned, plus Jev. A judge with no key is listed
    but not selectable, so the reason is on screen instead of the column
    silently missing.

    The list is the same shortlist the model race and the chat switcher use, so
    a model pinned once shows up everywhere it can run.
    """
    settings = settings or load_settings()
    jev_ready = bool((os.getenv("TYPESAFE_API_KEY") or "").strip())
    out = [{"spec": JEV_SPEC, "label": "Jev", "kind": "system-one", "ready": jev_ready,
            "picked": jev_ready, "why": "set TYPESAFE_API_KEY in .env"}]

    # Only the current flagship and fast model of each vendor. Older pins are
    # deliberately NOT offered here: a race between a current model and someone
    # else's superseded one measures nothing, and every extra column costs a
    # real API call per case.
    picks = set(DEFAULT_PICKS)
    specs = [f"{prov}:{m}" for prov, flagship, fast in JUDGE_TIERS
             for m in (flagship, fast)]
    for spec in specs:
        provider, _, model = spec.partition(":")
        if not model or provider not in PROVIDERS or provider in HIDE_PROVIDERS:
            continue
        env = PROVIDERS[provider].key_env
        ready = bool(os.getenv(env, "").strip())
        out.append({"spec": spec, "label": model, "kind": provider, "ready": ready,
                    "picked": ready and spec in picks,
                    "why": f"set {env} in .env"})
    return out


def save_key(key: str) -> dict:
    """Write the caller's own TypeSafe key to their .env.

    Jev is not a chat provider -- it cannot hold a conversation -- so it does not
    belong in the provider list that feeds the model switcher. It still needs a
    key, and the Models tab is where people look for that, so it gets a card of
    its own and this one endpoint.

    The key is the user's. It is written to their .env and set on this process;
    it is never sent anywhere except api.typesafe.ai, and never logged.
    """
    from dotenv import find_dotenv, set_key

    key = (key or "").strip()
    if not key:
        return {"error": "paste a key first"}
    if not key.isascii():
        return {"error": "that key has a non-ASCII character in it -- check for a smart quote"}
    env_path = find_dotenv(usecwd=True) or ".env"
    set_key(env_path, "TYPESAFE_API_KEY", key)
    os.environ["TYPESAFE_API_KEY"] = key
    return {"ok": True, "ready": True, "where": env_path}


def race(suite_id: str, specs: list[str], emit) -> None:
    """Ask every judge the fifteen questions of a suite, streaming answers.

    The three questions about one case go in ONE call, because asking several
    at once is the thing a System One model is for. Scoring is per question:
    each answer is right or wrong on its own against the label written by hand,
    and nothing is combined into a verdict.
    """
    suite = cases.SUITES.get(suite_id)
    if suite is None:
        emit("done", {"error": f"unknown suite {suite_id!r}"})
        return
    rows = suite["cases"]

    emit("start", {
        "suite": suite_id, "specs": specs,
        "questions": [{"i": i, "qid": q["id"], "type": q["type"],
                       "want": q["expected"], "asks": q["instructions"],
                       "shape": cases.answer_shape(q),
                       "state": rows[i]["state"], "note": rows[i]["note"]}
                      for i, q in cases.questions_of(suite_id)],
    })

    tally: dict[str, dict] = {}
    kept: dict[str, dict] = {}       # spec -> qid -> the cell we streamed

    def run_one(spec: str) -> None:
        hits, asked, lat, cost, broke = 0, 0, [], 0.0, 0
        for i, block in enumerate(rows):
            ask = {q["id"]: {k: v for k, v in q.items() if k not in ("id", "expected")}
                   for q in block["questions"]}
            try:
                answers, ms, c = (_jev(block["state"], ask) if spec == JEV_SPEC
                                  else _llm(spec, block["state"], ask))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                broke += len(ask)               # the failure a typed answer cannot have
                for q in block["questions"]:
                    emit("answer", {"spec": spec, "qid": q["id"], "i": i, "ok": False,
                                    "unparseable": True, "error": str(exc)[:120]})
                continue
            except (Exception, SystemExit) as exc:
                emit("failed", {"spec": spec, "error": str(exc)[:200]})
                return
            lat.append(ms)
            cost += c
            for q in block["questions"]:
                answer = dict(answers.get(q["id"], {}))
                ok = cases.is_right(q["type"], q["expected"], answer)
                hits += ok
                asked += 1
                cell = {"spec": spec, "qid": q["id"], "i": i, "ok": ok,
                        "want": q["expected"], "answer": answer}
                kept.setdefault(spec, {})[q["id"]] = cell
                emit("answer", cell)
        tally[spec] = {"spec": spec, "hits": hits,
                       "n": asked or len(cases.questions_of(suite_id)),
                       "unparseable": broke,
                       "p50_ms": round(statistics.median(lat)) if lat else None,
                       "per_1000": round(cost / max(1, len(lat)) * 1000, 4)}
        emit("column-done", tally[spec])

    with ThreadPoolExecutor(max_workers=min(len(specs), 4)) as pool:
        list(pool.map(run_one, specs))

    board = sorted(tally.values(), key=lambda r: -r["hits"])
    _save({"at": time.time(), "suite": suite_id, "board": board, "specs": specs,
           "cells": kept,
           "questions": [{"i": i, "qid": q["id"], "type": q["type"],
                          "want": q["expected"], "asks": q["instructions"],
                          "shape": cases.answer_shape(q), "note": rows[i]["note"],
                          "state": rows[i]["state"]}
                         for i, q in cases.questions_of(suite_id)]})
    emit("done", {"scoreboard": board})


# ── the scoreboard on disk ──────────────────────────────────────────────────
def _board_path(home: Path | None = None) -> Path:
    return (home or load_settings().home) / BOARD


def _save(run: dict) -> None:
    path = _board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(run) + "\n")


def load_runs(home: Path | None = None, limit: int = 20) -> list[dict]:
    path = _board_path(home)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out[-limit:][::-1]
