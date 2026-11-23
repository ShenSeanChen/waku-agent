"""The turn receipt: what one turn did and cost, built once (spec 011).

Under every reply the chat draws one line, for example

    claude-sonnet-5 · 12.4k in / 1.9k out · $0.064 est  |  treg 2 · $0.030  |
    memory 4 used · 2 kept  |  $0.094

`build()` makes it from the events the turn already emitted: every `llm`
event (the loop's and the side calls `metered()` reports), every `tool`
event, the `consolidation` event's kept facts and the `report` event. The
`done` payload carries it as `receipt`, the chat log keeps it in
`meta.receipt`, and the tracer writes it as one `receipt` event.

PRIVACY. A receipt holds names, counts, dollars and ids, never text: no
tool arguments, no tool output, no prompt, no reply, no memory body, no
header. A treg call's arguments can carry a header map and a tool's output
can echo a key someone pasted, so this module copies named fields into new
objects and checks every string it copies against a short pattern, rather
than filtering the event it was given.

Model dollars are an estimate from `pricing.py`, labelled "est". On the
hosted free tier, the metering proxy knows the exact charge and the credits
Waku Memory took for the turn; `charges` passes its answer in, and the
receipt shows those numbers instead.
"""

from __future__ import annotations

import json
import re

from waku.ops.pricing import price_for

# The receipt's keys, at every level. A new field is a deliberate change:
# evals/deterministic/test_turn_receipt.py pins these sets.
KEYS = ("turn_id", "model", "tools", "memory", "total_usd", "credits")
MODEL_KEYS = ("id", "in", "out", "usd", "estimate", "calls")
TOOL_KEYS = ("tool", "provider", "usd", "status")
MEMORY_KEYS = ("searches", "used", "kept", "report")
SEARCH_KEYS = ("tool", "found", "trace_id")
# `sent`: True when Waku Memory took the fact, False when it did not, None
# when no Waku Memory is connected (waku/memory/consolidation.py).
KEPT_KEYS = ("memory_id", "sent")

# The Waku Memory tools whose results are counted as searches, not tools.
# The loop names an MCP tool <server>_<tool>.
SEARCH_TOOLS = {"waku_memory_memory_search": "memory_search",
                "waku_memory_memory_recall": "memory_recall"}

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# A provider name comes out of a tool's own answer, so it is held to the
# shape of one ("tomba", "PredictLeads") and kept far shorter than any key.
_PROVIDER = re.compile(r"[A-Za-z][A-Za-z0-9 _.-]{0,23}")


def _id(value) -> str | None:
    """An id copied into the receipt, or None when it is not id-shaped."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _name(value, pattern: re.Pattern = _NAME) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else ""


def _json(text) -> dict | None:
    try:
        out = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _status(output) -> str:
    low = (output if isinstance(output, str) else "").lower()
    return "error" if ("failed" in low or "timed out" in low or low.startswith("error")) else "ok"


def tool_cost(event: dict) -> tuple[float, str] | None:
    """(dollars, provider) when a tool's result names what it cost, as treg's
    call does (through the hosted relay too), else None. A named provider wins
    over the endpoint's first segment ("tomba.email.find" -> "tomba"). The
    same rule render.js's toolCost used before the receipt moved it here."""
    out = _json(event.get("output"))
    if out is None:
        return None
    usd = out.get("cost_usd")
    if isinstance(usd, bool) or not isinstance(usd, int | float) or not usd >= 0:
        return None
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    endpoint = (out.get("endpoint_id") or out.get("endpoint")
                or args.get("endpoint_id") or args.get("endpoint") or "")
    provider = (_name(out.get("provider"), _PROVIDER)
                or _name(str(endpoint).split(".")[0], _PROVIDER))
    return float(usd), provider


def _search(tool: str, event: dict) -> dict:
    out = _json(event.get("output"))
    entries = out.get("entries") if out else None
    return {"tool": tool,
            "found": len(entries) if isinstance(entries, list) else None,
            "trace_id": _id(out.get("retrieval_trace_id")) if out else None}


def build(events: list[tuple[str, dict]], *, turn_id: str, model: str, provider: str,
          default_model: str, used: int = 0, charges: dict | None = None) -> dict:
    """One receipt from a turn's (kind, event) pairs, in the order they came.

    `model` is the id that answered (the small model on a quick graph turn),
    `default_model` the one a loop `llm` event that names none was sent to,
    and `used` how many memories went into the prompt. `charges` is the
    metering proxy's answer for this turn, or None."""
    tokens_in = tokens_out = 0
    usd = 0.0
    calls: dict[str, int] = {}
    tools: list[dict] = []
    searches: list[dict] = []
    kept: list[dict] = []
    report = None
    for kind, ev in events:
        if not isinstance(ev, dict):
            continue
        if kind == "llm":
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            n_in, n_out = (max(int(usage.get(k) or 0), 0) for k in ("in", "out"))
            call_model = ev.get("model") or default_model or ""
            p_in, p_out = price_for(provider, call_model)
            tokens_in, tokens_out = tokens_in + n_in, tokens_out + n_out
            usd += n_in / 1e6 * p_in + n_out / 1e6 * p_out
            call_kind = _name(ev.get("kind")) or "loop"
            calls[call_kind] = calls.get(call_kind, 0) + 1
        elif kind == "tool":
            name = ev.get("tool") if isinstance(ev.get("tool"), str) else ""
            if name in SEARCH_TOOLS:
                searches.append(_search(SEARCH_TOOLS[name], ev))
                continue
            cost = tool_cost(ev)
            tools.append({"tool": _name(name) or "tool",
                          "provider": cost[1] if cost else "",
                          "usd": round(cost[0], 6) if cost else None,
                          "status": _status(ev.get("output"))})
        elif kind == "consolidation":
            kept = [{"memory_id": _id(k.get("memory_id")),
                     "sent": k.get("sent") if isinstance(k.get("sent"), bool) else None}
                    for k in (ev.get("kept") or []) if isinstance(k, dict)]
        elif kind == "report":
            report = _id(ev.get("memory_id"))

    estimate = True
    credits = None
    if isinstance(charges, dict):
        exact = charges.get("model_usd")
        if isinstance(exact, int | float) and not isinstance(exact, bool) and exact >= 0:
            usd, estimate = float(exact), False
        got = charges.get("credits")
        if isinstance(got, int) and not isinstance(got, bool) and got >= 0:
            credits = got
    model_usd = round(usd, 6)
    total = model_usd + sum(t["usd"] for t in tools if t["usd"] is not None)
    return {
        "turn_id": _id(turn_id) or "",
        # the configured model id, never text from a tool or the model
        "model": {"id": str(model or "")[:80],
                  "in": tokens_in, "out": tokens_out, "usd": model_usd,
                  "estimate": estimate,
                  "calls": [{"kind": k, "n": n} for k, n in calls.items()]},
        "tools": tools,
        "memory": {"searches": searches, "used": max(int(used or 0), 0),
                   "kept": kept, "report": report},
        "total_usd": round(total, 6),
        "credits": credits,
    }
